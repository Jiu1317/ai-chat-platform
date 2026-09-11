"""Multimodal input/output tests without a real browser."""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import chatgpt_web2api.api_server as api_server
import chatgpt_web2api.multimodal as multimodal
from chatgpt_web2api.api_server import IMAGE_GENERATION_TIMEOUT_SECONDS, APIServer
from chatgpt_web2api.cdp_driver import StreamChunk
from chatgpt_web2api.chatgpt_dom import ChatGPTDom
from chatgpt_web2api.config import Config
from chatgpt_web2api.multimodal import (
    ImageInputError,
    ImageReference,
    _validate_public_url,
    extract_response_assets,
    parse_content_parts,
    prepare_image_files,
    wants_image_output,
)
from chatgpt_web2api.turn_anchor import TurnAnchor

# Structurally valid enough for the bridge's signature validation.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMA"
    "ASsJTYQAAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def _isolated_asset_disk_cache(monkeypatch):
    """Keep persistence tests isolated and remove every generated file."""
    with tempfile.TemporaryDirectory() as directory:
        monkeypatch.setenv("W2A_ASSET_CACHE_DIR", directory)
        yield


def test_parse_openai_image_url_and_text():
    text, images = parse_content_parts([
        {"type": "text", "text": "describe this"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA", "detail": "high"},
        },
    ])
    assert text == "describe this"
    assert images == [
        ImageReference("data:image/png;base64,AAAA", detail="high")
    ]


@pytest.mark.parametrize(
    ("payload", "text"),
    [
        ({"modalities": ["text", "image"]}, "hello"),
        ({"response_format": {"type": "image_url"}}, "hello"),
        ({"model": "gpt-image-1"}, "hello"),
        ({}, "生成一张粉发猫娘图片"),
        ({}, "Create an illustration of a moonlit city"),
    ],
)
def test_wants_image_output(payload, text):
    assert wants_image_output(payload, text) is True


def test_image_analysis_is_not_image_generation():
    assert wants_image_output({}, "分析这张图片里有什么") is False


def test_parse_responses_input_image_shape():
    text, images = parse_content_parts([
        {"type": "input_text", "text": "what color?"},
        {"type": "input_image", "image_url": "https://example.com/a.png"},
    ])
    assert text == "what color?"
    assert images[0].url == "https://example.com/a.png"


@pytest.mark.asyncio
async def test_prepare_base64_image(tmp_path):
    encoded = base64.b64encode(PNG_1X1).decode()
    paths = await prepare_image_files(
        [ImageReference(f"data:image/png;base64,{encoded}")], tmp_path
    )
    assert len(paths) == 1
    assert paths[0].endswith(".png")
    assert open(paths[0], "rb").read() == PNG_1X1


def test_public_image_and_asset_limits_are_thirty_mib():
    thirty_mib = 30 * 1024 * 1024
    assert multimodal.MAX_IMAGE_COUNT == 8
    assert multimodal.MAX_IMAGE_BYTES == thirty_mib
    assert multimodal.MAX_TOTAL_IMAGE_BYTES == thirty_mib
    assert api_server.MAX_ASSET_CACHE_BYTES == thirty_mib


@pytest.mark.asyncio
async def test_reference_image_size_errors_match_public_thirty_mib_limit(
    tmp_path, monkeypatch
):
    encoded = base64.b64encode(PNG_1X1).decode()
    reference = ImageReference(f"data:image/png;base64,{encoded}")
    monkeypatch.setattr(multimodal, "MAX_IMAGE_BYTES", len(PNG_1X1) - 1)

    with pytest.raises(ImageInputError, match="Image exceeds the 30 MiB limit"):
        await prepare_image_files([reference], tmp_path)

    monkeypatch.setattr(multimodal, "MAX_IMAGE_BYTES", len(PNG_1X1))
    monkeypatch.setattr(multimodal, "MAX_TOTAL_IMAGE_BYTES", len(PNG_1X1) * 2 - 1)
    with pytest.raises(
        ImageInputError, match="Combined images exceed the 30 MiB limit"
    ):
        await prepare_image_files([reference, reference], tmp_path)


@pytest.mark.asyncio
async def test_remote_reference_images_download_concurrently(tmp_path, monkeypatch):
    active = 0
    maximum_active = 0

    async def fake_download(url):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return PNG_1X1, "image/png", url

    monkeypatch.setattr(multimodal, "_download_remote_image", fake_download)
    paths = await prepare_image_files(
        [
            ImageReference("https://example.com/one.png"),
            ImageReference("https://example.com/two.png"),
            ImageReference("https://example.com/three.png"),
        ],
        tmp_path,
    )

    assert len(paths) == 3
    assert maximum_active >= 2
    assert all(open(path, "rb").read() == PNG_1X1 for path in paths)


