"""Wiring tests for CompletionDetector (Phase 5 PR4 extraction).

These verify the extraction moved the Phase-1 (assistant-node appear) and
Phase-2 (DOM stream + completion-detection) loop bodies onto
CompletionDetector.stream_until_complete, and that the detector reaches the
driver's transport / backend / conversation-id through the correct seam. They
are NOT behavioral tests — completion behavior is already covered by
test_end_turn_primary / test_reliability / test_rate_limit and the broad suite,
which stub the driver and confirm the driver-facing surface is preserved. This
file guards the wiring:

  - CompletionDetector exists and exposes stream_until_complete (delta-only
    async sub-generator).
  - CDPDriver wires self._completion = CompletionDetector(self) in __init__.
  - The detector holds no long-lived config beyond _driver (the two per-call
    result attrs are transient, reset each call).
  - The detector routes transport/backend/conv-id through self._driver, NOT
    local copies — so driver-side monkeypatches still intercept.
  - is_rate_limited_text / PHASE_STALL_SECONDS stay importable from cdp_driver
    (back-compat for api_server / chatgpt_dom / tests).
  - No cdp_driver import at completion_detector module load (circular-import
    rule); error classes / StreamChunk are imported lazily inside the method.
"""

import inspect
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.completion_detector import (
    PHASE_STALL_SECONDS,
    CompletionDetector,
    DetectorBudgets,
    append_only_delta,
    is_transient_status_text,
    strip_reasoning_ui_prefix,
)
from chatgpt_web2api.turn_anchor import TurnAnchor, TurnEndResult


def _make_detector():
    """A CompletionDetector backed by a mock driver with the seam it reaches
    through. JS/backend defaults to AsyncMocks so individual tests override
    only what they assert on."""
    driver = MagicMock()
    driver._current_conv_id = None
    driver._js_strict = AsyncMock(return_value="")
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="not_ready"))
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="")
    return CompletionDetector(driver), driver


# ── 1. CompletionDetector exposes the sub-generator ───────────────────


def test_detector_has_stream_until_complete():
    """The single extracted method exists and is an async generator function
    (it yields delta chunks)."""
    detector, _ = _make_detector()
    assert callable(getattr(detector, "stream_until_complete", None)), (
        "CompletionDetector must expose stream_until_complete"
    )
    assert inspect.isasyncgenfunction(detector.stream_until_complete), (
        "stream_until_complete must be an async generator (it yields StreamChunks)"
    )


def test_stream_until_complete_is_keyword_only():
    """The driver shell calls it with initial_count= / timeout= by keyword."""
    sig = inspect.signature(CompletionDetector.stream_until_complete)
    for name in ("initial_count", "timeout"):
        assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{name} must be keyword-only"
        )


def test_driver_forwards_reference_attachment_presence_to_detector():
    """The driver must tell the detector when the request uploaded files."""
    from chatgpt_web2api.cdp_driver import CDPDriver

    source = inspect.getsource(CDPDriver.send_and_stream)
    assert "has_input_attachments=bool(attachments)" in source


# ── 2. CDPDriver wires _completion ────────────────────────────────────


def test_driver_wires_completion():
    """CDPDriver.__init__ must construct the detector with itself."""
    from chatgpt_web2api.cdp_driver import CDPDriver

    d = CDPDriver(cdp_port=9222)
    assert isinstance(d._completion, CompletionDetector), (
        "CDPDriver must wire self._completion = CompletionDetector(self)"
    )
    assert d._completion._driver is d, "detector's _driver must be the owner"


# ── 3. Per-call results, no long-lived config across calls ────────────


def test_detector_has_only_driver_and_transient_results():
    """The detector holds _driver plus two transient per-call result attrs
    (last_dom_text / had_non_text_content). No long-lived config migrates in."""
    detector, _ = _make_detector()
    own = vars(detector)
    assert set(own) == {
        "_driver", "last_dom_text", "had_non_text_content",
        "completed_via_exact_action", "non_text_dom_assets",
    }, (
        f"unexpected instance state on CompletionDetector: {set(own)}"
    )


