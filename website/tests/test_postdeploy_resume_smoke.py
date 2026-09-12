from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "postdeploy_resume_smoke.py"
)
SPEC = importlib.util.spec_from_file_location("postdeploy_resume_smoke", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


class ClosingStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, closed: asyncio.Event) -> None:
        self.body = body
        self.closed = closed

    async def __aiter__(self):
        yield self.body

    async def aclose(self) -> None:
        self.closed.set()


def _config() -> Any:
    return smoke.SmokeConfig(
        base_url="https://testserver",
        admin_username="owner",
        admin_password="not-a-real-password",
        model_selector="gpt-smoke",
        provider_selector="",
        marker="QA-FAST-MOCK-123456",
        effort="default",
        timeout_seconds=30.0,
    )


def _response(request: httpx.Request, status: int, **kwargs: Any) -> httpx.Response:
    return httpx.Response(status, request=request, **kwargs)


def test_smoke_disconnects_resumes_and_cleans(monkeypatch: pytest.MonkeyPatch) -> None:
    initial_closed = asyncio.Event()
    calls: list[str] = []
    generated_paths: list[Path] = []
    real_async_client = httpx.AsyncClient
    real_write_images = smoke._write_reference_images

    def tracked_write_images(directory: Path) -> tuple[Path, Path]:
        paths = real_write_images(directory)
        generated_paths.extend(paths)
        return paths

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}")
        if path == "/login":
            return _response(
                request,
                303,
                headers={
                    "location": "/",
                    "set-cookie": "ai_chat_session=mock-session; Secure; Path=/",
                },
            )
        if path == "/api/models":
            return _response(
                request,
                200,
                json={
                    "data": [
                        {
                            "id": "external.123456789abc.Z3B0LXNtb2tl",
                            "rawModel": "gpt-smoke",
                            "source": "external",
                            "providerId": "123456789abc",
                            "providerName": "Web2API",
                            "inputModalities": ["text", "image"],
                        }
                    ]
                },
            )
        if path == "/api/uploads":
            return _response(
                request,
                200,
                json={
                    "files": [
                        {"id": "red.png", "name": "smoke-red.png"},
                        {"id": "blue.png", "name": "smoke-blue.png"},
                    ]
                },
            )
        if path == "/api/turn":
            payload = json.loads(request.content)
            assert payload["client_conversation_id"] is None
            assert len(payload["attachments"]) == 2
            body = b'{"type":"started","cursor":1,"threadId":"external"}\n'
            return _response(
                request,
                200,
                stream=ClosingStream(body, initial_closed),
                headers={"content-type": "application/x-ndjson"},
            )
        if path.startswith("/api/turn/resume/"):
            assert initial_closed.is_set()
            assert request.url.params["cursor"] == "1"
            body = (
                b'{"type":"ping","cursor":1}\n'
                b'{"type":"delta","cursor":2,"text":"The red square and blue square."}\n'
                b'{"type":"done","cursor":3,"files":[]}\n'
            )
            return _response(
                request,
                200,
                content=body,
                headers={"content-type": "application/x-ndjson"},
            )
        if path == "/api/conversations/delete":
            payload = json.loads(request.content)
            assert payload["conversation_id"] is None
            assert payload["delete_workspace"] is True
            return _response(request, 200, json={"workspaceDeleted": True})
        if path == "/logout":
            return _response(request, 303, headers={"location": "/login"})
        raise AssertionError(f"unexpected mocked route: {request.method} {path}")

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(smoke.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(smoke, "_write_reference_images", tracked_write_images)

    result = asyncio.run(smoke._run_smoke(_config()))

    assert result["ok"] is True
    assert result["initialCursor"] == 1
    assert result["finalCursor"] == 3
    assert result["durableEvents"] == ["started", "delta", "done"]
    assert result["cleanup"] == "complete"
    assert initial_closed.is_set()
    assert "POST /api/conversations/delete" in calls
    assert "POST /logout" in calls
    assert generated_paths and all(not path.exists() for path in generated_paths)


def test_smoke_interrupts_and_cleans_when_resume_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_closed = asyncio.Event()
    calls: list[str] = []
    real_async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}")
        if path == "/login":
            return _response(
                request,
                303,
                headers={"set-cookie": "ai_chat_session=mock-session; Secure; Path=/"},
            )
        if path == "/api/models":
            return _response(
                request,
                200,
                json={
                    "data": [
                        {
                            "id": "external.123456789abc.Z3B0LXNtb2tl",
                            "rawModel": "gpt-smoke",
                            "source": "external",
                            "providerId": "123456789abc",
                            "providerName": "Web2API",
                            "inputModalities": ["text", "image"],
                        }
                    ]
                },
            )
        if path == "/api/uploads":
            return _response(
                request,
                200,
                json={
                    "files": [
                        {"id": "red.png", "name": "smoke-red.png"},
                        {"id": "blue.png", "name": "smoke-blue.png"},
                    ]
                },
            )
        if path == "/api/turn":
            body = b'{"type":"started","cursor":1,"threadId":"external"}\n'
            return _response(
                request,
                200,
                stream=ClosingStream(body, initial_closed),
                headers={"content-type": "application/x-ndjson"},
            )
        if path.startswith("/api/turn/resume/"):
            raise httpx.ReadTimeout("mock timeout", request=request)
        if path.startswith("/api/turn/external/") and path.endswith("/interrupt"):
            return _response(request, 200, json={"ok": True})
        if path == "/api/conversations/delete":
            return _response(request, 200, json={"workspaceDeleted": True})
        if path == "/logout":
            return _response(request, 303, headers={"location": "/login"})
        raise AssertionError(f"unexpected mocked route: {request.method} {path}")

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(smoke.httpx, "AsyncClient", client_factory)

    with pytest.raises(smoke.SmokeFailure, match="resumed turn"):
        asyncio.run(smoke._run_smoke(_config()))

    assert initial_closed.is_set()
    assert any(path.startswith("POST /api/turn/external/") for path in calls)
    assert "POST /api/conversations/delete" in calls
    assert "POST /logout" in calls


def test_durable_cursor_must_increase() -> None:
    state = smoke.StreamState(cursor=2)
    with pytest.raises(smoke.SmokeFailure, match="strictly increasing"):
        smoke._consume_event(state, {"type": "done", "cursor": 2})
