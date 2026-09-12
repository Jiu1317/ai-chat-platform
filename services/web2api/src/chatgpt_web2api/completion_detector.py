"""Streaming completion detection — Phase-1 appear loop + Phase-2 stream loop.

Phase 5 PR4 extraction (no behavior change). Owns the generation-completion
detection surface that was previously inlined in ``CDPDriver.send_and_stream``:

  - stall window constant (``PHASE_STALL_SECONDS``)
  - rate-limit pop-up phrase matchers (``_RATE_LIMIT_PHRASES``,
    ``is_rate_limited_text``)
  - Phase-1 appear loop: wait for a new assistant node, fail fast on a
    rate-limit pop-up, raise ``GenerationStuckError`` on a true stall
  - Phase-2 stream loop: poll the DOM for streamed text, yield deltas as
    ``StreamChunk``s, detect completion via the backend ``end_turn`` primary
    signal with the per-turn action button as a DOM fallback, raise
    ``GenerationStuckError`` on a three-signal stall

The driver-reference collaborator seam: ``CompletionDetector`` holds a
reference to its owning ``CDPDriver`` and reaches through it for the CDP
transport (``_js_strict``), the backend completion signals
(``_fetch_end_turn_for_turn`` / ``_get_live_conversation_id_best_effort``),
and the in-flight conversation id (``_current_conv_id``, read-only). No state
migrates into this module — it stays on the driver, and the detector is
stateless beyond ``_driver``.

Boundary: this module is the generation-completion detection layer.
``send_and_stream`` orchestration (pre-count, type/send, the post-loop
conversation-id resolve + ``_current_conv_id`` mutation, the
``_fetch_text_for_turn`` final reconcile, and the terminal
``finish_reason="stop"`` chunk) stays in ``cdp_driver.py``. The detector
yields **deltas only** — it never emits a
``finish_reason`` and never writes ``_current_conv_id``.

Per-call result surfacing: the driver's post-loop tail consumes two values the
Phase-2 loop accumulates as locals — ``last_dom_text`` (the streamed-text
baseline used to emit the ``_fetch_text_for_turn`` suffix delta) and
``had_non_text_content`` (drives the non-text placeholder). They cannot be
re-derived without re-running the poll, so after the generator exhausts the
driver reads ``self._completion.last_dom_text`` /
``self._completion.had_non_text_content``. These are transient per-call results
(reset at the start of each call), not long-lived configuration; the detector
holds no state across calls beyond ``_driver``.

Call-rule inside CompletionDetector method bodies — every internal call routes
through ``self._driver`` (NOT ``self``) to preserve monkeypatch interception on
the driver-facing seam (the PR2 lesson):

  transport:       self._driver._js_strict(...)
  backend signals: self._driver._fetch_end_turn_for_turn(...)
                   self._driver._get_live_conversation_id_best_effort(...)
  conv-id read:    self._driver._current_conv_id   (read once into a local;
                   NEVER assigned here)

Circular-import rule (mandatory): ``cdp_driver`` top-level re-exports
``PHASE_STALL_SECONDS`` and ``is_rate_limited_text`` from this module, so this
module must NOT import anything from ``cdp_driver`` at module load. The error
classes (``RateLimitError`` / ``GenerationStuckError``) and the ``StreamChunk``
yield type are imported **lazily inside ``stream_until_complete``**, after
``cdp_driver`` is fully initialized — mirroring the ``chatgpt_dom`` convention
(``chatgpt_dom.py`` lazy-imports ``CDPJSError``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .cdp_driver import StreamChunk
    from .config import ChatGPTConfig

logger = logging.getLogger(__name__)


# A generation is considered "stuck" (vs. merely slow) if no DOM progress
# signal occurs within this window. Slow-but-progressing generations
# (image rendering, deep-research thinking) legitimately exceed this and
# are allowed the full timeout; a true stall fails fast here instead of
# hanging silently to the deadline. Applied to both Phase 1 (node appear)
# and Phase 2 (text streaming). 90s accommodates reasoning/thinking models,
# whose ``result-thinking`` placeholder can hold the DOM static for a minute+
# while the model reasons before the first answer token renders; the
# is_thinking reset covers the labeled phase, but there is an unlabeled gap
# between thinking-end and answer-start that also needs this headroom.
#
# P1 (2026-07-08): this constant is now the FALLBACK for phase_1_appear
# (unchanged behavior) and the default-class phase-2 budgets when no
# DetectorBudgets is resolved. Phase-2 detection has been refactored into a
# model-aware two-state machine (awaiting_first_content →
# streaming_after_first_content) with budgets from DetectorBudgets. See
# classify_model / DetectorBudgets below. Kept for back-compat (re-exported
# from cdp_driver; referenced by phase_1_appear which is deliberately
# unchanged).
PHASE_STALL_SECONDS = 90

# A missing backend ``end_turn`` or action-button signal may leave an already
# completed assistant turn in the stream-idle path. Before accepting the final
# DOM as a terminal fallback, require the same non-empty text while every known
# generation signal is continuously inactive for this grace window. This
# rejects ordinary Stop-button flicker without extending the idle budget.
DOM_TERMINAL_STABILITY_SECONDS = 2.0

# A current, metadata-complete backend node can safely override a stale DOM
# aria-busy/thinking flag, but only after several fresh projections agree.
# With the normal 3-second backend poll cadence this is three observations
# spanning at least six seconds.
STRICT_TERMINAL_CONFIRMATIONS = 3
STRICT_TERMINAL_STABILITY_SECONDS = 6.0


def append_only_delta(emitted: str, observed: str) -> str:
    """Return only a safe append-only suffix for an SSE text stream.

    ChatGPT occasionally re-renders the tail of a message: the current DOM
    text may briefly shrink or replace characters that were already emitted.
    SSE cannot retract earlier bytes, so slicing solely by the previous DOM
    length duplicates the rewritten tail (for example ``么？么？``).  Only
    extend the client stream when the new snapshot has the exact emitted text
    as a prefix.  Final backend reconciliation applies the same rule.
    """
    if observed.startswith(emitted):
        return observed[len(emitted):]
    return ""


_TRANSIENT_STATUS_RE = re.compile(
    r"^\s*(?:(?:(?:gpt[-\w.]*)\s+)?pro\s+)?"
    r"(?:thinking|reasoning|正在思考|思考中|正在推理|推理中|考え中)"
    r"\s*(?:[.…·]{1,3})?\s*$",
    re.IGNORECASE,
)

_ATTACHMENT_STATUS_BODY = (
    r"(?:正在(?:分析|读取|阅读|处理|上传)\s*"
    r"(?:[0-9一二三四五六七八九十两]+\s*)?(?:幅|张|个|份)?\s*"
    r"(?:图片|图像|文件|附件)"
    r"|(?:analy[sz]ing|reading|processing|uploading)\s*"
    r"(?:\d+\s*)?(?:images?|files?|attachments?))"
)

_ATTACHMENT_STATUS_RE = re.compile(
    rf"^\s*{_ATTACHMENT_STATUS_BODY}\s*(?:[.…·]{{1,3}})?\s*$",
    re.IGNORECASE,
)

_ATTACHMENT_UI_PREFIX_RE = re.compile(
    rf"^\s*{_ATTACHMENT_STATUS_BODY}\s*(?:[.…·]{{1,3}})?\s*\r?\n+",
    re.IGNORECASE,
)

_REASONING_UI_PREFIX_RE = re.compile(
    r"^\s*(?:(?:thinking|reasoning)\s*[.…·]{1,3}"
    r"|(?:thought|reasoned)\s+for[^\r\n]*"
    r"|(?:(?:(?:gpt[-\w.]*)\s+)?pro\s+)"
    r"(?:thinking|reasoning|正在思考|思考中|正在推理|推理中|考え中)\s*[.…·]{0,3}"
    r"|(?:正在思考|思考中|正在推理|推理中|考え中)\s*[.…·]{1,3})"
    r"\s*(?:\r?\n+|\s+)",
    re.IGNORECASE,
)


def is_transient_status_text(text: str) -> bool:
    """Return true for a localized, stand-alone progress placeholder.

    The ChatGPT DOM can temporarily expose UI copy such as ``正在思考`` as the
    assistant node's ``innerText`` before the real markdown answer exists. The
    same happens after an attachment is submitted (for example
    ``正在分析 2 幅图片``). That copy is UI state, not model output, and must
    never be emitted to an append-only SSE client because the later answer
    replaces it in-place.
    """
    value = text or ""
    return bool(
        _TRANSIENT_STATUS_RE.fullmatch(value)
        or _ATTACHMENT_STATUS_RE.fullmatch(value)
    )


def strip_reasoning_ui_prefix(text: str) -> str:
    """Remove a leading transient UI label from DOM ``innerText``.

    The pattern deliberately requires dots, a localized status token, or a
    ``Thought for ...`` summary so a real answer beginning with a word such as
    ``Thinking`` is not stripped. Attachment-analysis labels are stripped only
    when followed by a newline and real content; normal prose such as
    ``正在分析图片内容，结论是……`` is preserved.
    """
    value = _REASONING_UI_PREFIX_RE.sub("", text or "", count=1)
    return _ATTACHMENT_UI_PREFIX_RE.sub("", value, count=1)


# ── P1: model-aware detector budgets ─────────────────────────────────────
#
# Co-designed with ChatGPT (conversation 6a4ebc1e, 2026-07-08). The single
# PHASE_STALL_SECONDS=90 conflated two different states: "model hasn't
# produced visible text yet" (reasoning models think silently for 30-60s+)
# and "model was streaming text and then stopped" (a genuine hang). These
# should NOT share a stall budget. Reasoning models stress case 1; real
# network/UI hangs stress case 2.
#
# The fix: split phase-2 into first-content-wait vs stream-idle, with
# model-aware budgets on first-content specifically. DOM "thinking" signals
# are advisory liveness hints (generation_active_signal) within the hard
# cap — never authoritative clock-pauses.

# Slugs that indicate a reasoning-capable model. These models have a long
# silent "thinking" phase before the first answer token renders, which the
# old uniform 90s stall window falsely aborted. Matched case-insensitively
# against the slug via substring. "thinking" covers gpt-5-*-thinking and
# gpt-5-*-t-mini (Thinking Mini); "o1"/"o3"/"o4" are the o-series reasoning
# families; "research" is Deep Research; "reasoning" is a catch-all in case
# OpenAI introduces slugs that name it directly. Inclusive on purpose — a
# false negative (reasoning model gets the shorter default budget) is worse
# than a false positive (non-reasoning model gets the longer budget).
_REASONING_MARKERS = (
    "thinking", "-t-mini", "-pro", "o1", "o3", "o4", "research", "reasoning",
)


def classify_model(model: str | None) -> str:
    """Classify a ChatGPT web model slug as ``"reasoning"`` or ``"default"``.

    Reasoning models (gpt-5-*-thinking, o3, research, Thinking Mini variants)
    have a long silent thinking phase before streaming text and need a longer
    first-content stall budget. ``auto`` is also conservative in the direction
    of correctness: the ChatGPT router may choose a reasoning model even though
    the caller cannot see that resolved slug. An otherwise unknown or empty
    model remains ``"default"`` so a possibly-dead generation fails fast.

    The classification is intentionally coarse (two buckets), not per-model.
    Per-model tuning can wait until field data proves the buckets insufficient.
    """
    if not model:
        return "default"
    lowered = model.lower()
    if lowered == "auto":
        return "reasoning"
    if any(marker in lowered for marker in _REASONING_MARKERS):
        return "reasoning"
    return "default"


@dataclass(frozen=True)
class DetectorBudgets:
    """Per-call detector timeout budgets, resolved from config + model class.

    - ``first_content_timeout_seconds``: how long to wait for the FIRST text
      content after the assistant node appears. Longer for reasoning models
      (they think silently before streaming).
    - ``stream_idle_timeout_seconds``: how long to wait for PROGRESS once text
      has already appeared and then stopped. Shorter than first-content — once
      streaming started, a long idle is suspicious.
    - ``hard_timeout_seconds``: absolute wall-clock cap on phase-2 observation,
      regardless of DOM liveness signals. Prevents infinite waits even if the
      UI claims it is still thinking.
    """

    first_content_timeout_seconds: float
    stream_idle_timeout_seconds: float
    hard_timeout_seconds: float

    @classmethod
    def default(cls) -> DetectorBudgets:
        """Default (non-reasoning) budgets — reproduces the legacy 90s behavior."""
        return cls(
            first_content_timeout_seconds=90,
            stream_idle_timeout_seconds=90,
            hard_timeout_seconds=900,
        )

    @classmethod
    def reasoning(cls) -> DetectorBudgets:
        """Reasoning-model budgets — longer first-content, shorter stream-idle."""
        return cls(
            first_content_timeout_seconds=300,
            stream_idle_timeout_seconds=120,
            hard_timeout_seconds=900,
        )

    @classmethod
    def from_config(cls, config: ChatGPTConfig, model: str | None) -> DetectorBudgets:
        """Resolve budgets from ChatGPTConfig + the classified model.

        Reads the 5 detector config keys. If the model classifies as reasoning,
        uses the reasoning first-content/stream-idle budgets; otherwise default.
        The hard cap is shared across both classes.
        """
        if classify_model(model) == "reasoning":
            return cls(
                first_content_timeout_seconds=config.detector_reasoning_first_content_timeout_seconds,
                stream_idle_timeout_seconds=config.detector_reasoning_stream_idle_timeout_seconds,
                hard_timeout_seconds=config.detector_hard_timeout_seconds,
            )
        return cls(
            first_content_timeout_seconds=config.detector_default_first_content_timeout_seconds,
            stream_idle_timeout_seconds=config.detector_default_stream_idle_timeout_seconds,
            hard_timeout_seconds=config.detector_hard_timeout_seconds,
        )

# Phrases ChatGPT uses in its rate-limit pop-up. Matched case-insensitively
# against scanned DOM text. Kept narrow to avoid false positives on normal
# chat content (e.g. a user asking about "rate limits" in a message).
_RATE_LIMIT_PHRASES = (
    "too many requests",
    "you're making requests too quickly",
    "temporarily limited access to your conversations",
    "you've reached the rate limit",
)


def is_rate_limited_text(text: str) -> bool:
    """Return True if *text* looks like ChatGPT's rate-limit pop-up copy."""
    if not text:
        return False
    lowered = text.lower()
    return any(phrase in lowered for phrase in _RATE_LIMIT_PHRASES)


