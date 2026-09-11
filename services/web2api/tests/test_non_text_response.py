"""Tests for broadened Phase-2 non-text response handling.

Verifies that image/tool-use/non-text responses don't falsely trigger the
stall detector, that text responses stream correctly (no regression), and
that the placeholder message surfaces when non-text content is detected
but _fetch_text_for_turn returns not_ready/empty.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import (
    AuthExpiredError,
    CDPDriver,
    GenerationStuckError,
)
from chatgpt_web2api.turn_anchor import TurnEndResult, TurnTextResult


def _make_driver():
    d = CDPDriver(cdp_port=9222)
    d._ws = MagicMock()
    d._access_token = "tok"
    d._token_fetched_at = time.time()
    return d


# ── 1. Image-like DOM: explicit media grows, text stays empty → no stall ─


@pytest.mark.asyncio
async def test_image_response_does_not_stall(monkeypatch):
    """Image generation: explicit img/canvas content is present while text
    stays empty. DOM growth keeps the turn live and the explicit content marker
    permits the non-text completion path."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    state = {"phase1_polls": 0, "phase2_polls": 0}

    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        if "has_action" in expr:
            # Phase 2 poll (now has html_len — distinguishes from Phase 1)
            state["phase2_polls"] += 1
            n = state["phase2_polls"]
            html_len = 10 + n * 50  # grows each poll (image rendering)
            # has_action: the per-turn action button appears only on a finished
            # message. False while the image renders, True after ~50s of
            # progress. This is the completion signal the driver now uses.
            has_action = n > 100
            return json.dumps(
                {
                    "text": "",
                    "html_len": html_len,
                    "child_count": 1,
                    "has_meaningful_non_text": True,
                    "has_action": has_action,
                }
            )
        if ".length" in expr and "querySelectorAll" in expr and "JSON.stringify" not in expr:
            # Phase 1 count poll
            state["phase1_polls"] += 1
            return "1" if state["phase1_polls"] > 1 else "0"
        if "location.href" in expr:
            return "https://chatgpt.com/c/test-image-conv"
        return ""

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    # A2: image turn — selector reports non_text so the reconciliation loop
    # breaks to the placeholder path (last_dom_text empty + had_non_text_content).
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="non_text")
    )

    chunks = []
    async for chunk in d.send_and_stream("generate an image", timeout=10000):
        chunks.append(chunk)

    # Did NOT raise GenerationStuckError — image generation completed.
    # Placeholder message should be present since non-text content was detected.
    text_chunks = [c.delta for c in chunks if c.delta]
    assert any("Non-text response" in c for c in text_chunks), f"chunks: {text_chunks}"
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_phase2_new_image_survives_projection_429(monkeypatch):
    """A transient assistant wrapper may disappear before an image completes.

    A new Estuary URL plus an increased image-action count and no Stop button
    is explicit current-turn completion evidence even while the backgrounded
    browser reports ``img.complete=false``/``naturalWidth=0``. The downloader
    validates its authenticated bytes later. The pre-send baseline still
    excludes an older image, and a projection 429 cannot hide the DOM result.
    """
    d = _make_driver()
    t = [10.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(seconds):
        t[0] += seconds

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    state = {"assistant_counts": 0, "phase2": 0, "global_probe": 0}

    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        if "actionCount" in expr:
            state["global_probe"] += 1
            return json.dumps(
                {
                    "generationActive": False,
                    "actionCount": 2,
                    "assets": [
                        {
                            "identity": "file_old",
                            "source_url": (
                                "https://chatgpt.com/backend-api/estuary/content"
                                "?id=file_old"
                            ),
                            "loaded": False,
                        },
                        {
                            "identity": "file_new",
                            "source_url": (
                                "https://chatgpt.com/backend-api/estuary/content"
                                "?id=file_new"
                            ),
                            "loaded": False,
                        },
                    ],
                }
            )
        if "image-turn-action-button" in expr:
            return "0"
        if "var seen={},ids=[]" in expr:
            return json.dumps(["file_old"])
        if "has_action" in expr:
            state["phase2"] += 1
            return json.dumps(
                {
                    "text": "",
                    "md_text": "",
                    "html_len": 0,
                    "child_count": 0,
                    "has_meaningful_non_text": False,
                    "has_action": False,
                    "has_exact_action": False,
                    "is_thinking": False,
                    "generation_active": False,
                    "current_assistant_present": False,
                }
            )
        if (
            ".length" in expr
            and "querySelectorAll" in expr
            and "JSON.stringify" not in expr
        ):
            state["assistant_counts"] += 1
            return "0" if state["assistant_counts"] == 1 else "1"
        if "location.href" in expr:
            return "https://chatgpt.com/c/image-429-conv"
        return ""

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._verify_send_acknowledged = AsyncMock(return_value=True)
    d._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(
            status="fetch_failed",
            diagnostic={"error": "projection HTTP 429 for image-429-conv"},
        )
    )
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(
            status="fetch_failed",
            diagnostic={"error": "projection HTTP 429 for image-429-conv"},
        )
    )
    d.get_conversation = AsyncMock(
        side_effect=RuntimeError("conversation HTTP 429")
    )

    chunks = []
    async for chunk in d.send_and_stream(
        "generate an image", timeout=10000, expect_non_text=True
    ):
        chunks.append(chunk)

    assert chunks[-1].finish_reason == "stop"
    assert d._fetch_end_turn_for_turn.await_count >= 1
    assert state["phase2"] >= 1
    assert state["global_probe"] >= 1
    d._fetch_text_for_turn.assert_not_awaited()
    assert d._last_response_assets == [
        {
            "type": "image",
            "name": "generated-image-1.png",
            "mime_type": "image/png",
            "file_id": "file_new",
            "source_url": (
                "https://chatgpt.com/backend-api/estuary/content?id=file_new"
            ),
        }
    ]


