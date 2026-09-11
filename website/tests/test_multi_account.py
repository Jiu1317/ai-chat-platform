import asyncio
import io
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


_website_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_website_dir))

_temporary_root = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_root = Path(_temporary_root.name)
os.environ["AI_CHAT_DATA_DIR"] = str(_root / "data")
os.environ["AI_CHAT_WORKSPACE_ROOT"] = str(_root / "workspaces")
os.environ["CODEX_HOME"] = str(_root / "codex")
os.environ["AI_CHAT_ACCESS_PASSWORD"] = "Initial-admin-password"
os.environ["AI_CHAT_ADMIN_USERNAME"] = "owner"

(_root / "data").mkdir(parents=True, exist_ok=True)
with closing(sqlite3.connect(_root / "data" / "conversations.sqlite3")) as _legacy_database, _legacy_database:
    _legacy_database.execute(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    _legacy_database.execute(
        "CREATE TABLE conversation_tombstones (id TEXT PRIMARY KEY, deleted_at INTEGER NOT NULL)"
    )
    _legacy_database.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    _legacy_database.execute(
        "CREATE TABLE project_tombstones (id TEXT PRIMARY KEY, deleted_at INTEGER NOT NULL)"
    )
    _legacy_database.execute(
        "INSERT INTO conversations (id, payload, updated_at) VALUES (?, ?, ?)",
        (
            "a" * 32,
            '{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","workspaceId":"99999999999999999999999999999999","title":"旧聊天","messages":[],"updatedAt":1}',
            1,
        ),
    )

import app  # noqa: E402


class ImageRequestDetectionTests(unittest.TestCase):
    def test_image_25_imperatives_are_detected(self) -> None:
        prompts = (
            "用image2.5画一只猫",
            "用 Image 2.5 画一只猫",
            "请使用 GPT Image 2.5 生成一幅海边日落",
            "调用 gpt-image-2.5-sunburst 绘制一只机械猫",
            "麻烦让 image_2_5 flare 帮我做一个头像",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertTrue(app._is_image_generation_request(prompt))

    def test_image_25_questions_are_not_generation_requests(self) -> None:
        prompts = (
            "Image 2.5 是什么？",
            "为什么 Image 2.5 生成图片这么慢？",
            "你支持 Image 2.5 吗？",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertFalse(app._is_image_generation_request(prompt))

    def test_image_preview_can_finish_before_high_quality_variant(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            output_root = Path(directory)
            source = output_root / "generated.png"
            app.Image.new("RGB", (1200, 800), "white").save(source)

            preview = app._prepare_image_variants(source, output_root, False)

            self.assertTrue(preview["previewPath"].is_file())
            self.assertNotIn("compressedSize", preview)
            complete = app._prepare_image_variants(source, output_root)
            self.assertTrue(complete["compressedPath"].is_file())
            self.assertGreater(int(complete["compressedSize"]), 0)

    def test_generated_image_is_streamed_to_disk(self) -> None:
        image_buffer = io.BytesIO()
        app.Image.new("RGB", (32, 24), "white").save(image_buffer, format="PNG")
        image_bytes = image_buffer.getvalue()

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "image/png", "content-length": str(len(image_bytes))}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def aiter_bytes(self, _chunk_size):
                midpoint = len(image_bytes) // 2
                yield image_bytes[:midpoint]
                yield image_bytes[midpoint:]

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return FakeResponse()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            workspace = Path(directory)
            with (
                patch.object(app, "IMAGE_BRIDGE_TOKEN", "test-token"),
                patch.object(app, "MAX_FILE_BYTES", 1),
                patch.object(app, "MAX_OUTPUT_FILE_BYTES", len(image_bytes)),
                patch.object(app.httpx, "AsyncClient", return_value=FakeClient()) as async_client,
                patch.object(app, "_schedule_image_variants") as schedule_variants,
            ):
                result = asyncio.run(app._generate_image("画一张白色图片", workspace))

            saved = workspace / "outputs" / result["path"]
            self.assertEqual(saved.read_bytes(), image_bytes)
            self.assertEqual(result["size"], len(image_bytes))
            self.assertTrue(result["previewSize"] > 0)
            schedule_variants.assert_called_once()
            timeout = async_client.call_args.kwargs["timeout"]
            self.assertEqual(timeout.read, app.IMAGE_BRIDGE_REQUEST_TIMEOUT_SECONDS)

    def test_image_stream_sends_heartbeat_without_cancelling_generation(self) -> None:
        payload = app.TurnRequest(
            session_id="1" * 32,
            message="画一只猫",
            model="image",
            effort="medium",
        )

        async def scenario() -> None:
            release = asyncio.Event()
            generation_cancelled = False

            async def slow_generate(_prompt, _workspace):
                nonlocal generation_cancelled
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    generation_cancelled = True
                    raise
                return {
                    "name": "image.png",
                    "size": 1,
                    "path": "image.png",
                    "mediaType": "image/png",
                    "inline": True,
                }

            with (
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory,
                patch.object(app, "LONG_TASK_HEARTBEAT_SECONDS", 0.01),
                patch.object(app, "_generate_image", side_effect=slow_generate),
            ):
                stream = app._image_stream(payload, Path(directory), "user-1")
                started = app.json.loads((await anext(stream)).decode())
                waiting = app.json.loads((await anext(stream)).decode())
                heartbeat = app.json.loads((await anext(stream)).decode())
                self.assertEqual(started["type"], "started")
                self.assertEqual(waiting["type"], "replace")
                self.assertEqual(heartbeat, {"type": "ping"})
                self.assertFalse(generation_cancelled)

                release.set()
                completed = [
                    app.json.loads((await anext(stream)).decode()),
                    app.json.loads((await anext(stream)).decode()),
                ]
                self.assertEqual(
                    [event["type"] for event in completed], ["replace", "done"]
                )
                with self.assertRaises(StopAsyncIteration):
                    await anext(stream)

        asyncio.run(scenario())

    def test_closing_image_stream_cancels_generation(self) -> None:
        payload = app.TurnRequest(
            session_id="9" * 32,
            message="画一只猫",
            model="image",
            effort="medium",
        )

        async def scenario() -> None:
            generation_cancelled = asyncio.Event()
            generation_started = asyncio.Event()

            async def slow_generate(_prompt, _workspace):
                generation_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    generation_cancelled.set()
                    raise

            with (
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory,
                patch.object(app, "LONG_TASK_HEARTBEAT_SECONDS", 0.01),
                patch.object(app, "_generate_image", side_effect=slow_generate),
            ):
                stream = app._image_stream(payload, Path(directory), "user-1")
                started = app.json.loads((await anext(stream)).decode())
                turn_id = started["turnId"]

                self.assertIn(turn_id, app.image_turns)
                await asyncio.wait_for(generation_started.wait(), timeout=0.1)
                await stream.aclose()
                self.assertTrue(generation_cancelled.is_set())
                self.assertNotIn(turn_id, app.image_turns)

        asyncio.run(scenario())

    def test_silent_external_stream_sends_heartbeat_without_losing_next_line(self) -> None:
        payload = app.TurnRequest(
            session_id="2" * 32,
            message="你好",
            model="external",
            effort="medium",
        )

        async def scenario() -> None:
            release = asyncio.Event()
            source_cancelled = False

            class FakeResponse:
                status_code = 200
                headers = {"content-type": "text/event-stream"}

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

                async def aiter_lines(self):
                    nonlocal source_cancelled
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        source_cancelled = True
                        raise
                    yield 'data: {"choices":[{"delta":{"content":"回答"},"finish_reason":"stop"}]}'
                    yield "data: [DONE]"

            class FakeClient:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

                def stream(self, *_args, **_kwargs):
                    return FakeResponse()

            provider = {
                "baseUrl": "https://provider.example/v1",
                "protocol": "openai",
                "apiKey": "test-key",
                "preset": "custom",
            }
            with (
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory,
                patch.object(app, "LONG_TASK_HEARTBEAT_SECONDS", 0.01),
                patch.object(
                    app,
                    "_validate_remote_base",
                    new=AsyncMock(return_value=provider["baseUrl"]),
                ),
                patch.object(
                    app,
                    "_external_current_content",
                    new=AsyncMock(return_value="你好"),
                ),
                patch.object(app.httpx, "AsyncClient", return_value=FakeClient()),
            ):
                stream = app._external_stream(
                    payload, Path(directory), provider, "test-model", "user-1"
                )
                started = app.json.loads((await anext(stream)).decode())
                heartbeat = app.json.loads((await anext(stream)).decode())
                self.assertEqual(started["type"], "started")
                self.assertEqual(heartbeat, {"type": "ping"})
                self.assertFalse(source_cancelled)

                release.set()
                delta = app.json.loads((await anext(stream)).decode())
                done = app.json.loads((await anext(stream)).decode())
                self.assertEqual(delta, {"type": "delta", "text": "回答"})
                self.assertEqual(done["type"], "done")
                with self.assertRaises(StopAsyncIteration):
                    await anext(stream)

        asyncio.run(scenario())


class RuntimeCleanupTests(unittest.TestCase):
    def test_lifespan_awaits_and_clears_background_tasks(self) -> None:
        cleanup_finished = asyncio.Event()
        worker_finished = asyncio.Event()

        async def fake_cleanup_loop() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_finished.set()

        async def lingering_worker() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                worker_finished.set()

        async def scenario() -> None:
            with (
                patch.object(app, "_prepare_directories"),
                patch.object(app, "_cleanup_loop", new=fake_cleanup_loop),
                patch.object(app.codex, "stop", new=AsyncMock()) as stop,
            ):
                async with app.lifespan(app.app):
                    task = asyncio.create_task(lingering_worker())
                    app.image_variant_tasks.add(task)
                    await asyncio.sleep(0)
                self.assertTrue(cleanup_finished.is_set())
                self.assertTrue(worker_finished.is_set())
                self.assertTrue(task.cancelled())
                self.assertEqual(app.image_variant_tasks, set())
                stop.assert_awaited_once()

        asyncio.run(scenario())

    def test_image_stream_enforces_outer_20_minute_deadline_and_cleans_task(self) -> None:
        generation_cancelled = False
        payload = app.TurnRequest(
            session_id="3" * 32,
            message="生成图片",
            model="codex-model",
            effort="default",
        )

        async def slow_generate(*_args, **_kwargs):
            nonlocal generation_cancelled
            try:
                await asyncio.sleep(60)
            finally:
                generation_cancelled = True

        async def scenario() -> list[dict]:
            app.image_turns.clear()
            with (
                patch.object(app, "_generate_image", new=slow_generate),
                patch.object(app, "IMAGE_BRIDGE_REQUEST_TIMEOUT_SECONDS", 0.01),
            ):
                events = [
                    app.json.loads(event.decode())
                    async for event in app._image_stream(payload, Path("."), "user-a")
                ]
            self.assertEqual(app.image_turns, {})
            return events

        self.assertEqual(app.IMAGE_BRIDGE_REQUEST_TIMEOUT_SECONDS, 20 * 60)
        events = asyncio.run(scenario())
        self.assertEqual(
            [event["type"] for event in events],
            ["started", "replace", "error"],
        )
        self.assertIn("20 分钟", events[-1]["message"])
        self.assertTrue(generation_cancelled)

    def test_unsupported_local_image_reference_does_not_reserve_quota(self) -> None:
        payload = app.TurnRequest(
            session_id="2" * 32,
            message="请生成一张图片",
            model="codex-model",
            effort="default",
            project_attachments=[app.AttachmentRef(id="project.png", name="project.png")],
        )

        async def scenario() -> list[dict]:
            with (
                patch.object(app, "_require_auth", return_value={"id": "user-a"}),
                patch.object(app, "_session_path", return_value=Path(".")),
                patch.object(app, "_validate_user_content_size"),
                patch.object(app, "_decode_external_model", return_value=None),
                patch.object(
                    app, "_clear_external_conversation_state", new=AsyncMock()
                ),
                patch.object(app, "_reserve_message_slot") as reserve,
            ):
                response = await app.turn(object(), payload)
                events = [
                    app.json.loads(event.decode())
                    async for event in response.body_iterator
                ]
            reserve.assert_not_called()
            return events

        events = asyncio.run(scenario())
        self.assertEqual([event["type"] for event in events], ["started", "error"])
        self.assertIn("参考图", events[-1]["message"])


class ExternalImageTransferTests(unittest.TestCase):
    def test_external_image_generation_prompt_keeps_selected_external_provider(self) -> None:
        payload = app.TurnRequest(
            session_id="4" * 32,
            message="请参考附件，用 Image 2.5 生成一张配图",
            model="external",
            effort="medium",
        )
        provider = {
            "baseUrl": "https://provider.example/v1",
            "protocol": "openai",
            "enabled": True,
            "models": ["test-model"],
        }
        external_calls = []

        async def fake_external_stream(*args, **kwargs):
            external_calls.append((args, kwargs))
            yield app._ndjson({"type": "done", "files": []})

        def forbidden_image_stream(*_args, **_kwargs):
            raise AssertionError("external image request was routed to the local image bridge")

        async def scenario() -> list[dict]:
            with (
                patch.object(app, "_require_auth", return_value={"id": "user-1"}),
                patch.object(app, "_session_path", return_value=Path(".")),
                patch.object(app, "_validate_user_content_size"),
                patch.object(
                    app, "_decode_external_model", return_value=("provider-id", "test-model")
                ),
                patch.object(app, "_provider_by_id", return_value=provider),
                patch.object(app, "_payload_has_images", return_value=False),
                patch.object(app, "_cloud_context_history", return_value=[]),
                patch.object(app, "_reserve_message_slot"),
                patch.object(app, "_external_stream", new=fake_external_stream),
                patch.object(app, "_image_stream", new=forbidden_image_stream),
            ):
                response = await app.turn(object(), payload)
                return [
                    app.json.loads(event.decode())
                    async for event in response.body_iterator
                ]

        events = asyncio.run(scenario())
        self.assertEqual(events, [{"type": "done", "files": []}])
        self.assertEqual(len(external_calls), 1)

    def test_reference_image_requires_signed_transfer_for_openai_protocol(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            workspace = Path(directory)
            upload_root = workspace / "uploads"
            upload_root.mkdir()
            image_path = upload_root / "reference.png"
            app.Image.new("RGB", (8, 8), "white").save(image_path)
            payload = app.TurnRequest(
                session_id="3" * 32,
                message="参考这张图",
                model="external",
                effort="medium",
                attachments=[
                    app.AttachmentRef(id=image_path.name, name="reference.png")
                ],
            )
            with patch.object(app, "TRANSFER_PUBLIC_BASE_URL", ""):
                with self.assertRaises(app.HTTPException) as raised:
                    asyncio.run(
                        app._external_current_content(
                            payload, workspace, "openai", "user-1"
                        )
                    )
            self.assertEqual(raised.exception.status_code, 503)
            self.assertIn("Base64", str(raised.exception.detail))


    def test_missing_reference_transfer_config_does_not_reserve_quota(self) -> None:
        payload = app.TurnRequest(
            session_id="5" * 32,
            message="参考图片回答",
            model="external",
            effort="medium",
        )
        provider = {
            "baseUrl": "https://provider.example/v1",
            "protocol": "openai",
            "enabled": True,
            "models": ["test-model"],
        }
        with (
            patch.object(app, "_require_auth", return_value={"id": "user-1"}),
            patch.object(app, "_session_path", return_value=Path(".")),
            patch.object(
                app, "_decode_external_model", return_value=("provider-id", "test-model")
            ),
            patch.object(app, "_provider_by_id", return_value=provider),
            patch.object(app, "_payload_has_images", return_value=True),
            patch.object(app, "TRANSFER_PUBLIC_BASE_URL", ""),
            patch.object(app, "_reserve_message_slot") as reserve_slot,
        ):
            with self.assertRaises(app.HTTPException) as raised:
                asyncio.run(app.turn(object(), payload))

        self.assertEqual(raised.exception.status_code, 503)
        reserve_slot.assert_not_called()


    def test_external_image_candidates_are_deduplicated(self) -> None:
        url = "https://provider.example/v1/assets/abcdefghijklmnopqrstuvwx"
        data = {
            "choices": [{
                "delta": {
                    "content": f"![生成图片]({url})",
                    "attachments": [{
                        "type": "image",
                        "name": "image.png",
                        "url": url,
                    }],
                }
            }]
        }

        candidates = app._openai_image_candidates(
            data, "https://provider.example/v1"
        )

        self.assertEqual(candidates, [{"url": url, "name": "image.png"}])


    def test_unrelated_markdown_image_remains_normal_text(self) -> None:
        markdown = "说明文字\n\n![外部示意图](https://cdn.example/images/demo.png)"
        data = {"choices": [{"delta": {"content": markdown}}]}

        candidates = app._openai_image_candidates(
            data, "https://provider.example/v1"
        )

        self.assertEqual(candidates, [])
        self.assertEqual(app._openai_message_text(data), markdown)


    def test_external_image_is_streamed_to_workspace(self) -> None:
        image_buffer = io.BytesIO()
        app.Image.new("RGB", (28, 20), "white").save(image_buffer, format="PNG")
        image_bytes = image_buffer.getvalue()
        url = "https://provider.example/v1/assets/abcdefghijklmnopqrstuvwx"

        class FakeResponse:
            status_code = 200
            headers = {
                "content-type": "image/png",
                "content-length": str(len(image_bytes)),
            }

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def aiter_bytes(self, _chunk_size):
                midpoint = len(image_bytes) // 2
                yield image_bytes[:midpoint]
                yield image_bytes[midpoint:]

        class FakeClient:
            def stream(self, *_args, **_kwargs):
                return FakeResponse()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            workspace = Path(directory)
            with (
                patch.object(
                    app,
                    "_validated_external_asset_url",
                    new=AsyncMock(return_value=url),
                ),
                patch.object(app, "_schedule_image_variants") as schedule_variants,
            ):
                result = asyncio.run(
                    app._persist_external_image(
                        FakeClient(),
                        {"url": url, "name": "generated.png"},
                        "https://provider.example/v1",
                        workspace,
                    )
                )

            saved = workspace / "outputs" / result["path"]
            self.assertEqual(saved.read_bytes(), image_bytes)
            self.assertEqual(result["mediaType"], "image/png")
            self.assertEqual((result["width"], result["height"]), (28, 20))
            self.assertNotIn("previewSize", result)
            self.assertEqual(list((workspace / "outputs").glob("*.part")), [])
            schedule_variants.assert_called_once()


    def test_external_image_retries_524_without_resending_generation(self) -> None:
        image_buffer = io.BytesIO()
        app.Image.new("RGB", (16, 12), "white").save(image_buffer, format="PNG")
        image_bytes = image_buffer.getvalue()
        url = "https://provider.example/v1/assets/abcdefghijklmnopqrstuvwx"

        class FakeResponse:
            def __init__(self, status_code):
                self.status_code = status_code
                self.headers = (
                    {
                        "content-type": "image/png",
                        "content-length": str(len(image_bytes)),
                    }
                    if status_code == 200
                    else {}
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def aiter_bytes(self, _chunk_size):
                yield image_bytes

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def stream(self, method, requested_url, **_kwargs):
                self.calls += 1
                self.last_method = method
                self.last_url = requested_url
                return FakeResponse(524 if self.calls == 1 else 200)

        client = FakeClient()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            workspace = Path(directory)
            with (
                patch.object(
                    app,
                    "_validated_external_asset_url",
                    new=AsyncMock(return_value=url),
                ),
                patch.object(app, "_schedule_image_variants"),
                patch.object(app.asyncio, "sleep", new=AsyncMock()) as retry_sleep,
            ):
                result = asyncio.run(
                    app._persist_external_image(
                        client,
                        {"url": url, "name": "generated.png"},
                        "https://provider.example/v1",
                        workspace,
                    )
                )

        self.assertEqual(client.calls, 2)
        self.assertEqual(client.last_method, "GET")
        self.assertEqual(client.last_url, url)
        self.assertEqual(result["mediaType"], "image/png")
        retry_sleep.assert_awaited_once()


    def test_external_image_retry_budget_fits_total_deadline(self) -> None:
        retry_delays = sum(
            0.5 * (2 ** attempt)
            for attempt in range(app.EXTERNAL_ASSET_DOWNLOAD_ATTEMPTS - 1)
        )
        worst_case = (
            app.EXTERNAL_ASSET_ATTEMPT_TIMEOUT_SECONDS
            * app.EXTERNAL_ASSET_DOWNLOAD_ATTEMPTS
            + retry_delays
        )
        self.assertLess(worst_case, app.EXTERNAL_ASSET_DOWNLOAD_TIMEOUT_SECONDS)


    def test_external_image_batch_has_one_total_deadline(self) -> None:
        cancelled = 0
        candidates = [
            {
                "url": f"https://provider.example/v1/assets/{index:024d}",
                "name": f"image-{index}.png",
            }
            for index in range(3)
        ]

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        async def slow_persist(*_args, **_kwargs):
            nonlocal cancelled
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise

        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory,
            patch.object(app, "EXTERNAL_ASSET_DOWNLOAD_TIMEOUT_SECONDS", 0.01),
            patch.object(app.httpx, "AsyncClient", return_value=FakeClient()),
            patch.object(app, "_persist_external_image", side_effect=slow_persist),
        ):
            saved, failed = asyncio.run(
                app._persist_external_images(
                    candidates,
                    "https://provider.example/v1",
                    Path(directory),
                )
            )

        self.assertEqual(saved, [])
        self.assertEqual(failed, len(candidates))
        self.assertEqual(cancelled, 2)


    def test_external_image_stream_returns_local_file_and_image_mode(self) -> None:
        url = "https://provider.example/v1/assets/abcdefghijklmnopqrstuvwx"
        payload = app.TurnRequest(
            session_id="4" * 32,
            message="生成一张图片",
            model="external",
            effort="medium",
        )
        provider = {
            "baseUrl": "https://provider.example/v1",
            "protocol": "openai",
            "apiKey": "test-key",
            "preset": "custom",
        }
        image_file = {
            "name": "generated.png",
            "size": 80,
            "path": "generated.png",
            "mediaType": "image/png",
            "inline": True,
        }
        line = app.json.dumps(
            {
                "choices": [{
                    "delta": {
                        "content": f"![生成图片]({url})",
                        "attachments": [{
                            "type": "image",
                            "name": "generated.png",
                            "url": url,
                        }],
                    },
                    "finish_reason": "stop",
                }]
            },
            ensure_ascii=False,
        )

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def aiter_lines(self):
                yield f"data: {line}"
                yield "data: [DONE]"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return FakeResponse()

        async def scenario() -> list[dict]:
            with (
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory,
                patch.object(
                    app,
                    "_validate_remote_base",
                    new=AsyncMock(return_value=provider["baseUrl"]),
                ),
                patch.object(
                    app,
                    "_external_current_content",
                    new=AsyncMock(return_value="生成一张图片"),
                ),
                patch.object(app.httpx, "AsyncClient", return_value=FakeClient()),
                patch.object(
                    app,
                    "_persist_external_images",
                    new=AsyncMock(return_value=([image_file], 0)),
                ) as persist_images,
            ):
                events = [
                    app.json.loads(event.decode())
                    async for event in app._external_response_stream(
                        payload, Path(directory), provider, "test-model", "user-1"
                    )
                ]
                persist_images.assert_awaited_once()
                return events

        events = asyncio.run(scenario())
        self.assertEqual([event["type"] for event in events], ["replace", "done"])
        self.assertEqual(events[0]["text"], "图片已生成。")
        self.assertEqual(events[1]["mode"], "image")
        self.assertEqual(events[1]["files"], [image_file])
        self.assertNotIn("/v1/assets/", app.json.dumps(events, ensure_ascii=False))


class UploadCapacityTests(unittest.TestCase):
    def setUp(self) -> None:
        app.chunk_upload_locks.clear()
        app.chunk_upload_lock_times.clear()

    def test_capacity_constants_use_30_mib_transport_and_bounded_context(self) -> None:
        self.assertEqual(app.MAX_FILE_BYTES, 30 * 1024 * 1024)
        self.assertEqual(app.MAX_UPLOAD_BATCH_BYTES, 30 * 1024 * 1024)
        self.assertEqual(app.MAX_USER_CONTENT_BYTES, 30 * 1024 * 1024)
        self.assertEqual(app.MAX_INLINE_MESSAGE_BYTES, 60_000)
        self.assertEqual(app.UPLOAD_CHUNK_BYTES, 3 * 1024 * 1024)
        self.assertEqual(app.MAX_OUTPUT_FILE_BYTES, 30 * 1024 * 1024)
        self.assertEqual(app.MAX_EXTERNAL_IMAGE_BYTES, 30 * 1024 * 1024)
        self.assertEqual(app.MAX_PROJECT_CONTEXT_SOURCE_BYTES, 30 * 1024 * 1024)
        self.assertEqual(app.MAX_CONTEXT_TEXT_CHARS, 150_000)
        self.assertEqual(app.MAX_RECENT_HISTORY_TURNS, 30)
        self.assertEqual(app.MAX_OLDER_CONTEXT_CHARS, 20_000)
        self.assertEqual(app.MAX_CLOUD_MESSAGES, 1_000)
        self.assertEqual(app.MAX_CLOUD_CONTENT_CHARS, 8_000_000)
        self.assertLess(app.MAX_CURRENT_ATTACHMENT_CONTEXT_CHARS, app.MAX_CONTEXT_TEXT_CHARS)

    def test_chunk_upload_is_sequential_idempotent_and_complete_is_atomic(self) -> None:
        user_id = "8" * 32
        session_id = "9" * 32
        request = object()

        async def scenario():
            with (
                patch.object(app, "_require_auth", return_value={"id": user_id}),
                patch.object(app, "UPLOAD_CHUNK_BYTES", 3),
            ):
                initialized = await app.init_chunk_upload(
                    request,
                    app.ChunkUploadInitRequest(
                        session_id=session_id,
                        name="notes.txt",
                        size=5,
                        source="composer_text",
                    ),
                )
                upload_id = initialized["uploadId"]
                first = app.UploadFile(filename="chunk.bin", file=io.BytesIO(b"abc"))
                first_result = await app.upload_chunk(
                    request,
                    upload_id,
                    session_id=session_id,
                    offset=0,
                    chunk=first,
                )
                replay = app.UploadFile(filename="chunk.bin", file=io.BytesIO(b"abc"))
                replay_result = await app.upload_chunk(
                    request,
                    upload_id,
                    session_id=session_id,
                    offset=0,
                    chunk=replay,
                )
                last = app.UploadFile(filename="chunk.bin", file=io.BytesIO(b"de"))
                last_result = await app.upload_chunk(
                    request,
                    upload_id,
                    session_id=session_id,
                    offset=3,
                    chunk=last,
                )
                completed = await app.complete_chunk_upload(
                    request,
                    upload_id,
                    app.ChunkUploadCompleteRequest(session_id=session_id),
                )
                completed_again = await app.complete_chunk_upload(
                    request,
                    upload_id,
                    app.ChunkUploadCompleteRequest(session_id=session_id),
                )
                after_complete = app.UploadFile(filename="chunk.bin", file=io.BytesIO(b"abc"))
                replay_after_complete = await app.upload_chunk(
                    request,
                    upload_id,
                    session_id=session_id,
                    offset=0,
                    chunk=after_complete,
                )
                return (
                    upload_id,
                    first_result,
                    replay_result,
                    last_result,
                    completed,
                    completed_again,
                    replay_after_complete,
                )

        (
            upload_id,
            first_result,
            replay_result,
            last_result,
            completed,
            completed_again,
            replay_after_complete,
        ) = asyncio.run(scenario())
        self.assertEqual(first_result["nextOffset"], 3)
        self.assertEqual(replay_result["nextOffset"], 3)
        self.assertTrue(last_result["complete"])
        self.assertEqual(completed, completed_again)
        self.assertTrue(replay_after_complete["complete"])
        uploaded = completed["files"][0]
        upload_root = app._session_path(user_id, session_id) / "uploads"
        self.assertEqual((upload_root / uploaded["id"]).read_bytes(), b"abcde")
        self.assertEqual(uploaded["source"], "composer_text")
        self.assertEqual(uploaded["preview"], "abcde")
        self.assertFalse((upload_root / app.UPLOAD_STAGING_DIR / f"{upload_id}.part").exists())

    def test_invalid_upload_ids_do_not_consume_chunk_locks(self) -> None:
        request = object()
        session_id = "7" * 32

        async def scenario() -> None:
            with patch.object(app, "_require_auth", return_value={"id": "6" * 32}):
                for index in range(5):
                    upload_id = f"{index:032x}"
                    with self.assertRaises(app.HTTPException) as caught:
                        await app.complete_chunk_upload(
                            request,
                            upload_id,
                            app.ChunkUploadCompleteRequest(session_id=session_id),
                        )
                    self.assertEqual(caught.exception.status_code, 404)

        asyncio.run(scenario())
        self.assertEqual(app.chunk_upload_locks, {})

    def test_partial_chunk_is_rolled_back_and_can_be_retried(self) -> None:
        user_id = "5" * 32
        session_id = "6" * 32
        request = object()

        async def scenario() -> bytes:
            with (
                patch.object(app, "_require_auth", return_value={"id": user_id}),
                patch.object(app, "UPLOAD_CHUNK_BYTES", 3),
            ):
                initialized = await app.init_chunk_upload(
                    request,
                    app.ChunkUploadInitRequest(session_id=session_id, name="recover.txt", size=6),
                )
                upload_id = initialized["uploadId"]
                await app.upload_chunk(
                    request,
                    upload_id,
                    session_id=session_id,
                    offset=0,
                    chunk=app.UploadFile(filename="chunk.bin", file=io.BytesIO(b"abc")),
                )
                upload_root = app._session_path(user_id, session_id) / "uploads"
                part_path, _metadata_path = app._chunk_upload_paths(upload_root, upload_id)
                with part_path.open("ab") as handle:
                    handle.write(b"x")
                await app.upload_chunk(
                    request,
                    upload_id,
                    session_id=session_id,
                    offset=3,
                    chunk=app.UploadFile(filename="chunk.bin", file=io.BytesIO(b"def")),
                )
                completed = await app.complete_chunk_upload(
                    request,
                    upload_id,
                    app.ChunkUploadCompleteRequest(session_id=session_id),
                )
                return (upload_root / completed["files"][0]["id"]).read_bytes()

        self.assertEqual(asyncio.run(scenario()), b"abcdef")

    def test_out_of_order_or_incomplete_chunks_never_create_final_file(self) -> None:
        user_id = "a" * 32
        session_id = "b" * 32
        request = object()

        async def scenario() -> str:
            with (
                patch.object(app, "_require_auth", return_value={"id": user_id}),
                patch.object(app, "UPLOAD_CHUNK_BYTES", 3),
            ):
                initialized = await app.init_chunk_upload(
                    request,
                    app.ChunkUploadInitRequest(
                        session_id=session_id,
                        name="ordered.txt",
                        size=6,
                    ),
                )
                upload_id = initialized["uploadId"]
                with self.assertRaises(app.HTTPException) as order_error:
                    await app.upload_chunk(
                        request,
                        upload_id,
                        session_id=session_id,
                        offset=3,
                        chunk=app.UploadFile(filename="chunk.bin", file=io.BytesIO(b"def")),
                    )
                self.assertEqual(order_error.exception.status_code, 409)
                with self.assertRaises(app.HTTPException) as incomplete_error:
                    await app.complete_chunk_upload(
                        request,
                        upload_id,
                        app.ChunkUploadCompleteRequest(session_id=session_id),
                    )
                self.assertEqual(incomplete_error.exception.status_code, 409)
                return upload_id

        upload_id = asyncio.run(scenario())
        upload_root = app._session_path(user_id, session_id) / "uploads"
        visible = [path for path in upload_root.iterdir() if path.name != app.UPLOAD_STAGING_DIR]
        self.assertEqual(visible, [])
        self.assertEqual(
            (upload_root / app.UPLOAD_STAGING_DIR / f"{upload_id}.part").stat().st_size,
            0,
        )

    def test_stale_chunk_receipt_and_lock_are_reclaimed(self) -> None:
        user_id = "3" * 32
        session_id = "4" * 32
        request = object()
        with patch.object(app, "_require_auth", return_value={"id": user_id}):
            initialized = asyncio.run(app.init_chunk_upload(
                request,
                app.ChunkUploadInitRequest(
                    session_id=session_id,
                    name="stale.txt",
                    size=0,
                ),
            ))
        upload_id = initialized["uploadId"]
        lock = app._chunk_upload_lock(upload_id)
        upload_root = app._session_path(user_id, session_id) / "uploads"
        part_path, metadata_path = app._chunk_upload_paths(upload_root, upload_id)
        old = time.time() - app.UPLOAD_STAGING_TTL_SECONDS - 5
        os.utime(part_path, (old, old))
        os.utime(metadata_path, (old, old))

        app._cleanup_stale_uploads(upload_root)

        self.assertFalse(part_path.exists())
        self.assertFalse(metadata_path.exists())
        self.assertIs(app.chunk_upload_locks.get(upload_id), None)
        self.assertFalse(lock.locked())

    def test_stale_cleanup_skips_a_locked_upload(self) -> None:
        user_id = "1" * 32
        session_id = "3" * 32
        request = object()

        async def scenario() -> None:
            with patch.object(app, "_require_auth", return_value={"id": user_id}):
                initialized = await app.init_chunk_upload(
                    request,
                    app.ChunkUploadInitRequest(session_id=session_id, name="active.txt", size=0),
                )
            upload_id = initialized["uploadId"]
            upload_root = app._session_path(user_id, session_id) / "uploads"
            part_path, metadata_path = app._chunk_upload_paths(upload_root, upload_id)
            old = time.time() - app.UPLOAD_STAGING_TTL_SECONDS - 5
            os.utime(part_path, (old, old))
            os.utime(metadata_path, (old, old))
            lock = app._chunk_upload_lock(upload_id)
            await lock.acquire()
            try:
                app._cleanup_stale_uploads(upload_root)
                self.assertTrue(part_path.exists())
                self.assertTrue(metadata_path.exists())
            finally:
                lock.release()
            app._cleanup_stale_uploads(upload_root)
            self.assertFalse(part_path.exists())
            self.assertFalse(metadata_path.exists())

        asyncio.run(scenario())

    def test_legacy_upload_rejects_batch_over_limit_without_partial_files(self) -> None:
        user_id = "c" * 32
        session_id = "d" * 32
        request = object()
        uploads = [
            app.UploadFile(filename="one.txt", file=io.BytesIO(b"1234")),
            app.UploadFile(filename="two.txt", file=io.BytesIO(b"567")),
        ]

        async def scenario() -> None:
            with (
                patch.object(app, "_require_auth", return_value={"id": user_id}),
                patch.object(app, "MAX_FILE_BYTES", 5),
                patch.object(app, "MAX_UPLOAD_BATCH_BYTES", 6),
            ):
                with self.assertRaises(app.HTTPException) as caught:
                    await app.upload_files(
                        request,
                        session_id=session_id,
                        files=uploads,
                    )
                self.assertEqual(caught.exception.status_code, 413)

        asyncio.run(scenario())
        upload_root = app._session_path(user_id, session_id) / "uploads"
        visible = [path for path in upload_root.iterdir() if path.name != app.UPLOAD_STAGING_DIR]
        self.assertEqual(visible, [])
        self.assertEqual(list((upload_root / app.UPLOAD_STAGING_DIR).iterdir()), [])

    def test_utf8_text_and_current_attachment_share_one_30_mib_budget(self) -> None:
        user_id = "e" * 32
        session_id = "f" * 32
        workspace = app._session_path(user_id, session_id)
        attachment = workspace / "uploads" / "four.txt"
        attachment.write_bytes(b"1234")
        base = app.TurnRequest(
            session_id=session_id,
            message="你你",
            model="external",
            effort="default",
            attachments=[app.AttachmentRef(id=attachment.name, name=attachment.name)],
        )
        with patch.object(app, "MAX_USER_CONTENT_BYTES", 10):
            self.assertEqual(app._validate_user_content_size(base, workspace), 10)
            with self.assertRaises(app.HTTPException) as caught:
                app._validate_user_content_size(
                    base.model_copy(update={"message": "你你a"}),
                    workspace,
                )
        self.assertEqual(caught.exception.status_code, 413)

        composer_payload = base.model_copy(update={
            "message": app._composer_text_preview(attachment, attachment.name),
            "attachments": [
                app.AttachmentRef(
                    id=attachment.name,
                    name=attachment.name,
                    source="composer_text",
                )
            ],
        })
        with patch.object(app, "MAX_USER_CONTENT_BYTES", 4):
            self.assertEqual(
                app._validate_user_content_size(composer_payload, workspace),
                4,
            )
            with self.assertRaises(app.HTTPException) as caught:
                app._validate_user_content_size(
                    composer_payload.model_copy(update={"message": "被编辑过的预览"}),
                    workspace,
                )
        self.assertEqual(caught.exception.status_code, 400)

    def test_composer_preview_reads_stable_unicode_head_and_tail(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            target = Path(directory) / "unicode.txt"
            text = ("开😀中" * 3_000) + "中间不应完整传入" + ("尾🚀文" * 3_000)
            target.write_text(text, encoding="utf-8")

            preview = app._composer_text_preview(target, target.name)

        self.assertTrue(preview.startswith(text[:1_600]))
        self.assertTrue(preview.endswith(text[-1_600:]))
        self.assertIn("完整原文已作为附件", preview)
        self.assertNotIn("中间不应完整传入", preview)

    def test_large_document_is_excerpted_instead_of_injected_in_full(self) -> None:
        user_id = "1" * 32
        session_id = "2" * 32
        workspace = app._session_path(user_id, session_id)
        raw = (
            "文件开头\n"
            + "x" * (app.MAX_EXTRACTED_CHARS * 2)
            + "\n问题关键词：蓝色火箭\n"
            + "y" * (app.MAX_EXTRACTED_CHARS * 2)
            + "\n文件结尾"
        )
        attachment = workspace / "uploads" / "large.txt"
        attachment.write_text(raw, encoding="utf-8")
        payload = app.TurnRequest(
            session_id=session_id,
            message="请查找蓝色火箭并概括附件",
            model="external",
            effort="default",
            attachments=[app.AttachmentRef(id=attachment.name, name=attachment.name)],
        )

        inputs = app._attachment_inputs(
            payload,
            workspace,
            include_history=False,
            max_text_chars=app.MAX_CONTEXT_TEXT_CHARS,
            external_limits=True,
        )
        transmitted = "".join(item.get("text", "") for item in inputs if item["type"] == "text")

        self.assertLess(len(transmitted), len(raw))
        self.assertIn("文件开头", transmitted)
        self.assertIn("蓝色火箭", transmitted)
        self.assertIn("文件结尾", transmitted)
        self.assertLessEqual(len(transmitted), app.MAX_CONTEXT_TEXT_CHARS)
        self.assertLessEqual(
            len(transmitted),
            len(payload.message) + app.MAX_CURRENT_ATTACHMENT_CONTEXT_CHARS + 64,
        )

    def test_large_plain_text_and_pdf_are_excerpted_as_streams(self) -> None:
        marker = "紫色彗星对应编号 STREAM-24680"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            target = Path(directory) / "large.txt"
            target.write_text(
                "文件开头" + "甲" * 150_000 + marker + "乙" * 150_000 + "文件结尾",
                encoding="utf-8",
            )
            with patch.object(Path, "read_text", side_effect=AssertionError("unbounded read")):
                excerpt = app._extract_attachment_text(target, "紫色彗星是什么")

        self.assertLessEqual(len(excerpt), app.MAX_EXTRACTED_CHARS)
        self.assertIn("文件开头", excerpt)
        self.assertIn(marker, excerpt)
        self.assertIn("文件结尾", excerpt)

        class FakePage:
            def __init__(self, text: str):
                self.text = text

            def extract_text(self) -> str:
                return self.text

        fake_reader = type("FakeReader", (), {"pages": [
            FakePage("PDF 开头" + "丙" * 130_000),
            FakePage(marker + "丁" * 130_000),
            FakePage("PDF 结尾"),
        ]})()
        with patch.object(app, "PdfReader", return_value=fake_reader):
            pdf_excerpt = app._extract_attachment_text(
                Path("document.pdf"), "紫色彗星是什么"
            )
        self.assertLessEqual(len(pdf_excerpt), app.MAX_EXTRACTED_CHARS)
        self.assertIn("PDF 开头", pdf_excerpt)
        self.assertIn(marker, pdf_excerpt)
        self.assertIn("PDF 结尾", pdf_excerpt)

    def test_large_document_retrieval_handles_short_chinese_english_and_long_queries(self) -> None:
        def large_document(marker: str) -> str:
            def chunk(prefix: str, fill: str) -> str:
                return (prefix + fill * 1_200)[:1_200]

            return "".join((
                chunk("文件开头。", "甲"),
                chunk("无关章节一。", "乙"),
                chunk(marker, "丙"),
                chunk("无关章节二。", "丁"),
                chunk("文件结尾。", "戊"),
            ))

        cases = (
            ("紫色彗星是什么", "紫色彗星对应编号 A-13579"),
            ("What is the blue-rocket specification?", "blue-rocket specification code BX-42"),
            (
                (
                    "请介绍一段非常冗长但与目标无关的背景说明以及已经完成的处理过程，"
                    "然后告诉我绿色灯塔对应什么编号"
                ),
                "绿色灯塔对应编号 B-24680",
            ),
        )
        for query, marker in cases:
            with self.subTest(query=query):
                terms = app._context_query_terms(query)
                excerpt = app._relevant_attachment_excerpt(large_document(marker), query, 3_610)

                self.assertLessEqual(len(terms), 32)
                self.assertIn(marker, excerpt)

    def test_query_terms_bound_work_for_very_long_mixed_language_input(self) -> None:
        query = ("alpha " * 100_000) + ("背景" * 100_000) + " 绿色灯塔"

        with (
            patch.object(app, "_normalize_context_text", wraps=app._normalize_context_text) as normalize,
            patch.object(app.re, "findall", side_effect=AssertionError("unbounded findall")),
        ):
            terms = app._context_query_terms(query)

        sampled_query = normalize.call_args.args[0]
        self.assertLessEqual(len(sampled_query), app.MAX_CONTEXT_QUERY_SCAN_CHARS)
        self.assertLessEqual(len(terms), 32)
        self.assertTrue(any("灯塔" in term for term in terms))

    def test_attachment_inputs_compute_query_terms_once_for_multiple_documents(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            workspace = Path(directory)
            upload_root = workspace / "uploads"
            upload_root.mkdir()
            attachments = []
            for index in range(2):
                name = f"large-{index}.txt"
                (upload_root / name).write_text("无关内容" * 30_000, encoding="utf-8")
                attachments.append(app.AttachmentRef(id=name, name=name))
            payload = app.TurnRequest(
                session_id="f" * 32,
                message="绿色灯塔",
                model="external",
                effort="default",
                attachments=attachments,
            )

            with patch.object(
                app, "_context_query_terms", wraps=app._context_query_terms
            ) as query_terms:
                app._attachment_inputs(payload, workspace, include_history=False)

            self.assertEqual(query_terms.call_count, 1)


class ExternalContextTests(unittest.TestCase):
    def test_short_history_remains_verbatim(self) -> None:
        history = [
            app.HistoryMessage(role="user", content="第一问"),
            app.HistoryMessage(role="assistant", content="第一答"),
            app.HistoryMessage(role="user", content="第二问"),
            app.HistoryMessage(role="assistant", content="第二答"),
        ]

        recent, older = app._fit_context_window(history, 20_000)

        self.assertEqual([(item.role, item.content) for item in recent], [
            (item.role, item.content) for item in history
        ])
        self.assertEqual(older, "")

    def test_long_history_keeps_30_recent_turns_and_bounded_older_excerpt(self) -> None:
        history: list[app.HistoryMessage] = []
        for index in range(40):
            user_text = "Cafe\u0301 的偏好" if index == 0 else f"用户问题 {index}"
            history.extend([
                app.HistoryMessage(role="user", content=user_text),
                app.HistoryMessage(role="assistant", content=f"助手回答 {index}"),
            ])

        recent, older = app._fit_context_window(history, 80_000)

        self.assertEqual(len(recent), 60)
        self.assertEqual(recent[0].content, "用户问题 10")
        self.assertIn("Café 的偏好", older)
        self.assertLessEqual(len(older), app.MAX_OLDER_CONTEXT_CHARS)

    def test_history_merge_preserves_order_repeated_text_and_new_cloud_tail(self) -> None:
        cloud = [
            app.HistoryMessage(id="1" * 32, role="user", content="继续"),
            app.HistoryMessage(id="2" * 32, role="assistant", content="好的"),
            app.HistoryMessage(id="3" * 32, role="user", content="继续"),
            app.HistoryMessage(id="4" * 32, role="assistant", content="好的"),
        ]

        merged_tail = app._merged_context_history(cloud, cloud[-2:])
        merged_stale = app._merged_context_history(cloud, cloud[:2])
        merged_updated = app._merged_context_history(cloud, [
            app.HistoryMessage(id="2" * 32, role="assistant", content="好的，完整回答"),
        ])
        local_branch = [
            app.HistoryMessage(id="5" * 32, role="user", content="继续"),
            app.HistoryMessage(id="6" * 32, role="assistant", content="好的"),
        ]
        merged_branch = app._merged_context_history(cloud, local_branch)

        self.assertEqual([item.id for item in merged_tail], [item.id for item in cloud])
        self.assertEqual([item.id for item in merged_stale], [item.id for item in cloud])
        self.assertEqual([item.id for item in merged_updated], [item.id for item in cloud])
        self.assertEqual(merged_updated[1].content, "好的，完整回答")
        self.assertEqual(merged_updated[-1].id, "4" * 32)
        self.assertEqual([item.id for item in merged_branch], [
            *(item.id for item in cloud),
            *(item.id for item in local_branch),
        ])
        self.assertEqual([item.content for item in merged_branch].count("继续"), 3)

    def test_cloud_history_is_complete_and_scoped_to_the_current_user(self) -> None:
        app._prepare_directories()
        with closing(sqlite3.connect(app.CONVERSATIONS_PATH)) as database:
            owner_id = str(database.execute(
                "SELECT id FROM users WHERE username = 'owner'"
            ).fetchone()[0])
        conversation_id = "e" * 32
        messages = []
        for index in range(30):
            messages.extend([
                {"id": f"{index * 2:032x}", "role": "user", "content": f"问题 {index}"},
                {"id": f"{index * 2 + 1:032x}", "role": "assistant", "content": f"回答 {index}"},
            ])
        app._sync_cloud_conversations(owner_id, [{
            "id": conversation_id,
            "workspaceId": "d" * 32,
            "title": "完整历史",
            "messages": messages,
            "updatedAt": int(time.time() * 1000),
            "externalConversationId": "upstream_context_1",
            "externalContextKey": "a" * 64,
        }], [])

        history = app._cloud_context_history(owner_id, conversation_id)

        self.assertEqual(len(history), 60)
        self.assertEqual(history[0].content, "问题 0")
        self.assertEqual(history[-1].content, "回答 29")
        self.assertEqual(app._cloud_context_history("f" * 32, conversation_id), [])
        normalized = app._normalize_cloud_conversation({
            "id": "c" * 32,
            "workspaceId": "b" * 32,
            "messages": [],
            "externalConversationId": "upstream_context_2",
            "externalContextKey": "b" * 64,
        })
        self.assertEqual(normalized["externalConversationId"], "upstream_context_2")
        self.assertEqual(normalized["externalContextKey"], "b" * 64)

    def test_context_key_changes_with_model_provider_and_project_context(self) -> None:
        payload = app.TurnRequest(
            session_id="1" * 32,
            message="问题",
            model="external",
            effort="default",
            project_instructions="项目规则",
            project_attachments=[app.AttachmentRef(id="file-a.txt", name="a.txt")],
        )
        provider = {"baseUrl": "https://one.example/v1", "protocol": "openai"}
        key = app._external_context_key(payload, "provider-1", provider, "model-a")

        self.assertNotEqual(key, app._external_context_key(payload, "provider-1", provider, "model-b"))
        self.assertNotEqual(
            key,
            app._external_context_key(
                payload,
                "provider-1",
                {"baseUrl": "https://two.example/v1", "protocol": "openai"},
                "model-a",
            ),
        )
        changed = payload.model_copy(update={"project_instructions": "另一套规则"})
        self.assertNotEqual(key, app._external_context_key(changed, "provider-1", provider, "model-a"))

    def test_current_images_are_kept_before_project_images(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            workspace = Path(directory)
            upload_root = workspace / "uploads"
            upload_root.mkdir()
            current = []
            project = []
            for index in range(5):
                name = f"current-{index}.png"
                (upload_root / name).write_bytes(b"c")
                current.append(app.AttachmentRef(id=name, name=name))
            for index in range(5):
                name = f"project-{index}.png"
                (upload_root / name).write_bytes(b"p")
                project.append(app.AttachmentRef(id=name, name=name))
            payload = app.TurnRequest(
                session_id="2" * 32,
                message="参考这些图片",
                model="external",
                effort="default",
                attachments=current,
                project_attachments=project,
            )

            inputs = app._attachment_inputs(
                payload,
                workspace,
                include_history=False,
                max_text_chars=app.MAX_CONTEXT_TEXT_CHARS,
                external_limits=True,
            )

            selected = [Path(item["path"]).name for item in inputs if item["type"] == "localImage"]
            self.assertEqual(selected, [
                *(f"current-{index}.png" for index in range(5)),
                *(f"project-{index}.png" for index in range(3)),
            ])
            self.assertIn("另有 2 张项目共享图片", inputs[0]["text"])

            continuation_inputs = app._attachment_inputs(
                payload,
                workspace,
                include_history=False,
                include_project_images=False,
            )
            continuation_images = [
                Path(item["path"]).name
                for item in continuation_inputs
                if item["type"] == "localImage"
            ]
            self.assertEqual(
                continuation_images,
                [f"current-{index}.png" for index in range(5)],
            )

    def test_project_images_are_omitted_before_current_images_hit_byte_limit(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            workspace = Path(directory)
            upload_root = workspace / "uploads"
            upload_root.mkdir()
            (upload_root / "current.png").write_bytes(b"1234")
            (upload_root / "project.png").write_bytes(b"56")
            payload = app.TurnRequest(
                session_id="7" * 32,
                message="参考图片",
                model="external",
                effort="default",
                attachments=[app.AttachmentRef(id="current.png", name="current.png")],
                project_attachments=[app.AttachmentRef(id="project.png", name="project.png")],
            )

            with patch.object(app, "MAX_EXTERNAL_IMAGE_BYTES", 5):
                inputs = app._attachment_inputs(payload, workspace)

            selected = [Path(item["path"]).name for item in inputs if item["type"] == "localImage"]
            self.assertEqual(selected, ["current.png"])
            self.assertIn("另有 1 张项目共享图片", inputs[0]["text"])

            (upload_root / "current.png").write_bytes(b"123456")
            with patch.object(app, "MAX_EXTERNAL_IMAGE_BYTES", 5):
                with self.assertRaises(app.HTTPException) as caught:
                    app._attachment_inputs(payload, workspace)
            self.assertEqual(caught.exception.status_code, 413)

    def test_project_source_budget_skips_files_before_extracting_them(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            workspace = Path(directory)
            upload_root = workspace / "uploads"
            upload_root.mkdir()
            (upload_root / "first.txt").write_text("1234", encoding="utf-8")
            (upload_root / "second.txt").write_text("56", encoding="utf-8")
            payload = app.TurnRequest(
                session_id="8" * 32,
                message="概括项目文件",
                model="external",
                effort="default",
                project_attachments=[
                    app.AttachmentRef(id="first.txt", name="first.txt"),
                    app.AttachmentRef(id="second.txt", name="second.txt"),
                ],
            )

            with patch.object(app, "MAX_PROJECT_CONTEXT_SOURCE_BYTES", 5):
                inputs = app._attachment_inputs(payload, workspace, include_history=False)

            transmitted = "".join(item.get("text", "") for item in inputs)
            self.assertIn("1234", transmitted)
            self.assertNotIn("56\n【文件结束】", transmitted)
            self.assertIn("另有 1 个项目共享文件", transmitted)

    def test_anthropic_images_are_optimized_before_base64_encoding(self) -> None:
        async def scenario() -> list[dict]:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
                workspace = Path(directory)
                upload_root = workspace / "uploads"
                upload_root.mkdir()
                original = upload_root / "original.png"
                optimized = upload_root / "optimized.webp"
                original.write_bytes(b"large-original")
                optimized.write_bytes(b"small")
                payload = app.TurnRequest(
                    session_id="9" * 32,
                    message="参考图片",
                    model="external",
                    effort="default",
                    attachments=[app.AttachmentRef(id=original.name, name=original.name)],
                )
                with patch.object(app, "_prepare_transfer_image", return_value=optimized) as prepare:
                    content = await app._external_current_content(
                        payload,
                        workspace,
                        "anthropic",
                        "user-a",
                    )
                prepare.assert_called_once_with(original, upload_root)
                return content

        content = asyncio.run(scenario())
        self.assertEqual(content[1]["source"]["media_type"], "image/webp")
        self.assertEqual(content[1]["source"]["data"], app.base64.b64encode(b"small").decode())

    def test_first_request_is_bounded_and_continuation_refreshes_project_excerpt(self) -> None:
        bodies: list[dict] = []
        first_line = app.json.dumps({
            "conversation_id": "upstream_context_3",
            "choices": [{"delta": {"content": "完成"}, "finish_reason": None}],
        }, ensure_ascii=False)
        final_line = app.json.dumps({
            "conversation_id": "upstream_context_3",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
        }, ensure_ascii=False)

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def aiter_lines(self):
                yield f"data: {first_line}"
                yield f"data: {final_line}"
                yield "data: [DONE]"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **kwargs):
                bodies.append(kwargs["json"])
                return FakeResponse()

        history: list[app.HistoryMessage] = []
        for index in range(40):
            history.extend([
                app.HistoryMessage(role="user", content=f"旧问题 {index}"),
                app.HistoryMessage(role="assistant", content=f"旧回答 {index}"),
            ])
        provider = {
            "baseUrl": "https://provider.example/v1",
            "protocol": "openai",
            "apiKey": "test-key",
            "preset": "custom",
        }

        def project_chunk(text: str) -> str:
            return text + ("填" * (1_200 - len(text)))

        project_document = "".join([
            project_chunk("项目资料开头。"),
            project_chunk("普通资料第一段。"),
            project_chunk("普通资料第二段。"),
            project_chunk("普通资料第三段。"),
            project_chunk("普通资料第四段。"),
            project_chunk("甲区答案 A-13579，仅用于首轮问题。"),
            project_chunk("乙区答案 B-24680，仅用于第二轮问题。"),
            project_chunk("普通资料第八段。"),
            project_chunk("项目资料结尾。"),
        ])
        first_payload = app.TurnRequest(
            session_id="3" * 32,
            client_conversation_id="4" * 32,
            message="甲区",
            model="external",
            effort="default",
            project_instructions="项目规则",
            project_attachments=[
                app.AttachmentRef(id="project-notes.txt", name="project-notes.txt")
            ],
        )
        continuation_payload = first_payload.model_copy(update={"message": "乙区"})

        async def scenario() -> tuple[list[dict], list[dict], dict[str, str]]:
            completion: dict[str, str] = {}
            with (
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory,
                patch.object(
                    app,
                    "_validate_remote_base",
                    new=AsyncMock(return_value=provider["baseUrl"]),
                ),
                patch.object(app.httpx, "AsyncClient", return_value=FakeClient()),
            ):
                upload_root = Path(directory) / "uploads"
                upload_root.mkdir()
                (upload_root / "project-notes.txt").write_text(project_document, encoding="utf-8")
                first_events = [
                    app.json.loads(event.decode())
                    async for event in app._external_response_stream(
                        first_payload,
                        Path(directory),
                        provider,
                        "model-a",
                        "user-a",
                        context_history=history,
                        external_context_key="a" * 64,
                        completion_state=completion,
                    )
                ]
                continuation_events = [
                    app.json.loads(event.decode())
                    async for event in app._external_response_stream(
                        continuation_payload,
                        Path(directory),
                        provider,
                        "model-a",
                        "user-a",
                        context_history=history,
                        reuse_conversation_id="upstream_context_3",
                        external_context_key="a" * 64,
                    )
                ]
                return first_events, continuation_events, completion

        first_events, continuation_events, completion = asyncio.run(scenario())
        first_body, continuation_body = bodies

        def content_chars(content) -> int:
            if isinstance(content, str):
                return len(content)
            return sum(
                len(str(item.get("text") or ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )

        self.assertLessEqual(
            sum(content_chars(item.get("content")) for item in first_body["messages"]),
            app.MAX_CONTEXT_TEXT_CHARS,
        )
        self.assertEqual(first_body["messages"][0]["role"], "system")
        self.assertIn("较早对话摘录", first_body["messages"][-1]["content"])
        self.assertIn("甲区答案 A-13579", first_body["messages"][-1]["content"])
        self.assertNotIn("乙区答案 B-24680", first_body["messages"][-1]["content"])
        self.assertEqual(first_events[-1]["externalConversationId"], "upstream_context_3")
        self.assertEqual(completion["external_context_key"], "a" * 64)
        self.assertEqual(continuation_body["conversation_id"], "upstream_context_3")
        self.assertEqual(len(continuation_body["messages"]), 1)
        continuation_content = continuation_body["messages"][0]["content"]
        self.assertIn("乙区答案 B-24680", continuation_content)
        self.assertNotIn("甲区答案 A-13579", continuation_content)
        self.assertNotIn("项目规则", app.json.dumps(continuation_body, ensure_ascii=False))
        self.assertEqual(continuation_events[-1]["type"], "done")

    def test_same_site_conversation_is_serialized_and_reuses_successful_state(self) -> None:
        calls: list[str] = []
        active = 0
        maximum_active = 0
        payload = app.TurnRequest(
            session_id="5" * 32,
            client_conversation_id="6" * 32,
            message="并发问题",
            model="external",
            effort="default",
        )
        provider = {"baseUrl": "https://provider.example/v1", "protocol": "openai"}

        async def fake_response(*_args, **kwargs):
            nonlocal active, maximum_active
            calls.append(kwargs.get("reuse_conversation_id", ""))
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                await asyncio.sleep(0.01)
                state = kwargs["completion_state"]
                state["external_conversation_id"] = "upstream_serial_1"
                state["external_context_key"] = kwargs["external_context_key"]
                yield app._ndjson({"type": "done", "files": []})
            finally:
                active -= 1

        async def scenario() -> None:
            app.external_conversation_locks.clear()
            app.external_conversation_states.clear()

            async def consume() -> None:
                async for _event in app._external_stream(
                    payload,
                    Path("."),
                    provider,
                    "model-a",
                    "user-a",
                    "provider-a",
                    [],
                ):
                    pass

            with patch.object(app, "_external_response_stream", new=fake_response):
                await asyncio.gather(consume(), consume())
            app.external_conversation_locks.clear()
            app.external_conversation_states.clear()

        asyncio.run(scenario())
        self.assertEqual(maximum_active, 1)
        self.assertEqual(calls, ["", "upstream_serial_1"])

    def test_external_stream_enforces_outer_20_minute_deadline_and_cancels_source(self) -> None:
        source_cancelled = False
        payload = app.TurnRequest(
            session_id="7" * 32,
            message="慢请求",
            model="external",
            effort="default",
        )
        provider = {"baseUrl": "https://provider.example/v1", "protocol": "openai"}

        async def slow_response(*_args, **_kwargs):
            nonlocal source_cancelled
            try:
                await asyncio.sleep(60)
                yield app._ndjson({"type": "done", "files": []})
            finally:
                source_cancelled = True

        async def scenario() -> list[dict]:
            with (
                patch.object(app, "_external_response_stream", new=slow_response),
                patch.object(app, "EXTERNAL_RESPONSE_TIMEOUT_SECONDS", 0.01),
            ):
                return [
                    app.json.loads(event.decode())
                    async for event in app._external_stream(
                        payload,
                        Path("."),
                        provider,
                        "model-a",
                        "user-a",
                    )
                ]

        self.assertEqual(app.EXTERNAL_RESPONSE_TIMEOUT_SECONDS, 20 * 60)
        events = asyncio.run(scenario())
        self.assertEqual([event["type"] for event in events], ["started", "error"])
        self.assertIn("20 分钟", events[-1]["message"])
        self.assertTrue(source_cancelled)

    def test_successful_continuation_keeps_existing_id_when_upstream_does_not_echo_it(self) -> None:
        line = app.json.dumps({
            "choices": [{"delta": {"content": "完成"}, "finish_reason": "stop"}],
        }, ensure_ascii=False)

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def aiter_lines(self):
                yield f"data: {line}"
                yield "data: [DONE]"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return FakeResponse()

        payload = app.TurnRequest(
            session_id="9" * 32,
            message="继续",
            model="external",
            effort="default",
        )
        provider = {"baseUrl": "https://provider.example/v1", "protocol": "openai"}

        async def scenario() -> tuple[list[dict], dict[str, str]]:
            completion: dict[str, str] = {}
            with (
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory,
                patch.object(
                    app,
                    "_validate_remote_base",
                    new=AsyncMock(return_value=provider["baseUrl"]),
                ),
                patch.object(app.httpx, "AsyncClient", return_value=FakeClient()),
            ):
                events = [
                    app.json.loads(event.decode())
                    async for event in app._external_response_stream(
                        payload,
                        Path(directory),
                        provider,
                        "model-a",
                        "user-a",
                        reuse_conversation_id="upstream_existing_1",
                        external_context_key="c" * 64,
                        completion_state=completion,
                    )
                ]
            return events, completion

        events, completion = asyncio.run(scenario())
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["externalConversationId"], "upstream_existing_1")
        self.assertEqual(completion["external_conversation_id"], "upstream_existing_1")
        self.assertEqual(completion["external_context_key"], "c" * 64)

    def test_external_id_is_not_committed_when_a_later_stream_error_occurs(self) -> None:
        final_line = app.json.dumps({
            "conversation_id": "upstream_must_not_commit",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
        })
        error_line = app.json.dumps({"error": {"message": "late failure"}})

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def aiter_lines(self):
                yield f"data: {final_line}"
                yield f"data: {error_line}"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return FakeResponse()

        payload = app.TurnRequest(
            session_id="8" * 32,
            message="问题",
            model="external",
            effort="default",
        )
        provider = {"baseUrl": "https://provider.example/v1", "protocol": "openai"}

        async def scenario() -> tuple[list[dict], dict[str, str]]:
            completion: dict[str, str] = {}
            with (
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory,
                patch.object(
                    app,
                    "_validate_remote_base",
                    new=AsyncMock(return_value=provider["baseUrl"]),
                ),
                patch.object(app.httpx, "AsyncClient", return_value=FakeClient()),
            ):
                events = [
                    app.json.loads(event.decode())
                    async for event in app._external_response_stream(
                        payload,
                        Path(directory),
                        provider,
                        "model-a",
                        "user-a",
                        external_context_key="a" * 64,
                        completion_state=completion,
                    )
                ]
            return events, completion

        events, completion = asyncio.run(scenario())
        self.assertEqual(events[-1], {"type": "error", "message": "late failure"})
        self.assertFalse(any(event["type"] == "done" for event in events))
        self.assertEqual(completion, {})

    def test_delta_then_eof_is_an_error_and_invalidates_reused_state(self) -> None:
        line = app.json.dumps({
            "choices": [{"delta": {"content": "未完成的半截回答"}, "finish_reason": None}],
        }, ensure_ascii=False)

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def aiter_lines(self):
                yield f"data: {line}"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return FakeResponse()

        provider = {"baseUrl": "https://provider.example/v1", "protocol": "openai"}
        payload = app.TurnRequest(
            session_id="a" * 32,
            client_conversation_id="b" * 32,
            message="继续",
            model="external",
            effort="default",
        )
        context_key = app._external_context_key(payload, "provider-a", provider, "model-a")
        payload = payload.model_copy(update={
            "external_conversation_id": "upstream_previous_1",
            "external_context_key": context_key,
        })

        async def scenario() -> list[dict]:
            site_key = ("user-a", payload.client_conversation_id)
            app.external_conversation_locks.clear()
            app.external_conversation_states.clear()
            app.external_conversation_states[site_key] = {
                "external_conversation_id": "upstream_previous_1",
                "external_context_key": context_key,
            }
            with (
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory,
                patch.object(
                    app,
                    "_validate_remote_base",
                    new=AsyncMock(return_value=provider["baseUrl"]),
                ),
                patch.object(app.httpx, "AsyncClient", return_value=FakeClient()),
            ):
                events = [
                    app.json.loads(event.decode())
                    async for event in app._external_stream(
                        payload,
                        Path(directory),
                        provider,
                        "model-a",
                        "user-a",
                        "provider-a",
                        [],
                    )
                ]
            state = app.external_conversation_states[site_key]
            self.assertEqual(state["external_conversation_id"], "")
            self.assertEqual(state["external_context_key"], context_key)
            app.external_conversation_locks.clear()
            app.external_conversation_states.clear()
            return events

        events = asyncio.run(scenario())
        self.assertEqual([event["type"] for event in events], ["started", "delta", "error"])
        self.assertIn("意外中断", events[-1]["message"])
        self.assertFalse(any(event["type"] == "done" for event in events))

    def test_frontend_sends_and_persists_external_context_fields(self) -> None:
        source = (app.BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")
        for marker in (
            "client_conversation_id: conversation.id",
            "external_conversation_id: conversation.externalConversationId",
            "external_context_key: conversation.externalContextKey",
            "externalConversationId: conversation.externalConversationId || null",
            "if (done) buffer += decoder.decode();",
            'throw new Error("回答连接意外中断，请重试")',
        ):
            self.assertIn(marker, source)


def conversation(identifier: str, workspace: str) -> dict:
    return {
        "id": identifier,
        "workspaceId": workspace,
        "title": "隔离测试",
        "messages": [],
        "updatedAt": 1_800_000_000_000,
        "pinned": False,
        "codexThreadIds": [],
    }


class MultiAccountIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app._prepare_directories()
        with closing(sqlite3.connect(app.CONVERSATIONS_PATH)) as database, database:
            cls.owner_id = str(database.execute(
                "SELECT id FROM users WHERE username = 'owner'"
            ).fetchone()[0])
            cls.member_id = "b" * 32
            database.execute(
                """INSERT INTO users
                (id, username, password_hash, is_admin, disabled, created_at)
                VALUES (?, 'member', ?, 0, 0, 1)""",
                (cls.member_id, app._password_hash("Member-password-1")),
            )
            database.commit()

    def test_passwords_are_hashed_and_verified(self) -> None:
        with closing(sqlite3.connect(app.CONVERSATIONS_PATH)) as database:
            stored = str(database.execute(
                "SELECT password_hash FROM users WHERE id = ?", (self.member_id,)
            ).fetchone()[0])
        self.assertNotIn("Member-password-1", stored)
        self.assertTrue(app._password_matches("Member-password-1", stored))
        self.assertFalse(app._password_matches("wrong-password", stored))

    def test_conversations_are_filtered_by_owner(self) -> None:
        app._sync_cloud_conversations(
            self.owner_id, [conversation("1" * 32, "2" * 32)], []
        )
        app._sync_cloud_conversations(
            self.member_id, [conversation("3" * 32, "4" * 32)], []
        )
        with closing(sqlite3.connect(app.CONVERSATIONS_PATH)) as database:
            owner_state = app._cloud_state(database, self.owner_id)
            member_state = app._cloud_state(database, self.member_id)
        self.assertIn("1" * 32, [item["id"] for item in owner_state["conversations"]])
        self.assertIn("a" * 32, [item["id"] for item in owner_state["conversations"]])
        self.assertEqual([item["id"] for item in member_state["conversations"]], ["3" * 32])

    def test_legacy_data_is_assigned_to_initial_admin(self) -> None:
        with closing(sqlite3.connect(app.CONVERSATIONS_PATH)) as database:
            owner = database.execute(
                "SELECT user_id FROM conversations WHERE id = ?", ("a" * 32,)
            ).fetchone()
        self.assertEqual(str(owner[0]), self.owner_id)

    def test_cross_account_identifier_collision_is_rejected(self) -> None:
        app._sync_cloud_conversations(
            self.owner_id, [conversation("5" * 32, "6" * 32)], []
        )
        with self.assertRaises(app.HTTPException) as caught:
            app._sync_cloud_conversations(
                self.member_id, [conversation("5" * 32, "7" * 32)], []
            )
        self.assertEqual(caught.exception.status_code, 409)

    def test_workspace_paths_are_separate(self) -> None:
        session_id = "8" * 32
        owner_path = app._session_path(self.owner_id, session_id)
        member_path = app._session_path(self.member_id, session_id)
        self.assertNotEqual(owner_path, member_path)
        self.assertEqual(owner_path.parent.name, self.owner_id)
        self.assertEqual(member_path.parent.name, self.member_id)

    def test_codex_thread_cannot_cross_accounts(self) -> None:
        thread_id = "thread_isolation_test"
        app._register_codex_thread(self.owner_id, thread_id)
        app._require_codex_thread_owner(self.owner_id, thread_id)
        with self.assertRaises(app.HTTPException) as caught:
            app._require_codex_thread_owner(self.member_id, thread_id)
        self.assertEqual(caught.exception.status_code, 403)

    def test_hourly_message_limit_is_enforced_server_side(self) -> None:
        with closing(sqlite3.connect(app.CONVERSATIONS_PATH)) as database, database:
            database.execute("DELETE FROM message_events WHERE user_id = ?", (self.member_id,))
            database.execute(
                "UPDATE users SET hourly_message_limit = 2 WHERE id = ?",
                (self.member_id,),
            )
        app._reserve_message_slot(self.member_id)
        app._reserve_message_slot(self.member_id)
        with self.assertRaises(app.HTTPException) as caught:
            app._reserve_message_slot(self.member_id)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn("2/2", str(caught.exception.detail))
        self.assertGreater(int(caught.exception.headers["Retry-After"]), 0)

    def test_expired_message_events_do_not_use_current_limit(self) -> None:
        with closing(sqlite3.connect(app.CONVERSATIONS_PATH)) as database, database:
            database.execute("DELETE FROM message_events WHERE user_id = ?", (self.member_id,))
            database.execute(
                "UPDATE users SET hourly_message_limit = 1 WHERE id = ?",
                (self.member_id,),
            )
            database.execute(
                "INSERT INTO message_events (id, user_id, created_at) VALUES (?, ?, ?)",
                ("expired-event", self.member_id, int(time.time()) - 3601),
            )
        app._reserve_message_slot(self.member_id)
        with closing(sqlite3.connect(app.CONVERSATIONS_PATH)) as database:
            count = int(database.execute(
                "SELECT COUNT(*) FROM message_events WHERE user_id = ?",
                (self.member_id,),
            ).fetchone()[0])
        self.assertEqual(count, 1)

    def test_administrator_is_not_rate_limited(self) -> None:
        with closing(sqlite3.connect(app.CONVERSATIONS_PATH)) as database, database:
            database.execute("DELETE FROM message_events WHERE user_id = ?", (self.owner_id,))
            database.execute(
                "UPDATE users SET hourly_message_limit = 1 WHERE id = ?",
                (self.owner_id,),
            )
        for _ in range(3):
            app._reserve_message_slot(self.owner_id)
        with closing(sqlite3.connect(app.CONVERSATIONS_PATH)) as database, database:
            count = int(database.execute(
                "SELECT COUNT(*) FROM message_events WHERE user_id = ?",
                (self.owner_id,),
            ).fetchone()[0])
            database.execute(
                "UPDATE users SET hourly_message_limit = 0 WHERE id = ?",
                (self.owner_id,),
            )
        self.assertEqual(count, 0)

    def test_public_http_provider_url_is_allowed(self) -> None:
        resolved = [(None, None, None, None, ("93.184.216.34", 80))]
        with patch("app.socket.getaddrinfo", return_value=resolved):
            value = asyncio.run(app._validate_remote_base("http://api.example.com:8080/v1/"))
        self.assertEqual(value, "http://api.example.com:8080/v1")

    def test_server_loopback_http_provider_url_is_allowed(self) -> None:
        resolved = [(None, None, None, None, ("127.0.0.1", 8317))]
        with patch("app.socket.getaddrinfo", return_value=resolved):
            value = asyncio.run(app._validate_remote_base("http://127.0.0.1:8317/v1/"))
        self.assertEqual(value, "http://127.0.0.1:8317/v1")

    def test_private_http_provider_url_is_allowed(self) -> None:
        resolved = [(None, None, None, None, ("192.168.50.8", 8080))]
        with patch("app.socket.getaddrinfo", return_value=resolved):
            value = asyncio.run(app._validate_remote_base("http://desktop.private:8080/v1"))
        self.assertEqual(value, "http://desktop.private:8080/v1")

    def test_link_local_http_provider_url_is_rejected(self) -> None:
        resolved = [(None, None, None, None, ("169.254.169.254", 80))]
        with patch("app.socket.getaddrinfo", return_value=resolved):
            with self.assertRaises(app.HTTPException) as caught:
                asyncio.run(app._validate_remote_base("http://169.254.169.254/latest"))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("链路本地", str(caught.exception.detail))

    def test_admin_can_remove_one_provider_model(self) -> None:
        provider_id = "d" * 12
        app._write_providers([{
            "id": provider_id,
            "preset": "custom",
            "name": "Model delete test",
            "baseUrl": "https://api.example.com/v1",
            "protocol": "openai",
            "apiKey": "test-key",
            "enabled": True,
            "models": ["model-alpha", "model-beta"],
            "updatedAt": 1,
        }])
        try:
            with TestClient(app.app, base_url="https://testserver") as owner_client:
                login = owner_client.post(
                    "/login",
                    data={"username": "owner", "password": "Initial-admin-password"},
                    follow_redirects=False,
                )
                self.assertEqual(login.status_code, 303)
                removed = owner_client.request(
                    "DELETE",
                    f"/api/providers/{provider_id}/models",
                    json={"model": "model-alpha"},
                )
                self.assertEqual(removed.status_code, 200)
                self.assertEqual(removed.json()["removed"], "model-alpha")
                self.assertEqual(removed.json()["provider"]["models"], ["model-beta"])
                self.assertTrue(removed.json()["provider"]["hasKey"])
                self.assertNotIn("apiKey", removed.json()["provider"])
                repeated = owner_client.request(
                    "DELETE",
                    f"/api/providers/{provider_id}/models",
                    json={"model": "model-alpha"},
                )
                self.assertEqual(repeated.status_code, 404)

            with TestClient(app.app, base_url="https://testserver") as member_client:
                login = member_client.post(
                    "/login",
                    data={"username": "member", "password": "Member-password-1"},
                    follow_redirects=False,
                )
                self.assertEqual(login.status_code, 303)
                forbidden = member_client.request(
                    "DELETE",
                    f"/api/providers/{provider_id}/models",
                    json={"model": "model-beta"},
                )
                self.assertEqual(forbidden.status_code, 403)
        finally:
            app._write_providers([])

    def test_admin_can_disable_and_enable_codex_model(self) -> None:
        model_id = "gpt-test-admin-toggle"
        model_result = {
            "data": [{
                "model": model_id,
                "displayName": "GPT Test Admin Toggle",
                "description": "Test model",
                "isDefault": True,
                "supportedReasoningEfforts": [],
            }]
        }
        app._write_disabled_codex_models(set())
        try:
            with patch.object(app.codex, "request", new=AsyncMock(return_value=model_result)):
                with TestClient(app.app, base_url="https://testserver") as owner_client:
                    login = owner_client.post(
                        "/login",
                        data={"username": "owner", "password": "Initial-admin-password"},
                        follow_redirects=False,
                    )
                    self.assertEqual(login.status_code, 303)
                    listed = owner_client.get("/api/admin/codex-models")
                    self.assertEqual(listed.status_code, 200)
                    self.assertTrue(listed.json()["models"][0]["enabled"])

                    disabled = owner_client.patch(
                        "/api/admin/codex-models",
                        json={"model": model_id, "enabled": False},
                    )
                    self.assertEqual(disabled.status_code, 200)
                    self.assertFalse(disabled.json()["enabled"])
                    public_models = owner_client.get("/api/models?include_codex=true")
                    self.assertEqual(public_models.status_code, 200)
                    self.assertNotIn(model_id, [item["id"] for item in public_models.json()["data"]])
                    blocked = owner_client.post(
                        "/api/turn",
                        json={
                            "session_id": "f" * 32,
                            "thread_id": None,
                            "message": "普通文本问题",
                            "model": model_id,
                            "effort": "medium",
                            "attachments": [],
                            "project_attachments": [],
                            "project_instructions": "",
                            "history": [],
                        },
                    )
                    self.assertEqual(blocked.status_code, 400)
                    self.assertIn("管理员关闭", blocked.json()["detail"])

                    enabled = owner_client.patch(
                        "/api/admin/codex-models",
                        json={"model": model_id, "enabled": True},
                    )
                    self.assertEqual(enabled.status_code, 200)
                    restored_models = owner_client.get("/api/models?include_codex=true")
                    self.assertIn(model_id, [item["id"] for item in restored_models.json()["data"]])

                with TestClient(app.app, base_url="https://testserver") as member_client:
                    login = member_client.post(
                        "/login",
                        data={"username": "member", "password": "Member-password-1"},
                        follow_redirects=False,
                    )
                    self.assertEqual(login.status_code, 303)
                    forbidden = member_client.patch(
                        "/api/admin/codex-models",
                        json={"model": model_id, "enabled": False},
                    )
                    self.assertEqual(forbidden.status_code, 403)
        finally:
            app._write_disabled_codex_models(set())

    def test_login_admin_controls_and_http_isolation(self) -> None:
        with TestClient(app.app, base_url="https://testserver") as owner_client:
            login = owner_client.post(
                "/login",
                data={"username": "owner", "password": "Initial-admin-password"},
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 303)
            self.assertEqual(owner_client.get("/api/site-account").json()["username"], "owner")
            created = owner_client.post(
                "/api/admin/users",
                json={
                    "username": "httpmember",
                    "password": "Http-member-password",
                    "is_admin": False,
                    "hourly_message_limit": 12,
                },
            )
            self.assertEqual(created.status_code, 200)
            self.assertEqual(created.json()["user"]["hourlyMessageLimit"], 12)
            http_member_id = created.json()["user"]["id"]
            changed = owner_client.patch(
                f"/api/admin/users/{http_member_id}",
                json={"hourly_message_limit": 7},
            )
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json()["user"]["hourlyMessageLimit"], 7)
            limited = owner_client.patch(
                f"/api/admin/users/{http_member_id}",
                json={"hourly_message_limit": 1},
            )
            self.assertEqual(limited.status_code, 200)
            app._reserve_message_slot(http_member_id)

            with TestClient(app.app, base_url="https://testserver") as member_client:
                login = member_client.post(
                    "/login",
                    data={"username": "httpmember", "password": "Http-member-password"},
                    follow_redirects=False,
                )
                self.assertEqual(login.status_code, 303)
                self.assertEqual(member_client.get("/api/admin/users").status_code, 403)
                rate_limited = member_client.post(
                    "/api/turn",
                    json={
                        "session_id": "e" * 32,
                        "thread_id": None,
                        "message": "生成一张测试图片",
                        "model": "gpt-5.6-luna",
                        "effort": "medium",
                        "attachments": [],
                        "project_attachments": [],
                        "project_instructions": "",
                        "history": [],
                    },
                )
                self.assertEqual(rate_limited.status_code, 429)
                self.assertIn("1/1", rate_limited.json()["detail"])
                member_sync = member_client.post(
                    "/api/conversations/sync",
                    json={"conversations": [conversation("c" * 32, "d" * 32)], "projects": []},
                )
                self.assertEqual(member_sync.status_code, 200)
                self.assertEqual(
                    [item["id"] for item in member_sync.json()["conversations"]], ["c" * 32]
                )

            owner_sync = owner_client.post(
                "/api/conversations/sync", json={"conversations": [], "projects": []}
            )
            self.assertEqual(owner_sync.status_code, 200)
            self.assertNotIn(
                "c" * 32, [item["id"] for item in owner_sync.json()["conversations"]]
            )

    def test_member_expiry_disables_login_and_invalidates_sessions(self) -> None:
        with TestClient(app.app, base_url="https://testserver") as owner_client:
            login = owner_client.post(
                "/login",
                data={"username": "owner", "password": "Initial-admin-password"},
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 303)
            before = int(time.time())
            created = owner_client.post(
                "/api/admin/users",
                json={
                    "username": "expiringmember",
                    "password": "Expiring-member-password",
                    "is_admin": False,
                    "hourly_message_limit": 0,
                    "expires_in_days": 3,
                },
            )
            self.assertEqual(created.status_code, 200)
            member_id = created.json()["user"]["id"]
            expires_at = int(created.json()["user"]["expiresAt"])
            self.assertGreaterEqual(expires_at, before + 3 * 24 * 60 * 60)
            self.assertLessEqual(expires_at, int(time.time()) + 3 * 24 * 60 * 60)
            create_hour_before = int(time.time())
            created_with_hours = owner_client.post(
                "/api/admin/users",
                json={
                    "username": "hourmember",
                    "password": "Hourly-member-password",
                    "is_admin": False,
                    "hourly_message_limit": 0,
                    "expires_in_hours": 2,
                },
            )
            self.assertEqual(created_with_hours.status_code, 200)
            created_hour_expiry = int(created_with_hours.json()["user"]["expiresAt"])
            self.assertGreaterEqual(created_hour_expiry, create_hour_before + 2 * 60 * 60)
            self.assertLessEqual(created_hour_expiry, int(time.time()) + 2 * 60 * 60)
            ambiguous_create = owner_client.post(
                "/api/admin/users",
                json={
                    "username": "ambiguousmember",
                    "password": "Ambiguous-member-password",
                    "expires_in_days": 1,
                    "expires_in_hours": 1,
                },
            )
            self.assertEqual(ambiguous_create.status_code, 400)
            hour_before = int(time.time())
            hour_rescheduled = owner_client.patch(
                f"/api/admin/users/{member_id}", json={"expires_in_hours": 3}
            )
            self.assertEqual(hour_rescheduled.status_code, 200)
            hour_expires_at = int(hour_rescheduled.json()["user"]["expiresAt"])
            self.assertGreaterEqual(hour_expires_at, hour_before + 3 * 60 * 60)
            self.assertLessEqual(hour_expires_at, int(time.time()) + 3 * 60 * 60)
            ambiguous_expiry = owner_client.patch(
                f"/api/admin/users/{member_id}",
                json={"expires_in_days": 1, "expires_in_hours": 1},
            )
            self.assertEqual(ambiguous_expiry.status_code, 400)
            admin_expiry = owner_client.patch(
                f"/api/admin/users/{self.owner_id}",
                json={"expires_in_hours": 1},
            )
            self.assertEqual(admin_expiry.status_code, 400)

            with TestClient(app.app, base_url="https://testserver") as member_client:
                member_login = member_client.post(
                    "/login",
                    data={
                        "username": "expiringmember",
                        "password": "Expiring-member-password",
                    },
                    follow_redirects=False,
                )
                self.assertEqual(member_login.status_code, 303)
                self.assertEqual(member_client.get("/api/site-account").status_code, 200)
                with closing(sqlite3.connect(app.CONVERSATIONS_PATH)) as database, database:
                    database.execute(
                        "UPDATE users SET expires_at = ? WHERE id = ?",
                        (int(time.time()) - 1, member_id),
                    )
                self.assertEqual(member_client.get("/api/site-account").status_code, 401)

            with closing(sqlite3.connect(app.CONVERSATIONS_PATH)) as database:
                state = database.execute(
                    "SELECT disabled FROM users WHERE id = ?", (member_id,)
                ).fetchone()
                sessions = int(database.execute(
                    "SELECT COUNT(*) FROM site_sessions WHERE user_id = ?", (member_id,)
                ).fetchone()[0])
            self.assertEqual(int(state[0]), 1)
            self.assertEqual(sessions, 0)

            expired_login = TestClient(app.app, base_url="https://testserver").post(
                "/login",
                data={
                    "username": "expiringmember",
                    "password": "Expiring-member-password",
                },
                follow_redirects=False,
            )
            self.assertEqual(expired_login.status_code, 401)
            blocked_enable = owner_client.patch(
                f"/api/admin/users/{member_id}", json={"disabled": False}
            )
            self.assertEqual(blocked_enable.status_code, 400)
            rescheduled = owner_client.patch(
                f"/api/admin/users/{member_id}", json={"expires_in_days": 2}
            )
            self.assertEqual(rescheduled.status_code, 200)
            enabled = owner_client.patch(
                f"/api/admin/users/{member_id}", json={"disabled": False}
            )
            self.assertEqual(enabled.status_code, 200)
            cleared = owner_client.patch(
                f"/api/admin/users/{member_id}", json={"expires_in_days": None}
            )
            self.assertEqual(cleared.status_code, 200)
            self.assertIsNone(cleared.json()["user"]["expiresAt"])


if __name__ == "__main__":
    unittest.main()
