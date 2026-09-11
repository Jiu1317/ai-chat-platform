"""Offline regression tests for the local two-worker DeepSeek gateway."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _load_gateway_module():
    path = Path(__file__).resolve().parents[1] / "dual_tab_gateway.py"
    spec = importlib.util.spec_from_file_location("tested_deepseek_gateway", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gateway_module = _load_gateway_module()


def test_gateway_default_request_limit_is_thirty_mib():
    assert gateway_module.DEFAULT_MAX_REQUEST_BYTES == 30 * 1024 * 1024


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
    def __init__(self, *, health=(), upstream=()):
        self.health = list(health)
        self.upstream = list(upstream)
        self.requests: list[tuple[tuple, dict]] = []

    def get(self, *_args, **_kwargs):
        return self.health.pop(0)

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        return self.upstream.pop(0)


def _health(payload: dict, status: int = 200):
    return SimpleNamespace(
        status=status,
        json=AsyncMock(return_value=payload),
    )


class _Content:
    def __init__(self, chunks=(), error: Exception | None = None):
        self.chunks = list(chunks)
        self.error = error

    def iter_chunked(self, _size):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.chunks:
            return self.chunks.pop(0)
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        raise StopAsyncIteration


class _Upstream:
    def __init__(self, *, status=200, headers=None, chunks=(), error=None):
        self.status = status
        self.reason = "upstream reason"
        self.headers = headers or {"Content-Type": "application/json"}
        self.content = _Content(chunks, error)
        self.closed = False

    def close(self):
        self.closed = True


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


class _DisconnectingDownstream(_Downstream):
    async def write(self, _chunk):
        raise ConnectionResetError("downstream client disconnected")


def _request(path="/v1/chat/completions", *, remote="127.0.0.1", headers=None):
    return SimpleNamespace(
        method="POST",
        rel_url=path,
        path=path,
        remote=remote,
        headers=headers or {},
        host="127.0.0.1:9191",
        scheme="http",
        read=AsyncMock(return_value=b'{"stream":true}'),
    )


def _ready_health():
    return _health(
        {
            "status": "ok",
            "cdp_connected": True,
            "logged_in": True,
            "busy": False,
        }
    )


def test_only_distinct_loopback_backends_are_accepted():
    with pytest.raises(ValueError, match="loopback"):
        gateway_module.DualTabGateway(["http://example.com:9192", "http://127.0.0.1:9193"])
    with pytest.raises(ValueError, match="distinct"):
        gateway_module.DualTabGateway(["http://127.0.0.1:9192"] * 2)


def test_authentication_headers_are_preserved_and_hop_headers_are_removed():
    gateway = gateway_module.DualTabGateway(
        ["http://127.0.0.1:9192", "http://127.0.0.1:9193"]
    )
    request = _request(
        headers={
            "Authorization": "Bearer local-secret",
            "X-API-Key": "local-secret",
            "Connection": "X-Remove",
            "X-Remove": "gone",
            "Host": "untrusted.invalid",
            "X-Forwarded-For": "203.0.113.9",
        }
    )

    headers = gateway.upstream_headers(request)

    assert headers["Authorization"] == "Bearer local-secret"
    assert headers["X-API-Key"] == "local-secret"
    assert "Connection" not in headers
    assert "X-Remove" not in headers
    assert "Host" not in headers
    assert headers["X-Forwarded-For"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_health_aggregates_ready_and_manual_attention_workers():
    gateway = gateway_module.DualTabGateway(
        ["http://127.0.0.1:9192", "http://127.0.0.1:9193"]
    )
    gateway.session = _Session(
        health=[
            _Context(response=_ready_health()),
            _Context(
                response=_health(
                    {
                        "status": "login_required",
                        "cdp_connected": True,
                        "logged_in": False,
                        "busy": False,
                    }
                )
            ),
        ]
    )

    response = await gateway.health(_request("/health"))
    payload = json.loads(response.body)

    assert response.status == 200
    assert payload["status"] == "degraded"
    assert payload["ready_backends"] == 1
    assert payload["reachable_backends"] == 2
    assert payload["backends"][1]["status"] == "login_required"


@pytest.mark.asyncio
async def test_proxy_skips_unready_worker_and_preserves_upstream_error(monkeypatch):
    gateway = gateway_module.DualTabGateway(
        ["http://127.0.0.1:9192", "http://127.0.0.1:9193"]
    )
    upstream = _Upstream(status=401, chunks=[b'{"error":{"code":"invalid_api_key"}}'])
    session = _Session(
        health=[
            _Context(
                response=_health(
                    {
                        "status": "login_required",
                        "cdp_connected": True,
                        "logged_in": False,
                        "busy": False,
                    }
                )
            ),
            _Context(response=_ready_health()),
        ],
        upstream=[_Context(response=upstream)],
    )
    gateway.session = session
    monkeypatch.setattr(gateway_module.web, "StreamResponse", _Downstream)
    request = _request(headers={"Authorization": "Bearer secret", "X-API-Key": "secret"})

    response = await gateway.proxy(request)

    assert response.status == 401
    assert b"".join(response.writes) == b'{"error":{"code":"invalid_api_key"}}'
    assert session.requests[0][0][1].startswith("http://127.0.0.1:9193/")
    assert session.requests[0][1]["headers"]["Authorization"] == "Bearer secret"
    assert session.requests[0][1]["headers"]["X-API-Key"] == "secret"
    assert gateway.available.qsize() == 2


@pytest.mark.asyncio
async def test_proxy_returns_503_without_sending_when_neither_worker_is_ready():
    gateway = gateway_module.DualTabGateway(
        ["http://127.0.0.1:9192", "http://127.0.0.1:9193"]
    )
    gateway.session = _Session(
        health=[
            _Context(response=_health({"status": "dom_changed"})),
            _Context(response=_health({"status": "captcha_required"})),
        ]
    )

    response = await gateway.proxy(_request())
    payload = json.loads(response.body)

    assert response.status == 503
    assert payload["error"]["type"] == "no_ready_worker"
    assert gateway.session.requests == []
    assert gateway.available.qsize() == 2


@pytest.mark.asyncio
async def test_bounded_queue_rejects_immediately_when_full():
    gateway = gateway_module.DualTabGateway(
        ["http://127.0.0.1:9192", "http://127.0.0.1:9193"],
        max_queued_requests=0,
    )
    gateway.session = object()
    await gateway.available.get()
    await gateway.available.get()

    response = await gateway.proxy(_request())

    assert response.status == 429
    assert response.headers["Retry-After"] == "1"
    assert b"queue_full" in response.body
    assert gateway.queued_requests == 0


@pytest.mark.asyncio
async def test_queue_wait_has_a_bounded_timeout():
    gateway = gateway_module.DualTabGateway(
        ["http://127.0.0.1:9192", "http://127.0.0.1:9193"],
        max_queued_requests=1,
        queue_timeout_seconds=0.01,
    )
    gateway.session = object()
    await gateway.available.get()
    await gateway.available.get()

    response = await gateway.proxy(_request())

    assert response.status == 503
    assert b"capacity_timeout" in response.body
    assert gateway.queued_requests == 0


@pytest.mark.asyncio
async def test_interrupted_sse_closes_upstream_and_emits_terminal_error(monkeypatch):
    gateway = gateway_module.DualTabGateway(
        ["http://127.0.0.1:9192", "http://127.0.0.1:9193"]
    )
    upstream = _Upstream(
        headers={"Content-Type": "text/event-stream"},
        chunks=[b'data: {"choices":[]}\n\n'],
        error=ConnectionResetError("client or upstream disconnected"),
    )
    gateway.session = _Session(
        health=[_Context(response=_ready_health())],
        upstream=[_Context(response=upstream)],
    )
    monkeypatch.setattr(gateway_module.web, "StreamResponse", _Downstream)

    response = await gateway.proxy(_request())
    combined = b"".join(response.writes)

    assert upstream.closed is True
    assert b"upstream DeepSeek stream interrupted" in combined
    assert combined.endswith(b"data: [DONE]\n\n")
    assert response.eof is True
    assert gateway.available.qsize() == 2


@pytest.mark.asyncio
async def test_downstream_disconnect_closes_upstream_stream(monkeypatch):
    gateway = gateway_module.DualTabGateway(
        ["http://127.0.0.1:9192", "http://127.0.0.1:9193"]
    )
    upstream = _Upstream(
        headers={"Content-Type": "text/event-stream"},
        chunks=[b"data: first chunk\n\n"],
    )
    gateway.session = _Session(
        health=[_Context(response=_ready_health())],
        upstream=[_Context(response=upstream)],
    )
    monkeypatch.setattr(
        gateway_module.web, "StreamResponse", _DisconnectingDownstream
    )

    response = await gateway.proxy(_request())

    assert response.prepared is True
    assert upstream.closed is True
    assert gateway.available.qsize() == 2


@pytest.mark.asyncio
async def test_non_loopback_client_is_rejected_before_upstream_access():
    gateway = gateway_module.DualTabGateway(
        ["http://127.0.0.1:9192", "http://127.0.0.1:9193"]
    )
    gateway.session = object()

    response = await gateway.proxy(_request(remote="192.0.2.10"))

    assert response.status == 403
    assert b"local_access_required" in response.body


def test_watchdog_classifies_each_worker_before_suppressing_recovery():
    script = (
        Path(__file__).resolve().parents[1] / "watch-deepseek-dual-tab.ps1"
    ).read_text(encoding="utf-8")

    assert "function Get-WorkerCondition" in script
    assert "$worker1Condition = Get-WorkerCondition -Worker 1" in script
    assert "$worker2Condition = Get-WorkerCondition -Worker 2" in script
    unavailable_check = script.index("if ($worker1Condition -eq 'unavailable'")
    manual_check = script.index(
        "if ($worker1Condition -eq 'manual_action'", unavailable_check
    )
    assert unavailable_check < manual_check
    assert "unavailable_with_manual_peer" in script
    assert "'send_unknown'" in script


def test_stop_rediscovers_only_browser_matching_state_profile_and_cdp():
    script = (
        Path(__file__).resolve().parents[1] / "stop-deepseek-dual-tab.ps1"
    ).read_text(encoding="utf-8")

    assert "function Find-DedicatedBrowserPidsFromState" in script
    assert "Test-BrowserIdentity -CommandLine $_.CommandLine -State $State" in script
    assert "@($state.browser_pids) + @(Find-DedicatedBrowserPidsFromState" in script
    assert "$CommandLine.Contains($profile)" in script
    assert "--remote-debugging-port" in script


def test_start_records_real_listener_owners_and_is_idempotent():
    script = (
        Path(__file__).resolve().parents[1] / "start-deepseek-dual-tab.ps1"
    ).read_text(encoding="utf-8")

    assert "function Get-LocalListenerProcessId" in script
    assert "$worker1ListenerPid = Get-LocalListenerProcessId" in script
    assert "$worker2ListenerPid = Get-LocalListenerProcessId" in script
    assert "$gatewayListenerPid = Get-LocalListenerProcessId" in script
    assert "launcher_pid = $worker1.Id" in script
    assert "launcher_pid = $worker2.Id" in script
    assert "launcher_pid = $gateway.Id" in script
    assert "$existingHealth.mode -eq 'deepseek-dual-worker'" in script
    assert "$stableBackends.Count -eq 2" in script
    assert "'send_unknown'" in script


def test_stop_includes_listener_owner_and_launcher_pid():
    script = (
        Path(__file__).resolve().parents[1] / "stop-deepseek-dual-tab.ps1"
    ).read_text(encoding="utf-8")

    assert "function Get-LocalListenerProcessIds" in script
    assert "Get-LocalListenerProcessIds -Port ([int]$state.port)" in script
    assert "Get-LocalListenerProcessIds -Port ([int]$state.api_port)" in script
    assert "[int]$state.launcher_pid" in script
    assert "Stop-VerifiedProcess -ProcessId $gatewayPid" in script
    assert "Stop-VerifiedProcess -ProcessId $workerPid" in script