@pytest.mark.asyncio
async def test_phase2_old_image_cannot_override_projection_429(monkeypatch):
    """A broad/stale action row plus a pre-existing image is not completion.

    With a 429 backend projection there is no authoritative backend answer, so
    the detector must keep waiting and eventually report a stall instead of
    returning an old image as the new result.
    """
    d = _make_driver()
    t = [10.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(seconds):
        t[0] += seconds

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    state = {"assistant_counts": 0, "phase2": 0, "global_probe": 0}

    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        if "actionCount" in expr:
            state["global_probe"] += 1
            return json.dumps(
                {
                    "generationActive": False,
                    "actionCount": 2,
                    "assets": [
                        {
                            "identity": "file_old",
                            "source_url": (
                                "https://chatgpt.com/backend-api/estuary/content"
                                "?id=file_old"
                            ),
                            "loaded": False,
                        }
                    ],
                }
            )
        if "image-turn-action-button" in expr:
            return "0"
        if "var seen={},ids=[]" in expr:
            return json.dumps(["file_old"])
        if "has_action" in expr:
            state["phase2"] += 1
            return json.dumps(
                {
                    "text": "",
                    "md_text": "",
                    "html_len": 500,
                    "child_count": 2,
                    # A current assistant shell contains a generic media node,
                    # so the 429 backend path is actually exercised; the only
                    # concrete Estuary identity is still the pre-send old one.
                    "has_meaningful_non_text": True,
                    "has_action": True,
                    "has_exact_action": False,
                    "is_thinking": False,
                    "generation_active": False,
                }
            )
        if (
            ".length" in expr
            and "querySelectorAll" in expr
            and "JSON.stringify" not in expr
        ):
            state["assistant_counts"] += 1
            return "0" if state["assistant_counts"] == 1 else "1"
        if "location.href" in expr:
            return "https://chatgpt.com/c/image-429-conv"
        return ""

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._verify_send_acknowledged = AsyncMock(return_value=True)
    d._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(
            status="fetch_failed",
            diagnostic={"error": "projection HTTP 429 for image-429-conv"},
        )
    )

    with pytest.raises(GenerationStuckError):
        async for _ in d.send_and_stream(
            "generate an image", timeout=10000, expect_non_text=True
        ):
            pass

    assert d._last_response_assets == []
    assert state["phase2"] > 1
    assert state["global_probe"] > 1
    assert d._fetch_end_turn_for_turn.await_count >= 1


