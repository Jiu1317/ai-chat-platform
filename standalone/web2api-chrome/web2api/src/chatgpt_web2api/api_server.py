"""OpenAI-compatible API server.

Endpoints:
  POST /v1/chat/completions  — chat (streaming + non-streaming)
  GET  /v1/models            — model catalog
  GET  /v1/projects          — ChatGPT projects
  GET  /health               — health + Chrome status
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path

from aiohttp import web

from .breakers import BreakerKind, BreakerRegistry, CircuitOpenError
from .cdp_driver import (
    AuthExpiredError,
    CDPDriver,
    GenerationStuckError,
    ModelSelectionError,
    RateLimitError,
    is_rate_limited_text,
)
from .config import Config
from .cross_process_lock import LockAcquisitionError
from .lock_resolver import MutationLock, OwnedTabRequiredError, resolve_mutation_lock
from .multimodal import (
    MAX_IMAGE_COUNT,
    ImageInputError,
    ImageReference,
    _sniff_image_type,
    parse_content_parts,
    prepare_image_files,
    wants_image_output,
)
from .resilience import retry_on_rate_limit

logger = logging.getLogger(__name__)
IMAGE_GENERATION_TIMEOUT_SECONDS = 15 * 60
REASONING_REQUEST_TIMEOUT_SECONDS = 420
SSE_HEARTBEAT_SECONDS = 15
ASSET_TTL_SECONDS = 60 * 60
ASSET_PREFETCH_TIMEOUT_SECONDS = 3 * 60
MAX_ASSET_CACHE_ITEMS = 4
MAX_ASSET_CACHE_BYTES = 30 * 1024 * 1024
MAX_ASSET_CACHE_TOTAL_BYTES = MAX_ASSET_CACHE_ITEMS * MAX_ASSET_CACHE_BYTES
MAX_SHARED_ASSET_CACHE_ITEMS = MAX_ASSET_CACHE_ITEMS * 2
MAX_SHARED_ASSET_CACHE_TOTAL_BYTES = (
    MAX_SHARED_ASSET_CACHE_ITEMS * MAX_ASSET_CACHE_BYTES
)
MAX_HISTORY_TURNS = 31
MAX_CONTEXT_CHARS = 150_000
MAX_SYSTEM_CONTEXT_CHARS = 24_000
_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _normalise_context_text(value: object) -> str:
    """Return browser-safe NFC text without embedded NUL characters."""
    return unicodedata.normalize("NFC", str(value or "")).replace("\x00", "")


def _clip_context_text(text: str, limit: int) -> str:
    """Clip long text by Unicode code point while preserving both ends."""
    text = _normalise_context_text(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n…[中间内容已省略]…\n"
    if limit <= len(marker) + 2:
        return text[:limit]
    payload = limit - len(marker)
    head = max(1, int(payload * 0.7))
    tail = payload - head
    return text[:head] + marker + text[-tail:]


def _valid_conversation_id(value: object) -> bool:
    """Accept only ChatGPT URL-safe conversation identifiers."""
    return isinstance(value, str) and bool(_CONVERSATION_ID_RE.fullmatch(value))


def _format_context_turn(turn: list[tuple[str, str, list[ImageReference]]]) -> str:
    return "\n".join(
        f"[{'User' if role == 'user' else 'Assistant'}]\n{content}"
        for role, content, _images in turn
    )


def _build_chat_context(
    messages: object, *, conversation_id: str | None = None
) -> tuple[str, list[ImageReference], str, bool, bool]:
    """Build a bounded prompt and return only images that remain in context.

    A request with an explicit conversation id is a true continuation: the
    browser already owns the prior context, so only the newest user turn is
    sent. A bootstrap request keeps at most 31 complete user-led turns (30
    historical turns plus the current turn) under a hard 150,000-character
    budget, with the newest turn taking priority.
    """
    if not isinstance(messages, list):
        messages = []

    system_parts: list[str] = []
    entries: list[tuple[str, str, list[ImageReference]]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").lower()
        content, images = parse_content_parts(message.get("content", ""))
        content = _normalise_context_text(content)
        if role == "system":
            if content:
                system_parts.append(content)
        elif role in {"user", "assistant"}:
            entries.append((role, content, images))

    if not entries or entries[-1][0] != "user":
        return "", [], "", bool(system_parts), False

    latest_role, latest_user_text, latest_images = entries[-1]
    assert latest_role == "user"
    has_history_messages = len(entries) > 1

    if conversation_id:
        current = _clip_context_text(
            f"[User]\n{latest_user_text}", MAX_CONTEXT_CHARS
        )
        return current, list(latest_images), latest_user_text, bool(system_parts), has_history_messages

    # Group messages into complete user-led turns, dropping orphan assistant
    # messages at the beginning of a truncated client history.
    turns: list[list[tuple[str, str, list[ImageReference]]]] = []
    for entry in entries:
        if entry[0] == "user":
            turns.append([entry])
        elif turns:
            turns[-1].append(entry)
    turns = turns[-MAX_HISTORY_TURNS:]
    current_turn = turns[-1]

    parts: list[str] = []
    if system_parts:
        system_text = _clip_context_text(
            "\n".join(system_parts), MAX_SYSTEM_CONTEXT_CHARS
        )
        if system_text:
            parts.append(f"[System Instructions]\n{system_text}")

    # Reserve room for the current turn before admitting any history.
    current_block = _format_context_turn(current_turn)
    current_separator = 2 if parts else 0
    current_block = _clip_context_text(
        current_block, MAX_CONTEXT_CHARS - sum(map(len, parts)) - current_separator
    )
    base_length = sum(map(len, parts)) + current_separator + len(current_block)
    remaining = MAX_CONTEXT_CHARS - base_length

    selected_prior: list[list[tuple[str, str, list[ImageReference]]]] = []
    for turn in reversed(turns[:-1]):
        block = _format_context_turn(turn)
        cost = len(block) + 2
        if cost <= remaining:
            selected_prior.append(turn)
            remaining -= cost
            continue
        # Preserve a bounded slice of the nearest oversized turn, then stop so
        # history stays contiguous and biased toward recency.
        if remaining > 258:
            clipped = _clip_context_text(block, remaining - 2)
            selected_prior.append([("user", clipped.removeprefix("[User]\n"), [])])
            remaining = 0
        break

    for turn in reversed(selected_prior):
        parts.append(_format_context_turn(turn))
    parts.append(current_block)
    full_text = "\n\n".join(part for part in parts if part)
    if len(full_text) > MAX_CONTEXT_CHARS:  # defensive invariant
        full_text = _clip_context_text(full_text, MAX_CONTEXT_CHARS)

    # Historical images cannot be attached to their original turns in the
    # ChatGPT composer. Re-uploading them here is both ambiguous and can make
    # an otherwise valid current request exceed the 8-image/30-MiB limits.
    image_references = list(latest_images)
    return (
        full_text,
        image_references,
        latest_user_text,
        bool(system_parts),
        has_history_messages,
    )

# Model mapping: user-facing names → ChatGPT web slugs
MODEL_MAP = {
    "gpt-5.5": "gpt-5-5",
    "gpt-5.5-instant": "gpt-5-5-instant",
    "gpt-5.5-thinking": "gpt-5-5-thinking",
    "gpt-5.5-pro": "gpt-5-5-pro",
    "gpt-5.6": "gpt-5-6",
    "gpt-5.6-instant": "gpt-5-6-instant",
    "gpt-5.6-thinking": "gpt-5-6-thinking",
    "gpt-5.6-sol": "gpt-5.6-sol-wm",
    "gpt-5.6-pro": "gpt-5-6-pro",
    "gpt-6": "gpt-6-astra-wm",
    "gpt-6-astra": "gpt-6-astra-wm",
    "gpt-6-pro": "gpt-6-pro",
    "gpt-5.3": "gpt-5-3",
    "gpt-5.2": "gpt-5-2",
    "gpt-5.1": "gpt-5-1",
    "gpt-5": "gpt-5",
    "gpt-5-mini": "gpt-5-mini",
    "gpt-5.3-mini": "gpt-5-3-mini",
    "auto": "auto",
    # Legacy aliases
    "gpt-4o": "auto",
    "gpt-4": "gpt-5",
    "gpt-3.5-turbo": "gpt-5-mini",
}


class APIServer:
    """OpenAI-compatible API backed by CDP automation."""

    def __init__(
        self, config: Config, driver: CDPDriver, breakers: BreakerRegistry | None = None
    ) -> None:
        self._config = config
        self._driver = driver
        self._cdp_port = config.chrome.cdp_port
        self._parallel_tabs = config.chatgpt.parallel_tabs
        self._request_count = 0
        # Health telemetry (event-derived, not polled). These are the only
        # fields that make sense to cache: they mark WHEN something happened,
        # not whether something is alive right now (that's computed live in
        # _handle_health). Without last_successful_send_at, a zombie process
        # that never connected (cdp_connected=false, requests_served=0) looks
        # identical to a freshly-started healthy one — both report "waiting".
        self._started_at = time.time()
        self._last_error: str | None = None
        self._last_successful_send_at: float | None = None
        # Non-rate-limit breaker registry (Phase 4). Injected by Service so the
        # REST process shares one registry across Chrome + driver + server.
        # Default-constructed for back-compat with tests that don't pass one.
        self._breakers = breakers or BreakerRegistry()
        # Track last conversation for multi-turn continuity
        self._last_conv_id: str | None = None
        self._last_project_id: str | None = None
        self._asset_tokens: dict[str, tuple[float, dict]] = {}
        self._asset_cache: dict[str, tuple[float, dict]] = {}
        configured_asset_dir = os.environ.get("W2A_ASSET_CACHE_DIR", "").strip()
        self._asset_cache_dir = (
            Path(configured_asset_dir).expanduser()
            if configured_asset_dir
            else Path(config.chrome.user_data_dir).expanduser().parent / "asset-cache"
        )

        self.app = web.Application(client_max_size=70 * 1024 * 1024)
        self.app.router.add_post("/v1/chat/completions", self._handle_chat)
        self.app.router.add_post("/chat/completions", self._handle_chat)
        self.app.router.add_get("/v1/models", self._handle_models)
        self.app.router.add_get("/v1/projects", self._handle_projects)
        self.app.router.add_get("/v1/assets/{token}", self._handle_asset)
        self.app.router.add_get("/health", self._handle_health)
        self.app.router.add_get("/", self._handle_health)

    # ── Auth ──────────────────────────────────────────────────

    def _check_auth(self, request: web.Request) -> web.Response | None:
        """Check API key if configured. Returns error response or None."""
        keys = self._config.server.api_keys
        if not keys:
            return None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:]
        else:
            key = request.query.get("key", "")
        if key not in keys:
            return web.json_response(
                {"error": {"message": "Invalid API key", "type": "auth_error"}},
                status=401,
            )
        return None

    def _purge_expired_assets(self, now: float | None = None) -> None:
        """Drop expired capabilities and their process-local cached bytes."""
        current = time.time() if now is None else now
        self._asset_tokens = {
            token: record
            for token, record in self._asset_tokens.items()
            if record[0] > current
        }
        self._asset_cache = {
            token: record
            for token, record in self._asset_cache.items()
            if record[0] > current and token in self._asset_tokens
        }

    @staticmethod
    def _is_image_asset(asset: dict) -> bool:
        return (
            str(asset.get("type") or "").lower() == "image"
            or str(asset.get("mime_type") or "").lower().startswith("image/")
        )

    @classmethod
    def _validated_asset_download(cls, asset: dict, downloaded: object) -> dict:
        """Validate generated image bytes before caching or returning them."""
        if not isinstance(downloaded, dict):
            raise ValueError("ChatGPT returned an invalid asset response")
        data = downloaded.get("data")
        if not isinstance(data, bytes) or not data:
            raise ValueError("ChatGPT returned empty asset data")
        result = dict(downloaded)
        is_image = cls._is_image_asset(asset)
        if len(data) > MAX_ASSET_CACHE_BYTES:
            kind = "image" if is_image else "asset"
            raise ValueError(f"Generated {kind} exceeds the 30 MiB limit")
        if is_image:
            detected_type = _sniff_image_type(data)
            if detected_type is None:
                raise ValueError("Generated image is not PNG, JPEG, WebP, or GIF")
            result["content_type"] = detected_type
        return result

    def _cache_asset_memory(self, token: str, expires_at: float, downloaded: dict) -> None:
        """Store bounded, expiring asset bytes in this worker's memory."""
        data = downloaded.get("data")
        if not isinstance(data, bytes) or len(data) > MAX_ASSET_CACHE_BYTES:
            return
        self._asset_cache.pop(token, None)
        self._asset_cache[token] = (expires_at, downloaded)
        total_bytes = sum(
            len(cached_data)
            for _, cached in self._asset_cache.values()
            if isinstance((cached_data := cached.get("data")), bytes)
        )
        while (
            len(self._asset_cache) > MAX_ASSET_CACHE_ITEMS
            or total_bytes > MAX_ASSET_CACHE_TOTAL_BYTES
        ):
            oldest = next(iter(self._asset_cache))
            evicted = self._asset_cache.pop(oldest)
            evicted_data = evicted[1].get("data")
            if isinstance(evicted_data, bytes):
                total_bytes -= len(evicted_data)

    @staticmethod
    def _asset_disk_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _asset_disk_paths(self, token: str) -> tuple[Path, Path]:
        key = self._asset_disk_key(token)
        return self._asset_cache_dir / f"{key}.json", self._asset_cache_dir / f"{key}.bin"

    def _purge_disk_assets_sync(self, now: float | None = None) -> None:
        """Remove expired/invalid disk records and enforce the shared bound."""
        current = time.time() if now is None else now
        try:
            metadata_files = list(self._asset_cache_dir.glob("*.json"))
        except OSError:
            return

        live: list[tuple[float, int, Path, Path]] = []
        for metadata_path in metadata_files:
            data_path = metadata_path.with_suffix(".bin")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                expires_at = float(metadata["expires_at"])
                size = data_path.stat().st_size
                if expires_at <= current or size <= 0 or size > MAX_ASSET_CACHE_BYTES:
                    raise ValueError("expired or invalid asset")
                live.append((metadata_path.stat().st_mtime, size, metadata_path, data_path))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                for path in (metadata_path, data_path):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass

        live.sort(key=lambda item: item[0])
        total_bytes = sum(item[1] for item in live)
        while (
            len(live) > MAX_SHARED_ASSET_CACHE_ITEMS
            or total_bytes > MAX_SHARED_ASSET_CACHE_TOTAL_BYTES
        ):
            _, size, metadata_path, data_path = live.pop(0)
            total_bytes -= size
            for path in (metadata_path, data_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _write_asset_disk_sync(
        self, token: str, expires_at: float, downloaded: dict
    ) -> None:
        """Atomically persist validated bytes for another worker or restart."""
        data = downloaded.get("data")
        if not isinstance(data, bytes) or not data or len(data) > MAX_ASSET_CACHE_BYTES:
            return
        self._asset_cache_dir.mkdir(parents=True, exist_ok=True)
        metadata_path, data_path = self._asset_disk_paths(token)
        suffix = f".{os.getpid()}.{secrets.token_hex(4)}.tmp"
        metadata_tmp = Path(str(metadata_path) + suffix)
        data_tmp = Path(str(data_path) + suffix)
        metadata = {
            "token": token,
            "expires_at": expires_at,
            "content_type": str(
                downloaded.get("content_type") or "application/octet-stream"
            ),
            "filename": str(downloaded.get("filename") or "attachment"),
        }
        try:
            data_tmp.write_bytes(data)
            metadata_tmp.write_text(json.dumps(metadata), encoding="utf-8")
            os.replace(data_tmp, data_path)
            os.replace(metadata_tmp, metadata_path)
            self._purge_disk_assets_sync()
        finally:
            for path in (metadata_tmp, data_tmp):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _load_asset_disk_sync(self, token: str) -> tuple[float, dict] | None:
        metadata_path, data_path = self._asset_disk_paths(token)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            expires_at = float(metadata["expires_at"])
            if metadata.get("token") != token or expires_at <= time.time():
                raise ValueError("asset record expired or mismatched")
            size = data_path.stat().st_size
            if size <= 0 or size > MAX_ASSET_CACHE_BYTES:
                raise ValueError("asset data size is invalid")
            data = data_path.read_bytes()
            if len(data) != size:
                raise ValueError("asset data changed while reading")
            downloaded = {
                "data": data,
                "content_type": str(
                    metadata.get("content_type") or "application/octet-stream"
                ),
                "filename": str(metadata.get("filename") or "attachment"),
            }
            return expires_at, downloaded
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            for path in (metadata_path, data_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            return None

    async def _cache_asset(
        self, token: str, expires_at: float, downloaded: dict
    ) -> None:
        """Cache an asset in memory and in the shared restart-safe store."""
        self._cache_asset_memory(token, expires_at, downloaded)
        try:
            await asyncio.to_thread(
                self._write_asset_disk_sync, token, expires_at, downloaded
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The in-memory capability remains usable even if disk persistence
            # is unavailable; report the degraded restart behavior explicitly.
            logger.warning("Generated asset disk cache write failed: %s", exc)

    async def _load_asset_disk(self, token: str) -> tuple[float, dict] | None:
        return await asyncio.to_thread(self._load_asset_disk_sync, token)

    async def _prefetch_published_assets(self, assets: list[dict]) -> None:
        """Fill the generated-image cache before capability URLs are exposed."""
        async def _prefetch(asset: dict) -> None:
            if not self._is_image_asset(asset):
                return
            token = str(asset.get("url") or "").rsplit("/", 1)[-1]
            record = self._asset_tokens.get(token)
            if not token or not record or record[0] <= time.time():
                return
            try:
                downloaded = await asyncio.wait_for(
                    self._driver.download_response_asset(record[1]),
                    timeout=ASSET_PREFETCH_TIMEOUT_SECONDS,
                )
                validated = self._validated_asset_download(record[1], downloaded)
                await self._cache_asset(token, record[0], validated)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Keep the capability alive: _handle_asset retains its existing
                # on-demand retry and human-readable 502 response.
                logger.warning("Generated image prefetch failed: %s", exc)

        candidates = [
            asset for asset in assets if self._is_image_asset(asset)
        ][:MAX_ASSET_CACHE_ITEMS]
        await asyncio.gather(*(_prefetch(asset) for asset in candidates))

    async def _prefetch_assets_with_heartbeat(
        self,
        resp: web.StreamResponse,
        assets: list[dict],
        interval: float = SSE_HEARTBEAT_SECONDS,
    ) -> None:
        """Prefetch generated images without leaving an SSE connection idle."""
        task = asyncio.create_task(self._prefetch_published_assets(assets))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    task.result()
                    return
                await resp.write(b": caching-generated-images\n\n")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _publish_assets(self, request: web.Request, assets: list[dict]) -> list[dict]:
        """Create one-hour capability URLs for model-generated assets."""
        if not isinstance(assets, list):
            return []
        now = time.time()
        self._purge_expired_assets(now)
        scheme = str(request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0]
        host = str(request.headers.get("X-Forwarded-Host") or request.host).split(",")[0]
        published: list[dict] = []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            token = secrets.token_urlsafe(24)
            self._asset_tokens[token] = (now + ASSET_TTL_SECONDS, dict(asset))
            item = dict(asset)
            item.pop("source_url", None)
            item["url"] = f"{scheme}://{host}/v1/assets/{token}"
            item["expires_at"] = int(now + ASSET_TTL_SECONDS)
            published.append(item)
        return published

    @staticmethod
    def _append_asset_markdown(text: str, assets: list[dict]) -> str:
        if not assets:
            return text
        if text.startswith("[Non-text response generated"):
            text = ""
        links = []
        for asset in assets:
            name = str(asset.get("name") or "attachment").replace("]", "_")
            url = asset["url"]
            links.append(
                f"![{name}]({url})" if asset.get("type") == "image" else f"[{name}]({url})"
            )
        return "\n\n".join(part for part in [text.strip(), *links] if part)

    async def _handle_asset(self, request: web.Request) -> web.Response:
        """Serve an opaque, expiring model-generated asset URL."""
        token = request.match_info.get("token", "")
        self._purge_expired_assets()
        record = self._asset_tokens.get(token)
        disk_cached = await self._load_asset_disk(token)
        if (not record or record[0] <= time.time()) and disk_cached is None:
            self._asset_tokens.pop(token, None)
            self._asset_cache.pop(token, None)
            raise web.HTTPNotFound(text="Asset link is invalid or expired")
        cached = self._asset_cache.get(token)
        if cached and cached[0] > time.time():
            downloaded = cached[1]
        elif disk_cached is not None:
            expires_at, downloaded = disk_cached
            self._cache_asset_memory(token, expires_at, downloaded)
        else:
            assert record is not None
            try:
                downloaded = await self._driver.download_response_asset(record[1])
                downloaded = self._validated_asset_download(record[1], downloaded)
            except Exception as exc:
                logger.warning("Asset proxy failed: %s", exc)
                raise web.HTTPBadGateway(text="Could not retrieve ChatGPT asset") from exc
            await self._cache_asset(token, record[0], downloaded)
        content_type = str(downloaded.get("content_type") or "application/octet-stream")
        content_type = content_type.split(";", 1)[0]
        source_name = record[1].get("name") if record is not None else None
        filename = str(downloaded.get("filename") or source_name or "attachment")
        filename = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)[:120]
        return web.Response(
            body=downloaded["data"],
            content_type=content_type,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Cache-Control": "private, max-age=300",
                "Access-Control-Allow-Origin": "*",
            },
        )

    # ── Handlers ──────────────────────────────────────────────

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Honest health endpoint — observes current reality, not a stale mirror.

        The old version returned ``"waiting"`` when CDP was disconnected, which
        is indistinguishable from "freshly started, connecting now" — a zombie
        process (HTTP listener up, CDP never connected) reported the same
        status as a healthy one. This version distinguishes four states:

        - ``starting``: listener up, driver not yet connected, never served
        - ``healthy``: Chrome alive AND driver connected
        - ``degraded``: Chrome alive but driver disconnected (zombie/recovering)
        - ``broken``: Chrome itself unreachable

        Live fields (chrome_running, driver_connected) are computed fresh on
        each call — /health is infrequent (supervisor poll), and cached state
        would lag reality. Event-derived fields (started_at, last_error,
        last_successful_send_at, requests_served) are tracked on the instance.
        """
        import urllib.request

        driver_connected = bool(self._driver.is_connected)

        # Chrome liveness: cheap HTTP GET to /json/version. If Chrome is dead,
        # this fails fast (connection refused). Run synchronously — /health is
        # infrequent and the call is sub-millisecond on loopback.
        chrome_running = False
        try:
            loop = asyncio.get_event_loop()

            def _probe():
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{self._cdp_port}/json/version", timeout=2
                    ) as r:
                        return r.status == 200
                except Exception:
                    return False

            chrome_running = await loop.run_in_executor(None, _probe)
        except Exception:
            chrome_running = False

        # Status logic — zombie case (Chrome up, driver dead) is "degraded",
        # never "ok"/"waiting". The old "waiting" non-answer is gone.
        if not chrome_running:
            status = "broken"
        elif not driver_connected:
            status = "degraded"
        elif self._last_successful_send_at is None and self._request_count == 0:
            status = "starting"
        else:
            status = "healthy"

        # An open breaker can only DOWNGRADE starting|healthy -> degraded. It
        # must never override "broken" (Chrome down is a harder failure than a
        # tripped circuit) and never force "broken" — auth_required is serious,
        # but "broken" invites a destructive supervisor restart, while
        # "degraded" correctly signals "up but refusing some/all traffic". A
        # disconnect-degraded stays degraded (not worse).
        if status in ("starting", "healthy") and self._breakers.first_open() is not None:
            status = "degraded"

        # Current-state summary, distinct from the historical/latching last_error.
        open_kinds = [k.value for k in BreakerKind if self._breakers.is_open(k)]

        return web.json_response(
            {
                "status": status,
                "chrome_running": chrome_running,
                "cdp_connected": driver_connected,
                "driver_connected": driver_connected,
                "requests_served": self._request_count,
                "started_at": self._started_at,
                "last_successful_send_at": self._last_successful_send_at,
                "last_error": self._last_error,
                "open_breakers": open_kinds,
                "breakers": self._breakers.snapshot(),
            }
        )

    async def _handle_models(self, request: web.Request) -> web.Response:
        if err := self._check_auth(request):
            return err
        try:
            raw = await self._driver.get_models()
        except Exception:
            raw = []

        models = []
        for m in raw:
            slug = m.get("slug", "")
            models.append(
                {
                    "id": slug,
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": "chatgpt-web",
                }
            )

        if not models:
            for slug in ["auto", "gpt-5-5", "gpt-5-mini"]:
                models.append(
                    {
                        "id": slug,
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "chatgpt-web",
                    }
                )

        return web.json_response({"object": "list", "data": models})

    async def _handle_projects(self, request: web.Request) -> web.Response:
        if err := self._check_auth(request):
            return err
        try:
            projects = await self._driver.get_projects()
        except Exception as e:
            logger.error("Failed to get projects: %s", e)
            projects = []
        return web.json_response({"object": "list", "data": projects})

    async def _handle_chat(self, request: web.Request) -> web.Response:
        if err := self._check_auth(request):
            return err

        self._request_count += 1

        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response(
                {"error": {"message": "Invalid JSON", "type": "invalid_request_error"}},
                status=400,
            )

        if not isinstance(body, dict):
            return web.json_response(
                {
                    "error": {
                        "message": "Request body must be a JSON object",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )

        messages = body.get("messages", [])
        if not isinstance(messages, list) or not messages:
            return web.json_response(
                {
                    "error": {
                        "message": "messages must be a non-empty array",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )

        model = body.get("model", self._config.chatgpt.default_model)
        if not isinstance(model, str) or not model.strip():
            return web.json_response(
                {
                    "error": {
                        "message": "model must be a non-empty string",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        model = model.strip()
        stream = body.get("stream", False)
        if not isinstance(stream, bool):
            return web.json_response(
                {
                    "error": {
                        "message": "stream must be a boolean",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        metadata = body.get("metadata")
        if metadata is None:
            metadata = {}
        elif not isinstance(metadata, dict):
            return web.json_response(
                {
                    "error": {
                        "message": "metadata must be an object",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        project_id = body.get("project_id")
        if project_id is None or project_id == "":
            project_id = body.get("gizmo_id")
        if project_id is None or project_id == "":
            project_id = metadata.get("project_id")
        if project_id is None or project_id == "":
            project_id = self._config.chatgpt.default_project_id
        if project_id is not None and not isinstance(project_id, str):
            return web.json_response(
                {
                    "error": {
                        "message": "project_id must be a string",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        raw_conversation_id = body.get("conversation_id")
        if raw_conversation_id is None or raw_conversation_id == "":
            conversation_id = None
        elif not _valid_conversation_id(raw_conversation_id):
            return web.json_response(
                {
                    "error": {
                        "message": "Invalid conversation_id",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        else:
            conversation_id = raw_conversation_id

        # Build a bounded bootstrap prompt, or a one-turn continuation when an
        # explicit ChatGPT conversation id is available.
        (
            full_text,
            image_references,
            latest_user_text,
            _has_system_parts,
            _has_history_messages,
        ) = _build_chat_context(messages, conversation_id=conversation_id)

        # A completion request must end at the pending user turn. Silently
        # searching backwards for a user would replay an already-answered
        # prompt when a client accidentally sends an assistant-ending history.
        dialogue_roles = [
            str(message.get("role") or "").lower()
            for message in messages
            if isinstance(message, dict)
            and str(message.get("role") or "").lower() in {"user", "assistant"}
        ]
        if not dialogue_roles or "user" not in dialogue_roles:
            return web.json_response(
                {"error": {"message": "No user message", "type": "invalid_request_error"}},
                status=400,
            )
        if dialogue_roles[-1] != "user":
            return web.json_response(
                {
                    "error": {
                        "message": "The last conversation message must be from the user",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )

        if len(image_references) > MAX_IMAGE_COUNT:
            return web.json_response(
                {
                    "error": {
                        "message": f"Too many images; maximum is {MAX_IMAGE_COUNT}",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )

        image_tempdir = None
        image_paths: list[str] = []
        try:
            if image_references and not stream:
                image_tempdir = tempfile.TemporaryDirectory(prefix="web2api-images-")
                image_paths = await prepare_image_files(image_references, image_tempdir.name)
        except ImageInputError as exc:
            if image_tempdir is not None:
                image_tempdir.cleanup()
            return web.json_response(
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
                status=400,
            )

        model_slug = MODEL_MAP.get(model, model)
        expect_non_text = wants_image_output(body, latest_user_text)
        # ChatGPT image tools can take many minutes before publishing their
        # first assistant asset, so all image-specific detector budgets share
        # the full 15-minute request window.
        timeout = max(
            self._config.server.request_timeout, IMAGE_GENERATION_TIMEOUT_SECONDS
        ) if expect_non_text else (
            self._config.server.request_timeout
        )
        # Pro/reasoning models can spend several minutes in web search or tool
        # execution before publishing their terminal answer.  The detector has
        # a 300-second first-content budget for these models, so the outer
        # request deadline must not expire earlier than that budget.
        from .completion_detector import classify_model

        if classify_model(model_slug) == "reasoning":
            timeout = max(timeout, REASONING_REQUEST_TIMEOUT_SECONDS)

        logger.info(
            "Request #%d: model=%s->%s conv=%s project=%s stream=%s non_text=%s msg=%.60s",
            self._request_count,
            model,
            model_slug,
            conversation_id,
            project_id,
            stream,
            expect_non_text,
            full_text,
        )

        # Serialize — cross-process lock so MCP + REST don't corrupt each other
        try:
            # Circuit-open fail-fast (Phase 4 PR2): refuse before touching Chrome
            # if a breaker is open. Placed inside the try so it flows through
            # the except below → _error_response + _last_error, consistent with
            # every other failure path. Checked before acquiring the lock so a
            # process that already knows it will refuse doesn't block on the
            # browser lock. If AUTH_EXPIRED is open, probes auth recovery first
            # (the user may have logged back in).
            await self._check_circuit_or_recover()

            # PR4/5: per-target lock in parallel mode (port-wide otherwise).
            # Resolver raises OwnedTabRequiredError (→ 503) if parallel mode
            # has no owned target rather than silently degrading to the port
            # lock (split-brain guard). When parallel mode is OFF, skip the
            # resolver entirely and use the cached port — preserves the exact
            # legacy path (the resolver would read driver.port, which is the
            # same value but needlessly couples the legacy path to the driver).
            if self._parallel_tabs:
                _port, _key = resolve_mutation_lock(self._driver, True)
            else:
                _port, _key = self._cdp_port, None
            async with MutationLock(_port, _key):
                # Drift guard (parallel mode only): if the owned target changed
                # while we waited for the lock, the key we hold no longer names
                # the active tab. Fail retryably instead of mutating under a
                # stale key.
                if self._parallel_tabs:
                    _, _current_key = resolve_mutation_lock(self._driver, True)
                    if _current_key != _key:
                        raise OwnedTabRequiredError(
                            "owned target changed while waiting for mutation lock"
                        )
                # Second circuit-open check, now that we hold the lock. A
                # concurrent request may have tripped a breaker while we were
                # waiting. Without this, we'd drive Chrome despite the process
                # already knowing the circuit is open.
                await self._check_circuit_or_recover()

                # Validate/select the requested model. Fail closed so the API
                # never labels an answer as one model while ChatGPT silently
                # used another active browser model.
                if model_slug and model_slug != "auto":
                    selected = await self._driver.select_model(model_slug)
                    if not selected:
                        raise ModelSelectionError(model_slug)

                # Conversation isolation is explicit: an omitted conversation_id
                # ALWAYS means a new chat.  Reusing _last_conv_id here caused two
                # independent HTTP requests on the same worker to share a page,
                # allowing the second request to observe the first request's DOM
                # answer/action row. Only a caller-supplied id may continue.
                if conversation_id:
                    # Explicit conversation_id from client — navigate to it
                    await self._driver.navigate_conversation(conversation_id)
                else:
                    # Invalidate both server and driver state before navigation,
                    # so even a navigation failure cannot leave an earlier id
                    # eligible for accidental reuse or misleading diagnostics.
                    logger.info(
                        "No conversation_id supplied; starting an isolated fresh conversation"
                    )
                    self._last_conv_id = None
                    self._last_project_id = None
                    self._driver._current_conv_id = None
                    await self._driver.navigate_new_chat(
                        gizmo_id=project_id, model_slug=model_slug
                    )
                    self._last_project_id = project_id

                if stream:
                    return await self._stream_response(
                        request, model_slug, full_text, timeout,
                        image_paths=image_paths, image_references=image_references,
                        expect_non_text=expect_non_text,
                    )
                else:
                    return await self._full_response(
                        request, model_slug, full_text, timeout,
                        image_paths=image_paths, expect_non_text=expect_non_text,
                    )

        except Exception as e:
            logger.error("Chat error: %s", e, exc_info=True)
            self._last_error = f"{type(e).__name__}: {e}"
            return self._error_response(e)
        finally:
            if image_tempdir is not None:
                image_tempdir.cleanup()

    async def _check_circuit_or_recover(self) -> None:
        """Fail-fast if a breaker is open, with one exception: if AUTH_EXPIRED
        is the open breaker, probe auth recovery first (the user may have logged
        back in via the browser since the trip). If recovery succeeds the breaker
        is reset and the request proceeds; if it fails, or if a non-auth breaker
        is open, raise CircuitOpenError.

        Called at each fail-fast checkpoint (pre-lock, post-lock, streaming
        pre-prepare). Does NOT drive a chat send — recovery is a lightweight
        ``/api/auth/session`` token fetch via ``driver.recover_auth()``.
        """
        open_kind = self._breakers.first_open()
        if open_kind is None:
            return
        if open_kind is BreakerKind.AUTH_EXPIRED:
            if await self._driver.recover_auth():
                # Auth restored — re-check in case another breaker is also open.
                open_kind = self._breakers.first_open()
                if open_kind is None:
                    return
        raise CircuitOpenError(open_kind)

    # ── Error mapping ─────────────────────────────────────────

    def _error_response(self, exc: Exception) -> web.Response:
        """Map a driver exception to an OpenAI-shaped error response.

        - RateLimitError → HTTP 429 with the canonical OpenAI
          ``rate_limit_exceeded`` type/code and a ``Retry-After`` header, so any
          OpenAI-aware agent framework (SDK, LangChain, LlamaIndex) automatically
          backs off and retries with zero client integration.
        - AuthExpiredError → HTTP 401 ``invalid_api_key`` — the ChatGPT session
          expired; previously this surfaced as silent empty data or a generic
          timeout.
        - GenerationStuckError → HTTP 504 ``generation_stuck`` — the generation
          stalled (no DOM progress within the stall window); the phase is in the
          message for diagnosis.
        - Everything else stays a 500 ``server_error`` (a real failure, not
          retriable).
        """
        if isinstance(exc, RateLimitError):
            retry_after = str(int(exc.retry_after))
            return web.json_response(
                {
                    "error": {
                        "message": str(exc),
                        "type": "rate_limit_exceeded",
                        "param": None,
                        "code": "rate_limit_exceeded",
                    }
                },
                status=429,
                headers={"Retry-After": retry_after},
            )
        if isinstance(exc, AuthExpiredError):
            return web.json_response(
                {
                    "error": {
                        "message": str(exc),
                        "type": "invalid_api_key",
                        "param": None,
                        "code": "invalid_api_key",
                    }
                },
                status=401,
            )
        if isinstance(exc, ModelSelectionError):
            return web.json_response(
                {
                    "error": {
                        "message": str(exc),
                        "type": "invalid_request_error",
                        "param": "model",
                        "code": "model_not_available",
                    }
                },
                status=400,
            )
        if isinstance(exc, GenerationStuckError):
            return web.json_response(
                {
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "param": None,
                        "code": "generation_stuck",
                    }
                },
                status=504,
            )
        if isinstance(exc, LockAcquisitionError):
            return web.json_response(
                {
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "param": None,
                        "code": "lock_timeout",
                    }
                },
                status=503,
            )
        if isinstance(exc, CircuitOpenError):
            return web.json_response(
                {
                    "error": {
                        "message": (
                            f"Circuit open for {exc.kind.value} — cooling down. Retry later."
                        ),
                        "type": "server_error",
                        "param": None,
                        "code": "circuit_open",
                    }
                },
                status=503,
            )
        if isinstance(exc, OwnedTabRequiredError):
            return web.json_response(
                {
                    "error": {
                        "message": f"{exc}. Retry later.",
                        "type": "server_error",
                        "param": None,
                        "code": "owned_tab_required",
                    }
                },
                status=503,
            )
        return web.json_response(
            {"error": {"message": str(exc), "type": "server_error"}},
            status=500,
        )

    # ── Response formatters ───────────────────────────────────

    async def _full_response(
        self, request: web.Request, model: str, text: str, timeout: float,
        image_paths: list[str] | None = None,
        expect_non_text: bool = False,
    ) -> web.Response:
        """Non-streaming: collect all chunks, return one JSON.

        The send is wrapped in ``retry_on_rate_limit`` so a transient
        ChatGPT "Too many requests" pop-up is dismissed and retried
        transparently — the client only sees it (as a 429) if the limit
        persists across all retries.
        """
        # P1: resolve model-aware detector budgets from config.
        from .completion_detector import DetectorBudgets

        budgets = DetectorBudgets.from_config(self._config.chatgpt, model)
        if expect_non_text:
            budgets = DetectorBudgets(
                first_content_timeout_seconds=max(
                    budgets.first_content_timeout_seconds,
                    IMAGE_GENERATION_TIMEOUT_SECONDS,
                ),
                stream_idle_timeout_seconds=max(
                    budgets.stream_idle_timeout_seconds,
                    IMAGE_GENERATION_TIMEOUT_SECONDS,
                ),
                hard_timeout_seconds=max(budgets.hard_timeout_seconds, timeout),
            )

        async def _send_and_collect() -> str:
            collected = ""
            send_kwargs = {"attachments": image_paths} if image_paths else {}
            if expect_non_text:
                send_kwargs["expect_non_text"] = True
            async for chunk in self._driver.send_and_stream(
                text, timeout=timeout, budgets=budgets, model=model, **send_kwargs,
            ):
                collected += chunk.delta
            return collected

        full_text = await retry_on_rate_limit(self._driver, _send_and_collect)

        conv_id = self._driver._current_conv_id or ""
        self._last_conv_id = conv_id
        self._last_successful_send_at = time.time()
        raw_assets = getattr(self._driver, "_last_response_assets", [])
        assets = self._publish_assets(
            request, raw_assets if isinstance(raw_assets, list) else []
        )
        await self._prefetch_published_assets(assets)
        full_text = self._append_asset_markdown(full_text, assets)
        message = {"role": "assistant", "content": full_text}
        if assets:
            message["attachments"] = assets

        return web.json_response(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "conversation_id": conv_id,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )

    async def _stream_response(
        self, request: web.Request, model: str, text: str, timeout: float,
        image_paths: list[str] | None = None,
        image_references: list[ImageReference] | None = None,
        expect_non_text: bool = False,
    ) -> web.Response:
        """Streaming: SSE chunks as they arrive.

        Rate-limit handling for streaming is split, because once
        ``resp.prepare()`` commits the HTTP 200 status we can no longer send a
        429:

        - **Pre-flight** (before prepare): a single DOM scan. If throttled, we
          retry transparently (dismiss + backoff). If it persists, we return a
          proper 429 here while the status is still changeable.
        - **Mid-stream** (after prepare): a throttle is rare here (pre-flight
          cleared it), but if one occurs it falls back to the inline
          ``[Error: ...]`` SSE chunk — documented as a known limitation.
        """
        # P1: resolve model-aware detector budgets from config.
        from .completion_detector import DetectorBudgets

        budgets = DetectorBudgets.from_config(self._config.chatgpt, model)
        if expect_non_text:
            budgets = DetectorBudgets(
                first_content_timeout_seconds=max(
                    budgets.first_content_timeout_seconds,
                    IMAGE_GENERATION_TIMEOUT_SECONDS,
                ),
                stream_idle_timeout_seconds=max(
                    budgets.stream_idle_timeout_seconds,
                    IMAGE_GENERATION_TIMEOUT_SECONDS,
                ),
                hard_timeout_seconds=max(budgets.hard_timeout_seconds, timeout),
            )

        async def _preflight() -> None:
            """Raise RateLimitError if the pop-up is present right now."""
            try:
                scan = await self._driver._js_strict(
                    "(function(){var t=(document.body&&document.body.innerText)||'';"
                    "return JSON.stringify({text:t.slice(0,4000)});})()",
                    timeout=10,
                )
            except Exception:
                # CDP/JS error during scan — assume no rate limit (proceed).
                return
            try:
                body = json.loads(scan).get("text", "") if scan else ""
            except (json.JSONDecodeError, TypeError):
                body = ""
            if is_rate_limited_text(body):
                raise RateLimitError.from_text(body)

        # Transparent pre-flight retry — dismisses the pop-up and retries so a
        # transient limit never reaches the client as an error.
        try:
            await retry_on_rate_limit(self._driver, _preflight, max_attempts=3)
        except RateLimitError:
            # Persistent at pre-flight: still pre-prepare, so send a clean 429.
            raise

        # Circuit-open fail-fast (Phase 4 PR2): final check, after rate-limit
        # preflight but still before prepare() commits HTTP 200. A breaker may
        # have opened during model selection/navigation. After prepare() no
        # status change is possible, so this must stay pre-prepare.
        await self._check_circuit_or_recover()

        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        await resp.prepare(request)

        cid = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        created = int(time.time())

        # Role chunk
        await self._send_sse(
            resp,
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            },
        )

        stream_image_tempdir = None
        try:
            if image_references:
                stream_image_tempdir = tempfile.TemporaryDirectory(prefix="web2api-images-")
                image_paths = await self._prepare_images_with_heartbeat(
                    resp, image_references, stream_image_tempdir.name
                )
            send_kwargs = {"attachments": image_paths} if image_paths else {}
            if expect_non_text:
                send_kwargs["expect_non_text"] = True
            stream = self._driver.send_and_stream(
                text, timeout=timeout, budgets=budgets, model=model, **send_kwargs,
            )
            async for chunk in self._with_heartbeat(stream):
                if chunk is None:
                    # SSE comments are ignored by OpenAI-compatible clients,
                    # while still keeping Cloudflare and browsers from treating
                    # a long Pro/search turn as an idle, broken connection.
                    await resp.write(b": keep-alive\n\n")
                    continue
                if chunk.delta:
                    await self._send_sse(
                        resp,
                        {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": chunk.delta},
                                    "finish_reason": None,
                                }
                            ],
                        },
                    )
                if chunk.finish_reason:
                    conv_id = self._driver._current_conv_id or ""
                    self._last_conv_id = conv_id
                    if chunk.finish_reason == "stop":
                        self._last_successful_send_at = time.time()
                    raw_assets = getattr(self._driver, "_last_response_assets", [])
                    assets = self._publish_assets(
                        request, raw_assets if isinstance(raw_assets, list) else []
                    )
                    if assets:
                        await self._prefetch_assets_with_heartbeat(resp, assets)
                        markdown = self._append_asset_markdown("", assets)
                        await self._send_sse(
                            resp,
                            {
                                "id": cid,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"content": markdown, "attachments": assets},
                                    "finish_reason": None,
                                }],
                            },
                        )
                    await self._send_sse(
                        resp,
                        {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "conversation_id": conv_id,
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": chunk.finish_reason}
                            ],
                        },
                    )
        except RateLimitError as e:
            # Mid-stream throttle (rare after pre-flight). Status is locked at
            # 200, so we can't upgrade to 429; surface as an inline error chunk
            # with a recognizable marker so clients can detect it.
            logger.warning("Mid-stream rate limit: %s", e)
            await self._send_sse(
                resp,
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": f"\n\n[Error: rate_limit_exceeded — retry in {e.retry_after}s]"
                            },
                            "finish_reason": "error",
                        }
                    ],
                },
            )
        except AuthExpiredError:
            # Session expired mid-stream (status locked at 200). Surface with a
            # recognizable marker so clients can prompt re-login.
            logger.warning("Mid-stream auth expiry")
            await self._send_sse(
                resp,
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "\n\n[Error: auth_expired — re-login required]"},
                            "finish_reason": "error",
                        }
                    ],
                },
            )
        except GenerationStuckError as e:
            # Status is already locked at 200, so expose a machine-readable
            # error without injecting English diagnostics into assistant text.
            logger.warning("Mid-stream generation stuck: %s", e)
            await self._send_sse(
                resp,
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "error": {
                        "message": "生成等待超时，请检查专用浏览器后重试",
                        "type": "server_error",
                        "code": "generation_stuck",
                        "phase": e.phase,
                        "stalled_for_seconds": round(e.stalled_for_s, 1),
                    },
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "error",
                        }
                    ],
                },
            )
        except Exception as e:
            logger.error("Stream error: %s", e)
            await self._send_sse(
                resp,
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"\n\n[Error: {e}]"},
                            "finish_reason": "error",
                        }
                    ],
                },
            )
        finally:
            if stream_image_tempdir is not None:
                stream_image_tempdir.cleanup()

        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    @staticmethod
    async def _prepare_images_with_heartbeat(
        resp: web.StreamResponse,
        references: list[ImageReference],
        directory: str,
        interval: float = SSE_HEARTBEAT_SECONDS,
    ) -> list[str]:
        task = asyncio.create_task(prepare_image_files(references, directory))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    return task.result()
                await resp.write(b": preparing-reference-images\n\n")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


    @staticmethod
    async def _with_heartbeat(stream, interval: float = SSE_HEARTBEAT_SECONDS):
        """Yield stream items, inserting ``None`` while the source is idle.

        ``asyncio.wait_for(anext(...))`` would cancel the source generator at
        every heartbeat timeout.  Keeping one pending ``anext`` task avoids
        interrupting the browser turn while allowing the HTTP layer to write an
        SSE comment periodically.
        """
        iterator = stream.__aiter__()
        pending = asyncio.create_task(anext(iterator))
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=interval)
                if not done:
                    yield None
                    continue
                try:
                    item = pending.result()
                except StopAsyncIteration:
                    break
                yield item
                pending = asyncio.create_task(anext(iterator))
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            close = getattr(iterator, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception as exc:
                    # Cleanup must not replace the original client-disconnect
                    # or stream error that caused the consumer to stop early.
                    logger.debug("Async response iterator close failed: %s", exc)

    @staticmethod
    async def _send_sse(resp: web.StreamResponse, data: dict) -> None:
        await resp.write(f"data: {json.dumps(data)}\n\n".encode())
