"""Offline regression tests for the two-worker streaming gateway."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import client_exceptions

_DOWNSTREAM_DISCONNECT_CASES = [
    pytest.param(ConnectionResetError("connection reset"), id="connection-reset"),
    pytest.param(BrokenPipeError("broken pipe"), id="broken-pipe"),
]
_aiohttp_client_reset = getattr(
    client_exceptions, "ClientConnectionResetError", None
)
if _aiohttp_client_reset is not None:
    _DOWNSTREAM_DISCONNECT_CASES.append(
        pytest.param(
            _aiohttp_client_reset("closing transport"),
            id="aiohttp-client-connection-reset",
        )
    )


def _load_gateway_module():
    path = (
        Path(__file__).resolve().parents[3]
        / "bridge"
        / "windows"
        / "dual_tab_gateway.py"
    )
    spec = importlib.util.spec_from_file_location("tested_dual_tab_gateway", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gateway_module = _load_gateway_module()


def test_gateway_defaults_to_four_concurrent_asset_transfers():
    assert gateway_module.DEFAULT_MAX_CONCURRENT_ASSETS == 4


@pytest.mark.asyncio
async def test_default_gateway_starts_four_asset_transfers_without_queueing(monkeypatch):
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    release = asyncio.Event()
    started = [asyncio.Event() for _ in range(4)]
    responses = [
        SimpleNamespace(
            status=200,
            reason="OK",
            headers={"Content-Type": "image/png"},
            content=_GatedContent(event, release, f"asset-{index}".encode()),
            read=AsyncMock(side_effect=AssertionError("assets must stream")),
        )
        for index, event in enumerate(started)
    ]
    gateway.session = _Session([_Context(response=response) for response in responses])
    monkeypatch.setattr(gateway_module.web, "StreamResponse", _Downstream)

    tasks = [
        asyncio.create_task(gateway.proxy_asset(_request(f"/v1/assets/{index}")))
        for index in range(4)
    ]
    await asyncio.gather(
        *(asyncio.wait_for(event.wait(), timeout=1) for event in started)
    )

    assert gateway.active_asset_transfers == 4
    assert len(gateway.session.requests) == 4
    release.set()
    downstream = await asyncio.gather(*tasks)
    assert all(response.eof for response in downstream)
    assert gateway.active_asset_transfers == 0


class _Context:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self.response

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, contexts=(), health_contexts=()):
        self.contexts = list(contexts)
        self.health_contexts = list(health_contexts)
        self.requests: list[tuple[tuple, dict]] = []
        self.health_requests: list[tuple[tuple, dict]] = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        return self.contexts.pop(0)

    def get(self, *args, **kwargs):
        self.health_requests.append((args, kwargs))
        return self.health_contexts.pop(0)


class _HangingContext:
    async def __aenter__(self):
        await asyncio.Event().wait()

    async def __aexit__(self, *_args):
        return False


def _health_response(*, status="healthy", chrome=True, cdp=True, http_status=200):
    return SimpleNamespace(
        status=http_status,
        json=AsyncMock(
            return_value={
                "status": status,
                "chrome_running": chrome,
                "cdp_connected": cdp,
            }
        ),
    )


def _request(path: str):
    return SimpleNamespace(
        method="GET",
        rel_url=path,
        path=path,
        headers={},
        host="127.0.0.1:9181",
        scheme="http",
        read=AsyncMock(return_value=b""),
    )


@pytest.mark.asyncio
async def test_asset_proxy_tries_second_worker_when_first_is_unavailable(monkeypatch):
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    response = SimpleNamespace(
        status=200,
        reason="OK",
        headers={"Content-Type": "image/png"},
        content=_Content([b"png-", b"bytes"]),
        read=AsyncMock(side_effect=AssertionError("successful assets must stream")),
    )
    gateway.session = _Session(
        [_Context(error=OSError("worker one down")), _Context(response=response)]
    )
    monkeypatch.setattr(gateway_module.web, "StreamResponse", _Downstream)

    proxied = await gateway.proxy_asset(_request("/v1/assets/token"))

    assert proxied.status == 200
    assert proxied.writes == [b"png-", b"bytes"]
    assert proxied.eof is True
    response.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_asset_proxy_returns_502_when_a_candidate_worker_is_unavailable():
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    not_found = SimpleNamespace(
        status=404,
        headers={"Content-Type": "text/plain"},
        read=AsyncMock(return_value=b"missing"),
    )
    gateway.session = _Session(
        [_Context(response=not_found), _Context(error=OSError("worker two down"))]
    )

    proxied = await gateway.proxy_asset(_request("/v1/assets/token"))

    assert proxied.status == 502
    assert b"asset backend unavailable" in proxied.body


class _FailingContent:
    def __init__(
        self,
        error: Exception | None = None,
        first_chunk: bytes = b'data: {"choices": []}\n\n',
    ):
        self.calls = 0
        self.error = error or RuntimeError("upstream disconnected")
        self.first_chunk = first_chunk

    def iter_chunked(self, _size):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.calls += 1
        if self.calls == 1:
            return self.first_chunk
        raise self.error


class _Content:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def iter_chunked(self, _size):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration from None


class _GatedContent:
    def __init__(self, started: asyncio.Event, release: asyncio.Event, chunk: bytes):
        self.started = started
        self.release = release
        self.chunk = chunk
        self.sent = False

    def iter_chunked(self, _size):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        self.started.set()
        await self.release.wait()
        return self.chunk


class _Downstream:
    def __init__(self, *, status, reason, headers):
        self.status = status
        self.reason = reason
        self.headers = dict(headers)
        self.prepared = False
        self.writes: list[bytes] = []
        self.write_attempts = 0
        self.eof = False
        self.eof_attempts = 0
        self.force_closed = False

    async def prepare(self, _request):
        self.prepared = True

    async def write(self, chunk):
        self.write_attempts += 1
        self.writes.append(chunk)

    async def write_eof(self):
        self.eof_attempts += 1
        self.eof = True

    def force_close(self):
        self.force_closed = True


class _DisconnectingDownstream(_Downstream):
    disconnect_error: Exception = ConnectionResetError("client disconnected")
    disconnect_stage = "write"

    async def prepare(self, request):
        self.prepared = True
        if self.disconnect_stage == "prepare":
            raise self.disconnect_error
        return await super().prepare(request)

    async def write(self, chunk):
        self.write_attempts += 1
        if self.disconnect_stage == "write":
            raise self.disconnect_error
        self.writes.append(chunk)

    async def write_eof(self):
        self.eof_attempts += 1
        if self.disconnect_stage == "write_eof":
            raise self.disconnect_error
        self.eof = True


@pytest.mark.asyncio
async def test_asset_proxy_bounds_concurrency_and_streams_without_full_read(monkeypatch):
    gateway = gateway_module.DualTabGateway(
        ["http://one", "http://two"], max_concurrent_assets=1
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    first = SimpleNamespace(
        status=200,
        reason="OK",
        headers={"Content-Type": "image/png", "Content-Length": "5"},
        content=_GatedContent(first_started, release_first, b"first"),
        read=AsyncMock(side_effect=AssertionError("successful assets must stream")),
    )
    second = SimpleNamespace(
        status=200,
        reason="OK",
        headers={"Content-Type": "image/png"},
        content=_Content([b"second"]),
        read=AsyncMock(side_effect=AssertionError("successful assets must stream")),
    )
    gateway.session = _Session(
        contexts=[_Context(response=first), _Context(response=second)],
        health_contexts=[
            _Context(response=_health_response(status="degraded")),
            _Context(response=_health_response(chrome=False, cdp=False)),
        ],
    )
    monkeypatch.setattr(gateway_module.web, "StreamResponse", _Downstream)

    first_task = asyncio.create_task(
        gateway.proxy_asset(_request("/v1/assets/first"))
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second_task = asyncio.create_task(
        gateway.proxy_asset(_request("/v1/assets/second"))
    )
    await asyncio.sleep(0)

    assert len(gateway.session.requests) == 1
    assert gateway.active_asset_transfers == 1
    health_response = await gateway.health(_request("/health"))
    health = json.loads(health_response.body)
    assert health_response.status == 503
    assert health["status"] == "degraded"
    assert health["active_asset_transfers"] == 1
    assert health["busy"] is True
    release_first.set()
    first_downstream, second_downstream = await asyncio.gather(
        first_task, second_task
    )

    assert len(gateway.session.requests) == 2
    assert first_downstream.writes == [b"first"]
    assert second_downstream.writes == [b"second"]
    assert "Content-Length" not in first_downstream.headers
    first.read.assert_not_awaited()
    second.read.assert_not_awaited()
    assert gateway.active_asset_transfers == 0


@pytest.mark.asyncio
async def test_started_sse_stream_finishes_with_error_instead_of_second_response(
    monkeypatch,
):
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    upstream = SimpleNamespace(
        status=200,
        reason="OK",
        headers={"Content-Type": "text/event-stream"},
        content=_FailingContent(),
    )
    gateway.session = _Session([_Context(response=upstream)])
    gateway.session.health_contexts.append(_Context(response=_health_response()))
    monkeypatch.setattr(gateway_module.web, "StreamResponse", _Downstream)

    downstream = await gateway.proxy(_request("/v1/chat/completions"))

    assert isinstance(downstream, _Downstream)
    combined = b"".join(downstream.writes)
    assert b"upstream stream interrupted" in combined
    assert combined.endswith(b"data: [DONE]\n\n")
    assert downstream.eof is True
    assert gateway.available.qsize() == 2


@pytest.mark.asyncio
async def test_upstream_connection_reset_before_response_returns_502():
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    gateway.session = _Session(
        contexts=[_Context(error=ConnectionResetError("upstream reset"))],
        health_contexts=[_Context(response=_health_response())],
    )

    response = await gateway.proxy(_request("/v1/chat/completions"))

    assert response.status == 502
    assert b"upstream_error" in response.body
    assert b"upstream reset" in response.body
    assert gateway.available.qsize() == 2


@pytest.mark.asyncio
async def test_upstream_connection_reset_mid_sse_emits_error_and_done(monkeypatch):
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    upstream = SimpleNamespace(
        status=200,
        reason="OK",
        headers={"Content-Type": "text/event-stream"},
        content=_FailingContent(ConnectionResetError("upstream reset")),
    )
    gateway.session = _Session(
        contexts=[_Context(response=upstream)],
        health_contexts=[_Context(response=_health_response())],
    )
    monkeypatch.setattr(gateway_module.web, "StreamResponse", _Downstream)

    downstream = await gateway.proxy(_request("/v1/chat/completions"))

    combined = b"".join(downstream.writes)
    assert b"upstream stream interrupted" in combined
    assert combined.endswith(b"data: [DONE]\n\n")
    assert downstream.eof is True
    assert downstream.force_closed is False
    assert gateway.available.qsize() == 2


@pytest.mark.asyncio
async def test_upstream_connection_reset_mid_non_sse_forces_close(monkeypatch):
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    upstream = SimpleNamespace(
        status=200,
        reason="OK",
        headers={"Content-Type": "application/json"},
        content=_FailingContent(
            ConnectionResetError("upstream reset"),
            first_chunk=b'{"partial":',
        ),
    )
    gateway.session = _Session(
        contexts=[_Context(response=upstream)],
        health_contexts=[_Context(response=_health_response())],
    )
    monkeypatch.setattr(gateway_module.web, "StreamResponse", _Downstream)

    downstream = await gateway.proxy(_request("/v1/chat/completions"))

    assert downstream.writes == [b'{"partial":']
    assert downstream.force_closed is True
    assert downstream.eof_attempts == 0
    assert gateway.available.qsize() == 2


@pytest.mark.parametrize("disconnect_error", _DOWNSTREAM_DISCONNECT_CASES)
@pytest.mark.asyncio
async def test_downstream_disconnect_is_client_cancel_and_releases_worker(
    monkeypatch, caplog, disconnect_error,
):
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    upstream = SimpleNamespace(
        status=200,
        reason="OK",
        headers={"Content-Type": "text/event-stream"},
        content=_Content([b'data: {"choices": []}\n\n']),
    )
    gateway.session = _Session(
        contexts=[_Context(response=upstream)],
        health_contexts=[_Context(response=_health_response())],
    )
    monkeypatch.setattr(
        _DisconnectingDownstream, "disconnect_error", disconnect_error
    )
    monkeypatch.setattr(_DisconnectingDownstream, "disconnect_stage", "write")
    monkeypatch.setattr(
        gateway_module.web, "StreamResponse", _DisconnectingDownstream
    )

    with caplog.at_level(logging.INFO):
        downstream = await gateway.proxy(_request("/v1/chat/completions"))

    assert isinstance(downstream, _DisconnectingDownstream)
    assert downstream.write_attempts == 1
    assert downstream.eof_attempts == 0
    assert gateway.available.qsize() == 2
    assert gateway.queued_requests == 0
    assert gateway._not_ready_until == [0.0, 0.0]
    assert not any(
        "backend 1 failed" in record.getMessage() for record in caplog.records
    )


@pytest.mark.parametrize("disconnect_stage", ["prepare", "write_eof"])
@pytest.mark.asyncio
async def test_other_downstream_disconnect_boundaries_release_worker(
    monkeypatch, caplog, disconnect_stage,
):
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    upstream = SimpleNamespace(
        status=200,
        reason="OK",
        headers={"Content-Type": "text/event-stream"},
        content=_Content([b'data: {"choices": []}\n\n']),
    )
    gateway.session = _Session(
        contexts=[_Context(response=upstream)],
        health_contexts=[_Context(response=_health_response())],
    )
    monkeypatch.setattr(
        _DisconnectingDownstream,
        "disconnect_error",
        ConnectionResetError("client disconnected"),
    )
    monkeypatch.setattr(
        _DisconnectingDownstream, "disconnect_stage", disconnect_stage
    )
    monkeypatch.setattr(
        gateway_module.web, "StreamResponse", _DisconnectingDownstream
    )

    with caplog.at_level(logging.INFO):
        downstream = await gateway.proxy(_request("/v1/chat/completions"))

    assert isinstance(downstream, _DisconnectingDownstream)
    assert downstream.force_closed is False
    assert gateway.available.qsize() == 2
    assert gateway.queued_requests == 0
    assert not any(
        "backend 1 failed" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_busy_gateway_queue_has_bounded_readable_timeout():
    gateway = gateway_module.DualTabGateway(
        ["http://one", "http://two"], queue_timeout_seconds=0.01
    )
    gateway.session = object()
    await gateway.available.get()
    await gateway.available.get()

    response = await gateway.proxy(_request("/v1/chat/completions"))

    assert response.status == 503
    assert response.headers["Retry-After"] == "1"
    assert b"both Web2API workers are busy" in response.body
    assert b"capacity_timeout" in response.body
    assert gateway.available.qsize() == 0


def test_main_enables_aiohttp_handler_cancellation(monkeypatch):
    captured = {}

    def fake_run_app(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(sys, "argv", ["dual_tab_gateway.py"])
    monkeypatch.delenv("W2A_GATEWAY_MAX_CONCURRENT_ASSETS", raising=False)
    monkeypatch.setattr(gateway_module.web, "run_app", fake_run_app)

    gateway_module.main()

    assert captured["handler_cancellation"] is True


@pytest.mark.asyncio
async def test_disconnected_queued_socket_is_cancelled_before_backend_acquire():
    gateway = gateway_module.DualTabGateway(
        ["http://one", "http://two"], queue_timeout_seconds=10
    )
    gateway.session = object()
    await gateway.available.get()
    await gateway.available.get()

    app = gateway_module.web.Application()
    app.router.add_route("*", "/{tail:.*}", gateway.proxy)
    runner = gateway_module.web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    site = gateway_module.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def wait_for_queue_size(expected: int) -> None:
        async def poll() -> None:
            while gateway.queued_requests != expected:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), timeout=1)

    try:
        writer.write(
            b"GET /v1/models HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{port}\r\n".encode()
            + b"\r\n"
        )
        await writer.drain()
        await wait_for_queue_size(1)

        writer.close()
        await writer.wait_closed()
        await wait_for_queue_size(0)

        assert gateway.available.qsize() == 0
    finally:
        if not writer.is_closing():
            writer.close()
            await writer.wait_closed()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_health_probe_has_bounded_read_timeout():
    gateway = gateway_module.DualTabGateway(
        ["http://one", "http://two"], health_timeout_seconds=0.01
    )
    gateway.session = _Session(
        health_contexts=[
            _HangingContext(),
            _Context(response=_health_response()),
        ]
    )

    response = await asyncio.wait_for(gateway.health(_request("/health")), timeout=0.2)
    payload = json.loads(response.body)

    assert response.status == 200
    assert payload["ready_backends"] == 1
    assert payload["backends"][0]["error"] == "TimeoutError"


@pytest.mark.asyncio
async def test_health_probe_connection_reset_marks_backend_unhealthy():
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    gateway.session = _Session(
        health_contexts=[_Context(error=ConnectionResetError("worker reset"))]
    )

    result = await gateway.probe_backend(0)

    assert result["ready"] is False
    assert result["reachable"] is False
    assert result["error"] == "ConnectionResetError"


@pytest.mark.asyncio
async def test_gateway_rejects_immediately_when_waiting_queue_is_full():
    gateway = gateway_module.DualTabGateway(
        ["http://one", "http://two"], max_queued_requests=0
    )
    gateway.session = object()
    await gateway.available.get()
    await gateway.available.get()

    response = await gateway.proxy(_request("/v1/chat/completions"))

    assert response.status == 429
    assert response.headers["Retry-After"] == "1"
    assert b"queue_full" in response.body
    assert gateway.queued_requests == 0


@pytest.mark.asyncio
async def test_proxy_avoids_worker_recently_observed_as_unhealthy(monkeypatch):
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    gateway.remember_backend_health(0, {"ready": False})
    upstream = SimpleNamespace(
        status=200,
        reason="OK",
        headers={"Content-Type": "application/json"},
        content=_Content([b'{}']),
    )
    gateway.session = _Session(
        contexts=[_Context(response=upstream)],
        health_contexts=[_Context(response=_health_response())],
    )
    monkeypatch.setattr(gateway_module.web, "StreamResponse", _Downstream)

    response = await gateway.proxy(_request("/v1/chat/completions"))

    assert response.status == 200
    assert gateway.session.requests[0][0][1].startswith("http://two/")
    assert len(gateway.session.health_requests) == 1
    assert gateway.available.qsize() == 2


@pytest.mark.asyncio
async def test_proxy_does_not_send_to_two_unready_workers():
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    gateway.session = _Session(
        health_contexts=[
            _Context(response=_health_response(status="degraded")),
            _Context(response=_health_response(chrome=False, cdp=False)),
        ]
    )

    response = await gateway.proxy(_request("/v1/chat/completions"))

    assert response.status == 503
    assert b"no_ready_worker" in response.body
    assert gateway.session.requests == []
    assert gateway.available.qsize() == 2


def test_worker_readiness_accepts_starting_but_rejects_failure_status():
    assert gateway_module.worker_is_ready(
        {
            "status": "starting",
            "chrome_running": True,
            "cdp_connected": True,
        },
        200,
    ) is True
    assert gateway_module.worker_is_ready(
        {
            "status": "login_required",
            "chrome_running": True,
            "cdp_connected": True,
        },
        200,
    ) is False


def test_gateway_copies_are_identical():
    root = Path(__file__).resolve().parents[3]
    standalone = root / "standalone" / "web2api-chrome" / "dual_tab_gateway.py"
    bridge = root / "bridge" / "windows" / "dual_tab_gateway.py"

    assert standalone.read_bytes() == bridge.read_bytes()