@pytest.mark.asyncio
async def test_phase2_unloaded_new_image_without_new_action_cannot_complete(monkeypatch):
    """An unloaded new URL alone is liveness, not completion evidence."""
    d = _make_driver()
    t = [10.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(seconds):
        t[0] += seconds

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    state = {"assistant_counts": 0, "phase2": 0, "global_probe": 0}

    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        if "actionCount" in expr:
            state["global_probe"] += 1
            return json.dumps(
                {
                    "generationActive": False,
                    "actionCount": 0,
                    "assets": [
                        {
                            "identity": "file_new",
                            "source_url": (
                                "https://chatgpt.com/backend-api/estuary/content"
                                "?id=file_new"
                            ),
                            "loaded": False,
                        }
                    ],
                }
            )
        if "image-turn-action-button" in expr:
            return "0"
        if "var seen={},ids=[]" in expr:
            return "[]"
        if "has_action" in expr:
            state["phase2"] += 1
            return json.dumps(
                {
                    "text": "",
                    "md_text": "",
                    "html_len": 0,
                    "child_count": 0,
                    "has_meaningful_non_text": False,
                    "has_action": False,
                    "has_exact_action": False,
                    "is_thinking": False,
                    "generation_active": False,
                    "current_assistant_present": False,
                }
            )
        if (
            ".length" in expr
            and "querySelectorAll" in expr
            and "JSON.stringify" not in expr
        ):
            state["assistant_counts"] += 1
            return "0" if state["assistant_counts"] == 1 else "1"
        if "location.href" in expr:
            return "https://chatgpt.com/c/image-no-action-conv"
        return ""

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._verify_send_acknowledged = AsyncMock(return_value=True)
    d._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(
            status="fetch_failed",
            diagnostic={"error": "projection HTTP 429"},
        )
    )

    with pytest.raises(GenerationStuckError):
        async for _ in d.send_and_stream(
            "generate an image", timeout=10000, expect_non_text=True
        ):
            pass

    assert d._last_response_assets == []
    assert state["phase2"] > 1
    assert state["global_probe"] > 1


@pytest.mark.asyncio
async def test_phase2_image_does_not_swallow_asset_fetch_auth_failure(monkeypatch):
    """A definitive DOM image must not turn asset-fetch 401 into success."""
    d = _make_driver()
    t = [10.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(seconds):
        t[0] += seconds

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    state = {"assistant_counts": 0, "global_probe": 0}

    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        if "actionCount" in expr:
            state["global_probe"] += 1
            return json.dumps(
                {
                    "generationActive": False,
                    "actionCount": 1,
                    "assets": [
                        {
                            "identity": "file_new",
                            "source_url": (
                                "https://chatgpt.com/backend-api/estuary/content"
                                "?id=file_new"
                            ),
                            "loaded": False,
                        }
                    ],
                }
            )
        if "image-turn-action-button" in expr:
            return "0"
        if "var seen={},ids=[]" in expr:
            return "[]"
        if "has_action" in expr:
            return json.dumps(
                {
                    "text": "",
                    "md_text": "",
                    "html_len": 500,
                    "child_count": 2,
                    "has_meaningful_non_text": False,
                    "has_action": False,
                    "has_exact_action": False,
                    "is_thinking": False,
                    "generation_active": False,
                    "current_assistant_present": False,
                }
            )
        if (
            ".length" in expr
            and "querySelectorAll" in expr
            and "JSON.stringify" not in expr
        ):
            state["assistant_counts"] += 1
            return "0" if state["assistant_counts"] == 1 else "1"
        if "location.href" in expr:
            return "https://chatgpt.com/c/image-auth-conv"
        return ""

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._verify_send_acknowledged = AsyncMock(return_value=True)
    d._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(
            status="fetch_failed",
            diagnostic={"error": "projection HTTP 429 for image-auth-conv"},
        )
    )
    d._fetch_text_for_turn = AsyncMock()
    d.get_conversation = AsyncMock(side_effect=AuthExpiredError())

    with pytest.raises(AuthExpiredError):
        async for _ in d.send_and_stream(
            "generate an image", timeout=10000, expect_non_text=True
        ):
            pass

    d._fetch_end_turn_for_turn.assert_awaited_once()
    d._fetch_text_for_turn.assert_not_awaited()
    d.get_conversation.assert_awaited_once_with("image-auth-conv")
    assert state["global_probe"] >= 1


