"""Offline regression tests for the two-worker streaming gateway."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _load_gateway_module():
    path = (
        Path(__file__).resolve().parents[3]
        / "standalone"
        / "web2api-chrome"
        / "dual_tab_gateway.py"
    )
    spec = importlib.util.spec_from_file_location("tested_dual_tab_gateway", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gateway_module = _load_gateway_module()


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
    def __init__(self, contexts):
        self.contexts = list(contexts)

    def request(self, *_args, **_kwargs):
        return self.contexts.pop(0)


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
async def test_asset_proxy_tries_second_worker_when_first_is_unavailable():
    gateway = gateway_module.DualTabGateway(["http://one", "http://two"])
    response = SimpleNamespace(
        status=200,
        headers={"Content-Type": "image/png"},
        read=AsyncMock(return_value=b"png-bytes"),
    )
    gateway.session = _Session(
        [_Context(error=OSError("worker one down")), _Context(response=response)]
    )

    proxied = await gateway.proxy_asset(_request("/v1/assets/token"))

    assert proxied.status == 200
    assert proxied.body == b"png-bytes"


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
    def __init__(self):
        self.calls = 0

    def iter_chunked(self, _size):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.calls += 1
        if self.calls == 1:
            return b'data: {"choices": []}\n\n'
        raise RuntimeError("upstream disconnected")


class _Downstream:
    def __init__(self, *, status, reason, headers):
        self.status = status
        self.reason = reason
        self.headers = dict(headers)
        self.prepared = False
        self.writes: list[bytes] = []
        self.eof = False

    async def prepare(self, _request):
        self.prepared = True

    async def write(self, chunk):
        self.writes.append(chunk)

    async def write_eof(self):
        self.eof = True


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
    monkeypatch.setattr(gateway_module.web, "StreamResponse", _Downstream)

    downstream = await gateway.proxy(_request("/v1/chat/completions"))

    assert isinstance(downstream, _Downstream)
    combined = b"".join(downstream.writes)
    assert b"upstream stream interrupted" in combined
    assert combined.endswith(b"data: [DONE]\n\n")
    assert downstream.eof is True
    assert gateway.available.qsize() == 2


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


def test_gateway_copies_are_identical():
    root = Path(__file__).resolve().parents[3]
    standalone = root / "standalone" / "web2api-chrome" / "dual_tab_gateway.py"
    bridge = root / "bridge" / "windows" / "dual_tab_gateway.py"

    assert standalone.read_bytes() == bridge.read_bytes()
