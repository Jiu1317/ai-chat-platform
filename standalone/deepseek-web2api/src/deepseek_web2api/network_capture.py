"""Bounded capture of one post-click DeepSeek completion response stream."""

from __future__ import annotations

import asyncio
import base64
import binascii
from typing import Any, Protocol
from urllib.parse import urlparse

from .deepseek_sse import DeepSeekSSEEvent, DeepSeekSSEParseError, DeepSeekSSEParser
from .exceptions import (
    CDPConnectionError,
    CDPProtocolError,
    DeepSeekCompletionRejected,
    GenerationRejected,
)

COMPLETION_PATH = "/api/v0/chat/completion"
NETWORK_EVENTS = frozenset(
    {
        "Network.requestWillBeSent",
        "Network.responseReceived",
        "Network.dataReceived",
        "Network.loadingFinished",
        "Network.loadingFailed",
    }
)


class EventLike(Protocol):
    method: str
    params: dict[str, Any]


class SubscriptionLike(Protocol):
    async def get(self, *, timeout: float | None = None) -> EventLike: ...

    async def close(self) -> None: ...


class NetworkPage(Protocol):
    async def command(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 15
    ) -> dict[str, Any]: ...

    def subscribe_events(
        self, methods: set[str] | frozenset[str], *, max_queue: int = 1024
    ) -> SubscriptionLike: ...