@pytest.mark.asyncio
async def test_phase2_image_probe_does_not_complete_text_request(monkeypatch):
    """Web-search thumbnails cannot finish a normal text request as an image."""
    d = _make_driver()
    t = [10.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(seconds):
        t[0] += seconds

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    state = {"assistant_counts": 0, "phase2": 0}

    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        if "has_action" in expr:
            state["phase2"] += 1
            return json.dumps(
                {
                    "text": "Text answer.",
                    "md_text": "Text answer.",
                    "html_len": 500,
                    "child_count": 2,
                    "has_meaningful_non_text": True,
                    "non_text_assets": [
                        {
                            "identity": "file_search_thumbnail",
                            "source_url": (
                                "https://chatgpt.com/backend-api/files/"
                                "file_search_thumbnail"
                            ),
                        }
                    ],
                    "has_action": True,
                    "has_exact_action": False,
                    "is_thinking": False,
                    "generation_active": False,
                }
            )
        if (
            ".length" in expr
            and "querySelectorAll" in expr
            and "JSON.stringify" not in expr
        ):
            state["assistant_counts"] += 1
            return "0" if state["assistant_counts"] == 1 else "1"
        if "location.href" in expr:
            return "https://chatgpt.com/c/text-with-thumbnail"
        return ""

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._verify_send_acknowledged = AsyncMock(return_value=True)
    d._fetch_end_turn_for_turn = AsyncMock(
        side_effect=[
            TurnEndResult(
                status="fetch_failed",
                diagnostic={"error": "projection HTTP 429"},
            ),
            TurnEndResult(status="matched"),
        ]
    )
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text="Text answer.")
    )
    d.get_conversation = AsyncMock(return_value={"mapping": {}})

    chunks = []
    async for chunk in d.send_and_stream("search the web", timeout=10000):
        chunks.append(chunk)

    assert d._fetch_end_turn_for_turn.await_count == 2
    assert state["phase2"] > 1
    assert d._last_response_assets == []
    assert "".join(chunk.delta for chunk in chunks) == "Text answer."
    assert chunks[-1].finish_reason == "stop"


# ── 2. Text DOM: streams delta as before, no regression ───────────────


@pytest.mark.asyncio
async def test_text_response_streams_delta_unchanged(monkeypatch):
    """Normal text response: .markdown text grows each poll. Must stream
    deltas exactly as before (no regression from the broadened signals)."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    state = {"phase1": 0, "phase2": 0, "text": ""}

    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        if "has_action" in expr:
            state["phase2"] += 1
            state["text"] += "Hello world. "  # text grows
            has_action = state["phase2"] > 5
            return json.dumps(
                {
                    "text": state["text"],
                    "html_len": len(state["text"]) + 20,
                    "child_count": 1,
                    "has_action": has_action,
                }
            )
        if ".length" in expr and "querySelectorAll" in expr and "JSON.stringify" not in expr:
            state["phase1"] += 1
            return "1" if state["phase1"] > 1 else "0"
        return ""

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    # A2: _fake_js has no location.href handler → conv_id never resolves →
    # reconciliation skipped. Mapped to not_ready (faithful to old "").
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="not_ready")
    )

    chunks = []
    async for chunk in d.send_and_stream("hello", timeout=10000):
        chunks.append(chunk)

    # Verify deltas were streamed (not empty)
    deltas = [c.delta for c in chunks if c.delta]
    assert len(deltas) > 0, f"no deltas streamed: {chunks}"
    # No placeholder (text was captured)
    assert not any("Non-text response" in c for c in deltas)
    assert chunks[-1].finish_reason == "stop"


# ── 3. _fetch_text_for_turn not_ready + non-text content → placeholder ─


@pytest.mark.asyncio
async def test_placeholder_on_empty_fetch_with_non_text_content(monkeypatch):
    """When Phase-2 detects an explicit non-text result element but
    _fetch_text_for_turn returns not_ready, the reconcile should yield a
    placeholder message."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    state = {"phase1": 0, "phase2": 0}

    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        if "has_action" in expr:
            state["phase2"] += 1
            has_action = state["phase2"] > 3
            return json.dumps(
                {
                    "text": "",
                    "html_len": 200,  # non-text content present
                    "child_count": 2,
                    "has_meaningful_non_text": True,
                    "has_action": has_action,
                }
            )
        if ".length" in expr and "querySelectorAll" in expr and "JSON.stringify" not in expr:
            state["phase1"] += 1
            return "1" if state["phase1"] > 1 else "0"
        if "location.href" in expr:
            return "https://chatgpt.com/c/test-conv-123"
        return ""

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    # A2: explicit non-text result → had_non_text_content. Selector
    # reports non_text → reconciliation breaks to the placeholder path.
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="non_text")
    )
    # Updated for #12: backend end_turn is primary when conv_id is available.
    # This test's URL resolves to /c/test-conv-123, so the backend is consulted.
    # end_turn confirms completion once the non-text content is present.
    d._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))

    chunks = []
    async for chunk in d.send_and_stream("generate image", timeout=10000):
        chunks.append(chunk)

    # Placeholder should be present
    text_chunks = [c.delta for c in chunks if c.delta]
    assert any("Non-text response" in c for c in text_chunks), f"no placeholder: {text_chunks}"
