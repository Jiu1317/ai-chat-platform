"""Behavioral tests for P1 phase-2 two-state detector machine.

These verify the core co-designed behavior (conversation 6a4ebc1e):

  1. Reasoning first-content wait does NOT fail at the old 90s default — the
     longer reasoning budget keeps the detector alive during the silent
     thinking phase.
  2. Stream-idle (text appeared then stopped) fails with its OWN budget,
     shorter than first-content.
  3. Hard cap wins over an active DOM liveness signal — no infinite waits.
  4. Final reconciliation: if the turn completed in the backend, the detector
     returns normally instead of raising (generation actually finished).
  5. Structured error: phase-2 stalls carry stall_kind, model_class, elapsed
     time, and liveness-signal fields.

Timing is controlled via a fake clock to keep tests deterministic and fast.
"""

import asyncio
import hashlib
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.completion_detector import (
    BACKEND_RATE_LIMIT_BACKOFF_INITIAL_SECONDS,
    BACKEND_RATE_LIMIT_BACKOFF_MAX_SECONDS,
    CompletionDetector,
    DetectorBudgets,
    _next_backend_rate_limit_backoff_seconds,
)
from chatgpt_web2api.turn_anchor import TurnAnchor, TurnEndResult


def _make_detector(budgets=None, conv_id="conv-1"):
    """Detector backed by a mock driver. budgets defaults to reasoning (the
    case the P1 fix targets)."""
    driver = MagicMock()
    driver._current_conv_id = conv_id
    driver._js_strict = AsyncMock(return_value="")
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="not_ready"))
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value=conv_id or "")
    detector = CompletionDetector(driver)
    if budgets is not None:
        detector._budgets_override = budgets
    return detector, driver


def _phase2_poll_payload(*, text="", md_text="", is_thinking=False,
                          has_action=False, html_len=0, child_count=0,
                          has_meaningful_non_text=False,
                          generation_active=False, has_exact_action=False,
                          has_error=False, current_assistant_present=False,
                          stop_visible=False, aria_busy=False):
    """Build the JSON the phase-2 poll JS returns."""
    return json.dumps({
        "text": text, "md_text": md_text, "html_len": html_len,
        "child_count": child_count, "has_action": has_action,
        "has_exact_action": has_exact_action,
        "is_thinking": is_thinking,
        "has_meaningful_non_text": has_meaningful_non_text,
        "generation_active": generation_active,
        "stop_visible": stop_visible,
        "aria_busy": aria_busy,
        "has_error": has_error,
        "current_assistant_present": current_assistant_present,
    })


class _ScriptedPoll:
    """A scripted _js_strict that returns pre-set payloads per phase.

    - body scan → always empty (no rate limit)
    - assistant count → always "1" (exceeds initial_count=0, exits phase-1)
    - phase-2 poll → call next() from a user-provided sequence
    """

    def __init__(self, poll_sequence):
        self.scan = '{"text": ""}'
        self.polls = list(poll_sequence)
        self._idx = 0

    async def __call__(self, expr):
        if "getBoundingClientRect" in expr:
            if self._idx < len(self.polls):
                return self.polls[self._idx]
            return self.polls[-1] if self.polls else _phase2_poll_payload()
        if "innerText" in expr:
            return self.scan
        return "1"


def _install_fast_clock(monkeypatch):
    t = [0.0]
    original_sleep = asyncio.sleep

    async def fast_sleep(delay):
        t[0] += delay
        await original_sleep(0)

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    return t


def _strict_end_result(
    node="a-final", text="red and blue", *, current_node=None,
    status="finished_successfully", recipient="all", finish_type="stop",
    children_count=0, strict_terminal=True,
):
    return TurnEndResult(status="matched", diagnostic={
        "assistant_node": node,
        "current_node": current_node or node,
        "status": status,
        "recipient": recipient,
        "finish_type": finish_type,
        "children_count": children_count,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_length": len(text),
        "strict_terminal": strict_terminal,
    })


# ── 1. Reasoning first-content does NOT fail at 90s ─────────────────────


