"""Streaming gateway for exactly two chatgpt-web2api workers."""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import logging
import math
import os
import time
from collections.abc import Iterable
from contextlib import asynccontextmanager

from aiohttp import ClientSession, ClientTimeout, web

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_QUEUED_REQUESTS = 32
DEFAULT_HEALTH_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_CONCURRENT_ASSETS = 4

_DOWNSTREAM_DISCONNECT_ERRNOS = {
    errno.ECONNABORTED,
    errno.ECONNRESET,
    errno.EPIPE,
}
_DOWNSTREAM_DISCONNECT_WINERRORS = {64, 109, 10053, 10054}


class GatewayQueueFull(Exception):
    """Raised when both workers and the bounded waiting queue are full."""


class GatewayQueueTimeout(Exception):
    """Raised when a queued request does not receive a worker in time."""


def downstream_client_disconnected(exc: BaseException) -> bool:
    """Identify transport errors raised while writing to the caller."""

    if isinstance(exc, ConnectionError):
        return True
    return isinstance(exc, OSError) and (
        exc.errno in _DOWNSTREAM_DISCONNECT_ERRNOS
        or getattr(exc, "winerror", None) in _DOWNSTREAM_DISCONNECT_WINERRORS
    )


async def await_downstream_io(operation) -> bool:
    """Run one response operation and quietly classify a disconnected caller."""

    try:
        await operation
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if not downstream_client_disconnected(exc):
            raise
        logger.info("downstream client disconnected: %s", type(exc).__name__)
        return False
    return True

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
}


def filtered_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {name: value for name, value in headers if name.lower() not in HOP_BY_HOP}


def worker_is_ready(payload: dict[str, object], http_status: int) -> bool:
    """Return whether a worker can safely accept a new browser request."""

    if not 200 <= http_status < 300:
        return False
    if payload.get("chrome_running") is not True:
        return False
    if payload.get("cdp_connected") is not True:
        return False
    return str(payload.get("status", "")).strip().lower() in {
        "",
        "healthy",
        "ok",
        "ready",
        "running",
        "starting",
    }