class DeepSeekNetworkMonitor:
    """Stream one new completion request without retaining request metadata.

    The subscription is created immediately before the click. The first exact
    completion URL observed after that barrier owns the monitor; every other
    request id is ignored.
    """

    def __init__(
        self,
        page: NetworkPage,
        *,
        event_queue_size: int = 1024,
        output_queue_size: int = 1024,
    ) -> None:
        self._page = page
        self._event_queue_size = event_queue_size
        self._output: asyncio.Queue[DeepSeekSSEEvent | BaseException] = asyncio.Queue(
            output_queue_size
        )
        self._subscription: SubscriptionLike | None = None
        self._task: asyncio.Task[None] | None = None
        self._parser = DeepSeekSSEParser()
        self._ready = asyncio.Event()
        self._terminal = asyncio.Event()
        self._ready_seen = False
        self._finished_seen = False
        self._request_id: str | None = None
        self._response_seen = False
        self._streaming = False
        self._error: BaseException | None = None

    @property
    def request_id(self) -> str | None:
        return self._request_id

    @property
    def ready_seen(self) -> bool:
        return self._ready_seen

    @property
    def terminal(self) -> bool:
        return self._terminal.is_set()

    async def start(self) -> None:
        await self._page.command(
            "Network.enable",
            {
                "maxTotalBufferSize": 16 * 1024 * 1024,
                "maxResourceBufferSize": 8 * 1024 * 1024,
            },
        )
        self._subscription = self._page.subscribe_events(
            NETWORK_EVENTS, max_queue=self._event_queue_size
        )
        self._task = asyncio.create_task(
            self._run(), name="deepseek-completion-network-stream"
        )

    async def wait_ready(self, *, timeout: float) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        if self._error is not None:
            raise self._error
        if not self._ready_seen:
            raise CDPProtocolError("DeepSeek completion stream ended before ready")

    async def next_event(self, *, timeout: float | None = None) -> DeepSeekSSEEvent:
        item = (
            await self._output.get()
            if timeout is None
            else await asyncio.wait_for(self._output.get(), timeout=timeout)
        )
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        subscription, self._subscription = self._subscription, None
        if subscription is not None:
            await subscription.close()

    async def _run(self) -> None:
        assert self._subscription is not None
        try:
            while True:
                event = await self._subscription.get()
                if await self._handle_event(event):
                    return
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fail(exc)

    async def _handle_event(self, event: EventLike) -> bool:
        request_id = event.params.get("requestId")
        if not isinstance(request_id, str):
            return False

        if event.method == "Network.requestWillBeSent":
            if self._request_id is None and self._is_completion_url(
                event.params.get("url")
            ) and event.params.get("method") == "POST":
                self._request_id = request_id
                restrict = getattr(self._subscription, "restrict_request_id", None)
                if callable(restrict):
                    restrict(request_id)
            return False
        if request_id != self._request_id:
            return False

        if event.method == "Network.responseReceived":
            status = event.params.get("status")
            if not self._is_completion_url(event.params.get("url")):
                raise CDPProtocolError(
                    "DeepSeek completion response changed to an unexpected URL"
                )
            if not isinstance(status, (int, float)) or not 200 <= status < 300:
                if status == 429:
                    raise GenerationRejected("DeepSeek is rate limited")
                raise DeepSeekCompletionRejected(
                    f"DeepSeek completion returned HTTP {status}"
                )
            self._response_seen = True
            try:
                result = await self._page.command(
                    "Network.streamResourceContent", {"requestId": request_id}
                )
            except (CDPConnectionError, CDPProtocolError):
                self._streaming = False
            else:
                self._streaming = True
                buffered = result.get("bufferedData", "")
                if isinstance(buffered, str) and buffered:
                    self._feed_base64(buffered)
            return False

        if event.method == "Network.dataReceived":
            if self._streaming:
                data = event.params.get("data")
                if isinstance(data, str) and data:
                    self._feed_base64(data)
            return False

        if event.method == "Network.loadingFailed":
            detail = event.params.get("errorText") or "network loading failed"
            raise CDPConnectionError(f"DeepSeek completion stream failed: {detail}")

        if event.method == "Network.loadingFinished":
            if not self._response_seen:
                raise CDPProtocolError(
                    "DeepSeek completion finished without response metadata"
                )
            if not self._streaming:
                result = await self._page.command(
                    "Network.getResponseBody", {"requestId": request_id}
                )
                body = result.get("body", "")
                if not isinstance(body, str):
                    raise CDPProtocolError("DeepSeek returned an invalid response body")
                if result.get("base64Encoded"):
                    self._feed_base64(body)
                else:
                    self._feed_bytes(body.encode("utf-8"))
            try:
                final_events = self._parser.finish()
            except DeepSeekSSEParseError as exc:
                raise CDPProtocolError(
                    f"Invalid DeepSeek completion stream: {exc}"
                ) from exc
            self._emit(final_events)
            if not self._ready_seen:
                raise CDPProtocolError("DeepSeek completion ended before ready")
            if not self._finished_seen:
                raise CDPProtocolError("DeepSeek completion ended without a terminal event")
            self._terminal.set()
            self._put_output(DeepSeekSSEEvent("finished"))
            return True
        return False

    @staticmethod
    def _is_completion_url(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        parsed = urlparse(value)
        try:
            exact_origin = (
                parsed.scheme == "https"
                and parsed.hostname == "chat.deepseek.com"
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            return False
        return exact_origin and parsed.path == COMPLETION_PATH

    def _feed_base64(self, encoded: str) -> None:
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CDPProtocolError("DeepSeek streamed invalid base64 data") from exc
        self._feed_bytes(chunk)

    def _feed_bytes(self, chunk: bytes) -> None:
        try:
            events = self._parser.feed(chunk)
        except DeepSeekSSEParseError as exc:
            raise CDPProtocolError(f"Invalid DeepSeek completion stream: {exc}") from exc
        self._emit(events)

    def _emit(self, events: list[DeepSeekSSEEvent]) -> None:
        for event in events:
            if event.kind == "ack":
                self._ready_seen = True
                self._ready.set()
                continue
            if event.kind == "error":
                raise self._map_business_error(event)
            if event.kind == "finished":
                self._finished_seen = True
                continue
            self._put_output(event)

    def _put_output(self, event: DeepSeekSSEEvent | BaseException) -> None:
        try:
            self._output.put_nowait(event)
        except asyncio.QueueFull as exc:
            raise CDPProtocolError("DeepSeek parsed event queue overflowed") from exc

    def _fail(self, error: BaseException) -> None:
        if self._error is not None:
            return
        self._error = error
        self._ready.set()
        self._terminal.set()
        while not self._output.empty():
            try:
                self._output.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self._output.put_nowait(error)
        except asyncio.QueueFull:
            pass

    @staticmethod
    def _map_business_error(event: DeepSeekSSEEvent) -> BaseException:
        code = event.code or "deepseek_error"
        message = event.message or "DeepSeek rejected the completion"
        signal = f"{code} {message}".lower()
        if any(
            marker in signal
            for marker in ("rate", "too many", "busy", "频繁", "请求过多", "稍后")
        ):
            return GenerationRejected(message)
        return DeepSeekCompletionRejected(f"{message} ({code})")