@pytest.mark.asyncio
async def test_reasoning_first_content_survives_past_90s(monkeypatch):
    """The core P1 fix: a reasoning model thinking silently for >90s must NOT
    be falsely aborted. The reasoning first-content budget (300s default) keeps
    the detector alive. With the old uniform 90s, this would have raised."""
    budgets = DetectorBudgets.reasoning()  # first_content=300, stream_idle=120
    detector, driver = _make_detector(budgets=budgets)

    # Simulate: thinking indicator present, no text, for 100 polls (50s of
    # "thinking" — well past the old 90s ceiling IF the clock were real, but
    # we control the clock). We just need to confirm no stall raises at the
    # old threshold. Use a fast-forward clock.
    thinking_payload = _phase2_poll_payload(is_thinking=True)
    script = _ScriptedPoll([thinking_payload])
    driver._js_strict = script

    # Fast-forward clock: control time.monotonic so 100s of "thinking" passes
    # in real-time milliseconds. This proves the 90s boundary is gone for
    # reasoning first-content.
    t = [0.0]
    original_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d  # each 0.5s poll advances simulated time
        await original_sleep(0)  # don't actually wait

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    # timeout is the overall wall-clock — set high enough that it doesn't fire
    # before the first-content budget.
    async def drain():
        async for _ in detector.stream_until_complete(
            initial_count=0, timeout=500,
            turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
            budgets=budgets,
        ):
            pass

    # Run for a bounded number of events then cancel — we just need to confirm
    # it does NOT raise within the first 100s of simulated thinking. Each poll
    # advances simulated time by 0.5s; let it run ~200 polls (100s simulated)
    # via real-time yielding (no actual sleeping — fast_sleep does original_sleep(0)).
    task = asyncio.create_task(drain())
    # Let the task run enough event-loop ticks to advance simulated time past 90s.
    # Each poll = one sleep(0) yield, so we need ~200 polls = ~200 iterations.
    for _ in range(300):
        await original_sleep(0)
        if t[0] > 100:
            break
    # If the detector raised, task.exception() will hold it.
    exc = task.exception() if task.done() else None
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    if exc and "GenerationStuckError" in type(exc).__name__:
        pytest.fail(f"Reasoning model falsely aborted during thinking phase: {exc}")

    # Confirm simulated time advanced past the old 90s ceiling without abort
    assert t[0] > 90, f"Expected simulated time >90s, got {t[0]}"


# ── 1b. Empty streaming shell is NOT first content ─────────────────────


@pytest.mark.asyncio
async def test_empty_streaming_shell_stays_in_first_content_state(monkeypatch):
    """A live ChatGPT response first creates a sizeable ``result-streaming``
    wrapper whose text and ``pre`` are still empty.  Its HTML length must not
    move the detector to the shorter stream-idle state or mark the response as
    non-text content.

    This is the exact live failure shape observed on 2026-09-11: assistant
    container present, ``aria-busy=true``, visible Stop button, zero answer
    characters, and more than 50 characters of wrapper HTML.
    """
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    budgets = DetectorBudgets(
        first_content_timeout_seconds=5,
        stream_idle_timeout_seconds=1,
        hard_timeout_seconds=100,
    )
    detector, driver = _make_detector(budgets=budgets, conv_id="")
    driver._js_strict = _ScriptedPoll([
        _phase2_poll_payload(
            text="",
            md_text="",
            html_len=120,
            child_count=1,
            has_meaningful_non_text=False,
            generation_active=True,
        )
    ])

    t = [0.0]
    original_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d
        await original_sleep(0)

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    with pytest.raises(GenerationStuckError) as exc_info:
        async for _ in detector.stream_until_complete(
            initial_count=0,
            timeout=50,
            turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
            budgets=budgets,
            model="auto",
        ):
            pass

    assert exc_info.value.stall_kind == "first_content_timeout"
    assert detector.had_non_text_content is False
    assert t[0] >= budgets.first_content_timeout_seconds
    assert t[0] > budgets.stream_idle_timeout_seconds


# ── 2. Stream-idle fails with its own (shorter) budget ──────────────────


@pytest.mark.asyncio
async def test_stream_idle_uses_shorter_budget_after_first_content(monkeypatch):
    """Once text has appeared and then stopped progressing, the stream-idle
    budget applies — which is shorter than first-content for reasoning models.
    Verify the stall_kind is 'stream_idle_timeout', not 'first_content_timeout'."""
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    budgets = DetectorBudgets(
        first_content_timeout_seconds=300,  # long
        stream_idle_timeout_seconds=5,       # short — will fire fast
        hard_timeout_seconds=900,
    )
    detector, driver = _make_detector(budgets=budgets)

    # First poll: text appears (exits first-content state). Then: text stops.
    polls = [
        _phase2_poll_payload(text="Hello", md_text="Hello"),
        _phase2_poll_payload(text="Hello", md_text="Hello"),  # no progress
    ] * 50  # repeat to keep polling
    script = _ScriptedPoll(polls)
    driver._js_strict = script
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )

    t = [0.0]
    original_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d
        await original_sleep(0)

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    with pytest.raises(GenerationStuckError) as exc_info:
        async for _ in detector.stream_until_complete(
            initial_count=0, timeout=500,
            turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
            budgets=budgets,
        ):
            pass

    # The stall should be classified as stream_idle (text appeared then stopped)
    assert exc_info.value.phase == "phase_2_stream"
    assert getattr(exc_info.value, "stall_kind", None) == "stream_idle_timeout", (
        f"Expected stall_kind='stream_idle_timeout', got {getattr(exc_info.value, 'stall_kind', None)}"
    )


