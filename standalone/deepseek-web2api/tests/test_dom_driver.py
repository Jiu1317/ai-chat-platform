from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from deepseek_web2api.api_server import APIServer, _build_prompt
from deepseek_web2api.cdp_driver import CDPDriver, CDPEvent, CDPPage, Reply, ReplyDelta
from deepseek_web2api.config import (
    ChromeConfig,
    Config,
    DeepSeekConfig,
    ServerConfig,
    load_config,
)
from deepseek_web2api.deepseek_sse import DeepSeekSSEEvent
from deepseek_web2api.dom_adapter import DeepSeekDOMAdapter, DOMSnapshot, TurnSnapshot
from deepseek_web2api.exceptions import (
    AuthenticationRequired,
    CDPConnectionError,
    CDPProtocolError,
    ConfigurationError,
    DeepSeekCompletionRejected,
    DOMElementNotFound,
    GenerationCancelled,
    GenerationRejected,
    HumanVerificationRequired,
    SendNotAcknowledged,
)
from deepseek_web2api.network_capture import DeepSeekNetworkMonitor
from deepseek_web2api.selectors import SELECTORS


def turn(
    identity: str,
    text: str,
    *,
    reasoning: str = "",
    content: str | None = None,
) -> TurnSnapshot:
    return TurnSnapshot(identity, text, reasoning, text if content is None else content)


def state(
    *,
    users: tuple[TurnSnapshot, ...] = (),
    assistants: tuple[TurnSnapshot, ...] = (),
    stop: bool = False,
    send: bool = False,
    composer_empty: bool = True,
    composer_text: str | None = None,
    composer_identity: str = "composer:node:1",
    document_ready: bool = True,
    url: str = "https://chat.deepseek.com/",
    logged_in: bool = True,
    login: bool = False,
    captcha: bool = False,
    limited: bool = False,
    reasoning_available: bool = True,
    reasoning_enabled: bool = False,
    search_available: bool = True,
    search_enabled: bool = False,
) -> DOMSnapshot:
    resolved_text = ("" if composer_empty else "typed") if composer_text is None else composer_text
    return DOMSnapshot(
        url=url,
        title="DeepSeek",
        document_ready=document_ready,
        composer_found=logged_in,
        composer_identity=composer_identity if logged_in else "",
        composer_empty=composer_empty,
        composer_text=resolved_text,
        send_found=send,
        stop_visible=stop,
        logged_in=logged_in,
        verification_required=captcha,
        login_visible=login,
        rate_limited=limited,
        rate_limit_text="请求过多" if limited else "",
        reasoning_available=reasoning_available,
        reasoning_enabled=reasoning_enabled,
        search_available=search_available,
        search_enabled=search_enabled,
        users=users,
        assistants=assistants,
    )


def raw_state(snapshot: DOMSnapshot) -> dict[str, Any]:
    return {
        "url": snapshot.url,
        "title": snapshot.title,
        "documentReady": snapshot.document_ready,
        "composerFound": snapshot.composer_found,
        "composerIdentity": snapshot.composer_identity,
        "composerEmpty": snapshot.composer_empty,
        "composerText": snapshot.composer_text,
        "sendFound": snapshot.send_found,
        "stopVisible": snapshot.stop_visible,
        "loggedIn": snapshot.logged_in,
        "verificationRequired": snapshot.verification_required,
        "loginVisible": snapshot.login_visible,
        "rateLimited": snapshot.rate_limited,
        "rateLimitText": snapshot.rate_limit_text,
        "reasoningAvailable": snapshot.reasoning_available,
        "reasoningEnabled": snapshot.reasoning_enabled,
        "searchAvailable": snapshot.search_available,
        "searchEnabled": snapshot.search_enabled,
        "users": [],
        "assistants": [],
    }


class FakePage:
    connected = True

    def __init__(
        self,
        evaluations: list[Any] | None = None,
        command_results: list[dict[str, Any]] | None = None,
    ) -> None:
        self.evaluations = deque(evaluations or [])
        self.command_results = deque(command_results or [])
        self.expressions: list[str] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def connect(self) -> None:
        return None

    async def evaluate(self, expression: str, *, timeout: float = 15) -> Any:
        del timeout
        self.expressions.append(expression)
        return self.evaluations.popleft()

    async def command(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 15
    ) -> dict[str, Any]:
        del timeout
        self.commands.append((method, params or {}))
        return self.command_results.popleft() if self.command_results else {}

    async def close(self) -> None:
        self.closed = True


