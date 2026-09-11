"""Local-only streaming gateway for two independent DeepSeek Web2API workers."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import math
import os
import time
from collections.abc import Iterable
from contextlib import suppress
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_QUEUED_REQUESTS = 32
DEFAULT_HEALTH_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_REQUEST_BYTES = 30 * 1024 * 1024

_HOP_BY_HOP = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_FORWARDED_HEADERS = {
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
}
_NOT_READY_STATUSES = {
    "authentication_required",
    "broken",
    "captcha",
    "captcha_required",
    "cdp_unavailable",
    "degraded",
    "dom_changed",
    "error",
    "failed",
    "login_required",
    "not_ready",
    "rate_limited",
    "starting",
    "stopped",
    "unavailable",
    "unhealthy",
}
_READY_STATUSES = {"healthy", "ok", "ready", "running"}


class GatewayQueueFull(Exception):
    """Raised when both workers and the bounded waiting queue are full."""


class GatewayQueueTimeout(Exception):
    """Raised when a queued request does not receive a worker in time."""


def _connection_header_names(headers: Iterable[tuple[str, str]]) -> set[str]:
    names: set[str] = set()
    for name, value in headers:
        if name.lower() == "connection":
            names.update(part.strip().lower() for part in value.split(",") if part.strip())
    return names


def filtered_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Drop HTTP hop-by-hop headers, including names listed by Connection."""

    pairs = list(headers)
    forbidden = _HOP_BY_HOP | _connection_header_names(pairs)
    return {name: value for name, value in pairs if name.lower() not in forbidden}