def test_per_call_results_reset_on_each_call():
    """last_dom_text / had_non_text_content are reset at the start of each
    stream_until_complete call (no state leaks between calls)."""
    detector, driver = _make_detector()
    # Pollute them; the first thing the method does is reset both to defaults.
    detector.last_dom_text = "stale"
    detector.had_non_text_content = True

    # The detector calls _js_strict with several distinct JS expressions:
    #   - the rate-limit body scan  -> JSON {"text": ...}
    #   - the assistant node count  -> integer-as-string
    #   - the Phase-2 poll           -> JSON {text, md_text, html_len, ...}
    # Discriminate by expression so each returns the right shape. The poll
    # carries text so the backend end_turn completion guard (which requires
    # last_dom_text OR had_non_text_content) can fire and end the loop fast.
    scan = '{"text": ""}'
    poll = '{"text":"hi","md_text":"hi","html_len":60,"child_count":1,"has_action":false,"is_thinking":false}'

    async def fake_js(expr):
        if "getBoundingClientRect" in expr:  # Phase-2 completion poll
            return poll
        if "innerText" in expr:  # Phase-1 rate-limit body scan
            return scan
        return "1"  # assistant-node count poll

    driver._js_strict = fake_js
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))
    # Provide a conv_id so the backend end_turn primary signal is eligible
    # (guard requires conv_id_for_check); without it the loop has no
    # completion path and would run to the timeout.
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="conv-1")

    import asyncio

    async def drain():
        async for _ in detector.stream_until_complete(
            initial_count=0, timeout=5,
            turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
        ):
            pass

    asyncio.run(drain())
    # Reset happened: the "stale" value did not survive the call boundary. With
    # the poll carrying text="hi", last_dom_text reflects THIS call ("hi").
    assert detector.last_dom_text == "hi"
    assert detector.last_dom_text != "stale"


# ── 4. Back-compat re-exports from cdp_driver ─────────────────────────


def test_is_rate_limited_text_reexported_identity():
    """is_rate_limited_text must remain importable from cdp_driver, and it must
    be the SAME object (identity) as the one in completion_detector."""
    from chatgpt_web2api.cdp_driver import is_rate_limited_text as drv_fn
    from chatgpt_web2api.completion_detector import is_rate_limited_text as det_fn

    assert drv_fn is det_fn, "is_rate_limited_text must be re-exported by identity"
    assert drv_fn("Too many requests") is True
    assert drv_fn("normal chat answer") is False


def test_phase_stall_seconds_reexported_equal():
    """PHASE_STALL_SECONDS must remain importable from cdp_driver with the same
    value (equality, not identity — ints are not guaranteed interned)."""
    from chatgpt_web2api.cdp_driver import PHASE_STALL_SECONDS as drv_val

    assert drv_val == PHASE_STALL_SECONDS == 90


def test_append_only_delta_rejects_dom_tail_rewrite():
    emitted = "今天想让我帮你做点什么？"
    assert append_only_delta(emitted, "今天想让我帮你做点什") == ""
    assert append_only_delta(emitted, "今天想让我帮你做点什么？") == ""
    assert append_only_delta(emitted, emitted + " 好的") == " 好的"


@pytest.mark.asyncio
async def test_exact_turn_action_finishes_with_stable_inner_text_source():
    """A final markdown reflow must not truncate an already streamed answer.

    The exact action belongs to the current assistant SECTION, so it may end
    the request even when the backend projection is still not_ready.
    """
    detector, driver = _make_detector()
    polls = iter([
        '{"text":"A\\n\\nB","md_text":"","html_len":60,"child_count":1,"has_action":false,"has_exact_action":false,"is_thinking":false}',
        '{"text":"A\\n\\nB\\n\\nC","md_text":"A\\nB\\n\\nC","html_len":90,"child_count":1,"has_action":true,"has_exact_action":true,"is_thinking":false}',
    ])

    async def fake_js(expr):
        if "getBoundingClientRect" in expr:
            return next(polls)
        if "body.innerText" in expr:
            return '{"text":""}'
        return "1"

    driver._js_strict = fake_js
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="conv-1")
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=10,
        turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
    ):
        chunks.append(chunk.delta)

    assert "".join(chunks) == "A\n\nB\n\nC"
    assert detector.completed_via_exact_action is True