@pytest.mark.asyncio
async def test_remote_reference_image_retries_transient_failures(monkeypatch):
    attempts = 0

    async def flaky_download(_url):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise multimodal._RetryableImageDownload("HTTP 524")
        return PNG_1X1, "image/png", "https://example.com/image.png"

    monkeypatch.setattr(multimodal, "_download_remote_image_once", flaky_download)
    monkeypatch.setattr(multimodal, "IMAGE_DOWNLOAD_RETRY_DELAYS", (0, 0))

    data, mime_type, final_url = await multimodal._download_remote_image(
        "https://example.com/image.png"
    )

    assert attempts == 3
    assert data == PNG_1X1
    assert mime_type == "image/png"
    assert final_url.endswith("/image.png")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private.png",
        "http://[::1]/private.png",
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
async def test_private_image_urls_are_rejected(url):
    with pytest.raises(ImageInputError):
        await _validate_public_url(url)


def test_extract_generated_image_for_exact_turn():
    conversation = {
        "mapping": {
            "u-1": {
                "parent": None,
                "children": ["a-1"],
                "message": {
                    "id": "user-exact",
                    "author": {"role": "user"},
                    "create_time": 10,
                    "content": {"content_type": "text", "parts": ["draw"]},
                },
            },
            "a-1": {
                "parent": "u-1",
                "children": [],
                "message": {
                    "id": "assistant-image",
                    "author": {"role": "assistant"},
                    "create_time": 11,
                    "end_turn": True,
                    "content": {
                        "content_type": "multimodal_text",
                        "parts": [
                            {
                                "content_type": "image_asset_pointer",
                                "asset_pointer": "sediment://file_abc123",
                                "size_bytes": 123,
                            }
                        ],
                    },
                },
            },
        }
    }
    anchor = TurnAnchor(
        sent_text="draw",
        mode="captured_id",
        captured_user_message_id="user-exact",
    )
    assets = extract_response_assets(conversation, anchor)
    assert assets == [{
        "type": "image",
        "name": "file_abc123.png",
        "mime_type": "image/png",
        "file_id": "file_abc123",
    }]


@pytest.mark.asyncio
async def test_chatgpt_dom_sets_real_file_input(tmp_path):
    image_path = tmp_path / "image.png"
    image_path.write_bytes(PNG_1X1)

    driver = MagicMock()
    driver._has_composer = AsyncMock(return_value=True)
    driver._capture_selector_diagnostic = AsyncMock()
    driver._js = AsyncMock(return_value=json.dumps({"count": 1, "error": False}))
    driver._cdp = AsyncMock(side_effect=[
        {"result": {"result": {"objectId": "node-object"}}},
        {"result": {"node": {"backendNodeId": 42}}},
        {"result": {}},
    ])
    dom = ChatGPTDom(driver)

    await dom.upload_files([str(image_path)])

    methods = [call.args[0] for call in driver._cdp.await_args_list]
    assert methods == ["Runtime.evaluate", "DOM.describeNode", "DOM.setFileInputFiles"]
    set_files = driver._cdp.await_args_list[2].args[1]
    assert set_files["files"] == [str(image_path)]
    assert set_files["backendNodeId"] == 42


@pytest.mark.asyncio
async def test_full_response_passes_images_and_publishes_assets():
    config = Config.load(None)
    driver = MagicMock()
    driver._current_conv_id = "conv-image"
    driver._last_response_assets = [{
        "type": "image",
        "name": "result.png",
        "mime_type": "image/png",
        "file_id": "file_result",
    }]
    driver.download_response_asset = AsyncMock(return_value={
        "data": PNG_1X1,
        "content_type": "application/octet-stream",
        "filename": "result.png",
    })
    captured = {}

    async def stream(text, timeout=120, *, budgets=None, model=None, attachments=None):
        captured["attachments"] = attachments
        yield StreamChunk(delta="done")
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = stream
    server = APIServer(config, driver)
    request = MagicMock()
    request.headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "api.example"}
    request.scheme = "http"
    request.host = "127.0.0.1:9181"

    response = await server._full_response(
        request, "gpt-5-5", "describe", 30, image_paths=["C:/tmp/image.png"]
    )
    body = json.loads(response.body)
    message = body["choices"][0]["message"]

    assert captured["attachments"] == ["C:/tmp/image.png"]
    assert message["attachments"][0]["url"].startswith(
        "https://api.example/v1/assets/"
    )
    assert "![result.png](https://api.example/v1/assets/" in message["content"]
    driver.download_response_asset.assert_awaited_once()
    token = message["attachments"][0]["url"].rsplit("/", 1)[-1]
    asset_request = MagicMock()
    asset_request.match_info = {"token": token}

    asset_response = await server._handle_asset(asset_request)

    assert asset_response.body == PNG_1X1
    assert asset_response.content_type == "image/png"
    driver.download_response_asset.assert_awaited_once()


