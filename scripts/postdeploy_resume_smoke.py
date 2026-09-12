#!/usr/bin/env python3
"""Production smoke test for resumable Web2API turns with image attachments.

The script intentionally disconnects after the first durable NDJSON event and
then resumes from its cursor.  It uses the deployed site's existing
configuration and never prints the configured URL, username, or password.

Required environment variables:

* ``AI_CHAT_PUBLIC_BASE_URL`` (or ``QUEENER_PUBLIC_BASE_URL``)
* ``AI_CHAT_ADMIN_USERNAME`` (or ``QUEENER_ADMIN_USERNAME``)
* ``AI_CHAT_ACCESS_PASSWORD`` (or ``QUEENER_ACCESS_PASSWORD``)
* ``SMOKE_MODEL`` (the public model id or an unambiguous raw external model id)

Optional environment variables:

* ``SMOKE_PROVIDER`` disambiguates a raw model id by provider id or name.
* ``SMOKE_MARKER`` supplies a safe marker used to find the upstream QA chat.
* ``SMOKE_EFFORT`` defaults to ``default``.
* ``SMOKE_TIMEOUT_SECONDS`` defaults to 1200 seconds.
* ``SMOKE_BASE_URL``, ``SMOKE_ADMIN_USERNAME``, and
  ``SMOKE_ADMIN_PASSWORD`` override the corresponding deployed-site values.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit

import httpx
from PIL import Image

MARKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{5,95}$")
TERMINAL_EVENT_TYPES = {"done", "error"}


class SmokeFailure(RuntimeError):
    """Expected smoke-test failure with a message safe for console output."""


@dataclass(frozen=True)
class SmokeConfig:
    base_url: str
    admin_username: str
    admin_password: str
    model_selector: str
    provider_selector: str
    marker: str
    effort: str
    timeout_seconds: float


@dataclass
class StreamState:
    cursor: int = 0
    answer: str = ""
    terminal: bool = False
    durable_types: list[str] | None = None

    def __post_init__(self) -> None:
        if self.durable_types is None:
            self.durable_types = []


def _first_environment(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _safe_base_url(raw: str) -> str:
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SmokeFailure("configured site address must be an HTTPS origin")
    return raw.rstrip("/")


def _float_environment(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SmokeFailure(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise SmokeFailure(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _load_config() -> SmokeConfig:
    base_url = _first_environment(
        "SMOKE_BASE_URL",
        "AI_CHAT_PUBLIC_BASE_URL",
        "QUEENER_PUBLIC_BASE_URL",
    )
    username = _first_environment(
        "SMOKE_ADMIN_USERNAME",
        "AI_CHAT_ADMIN_USERNAME",
        "QUEENER_ADMIN_USERNAME",
    )
    password = _first_environment(
        "SMOKE_ADMIN_PASSWORD",
        "AI_CHAT_ACCESS_PASSWORD",
        "QUEENER_ACCESS_PASSWORD",
    )
    model = _first_environment("SMOKE_MODEL")
    missing = [
        name
        for name, value in (
            ("site base URL", base_url),
            ("administrator username", username),
            ("administrator password", password),
            ("SMOKE_MODEL", model),
        )
        if not value
    ]
    if missing:
        raise SmokeFailure(f"missing configuration: {', '.join(missing)}")

    marker = _first_environment("SMOKE_MARKER")
    if not marker:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        marker = f"QA-FAST-{timestamp}-{uuid.uuid4().hex[:6]}"
    if not MARKER_RE.fullmatch(marker):
        raise SmokeFailure("SMOKE_MARKER has an invalid format")

    effort = _first_environment("SMOKE_EFFORT") or "default"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", effort):
        raise SmokeFailure("SMOKE_EFFORT has an invalid format")

    return SmokeConfig(
        base_url=_safe_base_url(base_url),
        admin_username=username,
        admin_password=password,
        model_selector=model,
        provider_selector=_first_environment("SMOKE_PROVIDER"),
        marker=marker,
        effort=effort,
        timeout_seconds=_float_environment(
            "SMOKE_TIMEOUT_SECONDS", 1200.0, 30.0, 1800.0
        ),
    )


def _require_status(response: httpx.Response, stage: str, *allowed: int) -> None:
    expected = set(allowed or (200,))
    if response.status_code not in expected:
        raise SmokeFailure(f"{stage} returned HTTP {response.status_code}")


def _json_object(response: httpx.Response, stage: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise SmokeFailure(f"{stage} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{stage} returned an invalid object")
    return payload


def _write_reference_images(directory: Path) -> tuple[Path, Path]:
    red = directory / "smoke-red.png"
    blue = directory / "smoke-blue.png"
    Image.new("RGB", (128, 128), (255, 0, 0)).save(red, format="PNG")
    Image.new("RGB", (128, 128), (0, 0, 255)).save(blue, format="PNG")
    return red, blue


def _select_external_model(payload: dict[str, Any], config: SmokeConfig) -> str:
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise SmokeFailure("model listing has no data array")

    candidates: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict) or item.get("source") != "external":
            continue
        modalities = item.get("inputModalities")
        if isinstance(modalities, list) and "image" not in modalities:
            continue
        candidates.append(item)

    exact = [item for item in candidates if item.get("id") == config.model_selector]
    matches = exact or [
        item
        for item in candidates
        if str(item.get("rawModel") or "") == config.model_selector
    ]
    if config.provider_selector:
        hint = config.provider_selector.casefold()
        matches = [
            item
            for item in matches
            if str(item.get("providerId") or "").casefold() == hint
            or str(item.get("providerName") or "").casefold() == hint
        ]
    if len(matches) != 1:
        raise SmokeFailure(
            "SMOKE_MODEL did not identify exactly one image-capable external model"
        )
    selected = str(matches[0].get("id") or "")
    if not selected.startswith("external."):
        raise SmokeFailure("selected model is not an external Web2API model")
    return selected


def _attachment_refs(payload: dict[str, Any]) -> list[dict[str, str]]:
    files = payload.get("files")
    if not isinstance(files, list) or len(files) != 2:
        raise SmokeFailure("upload did not return exactly two files")
    attachments: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            raise SmokeFailure("upload returned an invalid file entry")
        stored_id = str(item.get("id") or "")
        name = str(item.get("name") or "")
        if not stored_id or not name:
            raise SmokeFailure("upload returned an incomplete file entry")
        attachments.append({"id": stored_id, "name": name, "source": "file"})
    return attachments


async def _ndjson_events(response: httpx.Response, stage: str) -> AsyncIterator[dict[str, Any]]:
    async for line in response.aiter_lines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SmokeFailure(f"{stage} returned invalid NDJSON") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise SmokeFailure(f"{stage} returned an invalid event")
        yield event


def _consume_event(state: StreamState, event: dict[str, Any]) -> bool:
    event_type = str(event["type"])
    cursor_value = event.get("cursor")
    if event_type == "ping":
        if cursor_value is not None and (
            not isinstance(cursor_value, int) or cursor_value != state.cursor
        ):
            raise SmokeFailure("heartbeat cursor did not match consumed progress")
        return False

    if not isinstance(cursor_value, int) or cursor_value <= state.cursor:
        raise SmokeFailure("durable event cursors were not strictly increasing")
    state.cursor = cursor_value
    assert state.durable_types is not None
    state.durable_types.append(event_type)

    text = event.get("text")
    if event_type == "delta" and isinstance(text, str):
        state.answer += text
    elif event_type == "replace" and isinstance(text, str):
        state.answer = text

    if event_type in TERMINAL_EVENT_TYPES:
        state.terminal = True
    if event_type == "error":
        raise SmokeFailure("turn returned an error event")
    return True


async def _upload_images(
    client: httpx.AsyncClient, session_id: str, paths: tuple[Path, Path]
) -> list[dict[str, str]]:
    with ExitStack() as stack:
        files = [
            (
                "files",
                (path.name, stack.enter_context(path.open("rb")), "image/png"),
            )
            for path in paths
        ]
        try:
            response = await client.post(
                "/api/uploads", data={"session_id": session_id}, files=files
            )
        except httpx.RequestError as exc:
            raise SmokeFailure("network request failed during image upload") from exc
    _require_status(response, "image upload")
    return _attachment_refs(_json_object(response, "image upload"))


async def _cleanup(
    client: httpx.AsyncClient,
    *,
    logged_in: bool,
    workspace_id: str,
    turn_started: bool,
    turn_terminal: bool,
    client_turn_id: str,
) -> list[str]:
    failures: list[str] = []
    if logged_in and turn_started and not turn_terminal:
        # Each cleanup request is best-effort; keep going so one network failure
        # cannot prevent the remaining cleanup steps.
        try:
            response = await client.post(
                f"/api/turn/external/{client_turn_id}/interrupt"
            )
            if response.status_code != 200:
                failures.append("turn")
        except Exception:  # noqa: BLE001
            failures.append("turn")

    if logged_in:
        try:
            response = await client.post(
                "/api/conversations/delete",
                json={
                    "conversation_id": None,
                    "workspace_id": workspace_id,
                    "thread_id": None,
                    "thread_ids": [],
                    "delete_workspace": True,
                },
            )
            if response.status_code != 200:
                failures.append("workspace")
        except Exception:  # noqa: BLE001
            failures.append("workspace")

        try:
            response = await client.post("/logout", follow_redirects=False)
            if response.status_code != 303:
                failures.append("session")
        except Exception:  # noqa: BLE001
            failures.append("session")
    return failures


async def _run_smoke(config: SmokeConfig) -> dict[str, Any]:
    session_id = uuid.uuid4().hex
    client_turn_id = uuid.uuid4().hex
    state = StreamState()
    logged_in = False
    turn_started = False
    outcome: dict[str, Any] | None = None
    failure: SmokeFailure | None = None

    timeout = httpx.Timeout(config.timeout_seconds, connect=15.0)
    async with httpx.AsyncClient(
        base_url=config.base_url,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        with TemporaryDirectory(prefix="ai-chat-resume-smoke-") as temporary:
            try:
                image_paths = _write_reference_images(Path(temporary))
                try:
                    login = await client.post(
                        "/login",
                        data={
                            "username": config.admin_username,
                            "password": config.admin_password,
                        },
                    )
                except httpx.RequestError as exc:
                    raise SmokeFailure("network request failed during login") from exc
                _require_status(login, "login", 303)
                # Do not rely on CookieJar membership: a successful authenticated
                # /api/models response is the proof that the login session works.
                # Mark login now so every later failure still cleans up and logs out.
                logged_in = True

                try:
                    models_response = await client.get(
                        "/api/models", params={"include_codex": "false"}
                    )
                except httpx.RequestError as exc:
                    raise SmokeFailure("network request failed during model listing") from exc
                _require_status(models_response, "model listing")
                selected_model = _select_external_model(
                    _json_object(models_response, "model listing"), config
                )
                attachments = await _upload_images(client, session_id, image_paths)

                payload = {
                    "session_id": session_id,
                    "client_conversation_id": None,
                    "client_turn_id": client_turn_id,
                    "thread_id": None,
                    "message": (
                        f"{config.marker}\n"
                        "This is an automated transport check. Describe the two solid-colour "
                        "reference images in one short sentence. Your answer must contain the "
                        "literal lowercase English words red and blue."
                    ),
                    "model": selected_model,
                    "effort": config.effort,
                    "attachments": attachments,
                    "project_attachments": [],
                    "project_instructions": "",
                    "history": [],
                }

                started_at = time.monotonic()
                try:
                    async with client.stream("POST", "/api/turn", json=payload) as response:
                        _require_status(response, "initial turn")
                        turn_started = True
                        disconnected = False
                        async for event in _ndjson_events(response, "initial turn"):
                            if _consume_event(state, event):
                                if state.terminal:
                                    raise SmokeFailure(
                                        "turn became terminal before the intentional disconnect"
                                    )
                                disconnected = True
                                break
                        if not disconnected:
                            raise SmokeFailure(
                                "initial turn ended without a durable event to disconnect after"
                            )
                except httpx.RequestError as exc:
                    raise SmokeFailure("network request failed during initial turn") from exc

                initial_cursor = state.cursor
                try:
                    async with client.stream(
                        "GET",
                        f"/api/turn/resume/{client_turn_id}",
                        params={"cursor": initial_cursor},
                    ) as response:
                        _require_status(response, "resumed turn")
                        async for event in _ndjson_events(response, "resumed turn"):
                            _consume_event(state, event)
                            if state.terminal:
                                break
                except httpx.RequestError as exc:
                    raise SmokeFailure("network request failed during resumed turn") from exc

                elapsed = time.monotonic() - started_at
                if not state.terminal or not state.durable_types or state.durable_types[-1] != "done":
                    raise SmokeFailure("resumed turn did not finish with a done event")
                answer = state.answer.strip()
                if not answer:
                    raise SmokeFailure("resumed turn returned an empty answer")
                folded = answer.casefold()
                if "red" not in folded or "blue" not in folded:
                    raise SmokeFailure("answer did not identify both reference-image colours")

                outcome = {
                    "ok": True,
                    "marker": config.marker,
                    "elapsedSeconds": round(elapsed, 3),
                    "initialCursor": initial_cursor,
                    "finalCursor": state.cursor,
                    "durableEvents": state.durable_types,
                    "answerCharacters": len(answer),
                }
            except SmokeFailure as exc:
                failure = exc
            except httpx.TimeoutException:
                failure = SmokeFailure("production smoke test timed out")
            # Keep unexpected details, URLs, and secrets out of stdout.
            except Exception as exc:  # noqa: BLE001
                failure = SmokeFailure(f"unexpected {type(exc).__name__}")
            finally:
                cleanup_failures = await _cleanup(
                    client,
                    logged_in=logged_in,
                    workspace_id=session_id,
                    turn_started=turn_started,
                    turn_terminal=state.terminal,
                    client_turn_id=client_turn_id,
                )

    if cleanup_failures:
        cleanup_summary = ",".join(sorted(set(cleanup_failures)))
        if failure is None:
            failure = SmokeFailure(f"cleanup incomplete: {cleanup_summary}")
        else:
            failure = SmokeFailure(f"{failure}; cleanup incomplete: {cleanup_summary}")
    if failure is not None:
        raise failure
    if outcome is None:
        raise SmokeFailure("smoke test produced no result")
    outcome["cleanup"] = "complete"
    return outcome


def main() -> int:
    try:
        config = _load_config()
        outcome = asyncio.run(_run_smoke(config))
    except SmokeFailure as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "interrupted"}))
        return 130
    print(json.dumps(outcome, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