@pytest.mark.asyncio
async def test_disappeared_new_shell_never_returns_old_assistant_action():
    """If the post-send shell disappears, Phase 2 must not fall back to the
    baseline assistant node and return its stale text/action row."""
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    detector, driver = _make_detector()
    phase_2_scripts = []

    async def fake_js(expr):
        if "getBoundingClientRect" in expr:
            phase_2_scripts.append(expr)
            if "msgs.length <= initialCount" in expr:
                return json.dumps({
                    "text": "",
                    "md_text": "",
                    "html_len": 0,
                    "child_count": 0,
                    "has_meaningful_non_text": False,
                    "has_action": False,
                    "has_exact_action": False,
                    "is_thinking": False,
                    "generation_active": False,
                    "assistant_count": 1,
                    "current_assistant_present": False,
                })
            # This is what the old JS returned after selecting msgs[-1].
            return json.dumps({
                "text": "stale answer from previous request",
                "md_text": "stale answer from previous request",
                "html_len": 80,
                "child_count": 1,
                "has_meaningful_non_text": False,
                "has_action": True,
                "has_exact_action": True,
                "is_thinking": False,
                "generation_active": False,
            })
        if "body.innerText" in expr:
            return '{"text":""}'
        return "2"  # Phase 1 briefly sees baseline 1 -> 2.

    driver._js_strict = fake_js
    chunks = []
    with pytest.raises(GenerationStuckError):
        async for chunk in detector.stream_until_complete(
            initial_count=1,
            timeout=1.0,
            turn_anchor=TurnAnchor(sent_text="new request", mode="fresh_chat"),
            budgets=DetectorBudgets(
                first_content_timeout_seconds=0.01,
                stream_idle_timeout_seconds=0.01,
                hard_timeout_seconds=0.02,
            ),
        ):
            chunks.append(chunk.delta)

    assert chunks == []
    assert detector.completed_via_exact_action is False
    assert phase_2_scripts
    assert "var initialCount = 1" in phase_2_scripts[0]


@pytest.mark.parametrize(
    "text",
    [
        "Thinking...", "Reasoning…", "正在思考", "正在思考…", "思考中",
        "正在推理", "考え中", "Pro 思考中", "GPT-6 Pro 思考中…",
        "正在分析 2 幅图片", "正在分析 幅图片", "正在读取一张图像…",
        "正在上传 3 个文件", "Analyzing 2 images...", "Uploading 1 file",
    ],
)
def test_localized_progress_placeholders_are_not_model_output(text):
    assert is_transient_status_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "正在思考这个问题",
        "Thinking about the answer",
        "思考中常见的错误",
        "正在分析图片内容，结论是画面清晰。",
        "正在分析 2 幅图片\n正式回答已经开始。",
        "Analyzing the image shows a cat.",
        "",
    ],
)
def test_real_answer_text_is_not_treated_as_placeholder(text):
    assert not is_transient_status_text(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Thinking... answer chunk.", "answer chunk."),
        ("Thought for 8 seconds\nFinal answer", "Final answer"),
        ("正在思考…\n最终回答", "最终回答"),
        ("Pro 思考中\n最终回答", "最终回答"),
        ("GPT-6 Pro 思考中…\n最终回答", "最终回答"),
        ("正在分析 2 幅图片\n最终回答", "最终回答"),
        ("Uploading 1 file...\nFinal answer", "Final answer"),
        ("正在分析图片内容，结论是猫。", "正在分析图片内容，结论是猫。"),
        ("Thinking about the answer", "Thinking about the answer"),
    ],
)
def test_reasoning_ui_prefix_is_removed_without_eating_real_answer(text, expected):
    assert strip_reasoning_ui_prefix(text) == expected


@pytest.mark.asyncio
async def test_pro_status_inside_markdown_is_not_streamed_or_completed():
    detector, driver = _make_detector()
    polls = iter([
        '{"text":"Pro 思考中","md_text":"Pro 思考中","html_len":60,'
        '"child_count":1,"has_action":true,"has_exact_action":false,'
        '"is_thinking":false}',
        '{"text":"最终回答","md_text":"最终回答","html_len":80,'
        '"child_count":1,"has_action":true,"has_exact_action":true,'
        '"is_thinking":false}',
    ])

    async def fake_js(expr):
        if "getBoundingClientRect" in expr:
            return next(polls)
        if "body.innerText" in expr:
            return '{"text":""}'
        return "1"

    driver._js_strict = fake_js
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="conv-pro")
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=10,
        turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
    ):
        chunks.append(chunk.delta)

    assert "".join(chunks) == "最终回答"