@pytest.mark.asyncio
async def test_stable_latest_dom_completes_when_action_and_backend_miss(monkeypatch):
    """A finished latest assistant must not become a false stream-idle error.

    The broad action flag intentionally models the observed stale user-copy
    match. It is not accepted early: completion occurs only at the idle budget
    after the latest assistant text is continuously stable and inactive.
    """
    budgets = DetectorBudgets(
        first_content_timeout_seconds=30,
        stream_idle_timeout_seconds=3,
        hard_timeout_seconds=100,
    )
    detector, driver = _make_detector(budgets=budgets)
    driver._js_strict = _ScriptedPoll([
        _phase2_poll_payload(
            text="红色和蓝色。",
            md_text="红色和蓝色。",
            html_len=120,
            child_count=1,
            has_action=True,
            has_exact_action=False,
            generation_active=False,
            current_assistant_present=True,
        )
    ])
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )
    t = _install_fast_clock(monkeypatch)

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=100,
        turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
        budgets=budgets,
        model="auto",
    ):
        chunks.append(chunk.delta)

    assert chunks == ["红色和蓝色。"]
    assert t[0] > budgets.stream_idle_timeout_seconds
    assert detector.completed_via_stable_dom is True
    assert detector.completed_via_exact_action is False


@pytest.mark.asyncio
async def test_stable_reasoning_dom_emits_held_terminal_text_once(monkeypatch):
    """Held reasoning text becomes one final append-only delta at fallback."""
    budgets = DetectorBudgets(
        first_content_timeout_seconds=3,
        stream_idle_timeout_seconds=3,
        hard_timeout_seconds=100,
    )
    detector, driver = _make_detector(budgets=budgets)
    driver._js_strict = _ScriptedPoll([
        _phase2_poll_payload(
            text="红色和蓝色。",
            md_text="红色和蓝色。",
            html_len=120,
            child_count=1,
            generation_active=False,
            current_assistant_present=True,
        )
    ])
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )
    _install_fast_clock(monkeypatch)

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=100,
        turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
        budgets=budgets,
        model="gpt-5-6-thinking",
        has_input_attachments=True,
    ):
        chunks.append(chunk.delta)

    assert chunks == ["红色和蓝色。"]
    assert detector.last_dom_text == "红色和蓝色。"
    assert detector.completed_via_stable_dom is True


@pytest.mark.parametrize(
    "poll",
    [
        pytest.param(
            _phase2_poll_payload(
                text="partial",
                generation_active=True,
                current_assistant_present=True,
            ),
            id="still-generating",
        ),
        pytest.param(
            _phase2_poll_payload(current_assistant_present=True),
            id="empty-response",
        ),
        pytest.param(
            _phase2_poll_payload(
                text="Something went wrong",
                has_error=True,
                current_assistant_present=True,
            ),
            id="official-page-error",
        ),
        pytest.param(
            _phase2_poll_payload(text="stale prior answer"),
            id="missing-latest-assistant",
        ),
    ],
)
@pytest.mark.asyncio
async def test_stable_dom_fallback_rejects_non_terminal_shapes(
    monkeypatch, poll,
):
    """Active, empty, error, and non-current DOM shapes remain real stalls."""
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    budgets = DetectorBudgets(
        first_content_timeout_seconds=3,
        stream_idle_timeout_seconds=3,
        hard_timeout_seconds=100,
    )
    detector, driver = _make_detector(budgets=budgets)
    driver._js_strict = _ScriptedPoll([poll])
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )
    _install_fast_clock(monkeypatch)

    with pytest.raises(GenerationStuckError):
        async for _ in detector.stream_until_complete(
            initial_count=0,
            timeout=100,
            turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
            budgets=budgets,
            model="auto",
        ):
            pass

    assert detector.completed_via_stable_dom is False


@pytest.mark.asyncio
async def test_reasoning_strict_backend_terminal_ignores_stale_aria_busy(monkeypatch):
    """Three anchored final projections beat sticky aria-busy well before 90s."""
    budgets = DetectorBudgets(
        first_content_timeout_seconds=90,
        stream_idle_timeout_seconds=90,
        hard_timeout_seconds=120,
    )
    detector, driver = _make_detector(budgets=budgets)
    driver._js_strict = _ScriptedPoll([
        _phase2_poll_payload(
            is_thinking=True,
            generation_active=True,
            aria_busy=True,
            stop_visible=False,
            current_assistant_present=True,
        )
    ])
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=_strict_end_result()
    )
    t = _install_fast_clock(monkeypatch)

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=120,
        turn_anchor=TurnAnchor(
            sent_text="test",
            mode="captured_id",
            captured_user_message_id="u-current",
        ),
        budgets=budgets,
        model="gpt-5-6-thinking",
        has_input_attachments=True,
    ):
        chunks.append(chunk.delta)

    assert chunks == []
    assert driver._fetch_end_turn_for_turn.await_count == 3
    assert 6 <= t[0] < 15