class CompletionDetector:
    """Generation-completion detection, composed by ``CDPDriver``.

    Constructed once in ``CDPDriver.__init__`` and stored as
    ``self._completion``. Exposes a single delta-only async sub-generator;
    the driver re-yields its chunks verbatim (no buffering / post-processing)
    so the public ``send_and_stream`` yield sequence is byte-equivalent to
    pre-extraction.

    Stateless beyond ``_driver`` across calls — every loop variable is a method
    local. Per-call results (``last_dom_text`` / ``had_non_text_content`` /
    ``non_text_dom_assets``) are exposed as instance attributes for the
    driver's post-loop tail to read; they are reset at the start of each call
    and carry no state between calls.
    """

    def __init__(self, driver) -> None:
        self._driver = driver
        # Per-call results surfaced for the driver tail; reset each call.
        self.last_dom_text: str = ""
        self.had_non_text_content: bool = False
        self.completed_via_exact_action: bool = False
        self.completed_via_stable_dom: bool = False
        self.non_text_dom_assets: list[dict] = []

    async def _probe_non_text_dom_state(self, d) -> dict:
        """Read global image completion evidence for either detector phase.

        ChatGPT may remove the short-lived ordinary assistant message wrapper
        before an image turn finishes.  The durable surfaces are assistant-turn
        image URLs, the per-image feedback-action count, and the visible Stop
        button.  Keep this single probe shared by Phase 1 and Phase 2 so both
        apply identical identity and completion rules.  User-turn images are
        deliberately excluded because newly submitted reference images are not
        present in the pre-send baseline and must never be returned as output.
        """
        from .cdp_driver import CDPJSError

        try:
            raw = await d._js_strict(
                "(function(){"
                " var visible=function(el){return !!(el && (el.offsetParent!==null || el.getClientRects().length));};"
                " var stop=[].slice.call(document.querySelectorAll('[data-testid=\"stop-button\"], button[aria-label*=\"Stop\" i], button[aria-label*=\"停止\"]')).find(visible);"
                " var actionCount=document.querySelectorAll('[data-testid=\"good-image-turn-action-button\"], [data-testid=\"bad-image-turn-action-button\"]').length;"
                " var seen={},assets=[];"
                " document.querySelectorAll('[data-message-author-role=\"assistant\"] img, section[data-turn=\"assistant\"] img').forEach(function(img){"
                "   var src=img.currentSrc||img.src||'';"
                "   if(src.indexOf('/backend-api/estuary/content')<0 && src.indexOf('/backend-api/files/')<0)return;"
                "   try{var u=new URL(src,location.href),id=u.searchParams.get('id')||u.pathname;"
                "     if(id&&!seen[id]){seen[id]=true;assets.push({identity:id,source_url:u.href,loaded:!!(img.complete&&img.naturalWidth>0),turnRole:'assistant'});}}catch(e){}"
                " });"
                " return JSON.stringify({generationActive:!!stop,actionCount:actionCount,assets:assets});"
                "})()"
            )
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise TypeError("invalid image asset probe result")
            assets = state.get("assets", [])
            if not isinstance(assets, list):
                assets = []
            assets = [
                asset for asset in assets
                if isinstance(asset, dict) and asset.get("turnRole") == "assistant"
            ]
            return {
                "generation_active": state.get("generationActive") is True,
                "action_count": int(state.get("actionCount", -1)),
                "assets": assets,
            }
        except (CDPJSError, json.JSONDecodeError, TypeError, ValueError):
            return {
                "generation_active": False,
                "action_count": -1,
                "assets": [],
            }

    @staticmethod
    def _select_new_non_text_dom_assets(
        state: dict,
        *,
        initial_action_count: int,
        initial_asset_ids: tuple[str, ...],
    ) -> tuple[list[dict], list[dict], bool]:
        """Return ``(all_new, completion_ready, action_increased)`` assets.

        A fully loaded new image is independently usable.  A lazy-loaded image
        (``img.complete=false``/``naturalWidth=0``) is usable only when a new
        image feedback action proves the current image turn completed; the
        authenticated downloader validates the bytes afterward.  Every asset
        must still be absent from the pre-send identity baseline.
        """
        baseline_ids = set(initial_asset_ids)
        action_increased = int(state.get("action_count", -1)) > initial_action_count
        new_assets = [
            asset for asset in state.get("assets", [])
            if isinstance(asset, dict)
            and str(asset.get("identity") or "")
            and str(asset.get("identity") or "") not in baseline_ids
            and str(asset.get("source_url") or "").startswith(("http://", "https://"))
        ]
        completion_ready = [
            asset for asset in new_assets
            if asset.get("loaded") is True or action_increased
        ]
        return new_assets, completion_ready, action_increased

    @staticmethod
    def _format_non_text_dom_assets(assets: list[dict]) -> list[dict]:
        """Convert validated DOM probe records to response-asset records."""
        return [
            {
                "type": "image",
                "name": f"generated-image-{index}.png",
                "mime_type": "image/png",
                "file_id": str(asset.get("identity") or "")
                if str(asset.get("identity") or "").startswith("file_")
                else "",
                "source_url": str(asset["source_url"]),
            }
            for index, asset in enumerate(assets, start=1)
        ]

    async def _reconcile_before_stall(
        self, d, conv_id: str, turn_anchor, had_non_text_content: bool,
    ) -> bool:
        """P1: final reconciliation before raising a phase-2 stall.

        Field evidence shows the generation often completes after the detector
        would have given up (the stall kills the *observation*, not the
        generation). Before raising, check the backend one more time: if the
        turn completed, the detector should return normally rather than
        surfacing a false failure.

        Returns True if the turn completed (caller returns normally), False if
        not (caller raises). Safe by design — this is an OBSERVATION read, not
        a re-send; it cannot duplicate the user message. Leverages the A2
        turn-anchoring work to correlate against the correct turn.

        On any fetch failure or exception, returns False (let the stall raise)
        rather than degrading to a silent success. EXCEPT auth expiry: that
        must surface as auth expiry, not degrade to a generic stall (PR #39
        review finding #2 invariant — auth failure never degrades).
        """
        from .cdp_driver import AuthExpiredError
        from .turn_anchor import collapse_to_end_turn_status

        if not conv_id:
            return False
        try:
            end_result = await d._fetch_end_turn_for_turn(
                conv_id, turn_anchor,
                had_non_text_content=had_non_text_content,
            )
            status = collapse_to_end_turn_status(end_result)
            return status == "complete"
        except AuthExpiredError:
            raise  # never swallow auth expiry — it must surface as auth expiry
        except Exception as e:
            logger.debug("Final reconciliation fetch failed: %s", e)
            return False

    async def stream_until_complete(
        self,
        *,
        initial_count: int,
        timeout: float,
        turn_anchor,
        budgets: DetectorBudgets | None = None,
        model: str | None = None,
        expect_non_text: bool = False,
        has_input_attachments: bool = False,
        initial_non_text_action_count: int = 0,
        initial_non_text_asset_ids: tuple[str, ...] = (),
    ) -> AsyncIterator[StreamChunk]:
        """Run Phase-1 (appear) + Phase-2 (stream) and yield delta chunks.

        Yields ``StreamChunk(delta=...)`` as new streamed text arrives. Does
        NOT emit a terminal ``finish_reason`` chunk — that is the driver
        shell's responsibility after this generator returns. Returns (stops
        iterating) once generation is detected complete (backend ``end_turn``
        primary, action-button fallback, or a stall raises
        ``GenerationStuckError``).

        A2: ``turn_anchor`` is required. The backend ``end_turn`` completion
        signal is turn-correlated via ``_fetch_end_turn_for_turn`` (tri-state).
        The DOM
        ``has_action`` fallback gate is unchanged — ``backend_fetch_failed`` is
        set ONLY on ``fetch_failed`` (true transport failure), NOT on
        ``not_ready``/``ambiguous``/``degraded_not_fresh`` (which collapse to
        ``not_ready`` and must NOT unlock the DOM fallback).

        P1 (2026-07-08): ``budgets`` and ``model`` enable the model-aware
        two-state phase-2 machine. When ``budgets`` is None, the legacy
        behavior (single PHASE_STALL_SECONDS=90 for both phases) is preserved
        for back-compat. When provided, phase-2 splits into
        awaiting_first_content (first_content_timeout_seconds budget) and
        streaming_after_first_content (stream_idle_timeout_seconds budget),
        with a hard_timeout_seconds absolute cap. DOM thinking/generating
        signals are advisory liveness hints (generation_active_signal) within
        the hard cap — never authoritative clock-pauses. On phase-2 stall, a
        final reconciliation is attempted before raising: if the backend
        reports the turn completed, the detector returns normally instead of
        raising (the generation actually finished).
        """
        # Imported lazily to avoid a module-load circular dependency: cdp_driver
        # top-level re-exports PHASE_STALL_SECONDS / is_rate_limited_text from
        # this module, so this module must not import cdp_driver at load time.
        from .cdp_driver import (
            AuthExpiredError,
            CDPJSError,
            GenerationStuckError,
            RateLimitError,
            StreamChunk,
        )
        from .turn_anchor import collapse_to_end_turn_status

        d = self._driver

        # P1: resolve the model class for structured error reporting.
        model_class = classify_model(model) if model else "default"
        # Pro/reasoning turns expose mutable progress narration in the same
        # assistant DOM node while tools and web search are still running. It
        # is later replaced by the final answer, so it cannot be safely sent to
        # an append-only SSE client. Hold DOM text for these models until the
        # backend reports the anchored terminal text; final reconciliation then
        # emits that stable answer.
        hold_dom_text_until_terminal = model_class == "reasoning"
        # P1: budgets default to legacy behavior when not provided (back-compat).
        use_two_state = budgets is not None

        # Reset per-call results surfaced to the driver tail.
        self.last_dom_text = ""
        self.had_non_text_content = False
        self.completed_via_exact_action = False
        self.completed_via_stable_dom = False
        self.non_text_dom_assets = []

        # Wait for a new assistant message. The full `timeout` governs (was
        # capped at 60s, which killed slow-to-appear responses like image
        # generation). A stall detector (PHASE_STALL_SECONDS) catches a true
        # hang fast: if the assistant node count doesn't change at all — even
        # 0→1 with empty text counts as progress — for longer than the stall
        # window, we raise GenerationStuckError instead of waiting out the
        # whole deadline. Slow-but-progressing generations (image render,
        # deep-research thinking) keep resetting the stall clock and are
        # allowed the full timeout.
        deadline = time.monotonic() + timeout
        last_node_count = initial_count
        last_progress = time.monotonic()
        phase_1_backend_check = 0.0
        phase_1_conv_id = ""
        phase_1_stall_budget = (
            budgets.first_content_timeout_seconds if budgets is not None
            else PHASE_STALL_SECONDS
        )
        while time.monotonic() < deadline:
            # First check for ChatGPT's rate-limit pop-up — if present, fail
            # fast with a clear error instead of waiting out the whole timeout.
            # The pop-up blocks the assistant from responding, so the assistant
            # count would never increase; without this check we'd hit a generic
            # timeout that hides the real cause.
            try:
                dom_scan = await d._js_strict(
                    "(function(){"
                    "  var t = (document.body && document.body.innerText) || '';"
                    "  return JSON.stringify({text: t.slice(0, 4000)});"
                    "})()"
                )
                scanned_text = json.loads(dom_scan).get("text", "")
            except (CDPJSError, json.JSONDecodeError, TypeError):
                scanned_text = ""
            if is_rate_limited_text(scanned_text):
                # from_text parses any explicit wait from the pop-up copy.
                raise RateLimitError.from_text(scanned_text)

            try:
                raw = await d._js_strict(
                    "document.querySelectorAll('[data-message-author-role=\"assistant\"]').length"
                )
                current_count = int(raw or 0)
            except CDPJSError:
                current_count = last_node_count  # no progress signal
            if current_count != last_node_count:
                # Any node-count change is progress (incl. 0→1 with empty text,
                # the slow-render case). Reset the stall clock.
                last_node_count = current_count
                last_progress = time.monotonic()
            if current_count > initial_count:
                break

            # Generated images don't necessarily live under an element with
            # data-message-author-role="assistant". Probe the durable global
            # conversation-turn surface and compare it with both pre-send
            # baselines. A fully loaded new URL can complete independently;
            # a lazy-loaded URL additionally requires a newly appeared image
            # action and an inactive generation signal. The authenticated
            # downloader validates the bytes after this detector returns.
            if expect_non_text:
                non_text_state = await self._probe_non_text_dom_state(d)
                _, completed_assets, action_increased = (
                    self._select_new_non_text_dom_assets(
                        non_text_state,
                        initial_action_count=initial_non_text_action_count,
                        initial_asset_ids=initial_non_text_asset_ids,
                    )
                )
                if (
                    completed_assets
                    and not non_text_state.get("generation_active")
                ):
                    self.non_text_dom_assets = self._format_non_text_dom_assets(
                        completed_assets
                    )
                    self.had_non_text_content = True
                    logger.info(
                        "Completed generated image asset appeared in Phase 1 "
                        "(%d new, action_increased=%s)",
                        len(completed_assets),
                        action_increased,
                    )
                    return

            # Image/tool responses can complete entirely in backend nodes
            # without ever creating the ordinary assistant DOM wrapper.  When
            # the caller explicitly expects non-text output, poll the anchored
            # backend turn and finish as soon as end_turn is authoritative.
            # This is read-only and cannot duplicate the submitted message.
            now = time.monotonic()
            if expect_non_text and now - phase_1_backend_check >= 3.0:
                phase_1_backend_check = now
                try:
                    if not phase_1_conv_id:
                        phase_1_conv_id = await d._get_live_conversation_id_best_effort()
                    if phase_1_conv_id:
                        end_result = await d._fetch_end_turn_for_turn(
                            phase_1_conv_id,
                            turn_anchor,
                            had_non_text_content=True,
                        )
                        if collapse_to_end_turn_status(end_result) == "complete":
                            self.had_non_text_content = True
                            logger.info(
                                "Backend non-text turn completed before assistant DOM appeared: %s",
                                phase_1_conv_id,
                            )
                            return
                except AuthExpiredError:
                    raise
                except Exception as e:
                    logger.debug("Phase-1 backend completion probe failed: %s", e)

            if now - last_progress > phase_1_stall_budget:
                raise GenerationStuckError("phase_1_appear", time.monotonic() - last_progress)
            await asyncio.sleep(0.5)
        else:
            raise GenerationStuckError("phase_1_appear", timeout)

        logger.info(
            "Assistant response container appeared, waiting for content/completion..."
        )

        # Poll until generation is done (Stop button gone). A stall detector
        # (PHASE_STALL_SECONDS) catches a stuck generation: if NO DOM progress
        # occurs for longer than the stall window, we raise GenerationStuckError.
        #
        # Progress is tracked on THREE signals, not just text, so non-text
        # responses (images, tool-use, code interpreter) don't falsely stall:
        #   - text:       .markdown textContent (streamed as deltas for text)
        #   - html_len:   assistant message innerHTML length (grows when img/
        #                 canvas/tool-use elements are added)
        #   - child_count: direct children count (grows when new blocks render)
        # Any of these changing resets the stall clock.
        #
        # Done detection: Stop button gone AND there's meaningful content
        # (either answer text or an explicit media/tool-result element).  Raw
        # HTML length is only a liveness signal: current ChatGPT creates a
        # >50-character ``result-streaming`` shell before producing any answer,
        # so treating the wrapper itself as content incorrectly switches the
        # detector from first-content wait to stream-idle.
        # ``last_dom_text`` is deliberately the exact text already emitted to
        # the SSE client. ``last_observed_text`` tracks mutable DOM snapshots
        # separately so a temporary contraction/rewrite cannot make a later
        # length-based slice append duplicate punctuation or words.
        last_dom_text = ""
        last_observed_text = ""
        last_html_len = 0
        last_child_count = 0
        last_meaningful_non_text = False
        had_non_text_content = False
        # saw_thinking: has the model shown a reasoning phase this turn? Used
        # to unlock the R4 backend fallback DURING thinking (when last_dom_text
        # is empty) — without this, a long reasoning response (>90s think time
        # with no DOM text change) stalls before the answer ever streams.
        saw_thinking = False
        # Completion detection for Phase-2. The history here matters — three
        # earlier signals each failed in live testing, all producing an
        # off-by-one where request N returned request N-1's text:
        #   1. ``done = !stopBtn && hasContent`` — broke on the FIRST poll.
        #      Right after send the Stop button hasn't appeared yet (generation
        #      not begun) but html_len > 50 (the message wrapper), so this was
        #      True immediately, leaving last_dom_text empty.
        #   2. ``generation_started && not is_generating`` — the Stop button
        #      FLICKERS off between token batches, breaking mid-generation with
        #      truncated text.
        #   3. Text-stability alone — ``.markdown`` textContent is empty during
        #      streaming (text renders elsewhere until the turn settles), so
        #      "stable empty" never completes and the stall detector fires.
        #
        # The robust signal is the per-turn ACTION BUTTON. ChatGPT renders a
        # copy/feedback action row (data-testid containing "copy" or
        # "response-turn") on an assistant message ONLY once it has finished
        # generating — it is absent while the message is streaming or thinking.
        # Polling for that button on the NEW message is immune to the Stop
        # flicker and to the empty-.markdown-during-streaming quirk. Text is
        # captured from the message's innerText (which IS populated during
        # streaming) rather than .markdown textContent (which lags).
        last_change_time = time.monotonic()
        deadline = time.monotonic() + timeout
        # P1: two-state phase-2 machine. phase_2_start tracks total observation
        # time for the hard cap; first_content_seen tracks whether we've
        # transitioned from awaiting_first_content to streaming_after_first_content.
        # The active stall budget depends on this state: first-content uses
        # budgets.first_content_timeout_seconds (longer for reasoning models that
        # think silently); stream-idle uses budgets.stream_idle_timeout_seconds.
        phase_2_start = time.monotonic()
        first_content_seen = False
        generation_active_signal = False  # advisory liveness (DOM thinking/generating)
        # Backend end_turn fallback throttle (R4): if the DOM action-button
        # selector drifts again, the conversation API's end_turn flag is a
        # secondary completion signal. Throttled to once per 3s to respect the
        # shared account rate budget, and only fires when has_action is false
        # (so it never races the primary DOM signal). Never the sole signal.
        last_backend_check = 0.0
        # A Pro/tool turn may briefly expose an end_turn=true preamble before
        # the tool chain and final answer are appended.  The strict proof
        # below therefore binds repeated observations to the same branch tip
        # and final-text hash rather than trusting a bare end_turn flag.
        strict_terminal_signature: tuple | None = None
        strict_terminal_confirmations = 0
        strict_terminal_first_seen: float | None = None
        conv_id_for_check = d._current_conv_id or ""
        last_global_non_text_asset_ids: set[str] = set()
        # Mid-loop conv_id probe throttle. On a NEW chat (REST /health path or
        # the SSE/MCP path) _current_conv_id is None here and conv_id_for_check
        # is "" — which silently disables the backend end_turn fallback below
        # (its guard is ``conv_id_for_check and ...``). That fallback is the
        # stable completion signal when the DOM action-button selector drifts.
        # ChatGPT navigates to /c/{id} within ~1s of send, so we probe the live
        # URL (cheap) until a conv_id is available, then the existing backend
        # check can fire. See _get_live_conversation_id_best_effort.
        last_conv_id_probe = 0.0
        stable_terminal_text = ""
        stable_terminal_since: float | None = None
        while time.monotonic() < deadline:
            try:
                result = await d._js_strict(
                    "(function() {"
                    "  var msgs = document.querySelectorAll('[data-message-author-role=\"assistant\"]');"
                    f"  var initialCount = {max(0, int(initial_count))};"
                    # Phase 1 may observe a short-lived assistant shell and then
                    # enter this loop just as ChatGPT removes it.  Never fall
                    # back to the pre-send last assistant: its text and action
                    # row belong to the previous request.
                    "  if (msgs.length <= initialCount) return JSON.stringify({text:'', md_text:'', html_len:0, child_count:0, has_meaningful_non_text:false, has_action:false, has_exact_action:false, has_error:false, is_thinking:false, stop_visible:false, aria_busy:false, generation_active:false, assistant_count:msgs.length, current_assistant_present:false});"
                    "  var last = msgs[msgs.length - 1];"
                    # Text: the clean answer lives in ``.markdown`` textContent.
                    # It's empty during streaming and populates as the turn
                    # settles — so we ALSO capture ``innerText`` (populated
                    # during streaming) as a fallback. innerText includes the
                    # reasoning UI label ("Thinking.../Thought for N seconds"),
                    # so md_text is captured SEPARATELY and Python prefers it;
                    # the innerText fallback is trimmed of the leading label.
                    "  var markdownNodes = Array.prototype.slice.call(last.querySelectorAll('.markdown'));"
                    "  var finalMarkdown = markdownNodes.filter(function(el) {"
                    "    return !el.closest('.result-thinking, [data-testid*=\"thinking\"], [data-testid*=\"reasoning\"]');"
                    "  });"
                    "  var md = finalMarkdown.length ? finalMarkdown[finalMarkdown.length - 1] : null;"
                    "  var mdText = md ? (md.textContent || '') : '';"
                    "  var rawText = (last.innerText || '').trim();"
                    # Strip a leading "Thinking..." / "Thought for …" reasoning
                    # label so the innerText fallback can't leak it as a delta.
                    "  var text = rawText;"
                    "  var html_len = last.innerHTML.length;"
                    "  var child_count = last.children.length;"
                    # Do not infer content from ``innerHTML.length``.  An empty
                    # live response currently looks like
                    # ``<div aria-busy=true class=result-streaming><pre></pre>``
                    # and is already well above the old 50-character threshold.
                    # Require an explicit media/file/tool-result surface instead.
                    '  var NON_TEXT = \'img[src], video[src], audio[src], canvas, '
                    'a[href*="/backend-api/files/"], a[href^="sandbox:"], '
                    '[data-testid*="artifact"], [data-testid*="tool-result"], '
                    '[data-testid*="code-interpreter"]\';'
                    "  var has_meaningful_non_text = Array.prototype.some.call("
                    "    last.querySelectorAll(NON_TEXT), function(el) {"
                    "      var tag = (el.tagName || '').toLowerCase();"
                    "      if (tag === 'img' || tag === 'video' || tag === 'audio')"
                    "        return !!(el.currentSrc || el.src);"
                    "      if (tag === 'canvas') return (el.width || 0) > 0 && (el.height || 0) > 0;"
                    "      return true;"
                    "    }"
                    "  );"
                    # Current ChatGPT builds one SECTION per assistant turn.
                    # An action button inside that exact section is a strong,
                    # turn-scoped completion signal and cannot belong to an
                    # older answer.  Keep the wider geometry search below only
                    # as a legacy fallback for older DOM layouts.
                    "  var turn = last.closest('section[data-turn=\"assistant\"]');"
                    "  var has_exact_action = !!(turn && turn.querySelector('[data-testid*=\"turn-action-button\"]'));"
                    # has_action: the per-turn copy/feedback action row appears
                    # only on a COMPLETED message. ChatGPT's DOM layout puts these
                    # buttons in a SIBLING/UNCLE container, NOT as descendants of
                    # the assistant message node — so a plain
                    # ``last.querySelector(...)`` finds nothing and completion is
                    # never detected (every send stalled at the 90s ceiling). The
                    # fix: walk up ancestors, querying down at each scope, and
                    # require the button to be GEOMETRICALLY NEAR the message so
                    # an older turn's action row can't falsely complete a brand-new
                    # answer. New testid scheme is ``*-turn-action-button``
                    # (copy/good-response/bad-response); the legacy
                    # ``response-turn`` selector is retained for older deployments
                    # but no longer matches anything on current ChatGPT.
                    #
                    # Depth was 4; raised to 8 after issue #12 found the action
                    # row at ancestor depth 6. The geometry window is widened to
                    # accept buttons rendered ABOVE the message (top - 180) —
                    # short answers place the action row in the spacing above the
                    # message node, which the old top-8 gate rejected. This is a
                    # FALLBACK signal now (backend end_turn is primary); kept as an
                    # escape hatch for when conv_id is unavailable or backend fails.
                    '  var ACT = \'[data-testid="copy-turn-action-button"],'
                    '            [data-testid="good-response-turn-action-button"],'
                    '            [data-testid="bad-response-turn-action-button"],'
                    '            [data-testid*="turn-action-button"],'
                    '            [data-testid*="copy"],'
                    '            [data-testid*="response-turn"]\';'
                    "  var has_action = (function() {"
                    "    var lastRect = last.getBoundingClientRect();"
                    "    var scope = last;"
                    "    for (var d = 0; scope && d <= 8; d++, scope = scope.parentElement) {"
                    "      var btns = Array.prototype.filter.call("
                    "        scope.querySelectorAll(ACT),"
                    "        function(el){ return el.offsetParent !== null || el.getClientRects().length > 0; }"
                    "      );"
                    "      if (!btns.length) continue;"
                    "      for (var i = 0; i < btns.length; i++) {"
                    "        var r = btns[i].getBoundingClientRect();"
                    "        if (r.top >= lastRect.top - 180 && r.top <= lastRect.bottom + 240) {"
                    "          return true;"
                    "        }"
                    "      }"
                    "    }"
                    "    return false;"
                    "  })();"
                    # is_thinking: the active-reasoning indicator.  A completed
                    # turn may retain a collapsed ``.result-thinking`` section,
                    # so require the visible Stop button for element-based
                    # detection.  Plain placeholder text remains a secondary
                    # signal for layouts without the standard element.  The old
                    # unrestricted ``/thinking/i``
                    # word-match on innerText matched the persistent
                    # "Thought for N seconds" summary label — together they
                    # pinned is_thinking=true on every thinking-model turn
                    # and on any answer that mentioned the word "thinking",
                    # which suppressed all delta emission (see the elif below)
                    # and produced empty responses when the backend fetch lagged.
                    # Also recognize a plain "Thinking..." innerText placeholder
                    # (some layouts show reasoning text without .result-thinking)
                    # so the stall clock treats it as active generation, not a stall.
                    "  var stopButton = document.querySelector('[data-testid=\"stop-button\"], button[aria-label*=\"Stop\" i], button[aria-label*=\"停止\"]');"
                    "  var stopVisible = !!(stopButton && (stopButton.offsetParent !== null || stopButton.getClientRects().length > 0));"
                    "  var responseBusy = last.matches('[aria-busy=\"true\"]') || !!last.querySelector('[aria-busy=\"true\"]');"
                    "  var errorScope = turn || last;"
                    "  var errorElement = errorScope.querySelector('[data-testid*=\"error\" i], [data-testid*=\"retry\" i], button[aria-label*=\"Retry\" i], button[aria-label*=\"重试\"]');"
                    "  var normalizedErrorText = rawText.replace(/\\s+/g, ' ').trim();"
                    "  var has_error = !!errorElement || /^(something went wrong|there was an error generating (?:a |the )?response|生成回复时出错|出了点问题|发生错误)/i.test(normalizedErrorText);"
                    "  var hasThinkingEl = !!last.querySelector('.result-thinking, [data-testid*=\"thinking\"], [data-testid*=\"reasoning\"]');"
                    "  var visibleThinking = /^(thinking|reasoning)\\b/i.test(rawText.trim());"
                    "  var is_thinking = ((stopVisible || responseBusy) && hasThinkingEl) || (visibleThinking && !mdText);"
                    "  return JSON.stringify({text: text, md_text: mdText, html_len: html_len, child_count: child_count, has_meaningful_non_text: has_meaningful_non_text, has_action: has_action, has_exact_action: has_exact_action, has_error:has_error, is_thinking: is_thinking, stop_visible:stopVisible, aria_busy:responseBusy, generation_active:(stopVisible || responseBusy), assistant_count:msgs.length, current_assistant_present:true});"
                    "})()",
                )
                data = json.loads(result)
            except (CDPJSError, json.JSONDecodeError, TypeError):
                await asyncio.sleep(0.5)
                continue

            current = data.get("text", "")
            html_len = data.get("html_len", 0)
            child_count = data.get("child_count", 0)
            meaningful_non_text = data.get("has_meaningful_non_text") is True
            all_new_dom_assets: list[dict] = []
            current_dom_assets: list[dict] = []
            image_action_increased = False
            global_generation_active = False
            if expect_non_text:
                non_text_state = await self._probe_non_text_dom_state(d)
                (
                    all_new_dom_assets,
                    current_dom_assets,
                    image_action_increased,
                ) = self._select_new_non_text_dom_assets(
                    non_text_state,
                    initial_action_count=initial_non_text_action_count,
                    initial_asset_ids=initial_non_text_asset_ids,
                )
                global_generation_active = bool(
                    non_text_state.get("generation_active")
                )
                current_asset_ids = {
                    str(asset.get("identity") or "")
                    for asset in all_new_dom_assets
                }
                if current_asset_ids != last_global_non_text_asset_ids:
                    last_global_non_text_asset_ids = current_asset_ids
                    last_change_time = time.monotonic()
                meaningful_non_text = (
                    meaningful_non_text or bool(all_new_dom_assets)
                )
            has_action = data.get("has_action", False)
            has_exact_action = data.get("has_exact_action", False)
            has_page_error = data.get("has_error") is True
            current_assistant_present = data.get("current_assistant_present") is True
            stop_visible = data.get("stop_visible") is True
            aria_busy = data.get("aria_busy") is True
            is_thinking = data.get("is_thinking", False)
            generation_active = bool(
                data.get("generation_active", False)
                or global_generation_active
            )

            current = strip_reasoning_ui_prefix(current)

            # Keep one representation for the whole stream.  Switching from
            # innerText during generation to markdown.textContent at the end
            # changes list/newline whitespace and breaks append-only deltas,
            # leaving clients with a truncated prefix even though the page is
            # complete.  ``text`` is cleaned innerText on every poll.

            # Localized reasoning placeholders can appear as the assistant
            # node's raw innerText while the real answer container is still
            # empty. Treat them as activity only; emitting them would poison
            # the append-only SSE prefix when ChatGPT replaces the placeholder
            # with the actual answer (for example ``正在思考`` -> ``你好！``).
            # Some Pro layouts render the status inside the markdown container
            # itself (for example ``Pro 思考中``), so md_text being present does
            # not prove this is answer content. Filter by the actual text.
            if is_transient_status_text(current):
                is_thinking = True
                current = ""

            # Reasoning/search progress text is mutable and frequently gets
            # replaced wholesale by the final answer. Never poison an
            # append-only SSE stream with it. Backend terminal reconciliation
            # will emit the final text once end_turn is authoritative.
            observed_progress_text = current
            if hold_dom_text_until_terminal:
                current = ""

            # is_thinking means the model is actively reasoning — the DOM is
            # legitimately static for tens of seconds, which is NOT a stall.
            # It MUST reset the stall clock so a genuine reasoning phase isn't
            # killed early. But a STALE thinking state (.result-thinking lingers
            # as a collapsed section after the answer finishes) must not freeze
            # the stall detector forever — that's how a drift in the DOM
            # action-button selector produced the 120s completion hang (issue
            # #10): is_thinking pinned last_change_time every poll so the 90s
            # stall guard never fired. Compromise: let thinking reset the stall
            # clock ONLY while we have no stable backend completion signal
            # available. Once conv_id_for_check is resolved, the backend
            # end_turn fallback can detect completion even under stale thinking,
            # so we stop granting the indefinite thinking stall reset — the
            # normal stall clock applies and bounds the worst case. (Resetting
            # saw_thinking is independent of this and always happens.)
            if is_thinking:
                saw_thinking = True
                if not conv_id_for_check:
                    last_change_time = time.monotonic()
            # P1: track advisory liveness signal for structured error reporting.
            # True when the DOM shows active generation (thinking indicator).
            # This is advisory only — it informs the structured error and
            # logging, but does NOT pause the stall clock (a stuck indicator
            # must not create an infinite hang).
            generation_active_signal = bool(is_thinking or generation_active)
            if observed_progress_text != last_observed_text:
                last_change_time = time.monotonic()
                # P1: first text content transitions us from awaiting_first_content
                # to streaming_after_first_content. The stall budget changes with
                # the state (see the stall check below). When the state flips,
                # reset the stream-idle clock so a long reasoning wait followed
                # by first text doesn't immediately fail under the shorter
                # stream-idle budget (review finding A).
                if current and not first_content_seen:
                    first_content_seen = True
                    last_change_time = time.monotonic()  # reset stream-idle clock
                delta = append_only_delta(last_dom_text, current)
                if delta:
                    yield StreamChunk(delta=delta)
                    last_dom_text += delta
                elif current and current != last_dom_text:
                    logger.debug(
                        "Ignored non-append DOM rewrite while streaming: emitted=%d observed=%d",
                        len(last_dom_text),
                        len(current),
                    )
                last_observed_text = observed_progress_text
                self.last_dom_text = last_dom_text

            # Non-text progress signals (images, tool-use, etc.).  HTML/child
            # changes still prove DOM liveness, but only an explicit media or
            # tool-result node proves that actual non-text response content
            # exists.  This distinction keeps an empty ``result-streaming``
            # shell in the longer first-content state.
            if (
                html_len != last_html_len
                or child_count != last_child_count
                or meaningful_non_text != last_meaningful_non_text
            ):
                last_change_time = time.monotonic()
            if meaningful_non_text:
                had_non_text_content = True
                self.had_non_text_content = True
                if not first_content_seen and not hold_dom_text_until_terminal:
                    first_content_seen = True
                    last_change_time = time.monotonic()  # reset stream-idle clock
            last_html_len = html_len
            last_child_count = child_count
            last_meaningful_non_text = meaningful_non_text

            # A completed turn can occasionally miss both the backend end_turn
            # projection and its exact action-button event. Build a conservative
            # latest-assistant terminal candidate, but act on it only when an
            # existing phase timeout is reached. Final media may coexist with a
            # text answer, so it is not itself a blocker; active Stop/aria-busy,
            # thinking, empty text, missing current-turn identity, explicit
            # image-generation mode, and official page errors are blockers.
            terminal_candidate = (
                current_assistant_present
                and bool(observed_progress_text)
                and not generation_active
                and not is_thinking
                and not has_page_error
                and not expect_non_text
            )
            if terminal_candidate:
                if observed_progress_text != stable_terminal_text:
                    stable_terminal_text = observed_progress_text
                    stable_terminal_since = time.monotonic()
                elif stable_terminal_since is None:
                    stable_terminal_since = time.monotonic()
            else:
                stable_terminal_text = ""
                stable_terminal_since = None

            # ── Completion detection ─────────────────────────────────────
            # Two signals, ordered by stability. Backend end_turn is PRIMARY
            # (issue #12): it survived three DOM action-button drifts where the
            # DOM selector failed. The DOM has_action is a FALLBACK for the
            # window where conv_id is unavailable (before the URL resolves, ~1s
            # into a new chat) or when the backend fetch fails transiently.
            #
            # Resolve conv_id first (needed for the primary signal on new chats).
            if not conv_id_for_check:
                now = time.monotonic()
                if now - last_conv_id_probe >= 1.0:
                    last_conv_id_probe = now
                    try:
                        conv_id_for_check = await d._get_live_conversation_id_best_effort()
                        if conv_id_for_check:
                            logger.info(
                                "Resolved conversation id mid-loop: %s",
                                conv_id_for_check,
                            )
                    except Exception as e:
                        logger.debug("conv_id probe failed (ignored): %s", e)

            # PRIMARY: backend end_turn. Throttled to one fetch per 3s to respect
            # the shared account rate budget. Eligible when we have streamed text
            # OR have seen a thinking phase — a long reasoning response has empty
            # last_dom_text during thinking, but the backend reports end_turn once
            # the model finishes. Completion stays strict (end_turn AND usable
            # content) so this can't complete an empty answer. Fetch failures set
            # backend_fetch_failed so the DOM fallback below is unlocked this poll.
            backend_fetch_failed = False
            backend_rate_limited = False
            if (
                conv_id_for_check
                and (
                    last_dom_text
                    or saw_thinking
                    or is_thinking
                    or had_non_text_content
                    or hold_dom_text_until_terminal
                    or (
                        bool(turn_anchor.captured_user_message_id)
                        and not expect_non_text
                    )
                )
                and time.monotonic() - last_backend_check > 3.0
            ):
                last_backend_check = time.monotonic()
                try:
                    # A2: anchored tri-state completion. The selector returns
                    # a rich TurnEndResult; collapse_to_end_turn_status maps
                    # it to the detector's tri-state gate. Critical: not_ready/
                    # ambiguous/degraded_not_fresh collapse to not_ready and
                    # must NOT set backend_fetch_failed (would unlock the DOM
                    # fallback and risk completing off a prior turn's action
                    # row — the line-493 gate invariant).
                    end_result = await d._fetch_end_turn_for_turn(
                        conv_id_for_check, turn_anchor,
                        had_non_text_content=had_non_text_content,
                    )
                    status = collapse_to_end_turn_status(end_result)
                    diagnostic = end_result.diagnostic or {}
                    assistant_node = str(diagnostic.get("assistant_node") or "")
                    current_node = str(diagnostic.get("current_node") or "")
                    node_status = str(diagnostic.get("status") or "")
                    recipient = str(diagnostic.get("recipient") or "")
                    finish_type = str(diagnostic.get("finish_type") or "")
                    text_sha256 = str(diagnostic.get("text_sha256") or "")
                    text_length = diagnostic.get("text_length")
                    children_count = diagnostic.get("children_count")
                    strict_candidate = bool(
                        status == "complete"
                        and turn_anchor.captured_user_message_id
                        and not expect_non_text
                        and diagnostic.get("strict_terminal") is True
                        and assistant_node
                        and assistant_node == current_node
                        and children_count == 0
                        and node_status == "finished_successfully"
                        and recipient == "all"
                        and finish_type == "stop"
                        and text_sha256
                        and isinstance(text_length, int)
                        and text_length > 0
                        and not stop_visible
                        and not has_page_error
                    )
                    strict_terminal_confirmed = False
                    if strict_candidate:
                        signature = (
                            assistant_node,
                            current_node,
                            node_status,
                            recipient,
                            finish_type,
                            children_count,
                            text_sha256,
                            text_length,
                        )
                        now = time.monotonic()
                        if signature == strict_terminal_signature:
                            strict_terminal_confirmations += 1
                        else:
                            strict_terminal_signature = signature
                            strict_terminal_confirmations = 1
                            strict_terminal_first_seen = now
                        strict_terminal_confirmed = bool(
                            strict_terminal_confirmations
                            >= STRICT_TERMINAL_CONFIRMATIONS
                            and strict_terminal_first_seen is not None
                            and now - strict_terminal_first_seen
                            >= STRICT_TERMINAL_STABILITY_SECONDS
                        )
                    else:
                        strict_terminal_signature = None
                        strict_terminal_confirmations = 0
                        strict_terminal_first_seen = None
                    if status == "complete":
                        if strict_terminal_confirmed:
                            logger.info(
                                "Strict anchored backend terminal confirmed "
                                "(%d observations, aria_busy=%s, thinking=%s)",
                                strict_terminal_confirmations,
                                aria_busy,
                                is_thinking,
                            )
                            break
                        # The legacy completion path remains available when
                        # actual DOM/non-text response content is present.  A
                        # held reasoning snapshot is deliberately *not* usable
                        # content: only the strict, metadata-complete proof
                        # above may finish a text turn whose DOM answer is not
                        # safe to stream yet.
                        usable_content = (
                            last_dom_text
                            or had_non_text_content
                        )
                        terminal_confirmed = True
                        if hold_dom_text_until_terminal and not had_non_text_content:
                            terminal_node = str(
                                (end_result.diagnostic or {}).get("assistant_node") or ""
                            )
                            terminal_confirmed = False
                            logger.debug(
                                "Held reasoning terminal requires strict backend "
                                "proof (node=%s active=%s thinking=%s)",
                                terminal_node,
                                generation_active,
                                is_thinking,
                            )
                        if usable_content and terminal_confirmed:
                            logger.info(
                                "Backend end_turn=true (primary completion) for %s",
                                conv_id_for_check,
                            )
                            break
                        logger.debug(
                            "Backend end_turn=true but no content yet for %s — "
                            "not completing (strict content guard)",
                            conv_id_for_check,
                        )
                    if status == "fetch_failed":
                        backend_fetch_failed = True
                        backend_error = str(
                            (end_result.diagnostic or {}).get("error") or ""
                        ).lower()
                        backend_rate_limited = (
                            "http 429" in backend_error
                            or "status 429" in backend_error
                        )
                        logger.debug(
                            "end_turn fetch failed (status=%s): %s",
                            end_result.status, end_result.diagnostic,
                        )
                    # else: not_ready — no-op (do NOT set backend_fetch_failed).
                except AuthExpiredError:
                    # Auth failure must NEVER degrade to DOM fallback.
                    # (PR #39 review finding #2 — the prior broad except
                    # swallowed this, violating "auth failure never degrades.")
                    raise
                except Exception as e:
                    # Transport/backend failure — treat as fetch_failed so the
                    # DOM fallback unlocks for this poll.
                    strict_terminal_signature = None
                    strict_terminal_confirmations = 0
                    strict_terminal_first_seen = None
                    backend_fetch_failed = True
                    backend_error = str(e).lower()
                    backend_rate_limited = (
                        "http 429" in backend_error
                        or "status 429" in backend_error
                    )
                    logger.debug("end_turn fetch raised (ignored): %s", e)

            # A new generated-image URL on the global conversation-turn
            # surface, absent from the pre-send baseline, is explicit
            # current-turn evidence. Fully loaded images are independently
            # ready; lazy-loaded images are ready only when the image-action
            # count also increased. Once generation is inactive, preserve the
            # URL so the authenticated downloader can validate its bytes even
            # when the backend projection is temporarily rate-limited (429).
            # Empty shells, old images, and unrelated thumbnails cannot pass.
            if (
                expect_non_text
                and current_dom_assets
                and not generation_active
                and not is_thinking
            ):
                self.non_text_dom_assets = [
                    {
                        "type": "image",
                        "name": f"generated-image-{index}.png",
                        "mime_type": "image/png",
                        "file_id": str(asset.get("identity") or "")
                        if str(asset.get("identity") or "").startswith("file_")
                        else "",
                        "source_url": str(asset["source_url"]),
                    }
                    for index, asset in enumerate(current_dom_assets, start=1)
                ]
                self.had_non_text_content = True
                logger.info(
                    "Completed current-turn DOM image asset captured in Phase 2 "
                    "(%d new, action_increased=%s)",
                    len(current_dom_assets),
                    image_action_increased,
                )
                break

            # Exact per-turn DOM completion is authoritative.  Unlike the
            # broad ancestor/geometry fallback, this button is contained in
            # the same SECTION as the newest assistant message, so a stale
            # prior-turn action row cannot satisfy it.  This also covers the
            # live failure mode where message-ID capture is missed and the
            # backend projection remains not_ready after the DOM has finished.
            #
            # Reasoning models deliberately hold mutable DOM narration until
            # terminal (``current`` is cleared above).  For reference-image
            # requests, once the exact action row appears and generation is
            # inactive, ``observed_progress_text`` is the stable terminal
            # answer and is safe to emit in one chunk.
            # Without this promotion, ``last_dom_text`` can never become truthy
            # for a completed Pro/reference-image turn, leaving the detector to
            # wait for the full first-content timeout when the backend anchor
            # projection remains ``not_ready``.
            # Keep the promotion text-only and tool-free: generated-output
            # requests must wait for an actual asset, while a web-search/tool
            # surface can retain an action row through Stop-button flicker.
            attachment_text_terminal = (
                has_input_attachments
                and not expect_non_text
                and not had_non_text_content
                and bool(observed_progress_text)
            )
            if (
                has_exact_action
                and (
                    last_dom_text
                    or attachment_text_terminal
                )
                and not generation_active
                and not is_thinking
            ):
                if (
                    hold_dom_text_until_terminal
                    and attachment_text_terminal
                    and observed_progress_text
                ):
                    terminal_delta = append_only_delta(
                        last_dom_text, observed_progress_text
                    )
                    if terminal_delta:
                        yield StreamChunk(delta=terminal_delta)
                        last_dom_text += terminal_delta
                        self.last_dom_text = last_dom_text
                self.completed_via_exact_action = True
                logger.info("Exact current-turn action detected (DOM completion)")
                break

            # FALLBACK: DOM action button (has_action).  Ordinary models retain
            # the legacy no-conversation-id fallback, but Pro/reasoning models
            # must wait because nearby action rows can belong to an in-progress
            # reasoning/tool block.  Once a conversation id exists, this path is
            # allowed only when the primary backend fetch failed this poll. When
            # the backend IS available it is authoritative — a widened has_action
            # match (depth 8, top-180) can hit a PRIOR turn's action row, so DOM
            # must not override a live backend that says "not done yet" or that
            # hasn't been consulted yet due to throttle. Also requires usable
            # content (same strict guard as the primary) so it can't complete an
            # empty answer off a stale button.
            if (
                has_action
                and (
                    (not conv_id_for_check and not hold_dom_text_until_terminal)
                    or (
                        conv_id_for_check
                        and backend_fetch_failed
                        # HTTP 429 is not proof of completion.  Only the exact
                        # current-turn action row may override it; the broad
                        # geometry fallback could belong to an older turn.
                        and (not backend_rate_limited or has_exact_action)
                    )
                )
                and (last_dom_text or had_non_text_content)
                and not generation_active
                and not is_thinking
            ):
                logger.info("DOM has_action (fallback completion) — no backend signal")
                break

            # ── P1: model-aware two-state stall detection ───────────────────
            # Replaces the single PHASE_STALL_SECONDS check. When budgets are
            # provided, phase-2 splits into two states with separate budgets:
            #   - awaiting_first_content: no text yet. Uses
            #     first_content_timeout_seconds (longer for reasoning models).
            #   - streaming_after_first_content: text appeared then stopped.
            #     Uses stream_idle_timeout_seconds (shorter — once streaming
            #     started, a long idle is suspicious).
            # A hard_timeout_seconds absolute cap applies regardless of state
            # or DOM liveness. DOM thinking/generating signals are advisory
            # (generation_active_signal) — they inform logging but do NOT pause
            # the stall clock (a stuck thinking indicator must not create an
            # infinite hang).
            #
            # On stall: attempt ONE final reconciliation read before raising.
            # If the backend reports the turn completed, return normally — the
            # generation actually finished (field-verified case). Only if
            # reconciliation finds no completion do we raise a structured
            # GenerationStuckError.
            if use_two_state:
                elapsed_since_progress = time.monotonic() - last_change_time
                elapsed_total = time.monotonic() - phase_2_start

                # Determine which budget applies based on the current state.
                stall_budget = (
                    budgets.stream_idle_timeout_seconds
                    if first_content_seen
                    else budgets.first_content_timeout_seconds
                )
                stall_kind = (
                    "stream_idle_timeout"
                    if first_content_seen
                    else "first_content_timeout"
                )

                # Hard cap: absolute wall-clock limit regardless of DOM signals.
                hard_cap_hit = elapsed_total > budgets.hard_timeout_seconds
                budget_hit = elapsed_since_progress > stall_budget

                if hard_cap_hit or budget_hit:
                    # Final reconciliation: did the turn actually complete?
                    # Field evidence: the generation often completes after the
                    # detector would have given up. Before raising, check the
                    # backend one more time.
                    turn_id = getattr(turn_anchor, "captured_id", None)
                    reconciled = await self._reconcile_before_stall(
                        d, conv_id_for_check, turn_anchor,
                        had_non_text_content,
                    )
                    if reconciled:
                        logger.info(
                            "Phase-2 %s reconciled after stall — generation "
                            "had completed (elapsed=%.0fs, kind=%s, "
                            "model_class=%s, active=%s)",
                            stall_kind, elapsed_total, stall_kind,
                            model_class, generation_active_signal,
                        )
                        return  # generation completed — return normally
                    stable_dom_ready = (
                        stable_terminal_since is not None
                        and time.monotonic() - stable_terminal_since
                        >= DOM_TERMINAL_STABILITY_SECONDS
                        and stable_terminal_text == observed_progress_text
                        and current_assistant_present
                        and not generation_active
                        and not is_thinking
                        and not has_page_error
                        and not expect_non_text
                    )
                    if stable_dom_ready:
                        terminal_delta = append_only_delta(
                            last_dom_text, stable_terminal_text
                        )
                        if terminal_delta:
                            yield StreamChunk(delta=terminal_delta)
                            last_dom_text += terminal_delta
                            self.last_dom_text = last_dom_text
                        if last_dom_text == stable_terminal_text:
                            self.completed_via_stable_dom = True
                            logger.info(
                                "Stable inactive current-turn DOM accepted after "
                                "missed completion signal (elapsed=%.0fs, kind=%s)",
                                elapsed_total,
                                stall_kind,
                            )
                            return
                        logger.warning(
                            "Stable terminal DOM rewrote an emitted SSE prefix; "
                            "preserving the stall error (streamed=%d final=%d)",
                            len(last_dom_text),
                            len(stable_terminal_text),
                        )
                    # Reconciliation found no completion — raise structured error.
                    raise GenerationStuckError(
                        "phase_2_stream",
                        elapsed_since_progress,
                        stall_kind=("hard_timeout" if hard_cap_hit else stall_kind),
                        model_class=model_class,
                        elapsed_seconds=elapsed_total,
                        generation_active_signal=generation_active_signal,
                        turn_id=turn_id,
                    )
            else:
                # Legacy path (no budgets provided): single PHASE_STALL_SECONDS.
                if time.monotonic() - last_change_time > PHASE_STALL_SECONDS:
                    raise GenerationStuckError("phase_2_stream", time.monotonic() - last_change_time)

            await asyncio.sleep(0.5)

        # Per-call results (last_dom_text / had_non_text_content) are already
        # mirrored to self.* as they changed during the loop; the driver tail
        # reads them to emit the final-text suffix delta / non-text placeholder.
        return