@pytest.mark.asyncio
async def test_attachment_analysis_placeholder_cannot_complete_on_exact_action():
    """The action row can appear while ChatGPT is still analysing uploaded
    images. A pure progress label is not answer content and must neither stream
    nor satisfy exact-action completion; the later answer must still be read.
    """
    detector, driver = _make_detector()
    polls = iter([
        '{"text":"正在分析 2 幅图片","md_text":"正在分析 2 幅图片",'
        '"html_len":90,"child_count":1,"has_action":true,'
        '"has_exact_action":true,"is_thinking":false,'
        '"generation_active":false}',
        '{"text":"这是根据图片得出的正式回答。",'
        '"md_text":"这是根据图片得出的正式回答。","html_len":120,'
        '"child_count":1,"has_action":true,"has_exact_action":true,'
        '"is_thinking":false,"generation_active":false}',
    ])

    async def fake_js(expr):
        if "getBoundingClientRect" in expr:
            return next(polls)
        if "body.innerText" in expr:
            return '{"text":""}'
        return "1"

    driver._js_strict = fake_js
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="")

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=10,
        turn_anchor=TurnAnchor(sent_text="看图回答", mode="fresh_chat"),
    ):
        chunks.append(chunk.delta)

    assert "".join(chunks) == "这是根据图片得出的正式回答。"
    assert detector.completed_via_exact_action is True


@pytest.mark.asyncio
async def test_attachment_status_prefix_keeps_following_answer():
    """If one DOM snapshot contains both the progress row and final answer,
    discard only the UI prefix and retain the answer body."""
    detector, driver = _make_detector()
    poll = (
        '{"text":"正在分析 2 幅图片\\n这是正式回答。",'
        '"md_text":"这是正式回答。","html_len":130,"child_count":1,'
        '"has_action":true,"has_exact_action":true,"is_thinking":false,'
        '"generation_active":false}'
    )

    async def fake_js(expr):
        if "getBoundingClientRect" in expr:
            return poll
        if "body.innerText" in expr:
            return '{"text":""}'
        return "1"

    driver._js_strict = fake_js
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="")

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=10,
        turn_anchor=TurnAnchor(sent_text="看图回答", mode="fresh_chat"),
    ):
        chunks.append(chunk.delta)

    assert "".join(chunks) == "这是正式回答。"
    assert detector.completed_via_exact_action is True


@pytest.mark.parametrize("model", ["auto", "gpt-5-6-thinking"])
@pytest.mark.asyncio
async def test_reasoning_reference_image_uses_exact_dom_terminal_when_backend_lags(model):
    """A completed Pro/reference-image answer must not wait out the detector.

    Reasoning models hold mutable DOM text until terminal.  The attachment
    analysis placeholder may already share the current turn's action row, so
    it must remain filtered; once the real answer replaces it and the exact
    current-turn action is visible with no active generation, the stable DOM
    answer is authoritative even if the anchored backend projection is still
    ``not_ready``.
    """
    detector, driver = _make_detector()
    polls = [
        '{"text":"正在分析 1 幅图片","md_text":"正在分析 1 幅图片",'
        '"html_len":90,"child_count":1,"has_action":true,'
        '"has_exact_action":true,"is_thinking":false,'
        '"generation_active":false}',
        '{"text":"参考图中的主体颜色是深棕色。",'
        '"md_text":"参考图中的主体颜色是深棕色。","html_len":130,'
        '"child_count":1,"has_action":true,"has_exact_action":true,'
        '"is_thinking":false,"generation_active":false}',
    ]
    poll_index = 0

    async def fake_js(expr):
        nonlocal poll_index
        if "getBoundingClientRect" in expr:
            selected = polls[min(poll_index, len(polls) - 1)]
            poll_index += 1
            return selected
        if "body.innerText" in expr:
            return '{"text":""}'
        return "1"

    driver._current_conv_id = "conv-reference-image"
    driver._js_strict = fake_js
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=2,
        turn_anchor=TurnAnchor(sent_text="看参考图回答", mode="fresh_chat"),
        budgets=DetectorBudgets(
            first_content_timeout_seconds=1,
            stream_idle_timeout_seconds=1,
            hard_timeout_seconds=1,
        ),
        model=model,
        has_input_attachments=True,
    ):
        chunks.append(chunk.delta)

    assert "".join(chunks) == "参考图中的主体颜色是深棕色。"
    assert detector.last_dom_text == "参考图中的主体颜色是深棕色。"
    assert detector.completed_via_exact_action is True
    assert driver._fetch_end_turn_for_turn.await_count >= 1