@pytest.mark.asyncio
async def test_default_model_strict_backend_terminal_completes_without_dom_text(
    monkeypatch,
):
    """Captured default-model turns can finish from strict backend text alone."""
    budgets = DetectorBudgets(
        first_content_timeout_seconds=90,
        stream_idle_timeout_seconds=90,
        hard_timeout_seconds=120,
    )
    detector, driver = _make_detector(budgets=budgets)
    driver._js_strict = _ScriptedPoll([
        _phase2_poll_payload(current_assistant_present=True)
    ])
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=_strict_end_result(text="multimodal final")
    )
    t = _install_fast_clock(monkeypatch)

    chunks = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=120,
        turn_anchor=TurnAnchor(
            sent_text="test",
            mode="captured_id",
            captured_user_message_id="u-current",
        ),
        budgets=budgets,
        model="auto",
        has_input_attachments=True,
    ):
        chunks.append(chunk.delta)

    assert chunks == []
    assert driver._fetch_end_turn_for_turn.await_count == 3
    assert 6 <= t[0] < 15


@pytest.mark.asyncio
async def test_tool_preamble_resets_before_three_final_confirmations(monkeypatch):
    """A preamble must not borrow confirmations from the later final node."""
    budgets = DetectorBudgets(
        first_content_timeout_seconds=40,
        stream_idle_timeout_seconds=40,
        hard_timeout_seconds=60,
    )
    detector, driver = _make_detector(budgets=budgets)
    driver._js_strict = _ScriptedPoll([
        _phase2_poll_payload(
            is_thinking=True,
            generation_active=True,
            aria_busy=True,
            stop_visible=False,
            current_assistant_present=True,
        )
    ])
    driver._fetch_end_turn_for_turn = AsyncMock(side_effect=[
        _strict_end_result(node="a-preamble", text="I'll check that."),
        _strict_end_result(node="a-preamble", text="I'll check that."),
        TurnEndResult(
            status="not_ready",
            diagnostic={
                "reason": "text_end_turn_not_current_node",
                "assistant_node": "a-preamble",
                "current_node": "tool-1",
            },
        ),
        _strict_end_result(node="a-final", text="final answer"),
        _strict_end_result(node="a-final", text="final answer"),
        _strict_end_result(node="a-final", text="final answer"),
    ])
    t = _install_fast_clock(monkeypatch)

    async for _ in detector.stream_until_complete(
        initial_count=0,
        timeout=60,
        turn_anchor=TurnAnchor(
            sent_text="search",
            mode="captured_id",
            captured_user_message_id="u-current",
        ),
        budgets=budgets,
        model="gpt-5-6-thinking",
    ):
        pass

    assert driver._fetch_end_turn_for_turn.await_count == 6
    assert 18 <= t[0] < 30


@pytest.mark.asyncio
async def test_changed_backend_text_hash_restarts_strict_confirmation(monkeypatch):
    budgets = DetectorBudgets(
        first_content_timeout_seconds=40,
        stream_idle_timeout_seconds=40,
        hard_timeout_seconds=60,
    )
    detector, driver = _make_detector(budgets=budgets)
    driver._js_strict = _ScriptedPoll([
        _phase2_poll_payload(current_assistant_present=True)
    ])
    driver._fetch_end_turn_for_turn = AsyncMock(side_effect=[
        _strict_end_result(text="draft"),
        _strict_end_result(text="draft"),
        _strict_end_result(text="final"),
        _strict_end_result(text="final"),
        _strict_end_result(text="final"),
    ])
    t = _install_fast_clock(monkeypatch)

    async for _ in detector.stream_until_complete(
        initial_count=0,
        timeout=60,
        turn_anchor=TurnAnchor(
            sent_text="test",
            mode="captured_id",
            captured_user_message_id="u-current",
        ),
        budgets=budgets,
        model="auto",
    ):
        pass

    assert driver._fetch_end_turn_for_turn.await_count == 5
    assert 15 <= t[0] < 25


