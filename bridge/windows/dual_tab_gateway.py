"""Streaming gateway for exactly two chatgpt-web2api workers."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Iterable

from aiohttp import ClientSession, ClientTimeout, web


HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
}


def filtered_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {name: value for name, value in headers if name.lower() not in HOP_BY_HOP}


class DualTabGateway:
    def __init__(self, backends: list[str]) -> None:
        if len(backends) != 2:
            raise ValueError("exactly two backends are required")
        self.backends = [url.rstrip("/") for url in backends]
        self.available: asyncio.Queue[int] = asyncio.Queue(maxsize=2)
        self.available.put_nowait(0)
        self.available.put_nowait(1)
        self.session: ClientSession | None = None

    async def start(self, app: web.Application) -> None:
        self.session = ClientSession(
            timeout=ClientTimeout(total=None, connect=10, sock_connect=10, sock_read=None)
        )

    async def stop(self, app: web.Application) -> None:
        if self.session is not None:
            await self.session.close()

    def upstream_headers(self, request: web.Request) -> dict[str, str]:
        headers = filtered_headers(request.headers.items())
        headers.pop("Host", None)
        headers["X-Forwarded-Host"] = request.headers.get("X-Forwarded-Host", request.host)
        headers["X-Forwarded-Proto"] = request.headers.get(
            "X-Forwarded-Proto", request.scheme
        )
        return headers

    async def health(self, request: web.Request) -> web.Response:
        assert self.session is not None

        async def probe(index: int) -> dict:
            try:
                async with self.session.get(f"{self.backends[index]}/health") as response:
                    payload = await response.json(content_type=None)
                    payload["http_status"] = response.status
                    return payload
            except Exception as exc:
                return {"status": "broken", "error": str(exc), "http_status": 502}

        results = await asyncio.gather(probe(0), probe(1))
        ready = [
            result.get("chrome_running") is True
            and result.get("cdp_connected") is True
            and result.get("http_status") == 200
            for result in results
        ]
        return web.json_response(
            {
                "status": "healthy" if all(ready) else "degraded",
                "mode": "dual-tab",
                "capacity": 2,
                "ready_backends": sum(ready),
                "backends": results,
            },
            status=200 if any(ready) else 503,
        )

    async def proxy_asset(self, request: web.Request) -> web.StreamResponse:
        """Try both workers because generated asset tokens are worker-local."""
        assert self.session is not None
        body = await request.read()
        for index, backend in enumerate(self.backends):
            async with self.session.request(
                request.method, f"{backend}{request.rel_url}",
                headers=self.upstream_headers(request), data=body, allow_redirects=False,
            ) as response:
                response_body = await response.read()
                if response.status != 404 or index == len(self.backends) - 1:
                    return web.Response(
                        status=response.status, body=response_body,
                        headers=filtered_headers(response.headers.items()),
                    )
        raise web.HTTPNotFound()

    async def proxy(self, request: web.Request) -> web.StreamResponse:
        if request.path in ("/", "/health"):
            return await self.health(request)
        if request.path.startswith("/v1/assets/"):
            return await self.proxy_asset(request)

        assert self.session is not None
        backend_index = await self.available.get()
        try:
            body = await request.read()
            async with self.session.request(
                request.method, f"{self.backends[backend_index]}{request.rel_url}",
                headers=self.upstream_headers(request), data=body, allow_redirects=False,
            ) as upstream:
                downstream = web.StreamResponse(
                    status=upstream.status, reason=upstream.reason,
                    headers=filtered_headers(upstream.headers.items()),
                )
                await downstream.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await downstream.write(chunk)
                await downstream.write_eof()
                return downstream
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception as exc:
            logging.exception("backend %s failed", backend_index + 1)
            return web.json_response(
                {"error": {"message": f"backend {backend_index + 1} unavailable: {exc}",
                           "type": "upstream_error"}}, status=502,
            )
        finally:
            self.available.put_nowait(backend_index)


def main() -> None:
    parser = argparse.ArgumentParser(description="Two-tab chatgpt-web2api gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9181)
    parser.add_argument("--backends", nargs=2,
                        default=["http://127.0.0.1:9182", "http://127.0.0.1:9183"])
    args = parser.parse_args()
    gateway = DualTabGateway(args.backends)
    app = web.Application(client_max_size=70 * 1024 * 1024)
    app.on_startup.append(gateway.start)
    app.on_cleanup.append(gateway.stop)
    app.router.add_route("*", "/{tail:.*}", gateway.proxy)
    web.run_app(app, host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