class FakeAdapter:
    def __init__(self, snapshots: list[DOMSnapshot]) -> None:
        self.snapshots = deque(snapshots)
        self.last = snapshots[-1]
        self.prompts: list[str] = []
        self.send_clicks = 0
        self.stop_clicks = 0
        self.reasoning_values: list[bool] = []
        self.search_disable_calls = 0

    async def probe(self) -> DOMSnapshot:
        if self.snapshots:
            self.last = self.snapshots.popleft()
        return self.last

    @staticmethod
    def ensure_page_state(value: DOMSnapshot) -> None:
        DeepSeekDOMAdapter.ensure_page_state(value)

    @staticmethod
    def ensure_ready_to_send(value: DOMSnapshot) -> None:
        DeepSeekDOMAdapter.ensure_ready_to_send(value)

    @staticmethod
    def ensure_ready_to_compose(value: DOMSnapshot) -> None:
        DeepSeekDOMAdapter.ensure_ready_to_compose(value)

    async def type_prompt(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def click_send(self) -> None:
        self.send_clicks += 1

    async def click_stop(self) -> bool:
        self.stop_clicks += 1
        return True

    async def set_reasoning(
        self, enabled: bool, value: DOMSnapshot | None = None
    ) -> None:
        del value
        self.reasoning_values.append(enabled)

    async def disable_web_search(self, value: DOMSnapshot | None = None) -> None:
        del value
        self.search_disable_calls += 1


class FakeNetworkSubscription:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[CDPEvent | BaseException] = asyncio.Queue()
        self.request_id: str | None = None
        self.closed = False

    async def get(self, *, timeout: float | None = None) -> CDPEvent:
        item = (
            await self.queue.get()
            if timeout is None
            else await asyncio.wait_for(self.queue.get(), timeout=timeout)
        )
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    def restrict_request_id(self, request_id: str) -> None:
        self.request_id = request_id

    def push(self, event_method: str, **params: Any) -> None:
        if self.request_id is not None and params.get("requestId") != self.request_id:
            return
        self.queue.put_nowait(CDPEvent(event_method, params))


class FakeNetworkPage:
    def __init__(
        self,
        *,
        buffered_data: str = "",
        response_body: str = "",
        stream_error: BaseException | None = None,
    ) -> None:
        self.subscription = FakeNetworkSubscription()
        self.buffered_data = buffered_data
        self.response_body = response_body
        self.stream_error = stream_error
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def command(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 15
    ) -> dict[str, Any]:
        del timeout
        self.commands.append((method, params or {}))
        if method == "Network.streamResourceContent":
            if self.stream_error is not None:
                raise self.stream_error
            return {"bufferedData": self.buffered_data}
        if method == "Network.getResponseBody":
            return {"body": self.response_body, "base64Encoded": False}
        return {}

    def subscribe_events(
        self, methods: set[str] | frozenset[str], *, max_queue: int = 1024
    ) -> FakeNetworkSubscription:
        del methods, max_queue
        return self.subscription


def network_sse_body(content: str = "网络回复") -> bytes:
    def frame(event: str, payload: object) -> bytes:
        return (
            f"event: {event}\ndata: "
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n\n"
        ).encode()

    return b"".join(
        [
            frame("ready", {"request_message_id": 1, "response_message_id": 2}),
            frame(
                "update_session",
                {
                    "v": {
                        "response": {
                            "status": "WIP",
                            "fragments": [{"type": "RESPONSE", "content": content}],
                        }
                    }
                },
            ),
            frame(
                "update_session",
                {"p": "response/status", "o": "SET", "v": "FINISHED"},
            ),
            frame("close", {}),
        ]
    )


def driver_with(adapter: FakeAdapter, *, stable_polls: int = 2) -> CDPDriver:
    config = Config(
        server=ServerConfig(poll_interval_seconds=0.001, stable_polls=stable_polls)
    )
    driver = CDPDriver(config, fresh_chat_settle_seconds=0.003)
    driver._page = FakePage()  # type: ignore[assignment]
    driver._dom = adapter  # type: ignore[assignment]
    return driver


@pytest.mark.asyncio
async def test_driver_discards_page_when_cdp_initialization_fails() -> None:
    class FailingConnectPage(FakePage):
        async def connect(self) -> None:
            raise CDPConnectionError("initialization failed")

    page = FailingConnectPage()
    driver = CDPDriver(
        Config(chrome=ChromeConfig(auto_launch=False)),
        page_factory=lambda _url: page,
    )

    async def list_targets() -> list[dict[str, Any]]:
        return [
            {
                "type": "page",
                "url": "https://chat.deepseek.com/",
                "webSocketDebuggerUrl": "ws://test",
            }
        ]

    driver._list_targets = list_targets  # type: ignore[method-assign]

    with pytest.raises(CDPConnectionError, match="initialization failed"):
        await driver.connect()

    assert page.closed
    assert driver._page is None
    assert driver._dom is None
    assert not driver.connected


@pytest.mark.asyncio
async def test_cdp_event_subscription_keeps_only_safe_network_fields() -> None:
    page = CDPPage("ws://unused")
    subscription = page.subscribe_events({"Network.requestWillBeSent"}, max_queue=2)
    page._publish_event(
        "Network.requestWillBeSent",
        {
            "requestId": "request-1",
            "request": {
                "url": "https://chat.deepseek.com/api/v0/chat/completion",
                "headers": {"Cookie": "must-not-be-retained"},
                "postData": "must-not-be-retained",
            },
        },
    )
    event = await subscription.get(timeout=0.1)
    assert event.params == {
        "requestId": "request-1",
        "url": "https://chat.deepseek.com/api/v0/chat/completion",
        "method": "",
    }
    await subscription.close()


@pytest.mark.asyncio
async def test_cdp_event_subscription_fails_closed_on_overflow() -> None:
    page = CDPPage("ws://unused")
    subscription = page.subscribe_events({"Network.dataReceived"}, max_queue=1)
    page._publish_event(
        "Network.dataReceived", {"requestId": "request-1", "data": "first"}
    )
    page._publish_event(
        "Network.dataReceived", {"requestId": "request-1", "data": "second"}
    )
    with pytest.raises(CDPProtocolError, match="overflowed"):
        await subscription.get(timeout=0.1)


@pytest.mark.asyncio
async def test_cdp_event_subscription_close_wakes_pending_reader() -> None:
    page = CDPPage("ws://unused")
    subscription = page.subscribe_events({"Network.dataReceived"}, max_queue=1)
    waiter = asyncio.create_task(subscription.get())
    await asyncio.sleep(0)
    await subscription.close()
    with pytest.raises(Exception, match="subscription closed"):
        await waiter


@pytest.mark.asyncio
async def test_cancelled_cdp_command_removes_pending_router_entry() -> None:
    class SilentWebSocket:
        async def send(self, payload: str) -> None:
            del payload

    page = CDPPage("ws://unused")
    page._ws = SilentWebSocket()
    command = asyncio.create_task(page.command("Network.enable", timeout=10))
    await asyncio.sleep(0)
    assert page._pending
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    assert page._pending == {}


@pytest.mark.asyncio
async def test_network_monitor_streams_only_the_new_matching_post_request() -> None:
    body = network_sse_body()
    split = body.index("网络".encode()) + 1
    page = FakeNetworkPage(
        buffered_data=base64.b64encode(body[:split]).decode("ascii")
    )
    monitor = DeepSeekNetworkMonitor(page)
    await monitor.start()
    subscription = page.subscription
    subscription.push(
        "Network.requestWillBeSent",
        requestId="old-get",
        url="https://chat.deepseek.com/api/v0/chat/completion",
        method="GET",
    )
    subscription.push(
        "Network.requestWillBeSent",
        requestId="other-origin",
        url="https://example.invalid/api/v0/chat/completion",
        method="POST",
    )
    subscription.push(
        "Network.requestWillBeSent",
        requestId="chosen",
        url="https://chat.deepseek.com/api/v0/chat/completion",
        method="POST",
    )
    subscription.push(
        "Network.responseReceived",
        requestId="chosen",
        url="https://chat.deepseek.com/api/v0/chat/completion",
        status=200,
        mimeType="text/event-stream",
    )
    subscription.push(
        "Network.dataReceived",
        requestId="chosen",
        data=base64.b64encode(body[split:]).decode("ascii"),
    )

    await monitor.wait_ready(timeout=0.2)
    content = await monitor.next_event(timeout=0.2)
    assert content.kind == "content_delta"
    with pytest.raises(TimeoutError):
        await monitor.next_event(timeout=0.01)
    subscription.push("Network.loadingFinished", requestId="chosen")
    finished = await monitor.next_event(timeout=0.2)
    assert monitor.request_id == "chosen"
    assert subscription.request_id == "chosen"
    assert content.text == "网络回复"
    assert finished.kind == "finished"
    assert any(method == "Network.streamResourceContent" for method, _ in page.commands)
    assert all(method != "Network.getResponseBody" for method, _ in page.commands)
    await monitor.close()
    assert subscription.closed


@pytest.mark.asyncio
async def test_network_monitor_falls_back_to_completed_response_body() -> None:
    page = FakeNetworkPage(
        response_body=network_sse_body("回退").decode(),
        stream_error=CDPProtocolError("unsupported"),
    )
    monitor = DeepSeekNetworkMonitor(page)
    await monitor.start()
    subscription = page.subscription
    subscription.push(
        "Network.requestWillBeSent",
        requestId="fallback",
        url="https://chat.deepseek.com/api/v0/chat/completion",
        method="POST",
    )
    subscription.push(
        "Network.responseReceived",
        requestId="fallback",
        url="https://chat.deepseek.com/api/v0/chat/completion",
        status=200,
        mimeType="text/event-stream",
    )
    subscription.push("Network.loadingFinished", requestId="fallback")
    await monitor.wait_ready(timeout=0.2)
    events = []
    while not events or events[-1].kind != "finished":
        events.append(await monitor.next_event(timeout=0.2))
    assert "".join(item.text for item in events if item.kind == "content_delta") == "回退"
    assert any(method == "Network.getResponseBody" for method, _ in page.commands)
    await monitor.close()


@pytest.mark.asyncio
async def test_prompt_is_inserted_as_cdp_data_not_javascript() -> None:
    prompt = "`); window.stolen = document.cookie; //"
    page = FakePage(
        command_results=[
            {"result": {"type": "object", "objectId": "composer-1"}},
            {"result": {"type": "boolean", "value": True}},
            {},
        ]
    )
    adapter = DeepSeekDOMAdapter(page)
    await adapter.type_prompt(prompt)
    assert [method for method, _ in page.commands] == [
        "Runtime.evaluate",
        "Runtime.callFunctionOn",
        "Runtime.releaseObject",
    ]
    lookup = page.commands[0][1]["expression"]
    call = page.commands[1][1]
    assert prompt not in lookup
    assert prompt not in call["functionDeclaration"]
    assert call["arguments"] == [{"value": prompt}]
    assert "InputEvent" in call["functionDeclaration"]
    assert "Cookie" not in lookup


def test_selectors_are_centralized_and_have_semantic_fallbacks() -> None:
    assert len(SELECTORS.composer) >= 4
    assert len(SELECTORS.assistant) >= 4
    assert "发送" in SELECTORS.send_labels
    assert "停止" in SELECTORS.stop_labels
    assert any("0 0 14 16" in selector for selector in SELECTORS.send_button)
    assert any("ds-button--primary" in selector for selector in SELECTORS.primary_action)
    assert ".fbb737a4" in SELECTORS.user


@pytest.mark.parametrize(
    ("snapshot", "error_type", "code"),
    [
        (state(logged_in=False, login=True), AuthenticationRequired, "login_required"),
        (state(logged_in=False, captcha=True), HumanVerificationRequired, "captcha_required"),
        (state(limited=True), GenerationRejected, "rate_limited"),
    ],
)
def test_page_failures_are_distinct(
    snapshot: DOMSnapshot, error_type: type[Exception], code: str
) -> None:
    with pytest.raises(error_type) as caught:
        DeepSeekDOMAdapter.ensure_page_state(snapshot)
    assert caught.value.code == code


def test_send_button_is_required_only_before_send() -> None:
    generating = state(stop=True, send=False)
    DeepSeekDOMAdapter.ensure_page_state(generating)
    DeepSeekDOMAdapter.ensure_ready_to_compose(generating)
    with pytest.raises(DOMElementNotFound) as caught:
        DeepSeekDOMAdapter.ensure_ready_to_send(generating)
    assert caught.value.code == "dom_changed"


def test_send_readiness_requires_nonempty_composer() -> None:
    with pytest.raises(DOMElementNotFound):
        DeepSeekDOMAdapter.ensure_ready_to_send(state(send=True, composer_empty=True))
    DeepSeekDOMAdapter.ensure_ready_to_send(state(send=True, composer_empty=False))


@pytest.mark.asyncio
async def test_stop_click_uses_enabled_primary_action_only_when_composer_empty() -> None:
    page = FakePage([True])
    adapter = DeepSeekDOMAdapter(page)
    assert await adapter.click_stop() is True
    script = page.expressions[0]
    assert "structuralWhenEmpty = true" in script
    assert "ds-button--primary" in script
    assert script.count("button.click()") == 1
    assert page.commands == []


@pytest.mark.asyncio
async def test_send_uses_one_dom_click_and_never_retries_or_dispatches_mouse() -> None:
    page = FakePage([True])
    adapter = DeepSeekDOMAdapter(page)
    await adapter.click_send()
    assert len(page.expressions) == 1
    script = page.expressions[0]
    assert "structuralWhenEmpty = false" in script
    assert script.count("button.click()") == 1
    assert page.commands == []


@pytest.mark.asyncio
async def test_reasoning_and_search_toggles_are_selected_by_distinct_labels() -> None:
    reasoning_page = FakePage(
        [
            True,
            raw_state(state(reasoning_enabled=True, search_enabled=True)),
            raw_state(state(reasoning_enabled=True, search_enabled=True)),
        ]
    )
    reasoning = DeepSeekDOMAdapter(reasoning_page)
    await reasoning.set_reasoning(
        True, state(reasoning_enabled=False, search_enabled=True)
    )
    reasoning_click = reasoning_page.expressions[0]
    assert "深度思考" in reasoning_click
    assert "联网搜索" not in reasoning_click
    assert "requireLabel = true" in reasoning_click
    assert reasoning_click.count("button.click()") == 1
    assert reasoning_page.commands == []

    search_page = FakePage(
        [
            True,
            raw_state(state(reasoning_enabled=True, search_enabled=False)),
            raw_state(state(reasoning_enabled=True, search_enabled=False)),
        ]
    )
    search = DeepSeekDOMAdapter(search_page)
    await search.disable_web_search(
        state(reasoning_enabled=True, search_enabled=True)
    )
    search_click = search_page.expressions[0]
    assert "联网搜索" in search_click
    assert "深度思考" not in search_click
    assert "requireLabel = true" in search_click
    assert search_click.count("button.click()") == 1
    assert search_page.commands == []


def test_only_exact_official_host_is_adopted() -> None:
    targets = [
        {"type": "page", "url": "https://chat.deepseek.com.evil.example/"},
        {"type": "page", "url": "https://chat.deepseek.com/a", "id": "right"},
    ]
    assert CDPDriver._choose_deepseek_target(targets)["id"] == "right"


@pytest.mark.asyncio
async def test_every_request_navigation_starts_from_a_fresh_blank_chat() -> None:
    adapter = FakeAdapter([state(), state()])
    driver = driver_with(adapter)
    await driver.start_fresh_chat()
    await driver.start_fresh_chat()
    navigations = [
        command
        for command in driver._page.commands  # type: ignore[union-attr]
        if command == ("Page.navigate", {"url": "https://chat.deepseek.com/"})
    ]
    assert len(navigations) == 2


@pytest.mark.asyncio
async def test_fresh_chat_waits_for_hydration_and_stable_composer_identity() -> None:
    not_ready = state(document_ready=False)
    first_editor = state(composer_identity="composer:node:1")
    second_editor = state(composer_identity="composer:node:2")
    adapter = FakeAdapter(
        [not_ready, first_editor, first_editor, second_editor, second_editor, second_editor, second_editor]
    )
    driver = driver_with(adapter)
    result = await driver.start_fresh_chat()
    assert result.composer_identity == "composer:node:2"


@pytest.mark.asyncio
async def test_fresh_chat_does_not_require_optional_mode_controls() -> None:
    blank = state(reasoning_available=False, search_available=False)
    adapter = FakeAdapter([blank])
    driver = driver_with(adapter)

    result = await driver.start_fresh_chat()

    assert result.composer_found
    assert result.reasoning_available is False
    assert result.search_available is False


@pytest.mark.asyncio
async def test_send_ack_requires_a_new_user_turn() -> None:
    before = state(users=(turn("u1", "old"),))
    adapter = FakeAdapter([before, before, before])
    driver = driver_with(adapter)
    await adapter.click_send()
    with pytest.raises(SendNotAcknowledged) as caught:
        await driver._wait_for_send_ack(
            before, "new", cancel_event=asyncio.Event(), timeout=0.003
        )
    assert caught.value.code == "send_unknown"
    assert adapter.send_clicks == 1
    assert adapter.stop_clicks == 0


@pytest.mark.asyncio
async def test_slow_send_ack_is_waited_for_without_another_click() -> None:
    before = state()
    pending = state(url="https://chat.deepseek.com/a/chat/s/new-session")
    acknowledged = state(
        url="https://chat.deepseek.com/a/chat/s/new-session",
        users=(turn("user:node:1", "slow prompt"),),
        stop=True,
    )
    adapter = FakeAdapter([pending] * 8 + [acknowledged])
    driver = driver_with(adapter)
    await adapter.click_send()
    result = await driver._wait_for_send_ack(
        before,
        "slow prompt",
        cancel_event=asyncio.Event(),
        timeout=0.2,
    )
    assert result is acknowledged
    assert adapter.send_clicks == 1


@pytest.mark.asyncio
async def test_send_preflight_requires_exact_prompt_to_be_stable() -> None:
    wrong = state(send=True, composer_empty=False, composer_text="stale prompt")
    right = state(send=True, composer_empty=False, composer_text="expected prompt")
    adapter = FakeAdapter([wrong, right, right])
    driver = driver_with(adapter)
    result = await driver._wait_until_send_ready("expected prompt", timeout_seconds=0.05)
    assert result is right


def test_send_ack_does_not_accept_unrelated_manual_user_turn() -> None:
    before = state(users=(turn("u1", "old"),))
    unrelated = state(users=(turn("u1", "old"), turn("u2", "manual text")))
    assert not CDPDriver._new_user_turn(before, unrelated, "bridge prompt")


@pytest.mark.asyncio
async def test_observed_deepseek_turn_shape_supports_ack_and_completion() -> None:
    # Captured from the logged-in 2026 UI without cookies: the user text uses
    # .fbb737a4 inside .ds-message and the assistant uses
    # .ds-markdown.ds-assistant-message-main-content inside ._4f9bf79.
    assert ".fbb737a4" in SELECTORS.user
    assert ".ds-markdown" in SELECTORS.assistant
    assert "div._4f9bf79" in SELECTORS.message_block
    observed = raw_state(state())
    observed["users"] = [
        {"identity": "user:node:41", "text": "唯一回归消息"}
    ]
    observed["assistants"] = [
        {
            "identity": "assistant:node:42",
            "text": "收到",
            "reasoning": "",
            "content": "收到",
        }
    ]
    snapshot = await DeepSeekDOMAdapter(FakePage([observed])).probe()
    baseline = state()
    assert CDPDriver._new_user_turn(baseline, snapshot, "唯一回归消息")
    assistant = CDPDriver._new_assistant_turn(baseline, snapshot)
    assert assistant is not None
    assert assistant.identity == "assistant:node:42"
    assert assistant.content == "收到"


@pytest.mark.asyncio
async def test_stream_uses_fresh_chat_and_finishes_after_stable_new_turn() -> None:
    baseline = state()
    typed = state(send=True, composer_empty=False, composer_text="new")
    new_user = turn("u1", "new")
    ack = state(users=(new_user,), stop=True, send=False)
    partial = state(
        users=(new_user,),
        assistants=(turn("a1", "你"),),
        stop=True,
        send=False,
    )
    final = state(
        users=(new_user,),
        assistants=(turn("a1", "你好"),),
        stop=False,
    )
    adapter = FakeAdapter(
        [
            baseline,
            baseline,
            baseline,
            baseline,
            baseline,
            baseline,
            typed,
            typed,
            ack,
            partial,
            final,
            final,
        ]
    )
    driver = driver_with(adapter)
    deltas = [item async for item in driver.stream_reply("new", model="deepseek-chat")]
    assert "".join(item.content for item in deltas) == "你好"
    assert adapter.prompts == ["new"]
    assert adapter.send_clicks == 1
    assert adapter.stop_clicks == 0
    assert adapter.search_disable_calls == 1
    assert ("Page.navigate", {"url": "https://chat.deepseek.com/"}) in driver._page.commands  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_stream_uses_network_when_background_dom_never_renders() -> None:
    class StubMonitor:
        def __init__(self) -> None:
            self.ready_seen = False
            self.events = deque(
                [
                    DeepSeekSSEEvent("content_delta", text="后台"),
                    DeepSeekSSEEvent("content_delta", text="完成"),
                    DeepSeekSSEEvent("finished"),
                ]
            )
            self.closed = False

        async def start(self) -> None:
            return None

        async def wait_ready(self, *, timeout: float) -> None:
            del timeout
            self.ready_seen = True

        async def next_event(self, *, timeout: float | None = None) -> DeepSeekSSEEvent:
            del timeout
            return self.events.popleft()

        async def close(self) -> None:
            self.closed = True

    baseline = state()
    typed = state(send=True, composer_empty=False, composer_text="background")
    adapter = FakeAdapter([baseline] * 6 + [typed, typed])
    driver = driver_with(adapter)
    page = driver._page
    assert page is not None
    page.subscribe_events = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    monitor = StubMonitor()
    driver._network_monitor_factory = lambda unused: monitor  # type: ignore[assignment]

    deltas = [
        item
        async for item in driver.stream_reply("background", model="deepseek-chat")
    ]
    assert "".join(item.content for item in deltas) == "后台完成"
    assert adapter.send_clicks == 1
    assert adapter.stop_clicks == 0
    assert monitor.closed
    assert driver.busy is False


@pytest.mark.asyncio
async def test_dom_ack_waits_briefly_for_authoritative_network_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlightlyLaterMonitor:
        ready_seen = False

        async def wait_ready(self, *, timeout: float) -> None:
            del timeout
            await asyncio.sleep(0.005)
            self.ready_seen = True

    adapter = FakeAdapter([state()])
    driver = driver_with(adapter)

    async def immediate_dom_ack(*args: Any, **kwargs: Any) -> DOMSnapshot:
        del args, kwargs
        return state(users=(turn("u1", "prompt"),))

    monkeypatch.setattr(driver, "_wait_for_send_ack", immediate_dom_ack)
    result = await driver._wait_for_send_confirmation(
        state(),
        "prompt",
        cancel_event=asyncio.Event(),
        timeout=0.1,
        monitor=SlightlyLaterMonitor(),  # type: ignore[arg-type]
    )
    assert result == "network"


@pytest.mark.asyncio
async def test_dom_ack_becomes_fallback_when_network_grace_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NeverReadyMonitor:
        ready_seen = False

        async def wait_ready(self, *, timeout: float) -> None:
            await asyncio.sleep(timeout)

    adapter = FakeAdapter([state()])
    driver = driver_with(adapter)

    async def immediate_dom_ack(*args: Any, **kwargs: Any) -> DOMSnapshot:
        del args, kwargs
        return state(users=(turn("u1", "prompt"),))

    monkeypatch.setattr(driver, "_wait_for_send_ack", immediate_dom_ack)
    monkeypatch.setattr(
        "deepseek_web2api.cdp_driver.NETWORK_READY_GRACE_SECONDS", 0.005
    )
    result = await driver._wait_for_send_confirmation(
        state(),
        "prompt",
        cancel_event=asyncio.Event(),
        timeout=0.1,
        monitor=NeverReadyMonitor(),  # type: ignore[arg-type]
    )
    assert result == "dom"


@pytest.mark.asyncio
async def test_failed_click_closes_pre_click_network_subscription() -> None:
    class TrackingMonitor:
        ready_seen = False

        def __init__(self) -> None:
            self.closed = False

        async def start(self) -> None:
            return None

        async def wait_ready(self, *, timeout: float) -> None:
            await asyncio.sleep(timeout)

        async def next_event(self, *, timeout: float | None = None) -> DeepSeekSSEEvent:
            await asyncio.sleep(timeout or 0)
            raise AssertionError("network output must not be read after a failed click")

        async def close(self) -> None:
            self.closed = True

    baseline = state()
    typed = state(send=True, composer_empty=False, composer_text="not sent")
    adapter = FakeAdapter([baseline] * 6 + [typed, typed])

    async def fail_click() -> None:
        adapter.send_clicks += 1
        raise DOMElementNotFound("send control disappeared")

    adapter.click_send = fail_click  # type: ignore[method-assign]
    driver = driver_with(adapter)
    page = driver._page
    assert page is not None
    page.subscribe_events = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    monitor = TrackingMonitor()
    driver._network_monitor_factory = lambda unused: monitor  # type: ignore[assignment]

    with pytest.raises(DOMElementNotFound):
        _ = [
            item
            async for item in driver.stream_reply("not sent", model="deepseek-chat")
        ]
    assert monitor.closed
    assert driver.busy is False


@pytest.mark.asyncio
async def test_post_ready_network_error_does_not_quarantine_worker() -> None:
    class RejectingMonitor:
        ready_seen = False

        async def start(self) -> None:
            return None

        async def wait_ready(self, *, timeout: float) -> None:
            del timeout
            self.ready_seen = True

        async def next_event(self, *, timeout: float | None = None) -> DeepSeekSSEEvent:
            del timeout
            raise DeepSeekCompletionRejected("known rejection")

        async def close(self) -> None:
            return None

    baseline = state()
    typed = state(send=True, composer_empty=False, composer_text="known failure")
    adapter = FakeAdapter([baseline] * 6 + [typed, typed])
    driver = driver_with(adapter)
    page = driver._page
    assert page is not None
    page.subscribe_events = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    monitor = RejectingMonitor()
    driver._network_monitor_factory = lambda unused: monitor  # type: ignore[assignment]

    with pytest.raises(DeepSeekCompletionRejected):
        _ = [
            item
            async for item in driver.stream_reply(
                "known failure", model="deepseek-chat"
            )
        ]
    assert adapter.stop_clicks == 1
    assert driver.busy is False


@pytest.mark.asyncio
async def test_post_ready_network_transport_failure_falls_back_without_duplicates() -> None:
    class BrokenAfterDeltaMonitor:
        ready_seen = False

        def __init__(self) -> None:
            self.calls = 0

        async def start(self) -> None:
            return None

        async def wait_ready(self, *, timeout: float) -> None:
            del timeout
            self.ready_seen = True

        async def next_event(self, *, timeout: float | None = None) -> DeepSeekSSEEvent:
            del timeout
            self.calls += 1
            if self.calls == 1:
                return DeepSeekSSEEvent("content_delta", text="你")
            raise CDPConnectionError("stream interrupted")

        async def close(self) -> None:
            return None

    baseline = state()
    typed = state(send=True, composer_empty=False, composer_text="fallback")
    user = turn("u1", "fallback")
    ack = state(users=(user,), stop=True)
    partial = state(users=(user,), assistants=(turn("a1", "你"),), stop=True)
    final = state(users=(user,), assistants=(turn("a1", "你好"),), stop=False)
    adapter = FakeAdapter([baseline] * 6 + [typed, typed, ack, partial, final, final])
    driver = driver_with(adapter)
    page = driver._page
    assert page is not None
    page.subscribe_events = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    monitor = BrokenAfterDeltaMonitor()
    driver._network_monitor_factory = lambda unused: monitor  # type: ignore[assignment]

    deltas = [
        item async for item in driver.stream_reply("fallback", model="deepseek-chat")
    ]
    assert "".join(item.content for item in deltas) == "你好"
    assert adapter.stop_clicks == 0
    assert driver.busy is False


def test_unchanged_old_assistant_is_not_a_new_turn() -> None:
    old = turn("a1", "stale")
    baseline = state(assistants=(old,))
    assert CDPDriver._new_assistant_turn(baseline, state(assistants=(old,))) is None


@pytest.mark.asyncio
async def test_reasoner_does_not_finish_on_reasoning_without_final_text() -> None:
    baseline = state(users=(), assistants=(), reasoning_enabled=False)
    typed = state(send=True, composer_empty=False, composer_text="question")
    ack = state(users=(turn("u1", "question"),), stop=True, send=False)
    reasoning_only = state(
        users=ack.users,
        assistants=(turn("a1", "思考", reasoning="思考", content=""),),
        stop=False,
        send=False,
    )
    final = state(
        users=ack.users,
        assistants=(turn("a1", "思考\n答案", reasoning="思考", content="答案"),),
        stop=False,
    )
    adapter = FakeAdapter(
        [
            baseline,
            baseline,
            baseline,
            baseline,
            baseline,
            baseline,
            typed,
            typed,
            ack,
            reasoning_only,
            reasoning_only,
            final,
            final,
        ]
    )
    driver = driver_with(adapter)
    deltas = [
        item
        async for item in driver.stream_reply("question", model="deepseek-reasoner")
    ]
    assert "".join(item.reasoning_content for item in deltas) == "思考"
    assert "".join(item.content for item in deltas) == "答案"
    assert adapter.reasoning_values == [True]


@pytest.mark.asyncio
async def test_cancellation_stops_active_generation() -> None:
    baseline = state()
    typed = state(send=True, composer_empty=False, composer_text="cancel me")
    ack = state(users=(turn("u1", "cancel me"),), stop=True, send=False)
    generating = state(users=ack.users, stop=True, send=False)
    adapter = FakeAdapter(
        [
            baseline,
            baseline,
            baseline,
            baseline,
            baseline,
            baseline,
            typed,
            typed,
            ack,
            generating,
        ]
    )
    driver = driver_with(adapter)
    cancel = asyncio.Event()
    original_probe = adapter.probe

    async def cancel_during_generation() -> DOMSnapshot:
        snapshot = await original_probe()
        if snapshot is generating:
            cancel.set()
        return snapshot

    adapter.probe = cancel_during_generation  # type: ignore[method-assign]
    with pytest.raises(GenerationCancelled):
        _ = [
            item
            async for item in driver.stream_reply(
                "cancel me", model="deepseek-chat", cancel_event=cancel
            )
        ]
    assert adapter.stop_clicks == 1


@pytest.mark.asyncio
async def test_pre_cancelled_request_never_touches_or_sends_to_page() -> None:
    adapter = FakeAdapter([state()])
    driver = driver_with(adapter)
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(GenerationCancelled):
        _ = [
            item
            async for item in driver.stream_reply(
                "do not send", model="deepseek-chat", cancel_event=cancel
            )
        ]
    assert adapter.prompts == []
    assert adapter.send_clicks == 0
    assert adapter.stop_clicks == 0


@pytest.mark.asyncio
async def test_disconnect_after_typing_never_clicks_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = state()
    adapter = FakeAdapter([baseline, baseline, baseline, baseline, baseline, baseline])
    driver = driver_with(adapter)
    cancel = asyncio.Event()
    original_type = adapter.type_prompt

    async def type_then_disconnect(prompt: str) -> None:
        await original_type(prompt)
        cancel.set()

    monkeypatch.setattr(adapter, "type_prompt", type_then_disconnect)
    with pytest.raises(GenerationCancelled):
        _ = [
            item
            async for item in driver.stream_reply(
                "disconnect", model="deepseek-chat", cancel_event=cancel
            )
        ]
    assert adapter.prompts == ["disconnect"]
    assert adapter.send_clicks == 0
    assert adapter.stop_clicks == 0
    assert driver.busy is False


@pytest.mark.asyncio
async def test_timeout_zero_is_rejected_without_sending() -> None:
    adapter = FakeAdapter([state()])
    driver = driver_with(adapter)
    with pytest.raises(ValueError, match="Timeout must be positive"):
        _ = [
            item
            async for item in driver.stream_reply(
                "hello", model="deepseek-chat", timeout=0
            )
        ]
    assert adapter.send_clicks == 0


@pytest.mark.asyncio
async def test_empty_logged_in_page_is_healthy_even_without_send_button() -> None:
    adapter = FakeAdapter([state(send=False, composer_empty=True)])
    driver = driver_with(adapter)
    result = await driver.status()
    assert result["status"] == "ok"
    assert result["logged_in"] is True


@pytest.mark.asyncio
async def test_unknown_send_is_quarantined_until_blank_root_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = state(send=False, composer_empty=True)
    typed = state(send=True, composer_empty=False, composer_text="hello")
    adapter = FakeAdapter(
        [
            baseline,
            baseline,
            baseline,
            baseline,
            baseline,
            baseline,
            typed,
            typed,
        ]
    )
    driver = driver_with(adapter)

    async def unknown(*args: Any, **kwargs: Any) -> DOMSnapshot:
        del args, kwargs
        raise SendNotAcknowledged("unknown")

    monkeypatch.setattr(driver, "_wait_for_send_ack", unknown)
    with pytest.raises(SendNotAcknowledged):
        _ = [
            item
            async for item in driver.stream_reply("hello", model="deepseek-chat")
        ]
    assert adapter.send_clicks == 1
    assert adapter.stop_clicks == 1
    assert driver.busy is True

    adapter.last = typed
    assert (await driver.status())["status"] == "send_unknown"
    with pytest.raises(SendNotAcknowledged):
        await driver.start_fresh_chat()

    adapter.last = state(send=False, composer_empty=True)
    assert (await driver.status())["status"] == "ok"
    assert driver.busy is False


def test_load_config_expands_profile_and_validates_models(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "chrome": {"cdp_port": 9444, "profile_dir": str(tmp_path / "profile")},
                "server": {"api_keys": ["local-test-key"], "port": 9292},
                "deepseek": {
                    "default_model": "deepseek-reasoner",
                    "models": ["deepseek-chat", "deepseek-reasoner"],
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.chrome.cdp_port == 9444
    assert config.chrome.user_data_dir == tmp_path / "profile"
    assert config.server.api_keys == ("local-test-key",)
    assert config.deepseek.default_model == "deepseek-reasoner"
    assert not (tmp_path / "profile").exists()


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("chrome", "auto_launch", "false", "must be true or false"),
        ("chrome", "cdp_port", "9332", "must be an integer"),
        ("chrome", "startup_timeout_seconds", "30", "must be a number"),
        ("server", "port", True, "must be an integer"),
        ("server", "request_timeout_seconds", "600", "must be a number"),
        ("server", "max_request_bytes", 30.5, "must be an integer"),
        ("deepseek", "default_model", 1, "must be a string"),
    ],
)
def test_load_config_rejects_wrong_scalar_types(
    tmp_path: Path,
    section: str,
    field: str,
    value: Any,
    message: str,
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({section: {field: value}}), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_config(path)


def test_default_ports_are_isolated_from_chatgpt_bridge() -> None:
    config = load_config()
    assert config.chrome.cdp_port == 9332
    assert config.server.port == 9191


@pytest.mark.asyncio
async def test_chrome_launch_keeps_background_rendering_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_launch(*args: str, **kwargs: Any) -> Any:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_launch)
    config = Config(
        chrome=ChromeConfig(
            binary="C:/fake/chrome.exe",
            user_data_dir=tmp_path / "profile",
        )
    )
    driver = CDPDriver(config)
    await driver._launch_chrome()
    args = set(captured["args"])
    assert "--disable-background-timer-throttling" in args
    assert "--disable-backgrounding-occluded-windows" in args
    assert "--disable-renderer-backgrounding" in args


def test_prompt_builder_preserves_roles_and_rejects_non_text() -> None:
    assert _build_prompt(
        [
            {"role": "system", "content": "be concise"},
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        ]
    ) == "[System]\nbe concise\n\n[User]\nhello"


class FakeAPIDriver:
    connected = True
    busy = False

    def __init__(self) -> None:
        self.closed = False

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "cdp_connected": True,
            "logged_in": True,
            "busy": False,
            "target_url": "https://chat.deepseek.com/",
        }

    async def complete(self, prompt: str, **kwargs: Any) -> Reply:
        del prompt, kwargs
        return Reply(content="pong")

    async def stream_reply(self, prompt: str, **kwargs: Any):
        del prompt, kwargs
        yield ReplyDelta(reasoning_content="想")
        yield ReplyDelta(content="pong")

    async def stop_generation(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_api_accepts_bearer_and_x_api_key_and_health_is_public() -> None:
    config = Config(
        server=ServerConfig(api_keys=("secret",)),
        deepseek=DeepSeekConfig(),
        chrome=ChromeConfig(auto_launch=False),
    )
    fake = FakeAPIDriver()
    server = APIServer(config, fake)  # type: ignore[arg-type]
    async with TestClient(TestServer(server.app)) as client:
        health = await client.get("/health")
        assert health.status == 200
        assert (await health.json())["status"] == "ok"

        denied = await client.get("/v1/models")
        assert denied.status == 401
        bearer = await client.get(
            "/v1/models", headers={"Authorization": "Bearer secret"}
        )
        assert bearer.status == 200
        x_key = await client.get("/v1/models", headers={"X-API-Key": "secret"})
        assert x_key.status == 200
        either_key = await client.get(
            "/v1/models",
            headers={"Authorization": "Bearer wrong", "X-API-Key": "secret"},
        )
        assert either_key.status == 200

        chat = await client.post(
            "/v1/chat/completions",
            headers={"X-API-Key": "secret"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
        assert chat.status == 200
        assert (await chat.json())["choices"][0]["message"]["content"] == "pong"
    assert fake.closed


@pytest.mark.asyncio
async def test_api_accepts_explicit_no_tools_and_rejects_real_tool_calls() -> None:
    config = Config(
        server=ServerConfig(),
        deepseek=DeepSeekConfig(),
        chrome=ChromeConfig(auto_launch=False),
    )
    fake = FakeAPIDriver()
    server = APIServer(config, fake)  # type: ignore[arg-type]
    base_payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "ping"}],
    }

    async with TestClient(TestServer(server.app)) as client:
        accepted = await client.post(
            "/v1/chat/completions",
            json={**base_payload, "tools": [], "tool_choice": "none"},
        )
        assert accepted.status == 200

        with_tools = await client.post(
            "/v1/chat/completions",
            json={
                **base_payload,
                "tools": [{"type": "function", "function": {"name": "noop"}}],
            },
        )
        assert with_tools.status == 400
        assert (await with_tools.json())["error"]["type"] == "invalid_request"

        forced_tool = await client.post(
            "/v1/chat/completions",
            json={**base_payload, "tool_choice": "required"},
        )
        assert forced_tool.status == 400
        assert (await forced_tool.json())["error"]["type"] == "invalid_request"


