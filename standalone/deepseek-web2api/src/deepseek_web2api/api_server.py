"""Minimal OpenAI-compatible aiohttp API for the dedicated DeepSeek tab."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from typing import Any

from aiohttp import web

from .cdp_driver import CDPDriver, ReplyDelta
from .config import Config
from .exceptions import (
    DeepSeekWeb2APIError,
    GenerationCancelled,
    InvalidRequest,
    UnsupportedModel,
)

logger = logging.getLogger(__name__)
SSE_HEARTBEAT_SECONDS = 15.0


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise InvalidRequest("Each message content must be text or an array of text parts")
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") not in {"text", "input_text"}:
            raise InvalidRequest("This experimental bridge currently accepts text parts only")
        text = item.get("text")
        if not isinstance(text, str):
            raise InvalidRequest("Text content parts must contain a string 'text' field")
        parts.append(text)
    return "\n".join(parts)


def _build_prompt(messages: Any) -> str:
    if not isinstance(messages, list) or not messages:
        raise InvalidRequest("'messages' must be a non-empty array")
    blocks: list[str] = []
    role_names = {"system": "System", "user": "User", "assistant": "Assistant"}
    for message in messages:
        if not isinstance(message, dict):
            raise InvalidRequest("Every message must be an object")
        role = message.get("role")
        if role not in role_names:
            raise InvalidRequest("Only system, user, and assistant messages are supported")
        content = _text_content(message.get("content", "")).replace("\x00", "")
        blocks.append(f"[{role_names[role]}]\n{content}")
    if messages[-1].get("role") != "user":
        raise InvalidRequest("The final message must have role 'user'")
    return "\n\n".join(blocks).strip()


def _error_payload(message: str, code: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": code, "code": code}}


class APIServer:
    def __init__(self, config: Config, driver: CDPDriver | None = None) -> None:
        self.config = config
        self.driver = driver or CDPDriver(config)
        self._request_lock = asyncio.Lock()
        self._warmup_task: asyncio.Task[None] | None = None
        self.app = web.Application(client_max_size=config.server.max_request_bytes)
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/v1/models", self.handle_models)
        self.app.router.add_post("/v1/chat/completions", self.handle_chat)
        self.app.on_startup.append(self._on_startup)
        self.app.on_cleanup.append(self._on_cleanup)

    def _authorized(self, request: web.Request) -> bool:
        keys = self.config.server.api_keys
        if not keys:
            return True
        header = request.headers.get("Authorization", "")
        candidates: list[str] = []
        if header[:7].lower() == "bearer ":
            candidates.append(header[7:].strip())
        candidates.append(request.headers.get("X-API-Key", "").strip())
        candidates = [candidate for candidate in candidates if candidate]
        if not candidates:
            return False
        return any(
            secrets.compare_digest(candidate, key)
            for candidate in candidates
            for key in keys
        )

    def _auth_error(self) -> web.Response:
        return web.json_response(_error_payload("Invalid API key", "invalid_api_key"), status=401)

    async def _on_startup(self, app: web.Application) -> None:
        del app

        async def warmup() -> None:
            try:
                await self.driver.connect()
            except Exception as exc:
                # The API stays available so /health can tell the user to log
                # in or open the dedicated browser. No retry loop is hidden here.
                logger.info("DeepSeek browser warm-up needs attention: %s", type(exc).__name__)

        self._warmup_task = asyncio.create_task(warmup(), name="deepseek-browser-warmup")

    async def _on_cleanup(self, app: web.Application) -> None:
        del app
        if self._warmup_task:
            self._warmup_task.cancel()
            await asyncio.gather(self._warmup_task, return_exceptions=True)
        await self.driver.close()

    async def handle_health(self, request: web.Request) -> web.Response:
        del request
        state = await self.driver.status()
        return web.json_response(
            {
                **state,
                "models": list(self.config.deepseek.models),
            }
        )

    async def handle_models(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._auth_error()
        now = int(time.time())
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": model,
                        "object": "model",
                        "created": now,
                        "owned_by": "deepseek-web",
                    }
                    for model in self.config.deepseek.models
                ],
            }
        )

    async def _watch_disconnect(
        self, request: web.Request, cancel_event: asyncio.Event
    ) -> None:
        while not cancel_event.is_set():
            transport = request.transport
            if transport is None or transport.is_closing():
                cancel_event.set()
                return
            await asyncio.sleep(0.1)

    async def _acquire_request_slot(self, cancel_event: asyncio.Event) -> None:
        """Acquire the sole browser slot, but abandon a disconnected waiter."""

        acquire_task = asyncio.create_task(self._request_lock.acquire())
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {acquire_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done and cancel_event.is_set():
                if acquire_task.done() and acquire_task.result():
                    self._request_lock.release()
                else:
                    acquire_task.cancel()
                    await asyncio.gather(acquire_task, return_exceptions=True)
                raise GenerationCancelled("Client disconnected while waiting")
            await acquire_task
        finally:
            if not acquire_task.done():
                acquire_task.cancel()
                await asyncio.gather(acquire_task, return_exceptions=True)
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def handle_chat(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return self._auth_error()
        try:
            payload = await request.json(loads=json.loads)
            if not isinstance(payload, dict):
                raise InvalidRequest("Request body must be a JSON object")
            model = payload.get("model", self.config.deepseek.default_model)
            if not isinstance(model, str) or model not in self.config.deepseek.models:
                raise UnsupportedModel(f"Unsupported model: {model}")
            tools = payload.get("tools")
            tool_choice = payload.get("tool_choice")
            if tools not in (None, []) or tool_choice not in (None, "none"):
                raise InvalidRequest("Tool calls are not supported by this experimental bridge")
            prompt = _build_prompt(payload.get("messages"))
            stream = payload.get("stream", False)
            if not isinstance(stream, bool):
                raise InvalidRequest("'stream' must be true or false")
        except (json.JSONDecodeError, UnicodeError, ValueError):
            return web.json_response(_error_payload("Invalid JSON body", "invalid_request"), status=400)
        except DeepSeekWeb2APIError as exc:
            return web.json_response(
                _error_payload(str(exc), exc.code), status=exc.http_status
            )

        cancel_event = asyncio.Event()
        watcher = asyncio.create_task(
            self._watch_disconnect(request, cancel_event), name="deepseek-client-watch"
        )
        slot_acquired = False
        try:
            await self._acquire_request_slot(cancel_event)
            slot_acquired = True
            if stream:
                return await self._stream_response(
                    request, prompt, model, cancel_event
                )
            reply = await self.driver.complete(
                prompt,
                model=model,
                cancel_event=cancel_event,
                timeout=self.config.server.request_timeout_seconds,
            )
            message: dict[str, Any] = {"role": "assistant", "content": reply.content}
            if reply.reasoning_content:
                message["reasoning_content"] = reply.reasoning_content
            return web.json_response(
                {
                    "id": f"chatcmpl-{uuid.uuid4().hex}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {"index": 0, "message": message, "finish_reason": "stop"}
                    ],
                }
            )
        except GenerationCancelled as exc:
            if cancel_event.is_set():
                return web.Response(status=499)
            return web.json_response(_error_payload(str(exc), exc.code), status=exc.http_status)
        except DeepSeekWeb2APIError as exc:
            return web.json_response(_error_payload(str(exc), exc.code), status=exc.http_status)
        except asyncio.CancelledError:
            cancel_event.set()
            await self.driver.stop_generation()
            raise
        except Exception:
            logger.exception("Unexpected DeepSeek bridge failure")
            return web.json_response(
                _error_payload("Unexpected bridge failure", "internal_error"), status=500
            )
        finally:
            if slot_acquired:
                self._request_lock.release()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def _stream_response(
        self,
        request: web.Request,
        prompt: str,
        model: str,
        cancel_event: asyncio.Event,
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        async def write_event(delta: dict[str, Any], finish_reason: str | None = None) -> None:
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish_reason}
                ],
            }
            await response.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())

        generator = self.driver.stream_reply(
            prompt,
            model=model,
            cancel_event=cancel_event,
            timeout=self.config.server.request_timeout_seconds,
        )
        next_delta: asyncio.Task[ReplyDelta] | None = None
        try:
            await write_event({"role": "assistant", "content": ""})
            while True:
                if next_delta is None:
                    next_delta = asyncio.create_task(anext(generator))
                done, _ = await asyncio.wait(
                    {next_delta}, timeout=SSE_HEARTBEAT_SECONDS
                )
                if not done:
                    await response.write(b": keep-alive\n\n")
                    continue
                delta = next_delta.result()
                next_delta = None
                assert isinstance(delta, ReplyDelta)
                payload: dict[str, Any] = {}
                if delta.reasoning_content:
                    payload["reasoning_content"] = delta.reasoning_content
                if delta.content:
                    payload["content"] = delta.content
                if payload:
                    await write_event(payload)
        except StopAsyncIteration:
            await write_event({}, "stop")
            await response.write(b"data: [DONE]\n\n")
        except (ConnectionError, ConnectionResetError, BrokenPipeError):
            cancel_event.set()
            await self.driver.stop_generation()
        except GenerationCancelled:
            if not cancel_event.is_set():
                raise
        except DeepSeekWeb2APIError as exc:
            if not cancel_event.is_set():
                error = _error_payload(str(exc), exc.code)
                await response.write(
                    f"data: {json.dumps(error, ensure_ascii=False)}\n\n".encode()
                )
                await response.write(b"data: [DONE]\n\n")
        finally:
            if next_delta is not None and not next_delta.done():
                next_delta.cancel()
                await asyncio.gather(next_delta, return_exceptions=True)
            if request.transport is None:
                cancel_event.set()
            await generator.aclose()
        return response


def create_app(config: Config, driver: CDPDriver | None = None) -> web.Application:
    return APIServer(config, driver).app
