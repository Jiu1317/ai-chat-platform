"""Chrome lifecycle, CDP transport, and conservative turn state machine."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp
import websockets

from .config import DEEPSEEK_URL, Config
from .dom_adapter import DeepSeekDOMAdapter, DOMSnapshot, PageSession, TurnSnapshot
from .exceptions import (
    CDPConnectionError,
    CDPProtocolError,
    ChromeLaunchError,
    DeepSeekCompletionRejected,
    DeepSeekTabNotFound,
    DOMElementNotFound,
    GenerationCancelled,
    GenerationRejected,
    GenerationTimeout,
    SendNotAcknowledged,
    UnsupportedModel,
)
from .network_capture import DeepSeekNetworkMonitor

FRESH_CHAT_STABLE_POLLS = 4
FRESH_CHAT_SETTLE_SECONDS = 3.0
SEND_READY_STABLE_POLLS = 2
MAX_SEND_ACK_SECONDS = 60.0
NETWORK_READY_GRACE_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class ReplyDelta:
    content: str = ""
    reasoning_content: str = ""


@dataclass(frozen=True, slots=True)
class Reply:
    content: str
    reasoning_content: str = ""


@dataclass(frozen=True, slots=True)
class CDPEvent:
    method: str
    params: dict[str, Any]


class CDPEventSubscription:
    """Bounded subscription containing only explicitly sanitized CDP event fields."""

    def __init__(
        self,
        owner: CDPPage,
        methods: frozenset[str],
        *,
        max_queue: int,
    ) -> None:
        self._owner = owner
        self.methods = methods
        self._queue: asyncio.Queue[CDPEvent | BaseException] = asyncio.Queue(max_queue)
        self._closed = False
        self._request_id: str | None = None

    def _push(self, event: CDPEvent) -> None:
        if self._closed or event.method not in self.methods:
            return
        if self._request_id is not None and event.params.get("requestId") != self._request_id:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._fail(CDPProtocolError("CDP event subscription overflowed"))

    def _fail(self, error: BaseException) -> None:
        if self._closed:
            return
        self._closed = True
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(error)
        self._owner._subscriptions.discard(self)

    async def get(self, *, timeout: float | None = None) -> CDPEvent:
        item = (
            await self._queue.get()
            if timeout is None
            else await asyncio.wait_for(self._queue.get(), timeout=timeout)
        )
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        if self._closed:
            return
        self._fail(CDPConnectionError("CDP event subscription closed"))

    def restrict_request_id(self, request_id: str) -> None:
        """Drop queued/unseen events unrelated to one already-observed request."""

        if self._closed or not request_id:
            return
        self._request_id = request_id
        retained: list[CDPEvent | BaseException] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if isinstance(item, BaseException) or item.params.get("requestId") == request_id:
                retained.append(item)
        for item in retained:
            self._queue.put_nowait(item)


class CDPPage:
    """Small id-routed CDP websocket client."""

    def __init__(self, websocket_url: str) -> None:
        self._url = websocket_url
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._subscriptions: set[CDPEventSubscription] = set()

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._reader is not None and not self._reader.done()

    async def connect(self) -> None:
        # An 8 MiB raw response can expand to roughly 10.7 MiB when CDP wraps
        # base64 in JSON. The parser still enforces the 8 MiB decoded-body cap.
        kwargs: dict[str, Any] = {"open_timeout": 10, "max_size": 16 * 1024 * 1024}
        try:
            if "proxy" in inspect.signature(websockets.connect).parameters:
                kwargs["proxy"] = None
            self._ws = await websockets.connect(self._url, **kwargs)
        except Exception as exc:
            raise CDPConnectionError(f"Could not connect to Chrome CDP: {exc}") from exc
        self._reader = asyncio.create_task(self._reader_loop(), name="deepseek-cdp-reader")
        await self.command("Runtime.enable")
        await self.command("Page.enable")

    async def _reader_loop(self) -> None:
        failure: BaseException | None = None
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                message_id = message.get("id")
                if not isinstance(message_id, int):
                    method = message.get("method")
                    params = message.get("params")
                    if isinstance(method, str) and isinstance(params, dict):
                        self._publish_event(method, params)
                    continue
                future = self._pending.pop(message_id, None)
                if future is not None and not future.done():
                    future.set_result(message)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure = exc
        finally:
            suffix = f": {failure}" if failure else ""
            error = CDPConnectionError(f"Chrome CDP connection closed{suffix}")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            for subscription in tuple(self._subscriptions):
                subscription._fail(error)

    @staticmethod
    def _sanitize_event(method: str, params: dict[str, Any]) -> CDPEvent | None:
        request_id = params.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            return None
        safe: dict[str, Any] = {"requestId": request_id}
        if method == "Network.requestWillBeSent":
            request = params.get("request")
            if not isinstance(request, dict):
                return None
            safe["url"] = str(request.get("url", ""))
            safe["method"] = str(request.get("method", ""))
        elif method == "Network.responseReceived":
            response = params.get("response")
            if not isinstance(response, dict):
                return None
            safe.update(
                url=str(response.get("url", "")),
                status=response.get("status"),
                mimeType=str(response.get("mimeType", "")),
            )
        elif method == "Network.dataReceived":
            data = params.get("data")
            if isinstance(data, str):
                safe["data"] = data
        elif method == "Network.loadingFinished":
            safe["encodedDataLength"] = params.get("encodedDataLength")
        elif method == "Network.loadingFailed":
            safe.update(
                errorText=str(params.get("errorText", "")),
                canceled=bool(params.get("canceled")),
                blockedReason=str(params.get("blockedReason", "")),
            )
        else:
            return None
        return CDPEvent(method, safe)

    def _publish_event(self, method: str, params: dict[str, Any]) -> None:
        event = self._sanitize_event(method, params)
        if event is None:
            return
        for subscription in tuple(self._subscriptions):
            subscription._push(event)

    def subscribe_events(
        self, methods: set[str] | frozenset[str], *, max_queue: int = 1024
    ) -> CDPEventSubscription:
        allowed = {
            "Network.requestWillBeSent",
            "Network.responseReceived",
            "Network.dataReceived",
            "Network.loadingFinished",
            "Network.loadingFailed",
        }
        requested = frozenset(methods)
        if not requested or not requested.issubset(allowed):
            raise ValueError("Only sanitized Network events may be subscribed")
        if not 1 <= max_queue <= 4096:
            raise ValueError("max_queue must be between 1 and 4096")
        subscription = CDPEventSubscription(self, requested, max_queue=max_queue)
        self._subscriptions.add(subscription)
        return subscription

    async def command(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 15
    ) -> dict[str, Any]:
        if self._ws is None:
            raise CDPConnectionError("Chrome CDP page is not connected")
        self._next_id += 1
        message_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._ws.send(
                json.dumps({"id": message_id, "method": method, "params": params or {}})
            )
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.CancelledError:
            self._pending.pop(message_id, None)
            raise
        except TimeoutError as exc:
            self._pending.pop(message_id, None)
            raise CDPConnectionError(f"Chrome CDP command timed out: {method}") from exc
        except Exception as exc:
            self._pending.pop(message_id, None)
            if isinstance(exc, CDPConnectionError):
                raise
            raise CDPConnectionError(f"Chrome CDP command failed: {method}: {exc}") from exc
        if "error" in response:
            error = response["error"]
            detail = error.get("message", error) if isinstance(error, dict) else error
            raise CDPProtocolError(f"Chrome CDP rejected {method}: {detail}")
        return response.get("result", {})

    async def evaluate(self, expression: str, *, timeout: float = 15) -> Any:
        result = await self.command(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise CDPProtocolError(details.get("text", "JavaScript evaluation failed"))
        remote = result.get("result", {})
        if remote.get("subtype") == "error":
            raise CDPProtocolError(str(remote.get("description", "JavaScript error")))
        return remote.get("value")

    async def close(self) -> None:
        for subscription in tuple(self._subscriptions):
            await subscription.close()
        reader, self._reader = self._reader, None
        websocket, self._ws = self._ws, None
        if websocket is not None:
            await websocket.close()
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


class CDPDriver:
    """One-request-at-a-time driver for one dedicated DeepSeek browser tab."""

    def __init__(
        self,
        config: Config,
        *,
        page_factory: Callable[[str], PageSession] = CDPPage,
        network_monitor_factory: Callable[[Any], DeepSeekNetworkMonitor] = (
            DeepSeekNetworkMonitor
        ),
        fresh_chat_settle_seconds: float = FRESH_CHAT_SETTLE_SECONDS,
    ) -> None:
        self.config = config
        self._page_factory = page_factory
        self._network_monitor_factory = network_monitor_factory
        self._page: PageSession | None = None
        self._dom: DeepSeekDOMAdapter | None = None
        self._chrome_process: asyncio.subprocess.Process | None = None
        self._connect_lock = asyncio.Lock()
        self._generation_lock = asyncio.Lock()
        self._target_url = ""
        self._send_uncertain = False
        if fresh_chat_settle_seconds < 0:
            raise ValueError("fresh_chat_settle_seconds cannot be negative")
        self._fresh_chat_settle_seconds = fresh_chat_settle_seconds

    @property
    def connected(self) -> bool:
        return bool(self._page and self._page.connected)

    @property
    def busy(self) -> bool:
        return self._generation_lock.locked() or self._send_uncertain

    @property
    def target_url(self) -> str:
        return self._target_url

    async def connect(self) -> None:
        async with self._connect_lock:
            if self.connected:
                return
            try:
                targets = await self._list_targets()
            except CDPConnectionError:
                if not self.config.chrome.auto_launch:
                    raise
                await self._launch_chrome()
                targets = await self._wait_for_targets()

            target = self._choose_deepseek_target(targets)
            if target is None:
                target = await self._create_target(self.config.chrome.page_url)
            websocket_url = target.get("webSocketDebuggerUrl")
            if not isinstance(websocket_url, str) or not websocket_url:
                raise DeepSeekTabNotFound("DeepSeek tab has no CDP websocket endpoint")

            page = self._page_factory(websocket_url)
            try:
                await page.connect()
                self._page = page
                self._dom = DeepSeekDOMAdapter(page)
                self._target_url = str(target.get("url", ""))
                await self.navigate(self.config.chrome.page_url)
            except BaseException:
                # A websocket can be opened before CDP initialization fails.
                # Discard that half-connected page so the next API request can
                # perform a clean reconnect instead of treating it as healthy.
                if self._page is page:
                    self._page = None
                    self._dom = None
                    self._target_url = ""
                try:
                    await page.close()
                except Exception:
                    pass
                raise

    async def navigate(self, url: str = DEEPSEEK_URL) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "chat.deepseek.com":
            raise ValueError("The dedicated tab can navigate only to https://chat.deepseek.com")
        if not self._page:
            raise CDPConnectionError("Chrome CDP page is not connected")
        current = urlparse(self._target_url)
        if current.hostname != "chat.deepseek.com":
            await self._page.command("Page.navigate", {"url": url}, timeout=30)
            await asyncio.sleep(0.5)
        self._target_url = url

    async def _list_targets(self) -> list[dict[str, Any]]:
        endpoint = (
            f"http://{self.config.chrome.cdp_host}:{self.config.chrome.cdp_port}/json/list"
        )
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5), trust_env=False
            ) as session:
                async with session.get(endpoint) as response:
                    if response.status != 200:
                        raise CDPConnectionError(
                            f"Chrome CDP target list returned HTTP {response.status}"
                        )
                    payload = await response.json()
        except CDPConnectionError:
            raise
        except Exception as exc:
            raise CDPConnectionError(f"Chrome CDP endpoint is unavailable: {exc}") from exc
        if not isinstance(payload, list):
            raise CDPConnectionError("Chrome CDP target list has an invalid shape")
        return [item for item in payload if isinstance(item, dict)]

    @staticmethod
    def _choose_deepseek_target(
        targets: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for target in targets:
            if target.get("type") != "page":
                continue
            parsed = urlparse(str(target.get("url", "")))
            if parsed.scheme == "https" and parsed.hostname == "chat.deepseek.com":
                return target
        return None

    async def _create_target(self, url: str) -> dict[str, Any]:
        endpoint = (
            f"http://{self.config.chrome.cdp_host}:{self.config.chrome.cdp_port}"
            f"/json/new?{quote(url, safe='')}"
        )
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10), trust_env=False
            ) as session:
                async with session.put(endpoint) as response:
                    if response.status != 200:
                        raise DeepSeekTabNotFound(
                            f"Chrome could not create the DeepSeek tab (HTTP {response.status})"
                        )
                    payload = await response.json()
        except DeepSeekTabNotFound:
            raise
        except Exception as exc:
            raise DeepSeekTabNotFound(f"Could not create the DeepSeek tab: {exc}") from exc
        if not isinstance(payload, dict):
            raise DeepSeekTabNotFound("Chrome returned an invalid target")
        return payload

    async def _wait_for_targets(self) -> list[dict[str, Any]]:
        deadline = time.monotonic() + self.config.chrome.startup_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return await self._list_targets()
            except CDPConnectionError as exc:
                last_error = exc
                await asyncio.sleep(0.25)
        detail = last_error or "timeout"
        raise ChromeLaunchError(f"Chrome did not open its CDP endpoint in time: {detail}")

    async def _launch_chrome(self) -> None:
        if self.config.chrome.cdp_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ChromeLaunchError("Automatic Chrome launch requires a local CDP host")
        binary = self.config.chrome.binary or self._find_chrome_binary()
        if not binary:
            raise ChromeLaunchError(
                "Chrome or Edge was not found; set chrome.binary in the configuration"
            )
        profile = self.config.chrome.user_data_dir
        profile.mkdir(parents=True, exist_ok=True)
        args = [
            binary,
            f"--remote-debugging-address={self.config.chrome.cdp_host}",
            f"--remote-debugging-port={self.config.chrome.cdp_port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--new-window",
            self.config.chrome.page_url,
        ]
        kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            self._chrome_process = await asyncio.create_subprocess_exec(*args, **kwargs)
        except OSError as exc:
            raise ChromeLaunchError(f"Could not start the dedicated browser: {exc}") from exc

    @staticmethod
    def _find_chrome_binary() -> str | None:
        for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
            found = shutil.which(name)
            if found:
                return found
        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        relative_paths = (
            Path("Google/Chrome/Application/chrome.exe"),
            Path("Microsoft/Edge/Application/msedge.exe"),
        )
        for root in roots:
            if not root:
                continue
            for relative in relative_paths:
                candidate = Path(root) / relative
                if candidate.is_file():
                    return str(candidate)
        return None

    async def status(self) -> dict[str, Any]:
        if not self.connected:
            return {
                "status": "cdp_unavailable",
                "cdp_connected": False,
                "logged_in": False,
                "busy": self.busy,
                "target_url": "",
            }
        try:
            state = await self._adapter.probe()
        except Exception:
            return {
                "status": "dom_changed",
                "cdp_connected": False,
                "logged_in": False,
                "busy": self.busy,
                "target_url": self._target_url,
            }
        parsed = urlparse(state.url)
        manually_recovered = (
            parsed.hostname == "chat.deepseek.com"
            and parsed.path.rstrip("/") == ""
            and state.composer_found
            and state.composer_empty
            and not state.stop_visible
            and not state.users
            and not state.assistants
        )
        if self._send_uncertain and manually_recovered:
            self._send_uncertain = False
        if self._send_uncertain:
            page_status = "send_unknown"
        elif state.verification_required:
            page_status = "captcha_required"
        elif state.login_visible or not state.logged_in:
            page_status = "login_required"
        elif state.rate_limited:
            page_status = "rate_limited"
        elif not state.composer_found:
            page_status = "dom_changed"
        else:
            page_status = "ok"
        return {
            "status": page_status,
            "cdp_connected": True,
            "logged_in": state.logged_in and not state.verification_required,
            "busy": self.busy,
            "target_url": DEEPSEEK_URL,
        }

    @property
    def _adapter(self) -> DeepSeekDOMAdapter:
        if self._dom is None:
            raise CDPConnectionError("Chrome CDP page is not connected")
        return self._dom

    async def start_fresh_chat(self) -> DOMSnapshot:
        """Navigate to a verified empty chat without deleting any history."""

        if self._send_uncertain:
            raise SendNotAcknowledged(
                "Previous send state is unknown; manually stop it and open a blank chat first"
            )
        if self._page is None:
            raise CDPConnectionError("Chrome CDP page is not connected")
        await self._page.command("Page.navigate", {"url": DEEPSEEK_URL}, timeout=30)
        self._target_url = DEEPSEEK_URL
        deadline = time.monotonic() + self.config.chrome.startup_timeout_seconds
        last_state: DOMSnapshot | None = None
        stable_key: tuple[Any, ...] | None = None
        stable_count = 0
        stable_since: float | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(self.config.server.poll_interval_seconds)
            state = await self._adapter.probe()
            last_state = state
            if state.verification_required or state.login_visible or state.rate_limited:
                self._adapter.ensure_page_state(state)
            parsed = urlparse(state.url)
            is_root = parsed.hostname == "chat.deepseek.com" and parsed.path.rstrip("/") == ""
            hydrated_blank = (
                is_root
                and state.document_ready
                and state.composer_found
                and bool(state.composer_identity)
                and state.composer_empty
                and not state.stop_visible
                and not state.users
                and not state.assistants
            )
            if hydrated_blank:
                key = (
                    state.composer_identity,
                    state.reasoning_enabled,
                    state.search_enabled,
                )
                now = time.monotonic()
                if key == stable_key:
                    stable_count += 1
                else:
                    stable_count = 1
                    stable_since = now
                stable_key = key
                if (
                    stable_count >= FRESH_CHAT_STABLE_POLLS
                    and stable_since is not None
                    and now - stable_since >= self._fresh_chat_settle_seconds
                ):
                    return state
            else:
                stable_key = None
                stable_count = 0
                stable_since = None
        detail = "page loaded" if last_state else "no page state"
        raise DOMElementNotFound(f"Could not confirm a fresh empty DeepSeek chat ({detail})")

    @staticmethod
    def _new_user_turn(
        baseline: DOMSnapshot, current: DOMSnapshot, prompt: str
    ) -> bool:
        if not current.users:
            return False
        normalized_prompt = " ".join(prompt.split())
        normalized_latest = " ".join(current.users[-1].text.split())
        if normalized_latest != normalized_prompt:
            return False
        if len(current.users) > len(baseline.users):
            return True
        latest = current.users[-1]
        old_latest = baseline.users[-1] if baseline.users else None
        if old_latest is None:
            return bool(latest.text)
        if latest.identity == old_latest.identity:
            return False
        normalized_old = " ".join(old_latest.text.split())
        return normalized_latest == normalized_prompt and normalized_latest != normalized_old

    @staticmethod
    def _new_assistant_turn(
        baseline: DOMSnapshot, current: DOMSnapshot
    ) -> TurnSnapshot | None:
        if not current.assistants:
            return None
        baseline_ids = {turn.identity for turn in baseline.assistants}
        baseline_texts = {turn.text for turn in baseline.assistants if turn.text}
        latest = current.assistants[-1]
        if len(current.assistants) > len(baseline.assistants):
            return latest
        if latest.identity not in baseline_ids and latest.text not in baseline_texts:
            return latest
        return None

    async def _wait_for_send_ack(
        self,
        baseline: DOMSnapshot,
        prompt: str,
        *,
        cancel_event: asyncio.Event,
        timeout: float = MAX_SEND_ACK_SECONDS,
    ) -> DOMSnapshot:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                raise GenerationCancelled("Request cancelled before send confirmation")
            state = await self._adapter.probe()
            self._adapter.ensure_page_state(state)
            if self._new_user_turn(baseline, state, prompt):
                return state
            await asyncio.sleep(self.config.server.poll_interval_seconds)
        # A second click might duplicate the message, so this state is terminal.
        raise SendNotAcknowledged(
            "Send outcome is unknown because no new user turn appeared; not retried"
        )

    async def _start_network_monitor(self) -> DeepSeekNetworkMonitor | None:
        """Subscribe before the single click when this CDP page supports events."""

        page = self._page
        if page is None or not callable(getattr(page, "subscribe_events", None)):
            return None
        monitor = self._network_monitor_factory(page)
        try:
            await monitor.start()
        except (CDPConnectionError, CDPProtocolError):
            # Older CDP implementations can omit response-body streaming. The
            # foreground DOM state machine remains a conservative fallback.
            await monitor.close()
            return None
        return monitor

    async def _wait_for_send_confirmation(
        self,
        baseline: DOMSnapshot,
        prompt: str,
        *,
        cancel_event: asyncio.Event,
        timeout: float,
        monitor: DeepSeekNetworkMonitor | None,
    ) -> str:
        """Accept one authoritative network ready or the matching DOM user turn."""

        dom_task = asyncio.create_task(
            self._wait_for_send_ack(
                baseline,
                prompt,
                cancel_event=cancel_event,
                timeout=timeout,
            ),
            name="deepseek-dom-send-ack",
        )
        network_task = (
            asyncio.create_task(
                monitor.wait_ready(timeout=timeout),
                name="deepseek-network-send-ack",
            )
            if monitor is not None
            else None
        )
        cancel_task = asyncio.create_task(
            cancel_event.wait(), name="deepseek-send-ack-cancel"
        )
        tasks: set[asyncio.Task[Any]] = {dom_task, cancel_task}
        if network_task is not None:
            tasks.add(network_task)
        deadline = time.monotonic() + timeout
        dom_confirmed = False
        grace_deadline: float | None = None
        try:
            while tasks:
                active_deadline = grace_deadline or deadline
                remaining = active_deadline - time.monotonic()
                if remaining <= 0:
                    if dom_confirmed:
                        return "dom"
                    break
                done, _ = await asyncio.wait(
                    tasks,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    if dom_confirmed:
                        return "dom"
                    break
                if cancel_task in done and cancel_task.result():
                    raise GenerationCancelled("Request cancelled before send confirmation")

                # A DeepSeek SSE ready event is the strongest acknowledgement.
                if network_task is not None and network_task in done:
                    tasks.discard(network_task)
                    try:
                        network_task.result()
                    except (GenerationRejected, DeepSeekCompletionRejected):
                        raise
                    except (CDPConnectionError, CDPProtocolError, TimeoutError):
                        if dom_confirmed:
                            return "dom"
                    else:
                        return "network"

                if dom_task in done:
                    tasks.discard(dom_task)
                    try:
                        dom_task.result()
                    except GenerationCancelled:
                        raise
                    except GenerationRejected:
                        raise
                    except Exception:
                        pass
                    else:
                        if monitor is not None and monitor.ready_seen:
                            return "network"
                        if network_task is None or network_task.done():
                            return "dom"
                        dom_confirmed = True
                        grace_deadline = min(
                            deadline,
                            time.monotonic() + NETWORK_READY_GRACE_SECONDS,
                        )
                if not any(task in tasks for task in (dom_task, network_task)):
                    if dom_confirmed:
                        return "dom"
                    break
            raise SendNotAcknowledged(
                "Send outcome is unknown because neither the DeepSeek response "
                "stream nor a matching user turn acknowledged it; not retried"
            )
        finally:
            for task in (dom_task, network_task, cancel_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (dom_task, network_task, cancel_task) if task is not None),
                return_exceptions=True,
            )

    async def _wait_until_send_ready(
        self, expected_prompt: str, *, timeout_seconds: float = 5
    ) -> DOMSnapshot:
        deadline = time.monotonic() + timeout_seconds
        last_state: DOMSnapshot | None = None
        stable_key: tuple[str, str] | None = None
        stable_count = 0
        normalized_expected = " ".join(expected_prompt.split())
        while time.monotonic() < deadline:
            state = await self._adapter.probe()
            last_state = state
            self._adapter.ensure_ready_to_compose(state)
            normalized_actual = " ".join(state.composer_text.split())
            ready = (
                state.document_ready
                and bool(state.composer_identity)
                and state.send_found
                and not state.composer_empty
                and normalized_actual == normalized_expected
            )
            if ready:
                key = (state.composer_identity, normalized_actual)
                stable_count = stable_count + 1 if key == stable_key else 1
                stable_key = key
                if stable_count >= SEND_READY_STABLE_POLLS:
                    return state
            else:
                stable_key = None
                stable_count = 0
            await asyncio.sleep(self.config.server.poll_interval_seconds)
        del last_state
        raise DOMElementNotFound("DeepSeek send button did not become ready after typing")

    async def stream_reply(
        self,
        prompt: str,
        *,
        model: str,
        cancel_event: asyncio.Event | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[ReplyDelta]:
        """Send once, then yield monotonic reasoning/final deltas.

        The click is never retried. A send is acknowledged by this click's new
        completion stream (preferred) or by a matching new DOM user turn. The
        network stream is the output source in background/minimized Chrome;
        conservative DOM observation remains a foreground fallback.
        """

        if model not in self.config.deepseek.models:
            raise UnsupportedModel(f"Unsupported model: {model}")
        if not prompt.strip():
            raise ValueError("Prompt cannot be empty")
        event = cancel_event or asyncio.Event()
        timeout_seconds = (
            self.config.server.request_timeout_seconds if timeout is None else timeout
        )
        if timeout_seconds <= 0:
            raise ValueError("Timeout must be positive")
        if event.is_set():
            raise GenerationCancelled("Request cancelled before browser work started")
        request_deadline = time.monotonic() + timeout_seconds
        completed = False

        async with self._generation_lock:
            if not self.connected:
                await self.connect()
            adapter = self._adapter
            baseline = await self.start_fresh_chat()
            adapter.ensure_ready_to_compose(baseline)
            if baseline.stop_visible:
                raise GenerationRejected("The dedicated DeepSeek tab is already generating")
            await adapter.disable_web_search(baseline)
            baseline = await adapter.probe()
            adapter.ensure_ready_to_compose(baseline)
            await adapter.set_reasoning(model == "deepseek-reasoner", baseline)
            baseline = await adapter.probe()
            adapter.ensure_ready_to_compose(baseline)
            if event.is_set():
                raise GenerationCancelled("Request cancelled before typing")
            await adapter.type_prompt(prompt)
            if event.is_set():
                raise GenerationCancelled("Request cancelled after typing")
            await self._wait_until_send_ready(prompt)
            if event.is_set():
                raise GenerationCancelled("Request cancelled before send")
            remaining = request_deadline - time.monotonic()
            if remaining <= self.config.server.poll_interval_seconds:
                raise GenerationTimeout("Request timeout expired before send")
            monitor = await self._start_network_monitor()
            if event.is_set():
                if monitor is not None:
                    await monitor.close()
                raise GenerationCancelled("Request cancelled before send")
            remaining = request_deadline - time.monotonic()
            if remaining <= self.config.server.poll_interval_seconds:
                if monitor is not None:
                    await monitor.close()
                raise GenerationTimeout("Request timeout expired before send")
            click_attempted = False
            send_confirmed = False
            try:
                click_attempted = True
                await adapter.click_send()
                remaining = request_deadline - time.monotonic()
                if remaining <= 0:
                    raise SendNotAcknowledged(
                        "Send outcome is unknown because the request deadline expired"
                    )
                confirmation = await self._wait_for_send_confirmation(
                    baseline,
                    prompt,
                    cancel_event=event,
                    timeout=min(MAX_SEND_ACK_SECONDS, remaining),
                    monitor=monitor,
                )
                send_confirmed = True
            except BaseException as exc:
                if isinstance(exc, DOMElementNotFound):
                    # The click script returned false, so no send was dispatched.
                    if monitor is not None:
                        await monitor.close()
                    raise
                network_confirmed = bool(monitor and monitor.ready_seen)
                known_rejection = isinstance(
                    exc, (GenerationRejected, DeepSeekCompletionRejected)
                )
                # Only a click with neither network ready nor a matching DOM
                # user turn is quarantined as unknown. Known server rejection
                # and all post-ready failures must not poison the next request.
                if click_attempted and not (
                    send_confirmed or network_confirmed or known_rejection
                ):
                    self._send_uncertain = True
                try:
                    await adapter.click_stop()
                except Exception:
                    pass
                if monitor is not None:
                    await monitor.close()
                raise

            emitted_reasoning = ""
            emitted_content = ""
            network_fallback = False
            if confirmation == "network":
                assert monitor is not None
                try:
                    while time.monotonic() < request_deadline:
                        if event.is_set():
                            raise GenerationCancelled("Request cancelled")
                        remaining = request_deadline - time.monotonic()
                        try:
                            item = await monitor.next_event(
                                timeout=min(
                                    self.config.server.poll_interval_seconds,
                                    max(remaining, 0.001),
                                )
                            )
                        except TimeoutError:
                            continue
                        if item.kind == "reasoning_delta":
                            emitted_reasoning += item.text
                            yield ReplyDelta(reasoning_content=item.text)
                        elif item.kind == "content_delta":
                            emitted_content += item.text
                            yield ReplyDelta(content=item.text)
                        elif item.kind == "finished":
                            completed = True
                            return
                    raise GenerationTimeout(
                        f"DeepSeek did not finish within {timeout_seconds:g} seconds"
                    )
                except (CDPConnectionError, CDPProtocolError):
                    # A foreground page can still provide a verified monotonic
                    # answer if CDP response streaming becomes unavailable.
                    network_fallback = True
                finally:
                    await monitor.close()
                    if not completed and not network_fallback:
                        try:
                            await adapter.click_stop()
                        except Exception:
                            pass

            if monitor is not None:
                await monitor.close()

            deadline = request_deadline
            last_pair = (emitted_reasoning, emitted_content)
            stable_count = 0
            target_identity: str | None = None
            try:
                while time.monotonic() < deadline:
                    if event.is_set():
                        raise GenerationCancelled("Request cancelled")
                    state = await adapter.probe()
                    adapter.ensure_page_state(state)
                    target = None
                    if target_identity:
                        target = next(
                            (
                                turn
                                for turn in state.assistants
                                if turn.identity == target_identity
                            ),
                            None,
                        )
                    if target is None:
                        target = self._new_assistant_turn(baseline, state)
                        if target:
                            target_identity = target.identity

                    if target is not None:
                        reasoning = target.reasoning
                        content = target.content or (
                            target.text if not target.reasoning else ""
                        )
                        # DeepSeek can collapse its reasoning panel once the
                        # final answer appears. Already emitted text is retained.
                        if emitted_reasoning and not reasoning:
                            reasoning = emitted_reasoning
                        pair = (reasoning, content)
                        if state.stop_visible:
                            stable_count = 0
                        else:
                            stable_count = stable_count + 1 if pair == last_pair else 1
                        last_pair = pair

                        reasoning_delta = ""
                        content_delta = ""
                        if reasoning.startswith(emitted_reasoning):
                            reasoning_delta = reasoning[len(emitted_reasoning) :]
                        elif emitted_reasoning:
                            raise DOMElementNotFound(
                                "DeepSeek reasoning text changed non-monotonically"
                            )
                        if content.startswith(emitted_content):
                            content_delta = content[len(emitted_content) :]
                        elif emitted_content:
                            raise DOMElementNotFound(
                                "DeepSeek answer text changed non-monotonically"
                            )
                        if reasoning_delta or content_delta:
                            emitted_reasoning = reasoning
                            emitted_content = content
                            yield ReplyDelta(
                                content=content_delta,
                                reasoning_content=reasoning_delta,
                            )

                        if (
                            content
                            and not state.stop_visible
                            and stable_count >= self.config.server.stable_polls
                        ):
                            completed = True
                            return
                    await asyncio.sleep(self.config.server.poll_interval_seconds)
                raise GenerationTimeout(
                    f"DeepSeek did not finish within {timeout_seconds:g} seconds"
                )
            except asyncio.CancelledError:
                raise
            finally:
                if not completed:
                    try:
                        await adapter.click_stop()
                    except Exception:
                        pass

    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        cancel_event: asyncio.Event | None = None,
        timeout: float | None = None,
    ) -> Reply:
        content: list[str] = []
        reasoning: list[str] = []
        async for delta in self.stream_reply(
            prompt,
            model=model,
            cancel_event=cancel_event,
            timeout=timeout,
        ):
            content.append(delta.content)
            reasoning.append(delta.reasoning_content)
        return Reply(content="".join(content), reasoning_content="".join(reasoning))

    async def stop_generation(self) -> bool:
        if not self.connected:
            return False
        return await self._adapter.click_stop()

    async def close(self) -> None:
        page, self._page = self._page, None
        self._dom = None
        if page is not None:
            await page.close()
        # Keep Chrome open so the user can complete login/verification manually.