@pytest.mark.parametrize(
    ("case", "poll_kwargs", "captured", "expect_non_text", "end_result"),
    [
        (
            "missing-metadata", {}, True, False,
            TurnEndResult(
                status="matched",
                diagnostic={"assistant_node": "a-final"},
            ),
        ),
        (
            "no-captured-id", {"aria_busy": True}, False, False,
            _strict_end_result(),
        ),
        (
            "stop-visible", {"stop_visible": True}, True, False,
            _strict_end_result(),
        ),
        (
            "official-error", {"has_error": True}, True, False,
            _strict_end_result(),
        ),
        (
            "expected-non-text", {}, True, True,
            _strict_end_result(),
        ),
    ],
)
@pytest.mark.asyncio
async def test_unsafe_backend_evidence_never_uses_fast_path(
    monkeypatch, case, poll_kwargs, captured, expect_non_text, end_result,
):
    """Unsafe evidence may reconcile at timeout, but never before it."""
    budgets = DetectorBudgets(
        first_content_timeout_seconds=8,
        stream_idle_timeout_seconds=8,
        hard_timeout_seconds=20,
    )
    detector, driver = _make_detector(budgets=budgets)
    driver._js_strict = _ScriptedPoll([
        _phase2_poll_payload(
            current_assistant_present=True,
            **poll_kwargs,
        )
    ])
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=end_result)
    t = _install_fast_clock(monkeypatch)
    anchor = TurnAnchor(
        sent_text=case,
        mode="captured_id" if captured else "fresh_chat",
        captured_user_message_id="u-current" if captured else None,
    )

    async for _ in detector.stream_until_complete(
        initial_count=0,
        timeout=20,
        turn_anchor=anchor,
        budgets=budgets,
        model="gpt-5-6-thinking",
        expect_non_text=expect_non_text,
    ):
        pass

    assert t[0] > budgets.first_content_timeout_seconds, case


@pytest.mark.asyncio
async def test_fetch_failure_between_matches_restarts_confirmation(monkeypatch):
    budgets = DetectorBudgets(
        first_content_timeout_seconds=40,
        stream_idle_timeout_seconds=40,
        hard_timeout_seconds=60,
    )
    detector, driver = _make_detector(budgets=budgets)
    driver._js_strict = _ScriptedPoll([
        _phase2_poll_payload(current_assistant_present=True)
    ])
    driver._fetch_end_turn_for_turn = AsyncMock(side_effect=[
        _strict_end_result(),
        TurnEndResult(status="fetch_failed", diagnostic={"error": "reset"}),
        _strict_end_result(),
        _strict_end_result(),
        _strict_end_result(),
    ])
    t = _install_fast_clock(monkeypatch)

    async for _ in detector.stream_until_complete(
        initial_count=0,
        timeout=60,
        turn_anchor=TurnAnchor(
            sent_text="test",
            mode="captured_id",
            captured_user_message_id="u-current",
        ),
        budgets=budgets,
        model="auto",
    ):
        pass

    assert driver._fetch_end_turn_for_turn.await_count == 5
    assert 15 <= t[0] < 25


def test_backend_429_backoff_is_exponential_and_bounded():
    delay = 0.0
    observed = []
    for _ in range(5):
        delay = _next_backend_rate_limit_backoff_seconds(delay)
        observed.append(delay)

    assert observed == [15.0, 30.0, 60.0, 120.0, 120.0]
    assert observed[0] == BACKEND_RATE_LIMIT_BACKOFF_INITIAL_SECONDS
    assert max(observed) == BACKEND_RATE_LIMIT_BACKOFF_MAX_SECONDS


@pytest.mark.parametrize("raise_exception", [False, True])
@pytest.mark.asyncio
async def test_phase1_backend_429_honors_exponential_cooldown(
    monkeypatch, raise_exception
):
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    budgets = DetectorBudgets(
        first_content_timeout_seconds=40,
        stream_idle_timeout_seconds=40,
        hard_timeout_seconds=45,
    )
    detector, driver = _make_detector(budgets=budgets)

    async def phase1_only_dom(expr):
        if "innerText" in expr:
            return '{"text": ""}'
        if "actionCount" in expr:
            return json.dumps({
                "generationActive": True,
                "actionCount": 0,
                "assets": [],
            })
        return "0"

    driver._js_strict = phase1_only_dom
    call_times = []

    async def rate_limited(*args, **kwargs):
        call_times.append(time.monotonic())
        if raise_exception:
            raise RuntimeError("projection HTTP 429")
        return TurnEndResult(
            status="fetch_failed",
            diagnostic={"error": "projection status 429"},
        )

    driver._fetch_end_turn_for_turn = AsyncMock(side_effect=rate_limited)
    _install_fast_clock(monkeypatch)

    with pytest.raises(GenerationStuckError) as exc_info:
        async for _ in detector.stream_until_complete(
            initial_count=0,
            timeout=45,
            turn_anchor=TurnAnchor(
                sent_text="merge these images",
                mode="captured_id",
                captured_user_message_id="u-current",
            ),
            budgets=budgets,
            model="gpt-5-6-thinking",
            expect_non_text=True,
            has_input_attachments=True,
        ):
            pass

    assert exc_info.value.phase == "phase_1_appear"
    assert len(call_times) == 2
    assert call_times[1] - call_times[0] >= 15.0