@pytest.mark.asyncio
async def test_attachment_search_activity_survives_stop_button_flicker():
    """An exact action row is not terminal while a search/tool surface is live.

    The global Stop button can disappear between search batches.  Keep waiting
    on the sticky non-text/tool signal instead of promoting mutable narration.
    """
    detector, driver = _make_detector()
    polls = iter([
        '{"text":"正在搜索官方资料。","md_text":"","html_len":120,'
        '"child_count":2,"has_meaningful_non_text":true,'
        '"has_action":true,"has_exact_action":true,"is_thinking":false,'
        '"generation_active":true}',
        '{"text":"正在整理搜索结果。","md_text":"","html_len":140,'
        '"child_count":2,"has_meaningful_non_text":true,'
        '"has_action":true,"has_exact_action":true,"is_thinking":false,'
        '"generation_active":false}',
    ])

    async def fake_js(expr):
        if "getBoundingClientRect" in expr:
            return next(polls)
        if "body.innerText" in expr:
            return '{"text":""}'
        return "1"

    driver._current_conv_id = "conv-attachment-search"
    driver._js_strict = fake_js
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=0.75,
        turn_anchor=TurnAnchor(sent_text="结合附件搜索最新资料", mode="fresh_chat"),
        model="gpt-5-5-pro",
        has_input_attachments=True,
    ):
        chunks.append(chunk.delta)

    assert chunks == []
    assert detector.last_dom_text == ""
    assert detector.had_non_text_content is True
    assert detector.completed_via_exact_action is False


@pytest.mark.asyncio
async def test_attachment_expected_non_text_ignores_text_before_asset():
    """Image generation narration cannot satisfy attachment DOM promotion."""
    detector, driver = _make_detector()
    pre_asset_poll = (
        '{"text":"正在创建图片。","md_text":"正在创建图片。","html_len":90,'
        '"child_count":1,"has_meaningful_non_text":false,'
        '"has_action":true,"has_exact_action":true,"is_thinking":false,'
        '"generation_active":false}'
    )

    async def fake_js(expr):
        if "getBoundingClientRect" in expr:
            return pre_asset_poll
        if "body.innerText" in expr:
            return '{"text":""}'
        return "1"

    driver._current_conv_id = "conv-attachment-image-output"
    driver._js_strict = fake_js
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=0.1,
        turn_anchor=TurnAnchor(sent_text="参考附件生成图片", mode="fresh_chat"),
        model="gpt-5-6-thinking",
        expect_non_text=True,
        has_input_attachments=True,
    ):
        chunks.append(chunk.delta)

    assert chunks == []
    assert detector.last_dom_text == ""
    assert detector.had_non_text_content is False
    assert detector.completed_via_exact_action is False


@pytest.mark.asyncio
async def test_pro_search_progress_waits_for_backend_terminal_answer():
    """Mutable Pro web-search narration must never terminate or enter SSE."""
    detector, driver = _make_detector()
    process_poll = (
        '{"text":"我查一下官方最新说明。","md_text":"","html_len":90,'
        '"child_count":2,"has_action":true,"has_exact_action":true,'
        '"is_thinking":false,"generation_active":true}'
    )
    final_poll = (
        '{"text":"这是最终回答。","md_text":"这是最终回答。","html_len":120,'
        '"child_count":2,"has_action":true,"has_exact_action":true,'
        '"is_thinking":false,"generation_active":false}'
    )

    async def fake_js(expr):
        if "getBoundingClientRect" in expr:
            return (
                process_poll
                if driver._fetch_end_turn_for_turn.await_count < 1
                else final_poll
            )
        if "body.innerText" in expr:
            return '{"text":""}'
        return "1"

    driver._js_strict = fake_js
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="conv-pro-search")
    driver._fetch_end_turn_for_turn = AsyncMock(side_effect=[
        TurnEndResult(
            status="matched", diagnostic={"assistant_node": "a-preamble"},
        ),
        TurnEndResult(
            status="matched", diagnostic={"assistant_node": "a-final"},
        ),
        TurnEndResult(
            status="matched", diagnostic={"assistant_node": "a-final"},
        ),
    ])

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=10,
        turn_anchor=TurnAnchor(sent_text="查询最新说明", mode="fresh_chat"),
        model="gpt-5-5-pro",
    ):
        chunks.append(chunk.delta)

    assert chunks == []
    assert detector.last_dom_text == ""
    assert driver._fetch_end_turn_for_turn.await_count == 3