def _is_loopback_host(value: str | None) -> bool:
    if not value:
        return False
    candidate = value.strip().lower().strip("[]").split("%", 1)[0]
    if candidate == "localhost":
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def _normalize_backend_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or not _is_loopback_host(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("backend URLs must be plain HTTP loopback origins")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("backend URL has an invalid port") from exc
    return value.rstrip("/")


def _header_value(headers: object, wanted: str) -> str:
    items = getattr(headers, "items", None)
    if not callable(items):
        return ""
    wanted_lower = wanted.lower()
    for name, value in items():
        if str(name).lower() == wanted_lower:
            return str(value)
    return ""


def _worker_is_ready(payload: dict[str, object], http_status: int) -> bool:
    if not 200 <= http_status < 300:
        return False

    status = str(payload.get("status", "")).strip().lower()
    if status in _NOT_READY_STATUSES:
        return False

    for field in ("ready", "chrome_running", "cdp_connected", "logged_in"):
        if field in payload and payload[field] is not True:
            return False
    if payload.get("busy") is True:
        return False

    if payload.get("ready") is True:
        return True
    if status in _READY_STATUSES:
        return True
    return payload.get("cdp_connected") is True and payload.get("logged_in") is True


class DualTabGateway:
    """Dispatch at most one request to each browser-backed worker at a time."""

    def __init__(
        self,
        backends: list[str],
        *,
        queue_timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
        max_queued_requests: int = DEFAULT_MAX_QUEUED_REQUESTS,
        health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
    ) -> None:
        if len(backends) != 2:
            raise ValueError("exactly two backends are required")
        if queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be positive")
        if max_queued_requests < 0:
            raise ValueError("max_queued_requests must not be negative")
        if health_timeout_seconds <= 0:
            raise ValueError("health_timeout_seconds must be positive")

        normalized = [_normalize_backend_url(url) for url in backends]
        if normalized[0] == normalized[1]:
            raise ValueError("backend URLs must be distinct")

        self.backends = normalized
        self.queue_timeout_seconds = queue_timeout_seconds
        self.max_queued_requests = max_queued_requests
        self.health_timeout_seconds = health_timeout_seconds
        self.available: asyncio.Queue[int] = asyncio.Queue(maxsize=2)
        self.available.put_nowait(0)
        self.available.put_nowait(1)
        self._queued_requests = 0
        self._not_ready_until = [0.0, 0.0]
        self.session: ClientSession | None = None

    @property
    def queued_requests(self) -> int:
        return self._queued_requests

    async def start(self, _app: web.Application) -> None:
        self.session = ClientSession(
            timeout=ClientTimeout(total=None, connect=10, sock_connect=10, sock_read=None),
            auto_decompress=False,
            trust_env=False,
        )

    async def stop(self, _app: web.Application) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _acquire_backend(self) -> int:
        try:
            candidate = self.available.get_nowait()
            return self._prefer_non_quarantined(candidate)
        except asyncio.QueueEmpty:
            if self._queued_requests >= self.max_queued_requests:
                raise GatewayQueueFull from None

        self._queued_requests += 1
        try:
            try:
                candidate = await asyncio.wait_for(
                    self.available.get(), timeout=self.queue_timeout_seconds
                )
                return self._prefer_non_quarantined(candidate)
            except TimeoutError:
                raise GatewayQueueTimeout from None
        finally:
            self._queued_requests -= 1

    def _release_backend(self, backend_index: int) -> None:
        self.available.put_nowait(backend_index)

    def _prefer_non_quarantined(self, candidate: int) -> int:
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

    def _remember_backend_health(self, index: int, health: dict[str, object]) -> None:
        if health.get("ready") is True:
            self._not_ready_until[index] = 0.0
        else:
            self._not_ready_until[index] = time.monotonic() + 5.0

    def upstream_headers(self, request: web.Request) -> dict[str, str]:
        headers = filtered_headers(request.headers.items())
        headers = {
            name: value
            for name, value in headers.items()
            if name.lower() != "host" and name.lower() not in _FORWARDED_HEADERS
        }
        remote = getattr(request, "remote", None)
        if remote:
            headers["X-Forwarded-For"] = remote
        headers["X-Forwarded-Host"] = request.host
        headers["X-Forwarded-Proto"] = request.scheme
        return headers

    async def _probe_backend(
        self, index: int, *, timeout_seconds: float | None = None
    ) -> dict[str, object]:
        assert self.session is not None
        last_error: Exception | None = None
        timeout = timeout_seconds or self.health_timeout_seconds

        for health_path in ("/health", "/healthz"):
            try:
                async with asyncio.timeout(timeout):
                    async with self.session.get(
                        f"{self.backends[index]}{health_path}", allow_redirects=False
                    ) as response:
                        if response.status == 404 and health_path == "/health":
                            continue
                        try:
                            decoded = await response.json(content_type=None)
                        except Exception as exc:  # noqa: BLE001 - report malformed health
                            return {
                                "worker": index + 1,
                                "status": "invalid_health_response",
                                "error": type(exc).__name__,
                                "http_status": response.status,
                                "reachable": True,
                                "ready": False,
                                "health_path": health_path,
                            }
                        payload = decoded if isinstance(decoded, dict) else {}
                        ready = _worker_is_ready(payload, response.status)
                        return {
                            **payload,
                            "worker": index + 1,
                            "http_status": response.status,
                            "reachable": True,
                            "ready": ready,
                            "health_path": health_path,
                        }
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - aggregate health must stay available
                last_error = exc
                break

        return {
            "worker": index + 1,
            "status": "unavailable",
            "error": type(last_error).__name__ if last_error is not None else "health_not_found",
            "http_status": 502,
            "reachable": False,
            "ready": False,
            "health_path": None,
        }

    async def health(self, _request: web.Request) -> web.Response:
        results = await asyncio.gather(self._probe_backend(0), self._probe_backend(1))
        for index, result in enumerate(results):
            self._remember_backend_health(index, result)
        ready_count = sum(result.get("ready") is True for result in results)
        reachable_count = sum(result.get("reachable") is True for result in results)
        status = "healthy" if ready_count == 2 else "degraded"
        if reachable_count == 0:
            status = "unavailable"

        return web.json_response(
            {
                "status": status,
                "mode": "deepseek-dual-worker",
                "capacity": 2,
                "available_workers": self.available.qsize(),
                "queued_requests": self._queued_requests,
                "max_queued_requests": self.max_queued_requests,
                "ready_backends": ready_count,
                "reachable_backends": reachable_count,
                "backends": results,
            },
            status=200 if ready_count > 0 else 503,
            headers={"Cache-Control": "no-store"},
        )

    @staticmethod
    def _error_response(
        *, message: str, error_type: str, status: int, retry_after: int | None = None
    ) -> web.Response:
        headers = {"Cache-Control": "no-store"}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return web.json_response(
            {"error": {"message": message, "type": error_type}},
            status=status,
            headers=headers,
        )

    async def proxy(self, request: web.Request) -> web.StreamResponse:
        if not _is_loopback_host(getattr(request, "remote", None)):
            return self._error_response(
                message="the DeepSeek Web2API gateway accepts localhost clients only",
                error_type="local_access_required",
                status=403,
            )

        if request.path in ("/", "/health", "/healthz"):
            return await self.health(request)

        assert self.session is not None
        acquired_indices: list[int] = []
        try:
            backend_index = await self._acquire_backend()
            acquired_indices.append(backend_index)
        except GatewayQueueFull:
            return self._error_response(
                message="the local DeepSeek Web2API queue is full; retry later",
                error_type="queue_full",
                status=429,
                retry_after=1,
            )
        except GatewayQueueTimeout:
            retry_after = max(1, math.ceil(self.queue_timeout_seconds))
            return self._error_response(
                message=(
                    "both DeepSeek Web2API workers remained busy until the queue wait expired"
                ),
                error_type="capacity_timeout",
                status=503,
                retry_after=retry_after,
            )

        downstream: web.StreamResponse | None = None
        upstream_response: object | None = None
        try:
            selected_health = await self._probe_backend(
                backend_index,
                timeout_seconds=min(2.0, self.health_timeout_seconds),
            )
            self._remember_backend_health(backend_index, selected_health)
            if selected_health.get("ready") is not True:
                try:
                    other_index = await self._acquire_backend()
                    acquired_indices.append(other_index)
                except GatewayQueueFull:
                    return self._error_response(
                        message="the only ready DeepSeek worker is busy and the local queue is full",
                        error_type="queue_full",
                        status=429,
                        retry_after=1,
                    )
                except GatewayQueueTimeout:
                    return self._error_response(
                        message="no ready DeepSeek worker became available before the queue wait expired",
                        error_type="capacity_timeout",
                        status=503,
                        retry_after=max(1, math.ceil(self.queue_timeout_seconds)),
                    )

                other_health = await self._probe_backend(
                    other_index,
                    timeout_seconds=min(2.0, self.health_timeout_seconds),
                )
                self._remember_backend_health(other_index, other_health)
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
                                "message": "neither local DeepSeek browser worker is ready",
                                "type": "no_ready_worker",
                                "workers": summaries,
                            }
                        },
                        status=503,
                        headers={"Cache-Control": "no-store", "Retry-After": "5"},
                    )
                backend_index = other_index

            body = await request.read()
            async with self.session.request(
                request.method,
                f"{self.backends[backend_index]}{request.rel_url}",
                headers=self.upstream_headers(request),
                data=body,
                allow_redirects=False,
            ) as upstream:
                upstream_response = upstream
                downstream = web.StreamResponse(
                    status=upstream.status,
                    reason=upstream.reason,
                    headers=filtered_headers(upstream.headers.items()),
                )
                await downstream.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await downstream.write(chunk)
                await downstream.write_eof()
                return downstream
        except asyncio.CancelledError:
            close = getattr(upstream_response, "close", None)
            if callable(close):
                close()
            raise
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001 - translate transport failures
            logger.exception("DeepSeek worker %s request failed", backend_index + 1)
            if downstream is not None and downstream.prepared:
                close = getattr(upstream_response, "close", None)
                if callable(close):
                    close()
                if "text/event-stream" in _header_value(
                    downstream.headers, "Content-Type"
                ).lower():
                    payload = {
                        "error": {
                            "message": "upstream DeepSeek stream interrupted",
                            "type": "upstream_error",
                        }
                    }
                    with suppress(Exception):
                        await downstream.write(
                            f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()
                        )
                        await downstream.write(b"data: [DONE]\n\n")
                with suppress(Exception):
                    await downstream.write_eof()
                return downstream
            return self._error_response(
                message=f"DeepSeek worker {backend_index + 1} is unavailable",
                error_type="upstream_error",
                status=502,
            )
        finally:
            for acquired_index in acquired_indices:
                self._release_backend(acquired_index)