@pytest.mark.asyncio
async def test_backend_429_is_not_polled_every_three_seconds(monkeypatch):
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    budgets = DetectorBudgets(
        first_content_timeout_seconds=40,
        stream_idle_timeout_seconds=40,
        hard_timeout_seconds=45,
    )
    detector, driver = _make_detector(budgets=budgets)
    driver._js_strict = _ScriptedPoll([
        _phase2_poll_payload(current_assistant_present=True)
    ])
    call_times = []

    async def rate_limited(*args, **kwargs):
        call_times.append(time.monotonic())
        return TurnEndResult(
            status="fetch_failed",
            diagnostic={"error": "projection HTTP 429"},
        )

    driver._fetch_end_turn_for_turn = AsyncMock(side_effect=rate_limited)
    _install_fast_clock(monkeypatch)

    with pytest.raises(GenerationStuckError):
        async for _ in detector.stream_until_complete(
            initial_count=0,
            timeout=45,
            turn_anchor=TurnAnchor(
                sent_text="merge these images",
                mode="captured_id",
                captured_user_message_id="u-current",
            ),
            budgets=budgets,
            model="gpt-5-6-thinking",
            has_input_attachments=True,
        ):
            pass

    # A three-second loop would make about 13 calls in this window. Two normal
    # probes must occur (rather than passing vacuously on zero/one call), while
    # the next exponential retry remains beyond the 40-second stall.
    assert len(call_times) == 2
    assert call_times[1] - call_times[0] >= 15.0


@pytest.mark.asyncio
async def test_stall_does_not_bypass_active_429_cooldown(monkeypatch):
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    budgets = DetectorBudgets(
        first_content_timeout_seconds=4,
        stream_idle_timeout_seconds=4,
        hard_timeout_seconds=10,
    )
    detector, driver = _make_detector(budgets=budgets)
    driver._js_strict = _ScriptedPoll([
        _phase2_poll_payload(current_assistant_present=True)
    ])
    call_times = []

    async def rate_limited(*args, **kwargs):
        call_times.append(time.monotonic())
        return TurnEndResult(
            status="fetch_failed",
            diagnostic={"error": "HTTP 429 too many requests"},
        )

    driver._fetch_end_turn_for_turn = AsyncMock(side_effect=rate_limited)
    _install_fast_clock(monkeypatch)

    with pytest.raises(GenerationStuckError):
        async for _ in detector.stream_until_complete(
            initial_count=0,
            timeout=10,
            turn_anchor=TurnAnchor(
                sent_text="merge these images",
                mode="captured_id",
                captured_user_message_id="u-current",
            ),
            budgets=budgets,
            model="auto",
        ):
            pass

    # The scheduled fetch lands just before the stall. Final reconciliation
    # must not immediately issue a second request inside its 15-second delay.
    assert call_times == [3.5]


@pytest.mark.asyncio
async def test_valid_non_429_response_resets_429_backoff(monkeypatch):
    budgets = DetectorBudgets(
        first_content_timeout_seconds=45,
        stream_idle_timeout_seconds=45,
        hard_timeout_seconds=50,
    )
    detector, driver = _make_detector(budgets=budgets)
    _install_fast_clock(monkeypatch)

    async def clock_driven_dom(expr):
        if "getBoundingClientRect" in expr:
            return _phase2_poll_payload(
                text="merged result",
                current_assistant_present=True,
            )
        if "innerText" in expr:
            return '{"text": ""}'
        return "1"

    driver._js_strict = clock_driven_dom
    call_times = []
    results = [
        TurnEndResult(
            status="fetch_failed",
            diagnostic={"error": "HTTP 429"},
        ),
        TurnEndResult(status="not_ready"),
        TurnEndResult(
            status="fetch_failed",
            diagnostic={"error": "status 429"},
        ),
        _strict_end_result(text="merged result"),
    ]

    async def sequenced_backend(*args, **kwargs):
        call_times.append(time.monotonic())
        return results[min(len(call_times) - 1, len(results) - 1)]

    driver._fetch_end_turn_for_turn = AsyncMock(side_effect=sequenced_backend)

    async for _ in detector.stream_until_complete(
        initial_count=0,
        timeout=50,
        turn_anchor=TurnAnchor(
            sent_text="merge these images",
            mode="captured_id",
            captured_user_message_id="u-current",
        ),
        budgets=budgets,
        model="auto",
    ):
        pass

    # The strict terminal fixture needs later confirmations; only the first
    # four calls define the reset sequence under test.
    assert len(call_times) >= 4
    assert call_times[1] - call_times[0] >= 15.0
    assert 3.0 < call_times[2] - call_times[1] < 4.0
    assert 15.0 <= call_times[3] - call_times[2] < 16.0