@pytest.mark.asyncio
async def test_broad_action_cannot_finish_before_conversation_id_exists():
    detector, driver = _make_detector()
    polls = iter([
        '{"text":"回答开头","md_text":"回答开头","html_len":80,'
        '"child_count":1,"has_action":true,"has_exact_action":false,'
        '"is_thinking":false,"generation_active":true}',
        '{"text":"回答开头，最终完成","md_text":"回答开头，最终完成","html_len":100,'
        '"child_count":1,"has_action":true,"has_exact_action":true,'
        '"is_thinking":false,"generation_active":false}',
    ])

    async def fake_js(expr):
        if "getBoundingClientRect" in expr:
            return next(polls)
        if "body.innerText" in expr:
            return '{"text":""}'
        return "1"

    driver._js_strict = fake_js
    driver._get_live_conversation_id_best_effort = AsyncMock(side_effect=["", "conv-1"])
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="not_ready"))

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=10,
        turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
    ):
        chunks.append(chunk.delta)

    assert "".join(chunks) == "回答开头，最终完成"
    assert detector.completed_via_exact_action is True


@pytest.mark.asyncio
async def test_expected_non_text_can_complete_from_backend_before_dom_node():
    detector, driver = _make_detector()

    async def fake_js(expr):
        if "innerText" in expr:
            return '{"text": ""}'
        return "0"  # no ordinary assistant DOM node ever appears

    driver._js_strict = fake_js
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="conv-image")
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="matched")
    )

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=360,
        turn_anchor=TurnAnchor(sent_text="生成一张图片", mode="fresh_chat"),
        expect_non_text=True,
    ):
        chunks.append(chunk)

    assert chunks == []
    assert detector.had_non_text_content is True
    driver._fetch_end_turn_for_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_expected_image_completes_from_new_lazy_asset_and_action_without_assistant_node():
    detector, driver = _make_detector()

    async def fake_js(expr):
        if "actionCount" in expr:
            return json.dumps({
                "generationActive": False,
                "actionCount": 2,
                "assets": [
                    {
                        "identity": "file_new",
                        "source_url": "https://chatgpt.com/backend-api/estuary/content?id=file_new",
                        "loaded": False,
                        "turnRole": "assistant",
                    }
                ],
            })
        if "image-turn-action-button" in expr:
            return "2"
        if "innerText" in expr:
            return '{"text": ""}'
        return "0"

    driver._js_strict = fake_js
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="")

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        initial_non_text_action_count=0,
        timeout=420,
        turn_anchor=TurnAnchor(sent_text="生成一张图片", mode="fresh_chat"),
        expect_non_text=True,
    ):
        chunks.append(chunk)

    assert chunks == []
    assert detector.had_non_text_content is True
    assert detector.non_text_dom_assets[0]["file_id"] == "file_new"
    driver._fetch_end_turn_for_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_expected_image_completes_from_new_loaded_estuary_asset():
    detector, driver = _make_detector()

    async def fake_js(expr):
        if "actionCount" in expr:
            return json.dumps({
                "generationActive": False,
                "actionCount": 0,
                "assets": [
                    {
                        "identity": "file_new",
                        "source_url": "https://chatgpt.com/backend-api/estuary/content?id=file_new",
                        "loaded": True,
                        "turnRole": "assistant",
                    }
                ],
            })
        if "image-turn-action-button" in expr:
            return "0"
        if "innerText" in expr:
            return '{"text": ""}'
        return "0"

    driver._js_strict = fake_js
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="")

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        initial_non_text_asset_ids=("file_old",),
        timeout=420,
        turn_anchor=TurnAnchor(sent_text="生成一张图片", mode="fresh_chat"),
        expect_non_text=True,
    ):
        chunks.append(chunk)

    assert chunks == []
    assert detector.had_non_text_content is True
    assert detector.non_text_dom_assets == [{
        "type": "image",
        "name": "generated-image-1.png",
        "mime_type": "image/png",
        "file_id": "file_new",
        "source_url": "https://chatgpt.com/backend-api/estuary/content?id=file_new",
    }]
    driver._fetch_end_turn_for_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_probe_excludes_two_user_references_and_keeps_assistant_result():
    """Two newly submitted references must not become generated outputs."""
    detector, driver = _make_detector()

    async def fake_js(expr):
        assert '[data-testid^="conversation-turn-"] img' not in expr
        assert '[data-message-author-role="assistant"] img' in expr
        return json.dumps({
            "generationActive": False,
            "actionCount": 1,
            "assets": [
                {
                    "identity": "file_reference_1",
                    "source_url": "https://chatgpt.com/backend-api/estuary/content?id=file_reference_1",
                    "loaded": True,
                    "turnRole": "user",
                },
                {
                    "identity": "file_reference_2",
                    "source_url": "https://chatgpt.com/backend-api/estuary/content?id=file_reference_2",
                    "loaded": True,
                    "turnRole": "user",
                },
                {
                    "identity": "file_generated",
                    "source_url": "https://chatgpt.com/backend-api/estuary/content?id=file_generated",
                    "loaded": True,
                    "turnRole": "assistant",
                },
            ],
        })

    driver._js_strict = fake_js
    state = await detector._probe_non_text_dom_state(driver)
    new_assets, completed_assets, action_increased = (
        detector._select_new_non_text_dom_assets(
            state,
            initial_action_count=0,
            initial_asset_ids=(),
        )
    )

    assert action_increased is True
    assert [asset["identity"] for asset in new_assets] == ["file_generated"]
    assert [asset["identity"] for asset in completed_assets] == ["file_generated"]
    assert detector._format_non_text_dom_assets(completed_assets) == [{
        "type": "image",
        "name": "generated-image-1.png",
        "mime_type": "image/png",
        "file_id": "file_generated",
        "source_url": "https://chatgpt.com/backend-api/estuary/content?id=file_generated",
    }]