@pytest.mark.asyncio
async def test_image_response_uses_full_fifteen_minute_timeout():
    config = Config.load(None)
    driver = MagicMock()
    driver._current_conv_id = "conv-image-timeout"
    driver._last_response_assets = []
    captured = {}

    async def stream(
        text,
        timeout=120,
        *,
        budgets=None,
        model=None,
        attachments=None,
        expect_non_text=False,
    ):
        captured["timeout"] = timeout
        captured["budgets"] = budgets
        captured["expect_non_text"] = expect_non_text
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = stream
    server = APIServer(config, driver)
    request = MagicMock()
    request.headers = {}
    request.scheme = "http"
    request.host = "127.0.0.1:9181"

    await server._full_response(
        request,
        "gpt-5-5",
        "generate an image",
        IMAGE_GENERATION_TIMEOUT_SECONDS,
        expect_non_text=True,
    )

    assert IMAGE_GENERATION_TIMEOUT_SECONDS == 15 * 60
    assert captured["timeout"] == 15 * 60
    assert captured["expect_non_text"] is True
    assert captured["budgets"].first_content_timeout_seconds >= 15 * 60
    assert captured["budgets"].stream_idle_timeout_seconds >= 15 * 60
    assert captured["budgets"].hard_timeout_seconds >= 15 * 60


@pytest.mark.asyncio
async def test_asset_capability_url_serves_bytes():
    config = Config.load(None)
    driver = MagicMock()
    driver.download_response_asset = AsyncMock(return_value={
        "data": PNG_1X1,
        "content_type": "image/png",
    })
    server = APIServer(config, driver)
    server._asset_tokens["opaque"] = (
        9_999_999_999,
        {"type": "image", "name": "result.png", "file_id": "file_result"},
    )
    request = MagicMock()
    request.match_info = {"token": "opaque"}

    response = await server._handle_asset(request)
    cached_response = await server._handle_asset(request)

    assert response.status == 200
    assert response.body == PNG_1X1
    assert response.content_type == "image/png"
    assert cached_response.body == PNG_1X1
    driver.download_response_asset.assert_awaited_once()


@pytest.mark.asyncio
async def test_generated_asset_survives_worker_restart_via_shared_disk_cache():
    first_driver = MagicMock()
    first = APIServer(Config.load(None), first_driver)
    expires_at = time.time() + 600
    await first._cache_asset(
        "restart-safe-token",
        expires_at,
        {
            "data": PNG_1X1,
            "content_type": "image/png",
            "filename": "result.png",
        },
    )

    second_driver = MagicMock()
    second_driver.download_response_asset = AsyncMock(
        side_effect=AssertionError("disk hit must not call the browser")
    )
    restarted = APIServer(Config.load(None), second_driver)
    request = MagicMock()
    request.match_info = {"token": "restart-safe-token"}

    response = await restarted._handle_asset(request)

    assert response.status == 200
    assert response.body == PNG_1X1
    assert response.content_type == "image/png"
    second_driver.download_response_asset.assert_not_awaited()


@pytest.mark.asyncio
async def test_generated_image_prefetch_rejects_invalid_format_and_keeps_502():
    config = Config.load(None)
    driver = MagicMock()
    driver.download_response_asset = AsyncMock(return_value={
        "data": b"not-an-image",
        "content_type": "image/png",
    })
    server = APIServer(config, driver)
    publish_request = MagicMock()
    publish_request.headers = {}
    publish_request.scheme = "http"
    publish_request.host = "127.0.0.1:9181"
    published = server._publish_assets(publish_request, [{
        "type": "image",
        "name": "result.png",
        "mime_type": "image/png",
        "file_id": "file_result",
    }])

    await server._prefetch_published_assets(published)

    assert server._asset_cache == {}
    token = published[0]["url"].rsplit("/", 1)[-1]
    asset_request = MagicMock()
    asset_request.match_info = {"token": token}
    with pytest.raises(web.HTTPBadGateway) as exc_info:
        await server._handle_asset(asset_request)
    assert "Could not retrieve ChatGPT asset" in exc_info.value.text
    assert driver.download_response_asset.await_count == 2