@pytest.mark.asyncio
async def test_exact_current_turn_action_completes_during_429_backoff(monkeypatch):
    budgets = DetectorBudgets(
        first_content_timeout_seconds=40,
        stream_idle_timeout_seconds=40,
        hard_timeout_seconds=45,
    )
    detector, driver = _make_detector(budgets=budgets)
    t = _install_fast_clock(monkeypatch)

    async def clock_driven_dom(expr):
        if "getBoundingClientRect" in expr:
            return _phase2_poll_payload(
                text="merged result",
                current_assistant_present=True,
                # A broad old action appears first; only the exact current-turn
                # action that appears later may complete while rate limited.
                has_action=t[0] >= 4.0,
                has_exact_action=t[0] >= 6.0,
            )
        if "innerText" in expr:
            return '{"text": ""}'
        return "1"

    driver._js_strict = clock_driven_dom
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(
        status="fetch_failed",
        diagnostic={"error": "HTTP 429"},
    ))
    chunks = []

    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=45,
        turn_anchor=TurnAnchor(
            sent_text="merge these images",
            mode="captured_id",
            captured_user_message_id="u-current",
        ),
        budgets=budgets,
        model="gpt-5-6-thinking",
        has_input_attachments=True,
    ):
        chunks.append(chunk.delta)

    assert t[0] >= 6.0
    assert t[0] < 18.5
    assert driver._fetch_end_turn_for_turn.await_count == 1
    assert chunks == ["merged result"]
    assert detector.completed_via_exact_action is True


@pytest.mark.asyncio
async def test_same_detector_next_stream_inherits_active_429_cooldown(monkeypatch):
    budgets = DetectorBudgets(
        first_content_timeout_seconds=20,
        stream_idle_timeout_seconds=20,
        hard_timeout_seconds=25,
    )
    detector, driver = _make_detector(budgets=budgets)
    t = _install_fast_clock(monkeypatch)
    call_times = []

    async def rate_limited(*args, **kwargs):
        call_times.append(time.monotonic())
        return TurnEndResult(
            status="fetch_failed",
            diagnostic={"error": "HTTP 429"},
        )

    driver._fetch_end_turn_for_turn = AsyncMock(side_effect=rate_limited)

    async def first_stream_dom(expr):
        if "getBoundingClientRect" in expr:
            return _phase2_poll_payload(
                text="first answer",
                current_assistant_present=True,
                has_action=t[0] >= 4.0,
                has_exact_action=t[0] >= 4.0,
            )
        if "innerText" in expr:
            return '{"text": ""}'
        return "1"

    driver._js_strict = first_stream_dom
    async for _ in detector.stream_until_complete(
        initial_count=0,
        timeout=25,
        turn_anchor=TurnAnchor(
            sent_text="first",
            mode="captured_id",
            captured_user_message_id="u-first",
        ),
        budgets=budgets,
        model="gpt-4o",
    ):
        pass

    assert call_times == [3.5]
    assert t[0] < detector._backend_retry_not_before
    second_exact_at = t[0] + 1.0

    async def second_stream_dom(expr):
        if "getBoundingClientRect" in expr:
            return _phase2_poll_payload(
                text="second answer",
                current_assistant_present=True,
                has_action=t[0] >= second_exact_at,
                has_exact_action=t[0] >= second_exact_at,
            )
        if "innerText" in expr:
            return '{"text": ""}'
        return "1"

    driver._js_strict = second_stream_dom
    async for _ in detector.stream_until_complete(
        initial_count=0,
        timeout=25,
        turn_anchor=TurnAnchor(
            sent_text="second",
            mode="captured_id",
            captured_user_message_id="u-second",
        ),
        budgets=budgets,
        model="gpt-4o",
    ):
        pass

    assert t[0] == second_exact_at
    assert t[0] < detector._backend_retry_not_before
    assert call_times == [3.5]


@pytest.mark.asyncio
async def test_next_image_stream_phase1_inherits_429_cooldown_without_final_probe(
    monkeypatch,
):
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    first_budgets = DetectorBudgets(
        first_content_timeout_seconds=20,
        stream_idle_timeout_seconds=20,
        hard_timeout_seconds=25,
    )
    detector, driver = _make_detector(budgets=first_budgets)
    t = _install_fast_clock(monkeypatch)
    call_times = []

    async def rate_limited(*args, **kwargs):
        call_times.append(time.monotonic())
        return TurnEndResult(
            status="fetch_failed",
            diagnostic={"error": "HTTP 429"},
        )

    driver._fetch_end_turn_for_turn = AsyncMock(side_effect=rate_limited)

    async def first_stream_dom(expr):
        if "getBoundingClientRect" in expr:
            return _phase2_poll_payload(
                text="first answer",
                current_assistant_present=True,
                has_action=t[0] >= 4.0,
                has_exact_action=t[0] >= 4.0,
            )
        if "innerText" in expr:
            return '{"text": ""}'
        return "1"

    driver._js_strict = first_stream_dom
    async for _ in detector.stream_until_complete(
        initial_count=0,
        timeout=25,
        turn_anchor=TurnAnchor(
            sent_text="first",
            mode="captured_id",
            captured_user_message_id="u-first",
        ),
        budgets=first_budgets,
        model="gpt-4o",
    ):
        pass

    assert call_times == [3.5]
    assert t[0] < detector._backend_retry_not_before

    async def second_phase1_only_dom(expr):
        if "innerText" in expr:
            return '{"text": ""}'
        if "actionCount" in expr:
            return json.dumps({
                "generationActive": True,
                "actionCount": 0,
                "assets": [],
            })
        return "0"

    driver._js_strict = second_phase1_only_dom
    second_budgets = DetectorBudgets(
        first_content_timeout_seconds=4,
        stream_idle_timeout_seconds=4,
        hard_timeout_seconds=10,
    )
    with pytest.raises(GenerationStuckError) as exc_info:
        async for _ in detector.stream_until_complete(
            initial_count=0,
            timeout=10,
            turn_anchor=TurnAnchor(
                sent_text="merge these images",
                mode="captured_id",
                captured_user_message_id="u-second",
            ),
            budgets=second_budgets,
            model="gpt-5-6-thinking",
            expect_non_text=True,
            has_input_attachments=True,
        ):
            pass

    assert exc_info.value.phase == "phase_1_appear"
    assert t[0] < detector._backend_retry_not_before
    # Phase 1 must neither poll during the inherited cooldown nor issue a
    # last-second reconciliation when its appearance budget expires.
    assert call_times == [3.5]