def create_app(
    backends: list[str],
    *,
    queue_timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
    max_queued_requests: int = DEFAULT_MAX_QUEUED_REQUESTS,
    health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
) -> web.Application:
    if max_request_bytes <= 0:
        raise ValueError("max_request_bytes must be positive")
    gateway = DualTabGateway(
        backends,
        queue_timeout_seconds=queue_timeout_seconds,
        max_queued_requests=max_queued_requests,
        health_timeout_seconds=health_timeout_seconds,
    )
    app = web.Application(client_max_size=max_request_bytes)
    app["gateway"] = gateway
    app.on_startup.append(gateway.start)
    app.on_cleanup.append(gateway.stop)
    app.router.add_route("*", "/{tail:.*}", gateway.proxy)
    return app


def _environment_number(name: str, default: str, converter: type[int] | type[float]):
    try:
        return converter(os.environ.get(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid {converter.__name__}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Local two-worker DeepSeek Web2API gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9191)
    parser.add_argument(
        "--backends",
        nargs=2,
        default=["http://127.0.0.1:9192", "http://127.0.0.1:9193"],
    )
    parser.add_argument(
        "--queue-timeout",
        type=float,
        default=_environment_number("DEEPSEEK_GATEWAY_QUEUE_TIMEOUT", "60", float),
    )
    parser.add_argument(
        "--max-queued",
        type=int,
        default=_environment_number("DEEPSEEK_GATEWAY_MAX_QUEUED", "32", int),
    )
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=_environment_number("DEEPSEEK_GATEWAY_HEALTH_TIMEOUT", "5", float),
    )
    parser.add_argument(
        "--max-request-bytes",
        type=int,
        default=_environment_number(
            "DEEPSEEK_GATEWAY_MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES), int
        ),
    )
    args = parser.parse_args()

    if not _is_loopback_host(args.host):
        parser.error("--host must be localhost or a loopback IP address")
    try:
        app = create_app(
            args.backends,
            queue_timeout_seconds=args.queue_timeout,
            max_queued_requests=args.max_queued,
            health_timeout_seconds=args.health_timeout,
            max_request_bytes=args.max_request_bytes,
        )
    except ValueError as exc:
        parser.error(str(exc))

    logging.basicConfig(
        level=os.environ.get("DEEPSEEK_GATEWAY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    web.run_app(app, host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
