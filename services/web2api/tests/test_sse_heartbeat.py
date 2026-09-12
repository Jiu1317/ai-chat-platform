"""Regression tests for idle SSE keep-alive handling."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.api_server import APIServer
from chatgpt_web2api.cdp_driver import StreamChunk
from chatgpt_web2api.config import Config
from chatgpt_web2api.multimodal import ImageInputError


class _CapturingStreamResponse:
    def __init__(self):
        self.content_type = None
        self.headers = {}
        self.writes = []
        self.prepared = False
        self.eof_written = False

    async def prepare(self, _request):
        self.prepared = True
        return self

    async def write(self, data):
        self.writes.append(data)

    async def write_eof(self):
        self.eof_written = True


@pytest.mark.asyncio
async def test_with_heartbeat_keeps_pending_source_alive():
    completed = False

    async def slow_stream():
        nonlocal completed
        await asyncio.sleep(0.04)
        completed = True
        yield "final"

    observed = []
    async for item in APIServer._with_heartbeat(slow_stream(), interval=0.01):
        observed.append(item)

    assert completed is True
    assert observed[-1] == "final"
    assert observed[:-1]
    assert all(item is None for item in observed[:-1])


@pytest.mark.asyncio
async def test_with_heartbeat_does_not_add_marker_to_fast_stream():
    async def fast_stream():
        yield "one"
        yield "two"

    observed = [
        item async for item in APIServer._with_heartbeat(fast_stream(), interval=1)
    ]

    assert observed == ["one", "two"]


@pytest.mark.asyncio
async def test_with_heartbeat_closes_source_when_consumer_stops_after_item():
    closed = asyncio.Event()

    async def source():
        try:
            yield "first"
            await asyncio.Event().wait()
        finally:
            closed.set()

    wrapped = APIServer._with_heartbeat(source(), interval=1)
    assert await anext(wrapped) == "first"

    await wrapped.aclose()

    assert closed.is_set()


@pytest.mark.asyncio
async def test_reference_image_preparation_sends_heartbeats(monkeypatch, tmp_path):
    import chatgpt_web2api.api_server as api_server

    async def slow_prepare(_references, _directory):
        await asyncio.sleep(0.04)
        return [str(tmp_path / "reference.png")]

    monkeypatch.setattr(api_server, "prepare_image_files", slow_prepare)
    response = AsyncMock()

    paths = await api_server.APIServer._prepare_images_with_heartbeat(
        response,
        [object()],
        str(tmp_path),
        interval=0.01,
    )

    assert paths == [str(tmp_path / "reference.png")]
    assert response.write.await_count >= 1
    assert response.write.await_args_list[0].args[0].startswith(
        b": preparing-reference-images"
    )


@pytest.mark.asyncio
async def test_streaming_reference_prepare_has_early_byte_and_heartbeats(
    monkeypatch,
    tmp_path,
):
    import chatgpt_web2api.api_server as api_server

    async def slow_prepare(_references, _directory):
        await asyncio.sleep(0.04)
        return [str(tmp_path / "reference.png")]

    monkeypatch.setattr(api_server, "prepare_image_files", slow_prepare)
    monkeypatch.setattr(api_server.web, "StreamResponse", _CapturingStreamResponse)
    driver = MagicMock()
    driver._js_strict = AsyncMock(return_value='{"text": ""}')
    driver._current_conv_id = "conv-image"
    driver._last_response_assets = []
    captured = {}

    async def stream(_text, timeout=120, *, budgets=None, model=None, attachments=None):
        captured["attachments"] = attachments
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = stream
    server = APIServer(Config.load(None), driver)

    async def prepare_with_fast_heartbeat(response, references, directory):
        # Keep the production ordering but shorten the interval for the test.
        return await APIServer._prepare_images_with_heartbeat(
            response,
            references,
            directory,
            interval=0.01,
        )

    server._prepare_images_with_heartbeat = prepare_with_fast_heartbeat
    response = await server._stream_response(
        MagicMock(),
        "gpt-5-5",
        "describe",
        30,
        image_references=[object()],
        image_directory=str(tmp_path),
    )

    first_event = json.loads(
        response.writes[0].decode().removeprefix("data: ").strip()
    )
    assert response.prepared is True
    assert first_event["choices"][0]["delta"]["role"] == "assistant"
    assert any(
        raw.startswith(b": preparing-reference-images")
        for raw in response.writes[1:]
    )
    assert captured["attachments"] == [str(tmp_path / "reference.png")]
    assert response.writes[-1] == b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_streaming_reference_prepare_failure_is_structured_sse(
    monkeypatch,
    tmp_path,
):
    import chatgpt_web2api.api_server as api_server

    async def rejected_prepare(_references, _directory):
        raise ImageInputError("Image exceeds the 30 MiB limit")

    monkeypatch.setattr(api_server, "prepare_image_files", rejected_prepare)
    monkeypatch.setattr(api_server.web, "StreamResponse", _CapturingStreamResponse)
    driver = MagicMock()
    driver._js_strict = AsyncMock(return_value='{"text": ""}')
    driver.send_and_stream = MagicMock()
    server = APIServer(Config.load(None), driver)

    response = await server._stream_response(
        MagicMock(),
        "gpt-5-5",
        "describe",
        30,
        image_references=[object()],
        image_directory=str(tmp_path),
    )
    events = [
        json.loads(raw.decode().removeprefix("data: ").strip())
        for raw in response.writes
        if raw.startswith(b"data: {")
    ]
    error_event = next(event for event in events if "error" in event)

    assert error_event["error"] == {
        "message": "Image exceeds the 30 MiB limit",
        "type": "invalid_request_error",
        "code": "invalid_image_input",
    }
    assert error_event["choices"][0]["finish_reason"] == "error"
    assert response.writes[-1] == b"data: [DONE]\n\n"
    assert response.eof_written is True
    driver.send_and_stream.assert_not_called()
