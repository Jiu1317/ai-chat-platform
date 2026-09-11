"""Offline tests for bounded ChatGPT-web context construction."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.api_server import (
    MAX_CONTEXT_CHARS,
    MAX_HISTORY_TURNS,
    MAX_SYSTEM_CONTEXT_CHARS,
    _build_chat_context,
    _clip_context_text,
    _valid_conversation_id,
)


def _image_message(role: str, text: str, url: str) -> dict:
    return {
        "role": role,
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    }


def _request(payload: dict) -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.query = {}
    request.json = AsyncMock(return_value=payload)
    return request


def _server():
    import chatgpt_web2api.api_server as srv

    server = srv.APIServer.__new__(srv.APIServer)
    server._config = srv.Config.load(None)
    server._request_count = 0
    server._cdp_port = 9222
    server._parallel_tabs = False
    server._last_conv_id = None
    server._last_project_id = None
    server._last_error = None
    server._breakers = srv.BreakerRegistry()
    server._driver = MagicMock()
    server._driver._current_model = None
    server._driver._current_conv_id = None
    server._driver.navigate_conversation = AsyncMock()
    server._driver.navigate_new_chat = AsyncMock()
    server._driver.ensure_current_conversation = AsyncMock()
    server._driver.select_model = AsyncMock(return_value=True)
    return server


class _NullLock:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def test_public_context_limits_allow_thirty_history_turns_plus_current():
    assert MAX_CONTEXT_CHARS == 150_000
    assert MAX_HISTORY_TURNS == 31
    assert MAX_SYSTEM_CONTEXT_CHARS == 24_000


def test_bootstrap_keeps_current_plus_configured_recent_turns():
    messages = [{"role": "system", "content": "answer clearly"}]
    total_prior = MAX_HISTORY_TURNS + 4
    for index in range(total_prior):
        messages.extend(
            [
                {"role": "user", "content": f"old-user-{index}"},
                {"role": "assistant", "content": f"old-answer-{index}"},
            ]
        )
    messages.append({"role": "user", "content": "current-question"})

    text, _images, latest, has_system, has_history = _build_chat_context(messages)

    first_kept = total_prior - (MAX_HISTORY_TURNS - 1)
    assert f"old-user-{first_kept - 1}" not in text
    assert f"old-user-{first_kept}" in text
    assert f"old-answer-{total_prior - 1}" in text
    assert text.endswith("[User]\ncurrent-question")
    assert latest == "current-question"
    assert has_system is True
    assert has_history is True


def test_context_hard_cap_prioritises_both_ends_of_current_unicode_text():
    decomposed = "Cafe\u0301🙂"
    current = "CURRENT-START-" + decomposed + ("中" * 130_000) + "-CURRENT-END"
    messages = [
        {"role": "system", "content": "S" * 50_000},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": current},
    ]

    text, _images, latest, _has_system, _has_history = _build_chat_context(messages)

    assert len(text) <= MAX_CONTEXT_CHARS
    assert "CURRENT-START-Café🙂" in text
    assert "-CURRENT-END" in text
    assert "[中间内容已省略]" in text
    assert "Cafe\u0301" not in text
    assert latest.startswith("CURRENT-START-Café🙂")


def test_explicit_conversation_sends_only_current_turn_and_current_images():
    messages = [
        {"role": "system", "content": "must not be replayed"},
        _image_message("user", "old-user", "https://example.com/old.png"),
        {"role": "assistant", "content": "old-answer"},
        _image_message("user", "current", "https://example.com/current.png"),
    ]

    text, images, latest, has_system, has_history = _build_chat_context(
        messages, conversation_id="abc-123_DEF"
    )

    assert text == "[User]\ncurrent"
    assert [image.url for image in images] == ["https://example.com/current.png"]
    assert latest == "current"
    assert has_system is True
    assert has_history is True


def test_bootstrap_never_reuploads_historical_images():
    messages = []
    total_prior = MAX_HISTORY_TURNS + 4
    for index in range(total_prior):
        messages.extend(
            [
                _image_message(
                    "user", f"old-user-{index}", f"https://example.com/{index}.png"
                ),
                {"role": "assistant", "content": f"old-answer-{index}"},
            ]
        )
    messages.append({"role": "user", "content": "current"})

    text, images, _latest, _has_system, _has_history = _build_chat_context(messages)
    urls = [image.url for image in images]

    first_kept = total_prior - (MAX_HISTORY_TURNS - 1)
    assert f"old-user-{first_kept - 1}" not in text
    assert f"old-user-{total_prior - 1}" in text
    assert urls == []


def test_assistant_ending_history_is_not_turned_into_a_replayed_user_turn():
    messages = [
        {"role": "user", "content": "already sent"},
        {"role": "assistant", "content": "already answered"},
    ]

    text, images, latest, _has_system, has_history = _build_chat_context(
        messages, conversation_id="abc-123"
    )

    assert text == ""
    assert images == []
    assert latest == ""
    assert has_history is False


def test_clip_is_exact_and_conversation_id_is_strict():
    assert len(_clip_context_text("a" * 200, 80)) == 80
    assert _valid_conversation_id("6aa25495-ca80-83ee-828a-bb2276a26db3")
    assert _valid_conversation_id("abc_DEF-123")
    assert not _valid_conversation_id("WEB:abc")
    assert not _valid_conversation_id("contains/slash")
    assert not _valid_conversation_id("a" * 129)
    assert not _valid_conversation_id({"id": "abc"})


@pytest.mark.asyncio
async def test_handler_explicit_id_navigates_and_sends_only_current(monkeypatch):
    import chatgpt_web2api.api_server as srv

    server = _server()
    captured = {}

    async def response_stub(_request, _model, text, _timeout, **_kwargs):
        captured["text"] = text
        return MagicMock()

    server._full_response = response_stub
    server._stream_response = response_stub
    monkeypatch.setattr(srv, "MutationLock", _NullLock)
    request = _request(
        {
            "model": "auto",
            "conversation_id": "conv-123",
            "messages": [
                {"role": "system", "content": "do not replay"},
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "current"},
            ],
        }
    )

    await server._handle_chat(request)

    server._driver.navigate_conversation.assert_awaited_once_with("conv-123")
    server._driver.navigate_new_chat.assert_not_awaited()
    assert captured["text"] == "[User]\ncurrent"


@pytest.mark.asyncio
async def test_handler_history_without_id_starts_fresh(monkeypatch):
    import chatgpt_web2api.api_server as srv

    server = _server()
    server._last_conv_id = "conv-old"
    server._last_project_id = None
    server._driver._current_conv_id = "conv-old"
    captured = {}

    async def response_stub(_request, _model, text, _timeout, **_kwargs):
        captured["text"] = text
        return MagicMock()

    server._full_response = response_stub
    server._stream_response = response_stub
    monkeypatch.setattr(srv, "MutationLock", _NullLock)
    request = _request(
        {
            "model": "auto",
            "messages": [
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "current"},
            ],
        }
    )

    await server._handle_chat(request)

    server._driver.navigate_new_chat.assert_awaited_once()
    server._driver.ensure_current_conversation.assert_not_awaited()
    assert "[Assistant]\nold answer" in captured["text"]
    assert captured["text"].endswith("[User]\ncurrent")


@pytest.mark.asyncio
async def test_two_omitted_ids_on_same_worker_each_start_fresh(monkeypatch):
    """A prior request's captured id must never turn a later no-id request
    into an implicit continuation on the same worker."""
    import chatgpt_web2api.api_server as srv

    server = _server()
    send_entry_ids = []
    completed_ids = iter(["conv-first", "conv-second"])

    async def response_stub(_request, _model, _text, _timeout, **_kwargs):
        send_entry_ids.append(server._driver._current_conv_id)
        completed_id = next(completed_ids)
        # Mirror the state captured after a successful browser send.
        server._driver._current_conv_id = completed_id
        server._last_conv_id = completed_id
        return MagicMock()

    server._full_response = response_stub
    server._stream_response = response_stub
    monkeypatch.setattr(srv, "MutationLock", _NullLock)

    for text in ("first independent request", "second independent request"):
        await server._handle_chat(
            _request({"model": "auto", "messages": [{"role": "user", "content": text}]})
        )

    assert server._driver.navigate_new_chat.await_count == 2
    server._driver.ensure_current_conversation.assert_not_awaited()
    assert send_entry_ids == [None, None]
    assert server._driver._current_conv_id == "conv-second"


@pytest.mark.asyncio
async def test_handler_rejects_assistant_ending_request_before_browser_use():
    server = _server()
    response = await server._handle_chat(
        _request(
            {
                "model": "auto",
                "conversation_id": "conv-123",
                "messages": [
                    {"role": "user", "content": "already sent"},
                    {"role": "assistant", "content": "already answered"},
                ],
            }
        )
    )

    assert response.status == 400
    server._driver.navigate_conversation.assert_not_awaited()
    server._driver.navigate_new_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_rejects_nine_images_before_starting_stream():
    server = _server()
    parts = [{"type": "text", "text": "compare these"}]
    parts.extend(
        {
            "type": "image_url",
            "image_url": {"url": f"https://example.com/{index}.png"},
        }
        for index in range(9)
    )

    response = await server._handle_chat(
        _request(
            {
                "model": "auto",
                "stream": True,
                "messages": [{"role": "user", "content": parts}],
            }
        )
    )

    assert response.status == 400
    server._driver.navigate_new_chat.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "Request body must be a JSON object"),
        ({"messages": "not-an-array"}, "messages must be a non-empty array"),
        (
            {"model": [], "messages": [{"role": "user", "content": "hello"}]},
            "model must be a non-empty string",
        ),
        (
            {
                "model": "auto",
                "stream": "false",
                "messages": [{"role": "user", "content": "hello"}],
            },
            "stream must be a boolean",
        ),
        (
            {
                "model": "auto",
                "metadata": ["bad"],
                "messages": [{"role": "user", "content": "hello"}],
            },
            "metadata must be an object",
        ),
        (
            {
                "model": "auto",
                "project_id": 123,
                "messages": [{"role": "user", "content": "hello"}],
            },
            "project_id must be a string",
        ),
        (
            {
                "model": "auto",
                "project_id": 0,
                "messages": [{"role": "user", "content": "hello"}],
            },
            "project_id must be a string",
        ),
        (
            {
                "model": "auto",
                "gizmo_id": [],
                "messages": [{"role": "user", "content": "hello"}],
            },
            "project_id must be a string",
        ),
    ],
)
async def test_handler_rejects_malformed_request_fields_before_browser_use(
    payload, message
):
    server = _server()

    response = await server._handle_chat(_request(payload))

    assert response.status == 400
    assert message.encode() in response.body
    server._driver.navigate_conversation.assert_not_awaited()
    server._driver.navigate_new_chat.assert_not_awaited()