# ── 5. Detector routes through self._driver (the seam) ───────────────


@pytest.mark.asyncio
async def test_detector_routes_js_through_driver():
    """The detector must call self._driver._js_strict, NOT a local copy — so
    driver-side monkeypatches (used across the test suite) still intercept."""
    detector, driver = _make_detector()
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))
    # Discriminate by JS expression: body scan -> JSON, count -> "1" (exceeds
    # initial_count=0 so Phase-1 breaks), poll -> completed payload WITH text so
    # the backend end_turn strict-content guard can fire and end the loop.
    scan = '{"text": ""}'
    poll = '{"text":"hi","md_text":"hi","html_len":60,"child_count":1,"has_action":false,"is_thinking":false}'
    js_calls = {"n": 0}

    async def fake_js(expr):
        js_calls["n"] += 1  # proof the detector reached transport via the driver
        if "getBoundingClientRect" in expr:  # Phase-2 poll (also contains innerText)
            return poll
        if "innerText" in expr:  # Phase-1 body scan
            return scan
        return "1"

    driver._js_strict = fake_js
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))
    # Provide conv_id so the backend end_turn primary signal is eligible and
    # the loop completes (otherwise no completion path → runs to timeout).
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="conv-1")
    seen = []
    async for chunk in detector.stream_until_complete(
        initial_count=0, timeout=10000,
        turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
    ):
        seen.append(chunk)
    assert js_calls["n"] >= 1, "detector must reach transport via _driver._js_strict"
    assert driver._fetch_end_turn_for_turn.await_count >= 1, (
        "detector must reach backend via _driver._fetch_end_turn_for_turn"
    )


# ── 6. No cdp_driver import at completion_detector module load ────────


def test_no_cdp_driver_import_at_module_load():
    """Circular-import rule: completion_detector must NOT import anything from
    cdp_driver at module top level (cdp_driver top-level re-exports symbols FROM
    completion_detector). Error classes / StreamChunk are imported lazily inside
    the method body. Verify by inspecting the module's source for a top-level
    cdp_driver import."""
    src = inspect.getsource(importlib_import_module("chatgpt_web2api.completion_detector"))
    # A `from .cdp_driver import` at column 0 (module level, not inside a def)
    # would be a circular-import violation. The lazy import is indented inside
    # stream_until_complete, so it does not start at column 0.
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("from .cdp_driver import") or stripped.startswith(
            "from chatgpt_web2api.cdp_driver import"
        ):
            assert line.startswith(" "), (
                f"cdp_driver import must be lazy (indented inside a method), "
                f"not top-level: {line!r}"
            )


# importlib shim imported lazily so the test file's own imports stay clean
def importlib_import_module(name):
    import importlib

    return importlib.import_module(name)