@pytest.mark.asyncio
async def test_generated_image_prefetch_sends_sse_heartbeats():
    server = APIServer(Config.load(None), MagicMock())

    async def slow_prefetch(_assets):
        await asyncio.sleep(0.02)

    server._prefetch_published_assets = slow_prefetch
    response = MagicMock()
    response.write = AsyncMock()

    await server._prefetch_assets_with_heartbeat(
        response, [{"type": "image"}], interval=0.001
    )

    response.write.assert_any_await(b": caching-generated-images\n\n")


@pytest.mark.asyncio
async def test_generated_image_prefetch_is_cancelled_when_heartbeat_write_fails():
    server = APIServer(Config.load(None), MagicMock())
    cancelled = asyncio.Event()

    async def blocked_prefetch(_assets):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    server._prefetch_published_assets = blocked_prefetch
    response = MagicMock()
    response.write = AsyncMock(side_effect=ConnectionResetError("client disconnected"))

    with pytest.raises(ConnectionResetError, match="client disconnected"):
        await server._prefetch_assets_with_heartbeat(
            response, [{"type": "image"}], interval=0.001
        )

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_generated_image_prefetch_is_bounded_to_cache_capacity():
    driver = MagicMock()
    driver.download_response_asset = AsyncMock(return_value={
        "data": PNG_1X1,
        "content_type": "image/png",
    })
    server = APIServer(Config.load(None), driver)
    request = MagicMock()
    request.headers = {}
    request.scheme = "http"
    request.host = "127.0.0.1:9181"
    assets = server._publish_assets(request, [
        {
            "type": "image",
            "name": f"result-{index}.png",
            "mime_type": "image/png",
            "file_id": f"file_{index}",
        }
        for index in range(api_server.MAX_ASSET_CACHE_ITEMS + 2)
    ])

    await server._prefetch_published_assets(assets)

    assert driver.download_response_asset.await_count == api_server.MAX_ASSET_CACHE_ITEMS
    assert len(server._asset_cache) == api_server.MAX_ASSET_CACHE_ITEMS


def test_generated_asset_cache_enforces_size_and_cleans_expired_items(monkeypatch):
    server = APIServer(Config.load(None), MagicMock())
    image = {"type": "image", "mime_type": "image/png"}
    file_asset = {"type": "file", "mime_type": "application/pdf"}
    monkeypatch.setattr(api_server, "MAX_ASSET_CACHE_BYTES", len(PNG_1X1) - 1)

    with pytest.raises(ValueError, match="Generated image exceeds the 30 MiB limit"):
        server._validated_asset_download(
            image, {"data": PNG_1X1, "content_type": "image/png"}
        )
    with pytest.raises(ValueError, match="Generated asset exceeds the 30 MiB limit"):
        server._validated_asset_download(
            file_asset, {"data": PNG_1X1, "content_type": "application/pdf"}
        )

    server._asset_tokens["expired"] = (10.0, image)
    server._asset_tokens["live"] = (30.0, image)
    server._asset_cache["expired"] = (
        10.0, {"data": PNG_1X1, "content_type": "image/png"}
    )
    server._asset_cache["live"] = (
        30.0, {"data": PNG_1X1, "content_type": "image/png"}
    )

    server._purge_expired_assets(now=20.0)

    assert "expired" not in server._asset_tokens
    assert "expired" not in server._asset_cache
    assert "live" in server._asset_tokens
    assert "live" in server._asset_cache




def test_extract_generated_image_from_tool_node():
    conversation = {
        "mapping": {
            "u": {
                "children": ["tool"],
                "message": {
                    "id": "user-tool",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["draw"]},
                },
            },
            "tool": {
                "parent": "u",
                "children": ["a"],
                "message": {
                    "author": {"role": "tool"},
                    "content": {
                        "content_type": "multimodal_text",
                        "parts": [{
                            "content_type": "image_asset_pointer",
                            "asset_pointer": "sediment://file_tool123",
                        }],
                    },
                },
            },
            "a": {
                "parent": "tool",
                "message": {
                    "author": {"role": "assistant"},
                    "end_turn": True,
                    "content": {"content_type": "text", "parts": ["Here it is"]},
                },
            },
        }
    }
    anchor = TurnAnchor(
        sent_text="draw", mode="captured_id", captured_user_message_id="user-tool"
    )
    assert extract_response_assets(conversation, anchor)[0]["file_id"] == "file_tool123"