class DualTabGateway:
    def __init__(
        self,
        backends: list[str],
        queue_timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
        max_queued_requests: int = DEFAULT_MAX_QUEUED_REQUESTS,
        health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
        max_concurrent_assets: int = DEFAULT_MAX_CONCURRENT_ASSETS,
    ) -> None:
        if len(backends) != 2:
            raise ValueError("exactly two backends are required")
        if queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be positive")
        if max_queued_requests < 0:
            raise ValueError("max_queued_requests must not be negative")
        if health_timeout_seconds <= 0:
            raise ValueError("health_timeout_seconds must be positive")
        if max_concurrent_assets <= 0:
            raise ValueError("max_concurrent_assets must be positive")
        self.backends = [url.rstrip("/") for url in backends]
        self.queue_timeout_seconds = queue_timeout_seconds
        self.max_queued_requests = max_queued_requests
        self.health_timeout_seconds = health_timeout_seconds
        self.asset_slots = asyncio.Semaphore(max_concurrent_assets)
        self._active_asset_transfers = 0
        self.available: asyncio.Queue[int] = asyncio.Queue(maxsize=2)
        self.available.put_nowait(0)
        self.available.put_nowait(1)
        self._queued_requests = 0
        self._not_ready_until = [0.0, 0.0]
        self.session: ClientSession | None = None

    @property
    def queued_requests(self) -> int:
        return self._queued_requests

    @property
    def active_asset_transfers(self) -> int:
        return self._active_asset_transfers

    @asynccontextmanager
    async def reserve_asset_slot(self):
        async with self.asset_slots:
            self._active_asset_transfers += 1
            try:
                yield
            finally:
                self._active_asset_transfers -= 1

    async def start(self, app: web.Application) -> None:
        self.session = ClientSession(
            timeout=ClientTimeout(total=None, connect=10, sock_connect=10, sock_read=None),
            trust_env=False,
        )

    async def stop(self, app: web.Application) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def acquire_backend(self) -> int:
        try:
            candidate = self.available.get_nowait()
            return self.prefer_ready_backend(candidate)
        except asyncio.QueueEmpty:
            if self._queued_requests >= self.max_queued_requests:
                raise GatewayQueueFull from None

        self._queued_requests += 1
        try:
            try:
                candidate = await asyncio.wait_for(
                    self.available.get(), timeout=self.queue_timeout_seconds
                )
                return self.prefer_ready_backend(candidate)
            except TimeoutError:
                raise GatewayQueueTimeout from None
        finally:
            self._queued_requests -= 1

    def prefer_ready_backend(self, candidate: int) -> int:
        """Prefer a worker not recently observed as unhealthy when both are idle."""

        now = time.monotonic()
        if self._not_ready_until[candidate] <= now:
            return candidate
        try:
            alternative = self.available.get_nowait()
        except asyncio.QueueEmpty:
            return candidate
        if self._not_ready_until[alternative] <= now:
            self.available.put_nowait(candidate)
            return alternative
        self.available.put_nowait(alternative)
        return candidate

    def remember_backend_health(self, index: int, health: dict[str, object]) -> None:
        if health.get("ready") is True:
            self._not_ready_until[index] = 0.0
        else:
            self._not_ready_until[index] = time.monotonic() + 5.0

    def upstream_headers(self, request: web.Request) -> dict[str, str]:
        headers = filtered_headers(request.headers.items())
        headers.pop("Host", None)
        headers["X-Forwarded-Host"] = request.headers.get("X-Forwarded-Host", request.host)
        headers["X-Forwarded-Proto"] = request.headers.get(
            "X-Forwarded-Proto", request.scheme
        )
        return headers

    async def probe_backend(
        self, index: int, *, timeout_seconds: float | None = None
    ) -> dict[str, object]:
        assert self.session is not None
        timeout = (
            self.health_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        try:
            async with asyncio.timeout(timeout):
                async with self.session.get(
                    f"{self.backends[index]}/health", allow_redirects=False
                ) as response:
                    decoded = await response.json(content_type=None)
                    payload = decoded if isinstance(decoded, dict) else {}
                    ready = worker_is_ready(payload, response.status)
                    return {
                        **payload,
                        "worker": index + 1,
                        "http_status": response.status,
                        "reachable": True,
                        "ready": ready,
                    }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - health must stay available
            return {
                "worker": index + 1,
                "status": "unavailable",
                "error": type(exc).__name__,
                "http_status": 502,
                "reachable": False,
                "ready": False,
            }

    async def health(self, _request: web.Request) -> web.Response:
        results = await asyncio.gather(
            self.probe_backend(0), self.probe_backend(1)
        )
        for index, result in enumerate(results):
            self.remember_backend_health(index, result)
        ready_count = sum(result.get("ready") is True for result in results)
        reachable_count = sum(
            result.get("reachable") is True for result in results
        )
        busy = (
            self.available.qsize() < 2
            or self._queued_requests > 0
            or self._active_asset_transfers > 0
        )
        return web.json_response(
            {
                "status": "healthy" if ready_count == 2 else "degraded",
                "mode": "dual-tab",
                "capacity": 2,
                "available_workers": self.available.qsize(),
                "queued_requests": self._queued_requests,
                "active_asset_transfers": self._active_asset_transfers,
                "busy": busy,
                "max_queued_requests": self.max_queued_requests,
                "ready_backends": ready_count,
                "reachable_backends": reachable_count,
                "backends": results,
            },
            status=200 if ready_count else 503,
            headers={"Cache-Control": "no-store"},
        )

    async def proxy_asset(self, request: web.Request) -> web.StreamResponse:
        """Try both workers because generated asset tokens are worker-local."""
        assert self.session is not None
        async with self.reserve_asset_slot():
            body = await request.read()
            last_error: Exception | None = None
            for index, backend in enumerate(self.backends):
                downstream: web.StreamResponse | None = None
                try:
                    async with self.session.request(
                        request.method, f"{backend}{request.rel_url}",
                        headers=self.upstream_headers(request), data=body,
                        allow_redirects=False,
                    ) as response:
                        if response.status == 404:
                            continue
                        downstream = web.StreamResponse(
                            status=response.status,
                            reason=response.reason,
                            headers=filtered_headers(response.headers.items()),
                        )
                        if not await await_downstream_io(downstream.prepare(request)):
                            return downstream
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            if not await await_downstream_io(downstream.write(chunk)):
                                return downstream
                        if not await await_downstream_io(downstream.write_eof()):
                            return downstream
                        return downstream
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - try the other worker
                    if downstream is not None and downstream.prepared:
                        logger.warning(
                            "asset backend %s stream interrupted: %s", index + 1, exc
                        )
                        downstream.force_close()
                        return downstream
                    last_error = exc
                    logger.warning("asset backend %s unavailable: %s", index + 1, exc)
                    continue
            if last_error is not None:
                return web.json_response(
                    {"error": {"message": "asset backend unavailable",
                               "type": "upstream_error"}}, status=502,
                )
            raise web.HTTPNotFound()

    async def proxy(self, request: web.Request) -> web.StreamResponse:
        if request.path in ("/", "/health"):
            return await self.health(request)
        if request.path.startswith("/v1/assets/"):
            return await self.proxy_asset(request)

        assert self.session is not None
        acquired_indices: list[int] = []
        try:
            backend_index = await self.acquire_backend()
            acquired_indices.append(backend_index)
        except GatewayQueueFull:
            return web.json_response(
                {
                    "error": {
                        "message": "the Web2API queue is full; retry later",
                        "type": "queue_full",
                    }
                },
                status=429,
                headers={"Retry-After": "1"},
            )
        except GatewayQueueTimeout:
            retry_after = max(1, math.ceil(self.queue_timeout_seconds))
            return web.json_response(
                {
                    "error": {
                        "message": (
                            "both Web2API workers are busy; queue wait exceeded "
                            f"{self.queue_timeout_seconds:g} seconds, retry later"
                        ),
                        "type": "capacity_timeout",
                    }
                },
                status=503,
                headers={"Retry-After": str(retry_after)},
            )
        downstream: web.StreamResponse | None = None
        try:
            selected_health = await self.probe_backend(
                backend_index,
                timeout_seconds=min(2.0, self.health_timeout_seconds),
            )
            self.remember_backend_health(backend_index, selected_health)
            if selected_health.get("ready") is not True:
                try:
                    other_index = await self.acquire_backend()
                    acquired_indices.append(other_index)
                except GatewayQueueFull:
                    return web.json_response(
                        {
                            "error": {
                                "message": (
                                    "the only ready Web2API worker is busy and "
                                    "the queue is full"
                                ),
                                "type": "queue_full",
                            }
                        },
                        status=429,
                        headers={"Retry-After": "1"},
                    )
                except GatewayQueueTimeout:
                    return web.json_response(
                        {
                            "error": {
                                "message": (
                                    "no ready Web2API worker became available "
                                    "before the queue wait expired"
                                ),
                                "type": "capacity_timeout",
                            }
                        },
                        status=503,
                        headers={
                            "Retry-After": str(
                                max(1, math.ceil(self.queue_timeout_seconds))
                            )
                        },
                    )

                other_health = await self.probe_backend(
                    other_index,
                    timeout_seconds=min(2.0, self.health_timeout_seconds),
                )
                self.remember_backend_health(other_index, other_health)
                if other_health.get("ready") is not True:
                    summaries = [
                        {
                            "worker": health.get("worker"),
                            "status": health.get("status", "not_ready"),
                            "http_status": health.get("http_status"),
                        }
                        for health in (selected_health, other_health)
                    ]
                    return web.json_response(
                        {
                            "error": {
                                "message": "neither Web2API browser worker is ready",
                                "type": "no_ready_worker",
                                "workers": summaries,
                            }
                        },
                        status=503,
                        headers={"Retry-After": "5"},
                    )
                backend_index = other_index

            body = await request.read()
            async with self.session.request(
                request.method, f"{self.backends[backend_index]}{request.rel_url}",
                headers=self.upstream_headers(request), data=body, allow_redirects=False,
            ) as upstream:
                downstream = web.StreamResponse(
                    status=upstream.status, reason=upstream.reason,
                    headers=filtered_headers(upstream.headers.items()),
                )
                if not await await_downstream_io(downstream.prepare(request)):
                    return downstream
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    if not await await_downstream_io(downstream.write(chunk)):
                        return downstream
                if not await await_downstream_io(downstream.write_eof()):
                    return downstream
                return downstream
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("backend %s failed", backend_index + 1)
            # Once response headers (and possibly chunks) reached the caller,
            # aiohttp cannot replace that response with a new JSON 502. Doing
            # so raises a second protocol error and hides the real upstream
            # interruption. Finish an already-started SSE response with a
            # machine-readable error and [DONE]; for other content, close the
            # existing response cleanly.
            if downstream is not None and downstream.prepared:
                if "text/event-stream" in str(
                    downstream.headers.get("Content-Type", "")
                ).lower():
                    payload = {
                        "error": {
                            "message": "upstream stream interrupted",
                            "type": "upstream_error",
                        }
                    }
                    if not await await_downstream_io(downstream.write(
                        f"data: {json.dumps(payload)}\n\n".encode()
                    )):
                        return downstream
                    if not await await_downstream_io(
                        downstream.write(b"data: [DONE]\n\n")
                    ):
                        return downstream
                    if not await await_downstream_io(downstream.write_eof()):
                        return downstream
                    return downstream
                # Headers for a non-SSE response have already reached the caller,
                # so neither a replacement 502 nor a clean EOF can describe the
                # truncated body correctly. Closing the connection lets clients
                # detect an incomplete transfer instead of accepting it as whole.
                downstream.force_close()
                return downstream
            return web.json_response(
                {"error": {"message": f"backend {backend_index + 1} unavailable: {exc}",
                           "type": "upstream_error"}}, status=502,
            )
        finally:
            for acquired_index in acquired_indices:
                self.available.put_nowait(acquired_index)


def main() -> None:
    parser = argparse.ArgumentParser(description="Two-tab chatgpt-web2api gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9181)
    parser.add_argument("--backends", nargs=2,
                        default=["http://127.0.0.1:9182", "http://127.0.0.1:9183"])
    parser.add_argument(
        "--queue-timeout",
        type=float,
        default=float(os.environ.get("W2A_GATEWAY_QUEUE_TIMEOUT", "60")),
        help="maximum seconds to wait for a free worker (default: 60)",
    )
    parser.add_argument(
        "--max-queued",
        type=int,
        default=int(
            os.environ.get(
                "W2A_GATEWAY_MAX_QUEUED", str(DEFAULT_MAX_QUEUED_REQUESTS)
            )
        ),
        help="maximum requests waiting for a worker (default: 32)",
    )
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=float(
            os.environ.get(
                "W2A_GATEWAY_HEALTH_TIMEOUT", str(DEFAULT_HEALTH_TIMEOUT_SECONDS)
            )
        ),
        help="maximum seconds for each worker health probe (default: 5)",
    )
    parser.add_argument(
        "--max-concurrent-assets",
        type=int,
        default=int(
            os.environ.get(
                "W2A_GATEWAY_MAX_CONCURRENT_ASSETS",
                str(DEFAULT_MAX_CONCURRENT_ASSETS),
            )
        ),
        help="maximum concurrent generated-asset transfers (default: 4)",
    )
    args = parser.parse_args()
    gateway = DualTabGateway(
        args.backends,
        queue_timeout_seconds=args.queue_timeout,
        max_queued_requests=args.max_queued,
        health_timeout_seconds=args.health_timeout,
        max_concurrent_assets=args.max_concurrent_assets,
    )
    app = web.Application(client_max_size=70 * 1024 * 1024)
    app.on_startup.append(gateway.start)
    app.on_cleanup.append(gateway.stop)
    app.router.add_route("*", "/{tail:.*}", gateway.proxy)
    web.run_app(
        app,
        host=args.host,
        port=args.port,
        access_log=None,
        handler_cancellation=True,
    )


if __name__ == "__main__":
    main()