# ── 3. Hard cap wins over active DOM signal ─────────────────────────────


@pytest.mark.asyncio
async def test_hard_cap_wins_over_dom_liveness(monkeypatch):
    """Even if the DOM shows a thinking/generating indicator forever, the hard
    cap must eventually fire. No infinite waits."""
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    budgets = DetectorBudgets(
        first_content_timeout_seconds=300,
        stream_idle_timeout_seconds=300,
        hard_timeout_seconds=10,  # very short hard cap
    )
    detector, driver = _make_detector(budgets=budgets)

    # DOM shows thinking indicator forever, never produces text
    script = _ScriptedPoll([_phase2_poll_payload(is_thinking=True)])
    driver._js_strict = script

    t = [0.0]
    original_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d
        await original_sleep(0)

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    with pytest.raises(GenerationStuckError):
        async for _ in detector.stream_until_complete(
            initial_count=0, timeout=500,
            turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
            budgets=budgets,
        ):
            pass

    # The hard cap (10s simulated) should have fired, not the first-content (300s)
    assert t[0] >= 10, f"Hard cap should have fired around 10s, got t={t[0]}"
    assert t[0] < 300, "first-content budget should NOT have been the one to fire"


# ── 4. Final reconciliation success returns normally ────────────────────


@pytest.mark.asyncio
async def test_final_reconciliation_success_returns_normally(monkeypatch):
    """When the phase-2 stall fires but final reconciliation finds the turn DID
    complete in the backend, the detector returns normally instead of raising.
    This is the field-verified case: 'generation completed after detector gave up.'"""
    budgets = DetectorBudgets(
        first_content_timeout_seconds=5,  # short to trigger stall fast
        stream_idle_timeout_seconds=5,
        hard_timeout_seconds=900,
    )
    detector, driver = _make_detector(budgets=budgets)

    # Phase-2: no text, no thinking indicator → stall fires quickly.
    script = _ScriptedPoll([_phase2_poll_payload()])
    driver._js_strict = script

    t = [0.0]
    original_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d
        await original_sleep(0)

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    # Final reconciliation: backend says the turn completed
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="matched")
    )

    chunks = []
    # This should NOT raise — reconciliation finds the completed turn.
    async for chunk in detector.stream_until_complete(
        initial_count=0, timeout=500,
        turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
        budgets=budgets,
    ):
        chunks.append(chunk)

    # No exception — the detector returned normally after reconciliation.
    # (If we got here, the test passes. If reconciliation had failed, a
    # GenerationStuckError would have been raised.)


# ── 5. Structured error fields ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_structured_stall_error_has_diagnostic_fields(monkeypatch):
    """Phase-2 stall errors carry structured fields for observability:
    stall_kind, model_class, elapsed_seconds, generation_active_signal."""
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    budgets = DetectorBudgets(
        first_content_timeout_seconds=5,
        stream_idle_timeout_seconds=5,
        hard_timeout_seconds=900,
    )
    detector, driver = _make_detector(budgets=budgets)

    # No text, no thinking, no progress → first_content_timeout stall
    script = _ScriptedPoll([_phase2_poll_payload()])
    driver._js_strict = script
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )

    t = [0.0]
    original_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d
        await original_sleep(0)

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    with pytest.raises(GenerationStuckError) as exc_info:
        async for _ in detector.stream_until_complete(
            initial_count=0, timeout=500,
            turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
            budgets=budgets,
            model="gpt-5-5-thinking",
        ):
            pass

    err = exc_info.value
    assert err.phase == "phase_2_stream"
    assert getattr(err, "stall_kind", None) == "first_content_timeout"
    assert getattr(err, "model_class", None) == "reasoning"
    assert hasattr(err, "elapsed_seconds"), "error must carry elapsed_seconds"
    assert hasattr(err, "generation_active_signal"), "error must carry generation_active_signal"
