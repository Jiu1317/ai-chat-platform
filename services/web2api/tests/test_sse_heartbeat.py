"""Regression tests for idle SSE keep-alive handling."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from chatgpt_web2api.api_server import APIServer


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