@pytest.mark.asyncio
async def test_api_enforces_configured_request_size_before_browser_use() -> None:
    class NoCompletionDriver(FakeAPIDriver):
        async def complete(self, prompt: str, **kwargs: Any) -> Reply:
            del prompt, kwargs
            raise AssertionError("oversized request reached the browser driver")

    assert ServerConfig().max_request_bytes == 30 * 1024 * 1024
    config = Config(
        server=ServerConfig(max_request_bytes=1024),
        deepseek=DeepSeekConfig(),
        chrome=ChromeConfig(auto_launch=False),
    )
    fake = NoCompletionDriver()
    server = APIServer(config, fake)  # type: ignore[arg-type]
    oversized = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "x" * 2048}],
    }

    async with TestClient(TestServer(server.app)) as client:
        response = await client.post("/v1/chat/completions", json=oversized)
        assert response.status == 413


@pytest.mark.asyncio
async def test_disconnect_watcher_sets_cancel_event() -> None:
    class ClosingTransport:
        def is_closing(self) -> bool:
            return True

    server = APIServer(Config(), FakeAPIDriver())  # type: ignore[arg-type]
    cancel = asyncio.Event()
    request = SimpleNamespace(transport=ClosingTransport())
    await server._watch_disconnect(request, cancel)  # type: ignore[arg-type]
    assert cancel.is_set()
