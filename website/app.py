from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import heapq
import hmac
import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sqlite3
import time
import unicodedata
import uuid
import zipfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from PIL import Image, ImageOps
from pypdf import PdfReader


LOG = logging.getLogger("ai-chat")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("AI_CHAT_DATA_DIR", BASE_DIR / "data")).resolve()
WORKSPACE_ROOT = Path(os.getenv("AI_CHAT_WORKSPACE_ROOT", BASE_DIR / "workspaces")).resolve()
CODEX_HOME = Path(os.getenv("CODEX_HOME", DATA_DIR / "codex")).resolve()
CODEX_BIN = os.getenv("CODEX_BIN", "codex")
ACCESS_PASSWORD = os.getenv("AI_CHAT_ACCESS_PASSWORD", "")
SESSION_SECRET = os.getenv("AI_CHAT_SESSION_SECRET", "")
TRANSFER_SECRET = os.getenv("AI_CHAT_TRANSFER_SECRET", SESSION_SECRET)
TRANSFER_PUBLIC_BASE_URL = os.getenv("AI_CHAT_PUBLIC_BASE_URL", "").strip().rstrip("/")
ADMIN_USERNAME = os.getenv("AI_CHAT_ADMIN_USERNAME", "admin").strip() or "admin"
IMAGE_BRIDGE_URL = os.getenv("AI_CHAT_IMAGE_BRIDGE_URL", "http://127.0.0.1:13003").rstrip("/")
IMAGE_BRIDGE_TOKEN = os.getenv("AI_CHAT_IMAGE_BRIDGE_TOKEN", "")
UPSTREAM_URL = os.getenv("AI_CHAT_UPSTREAM_URL", "http://127.0.0.1:9000").rstrip("/")
RESTRICTED_API_KEY = os.getenv("AI_CHAT_RESTRICTED_API_KEY", "")
RESTRICTED_API_MODEL = os.getenv("AI_CHAT_RESTRICTED_API_MODEL", "gpt-5.3-codex-spark").strip()
PROVIDERS_PATH = DATA_DIR / "providers.json"
CODEX_MODEL_SETTINGS_PATH = DATA_DIR / "codex-model-settings.json"
CONVERSATIONS_PATH = DATA_DIR / "conversations.sqlite3"

MAX_FILE_BYTES = 30 * 1024 * 1024
MAX_UPLOAD_BATCH_BYTES = 30 * 1024 * 1024
MAX_USER_CONTENT_BYTES = 30 * 1024 * 1024
MAX_INLINE_MESSAGE_BYTES = 60_000
UPLOAD_CHUNK_BYTES = 3 * 1024 * 1024
UPLOAD_STREAM_BYTES = 1024 * 1024
UPLOAD_STAGING_DIR = ".upload-staging"
UPLOAD_STAGING_TTL_SECONDS = 60 * 60
MAX_CHUNK_UPLOAD_LOCKS = 2_048
MAX_OUTPUT_FILE_BYTES = 30 * 1024 * 1024
MAX_FILES_PER_UPLOAD = 5
MAX_EXTRACTED_CHARS = 120_000
MAX_OFFICE_XML_BYTES = 20 * 1024 * 1024
MAX_CLOUD_CONVERSATIONS = 200
MAX_CLOUD_MESSAGES = 1_000
MAX_CLOUD_CONTENT_CHARS = 8_000_000
MAX_CLOUD_PROJECTS = 50
MAX_PROJECT_FILES = 20
MAX_PROJECT_INSTRUCTIONS = 12_000
MAX_CONTEXT_TEXT_CHARS = 150_000
MAX_RECENT_HISTORY_TURNS = 30
MAX_OLDER_CONTEXT_CHARS = 20_000
MAX_CURRENT_ATTACHMENT_CONTEXT_CHARS = 48_000
MAX_PROJECT_ATTACHMENT_CONTEXT_CHARS = 24_000
MAX_PROJECT_FILE_EXCERPT_CHARS = 8_000
MAX_PROJECT_CONTEXT_SOURCE_BYTES = 30 * 1024 * 1024
MAX_CONTEXT_QUERY_SCAN_CHARS = 8_192
MAX_CONTEXT_QUERY_SAMPLE_WINDOWS = 16
MAX_EXTERNAL_IMAGES = 8
MAX_EXTERNAL_IMAGE_BYTES = 30 * 1024 * 1024
EXTERNAL_SYSTEM_VERSION = "ai-chat-v3"
EXTERNAL_SYSTEM_PROMPT = "你是 AI Chat 中的通用中文 AI 助手。直接、准确、自然地回答用户问题。"
SESSION_TTL_SECONDS = 24 * 60 * 60
SITE_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
LOGIN_MAX_FAILURES = 3
LOGIN_LOCK_SECONDS = 10 * 60
PASSWORD_HASH_ITERATIONS = 600_000
MAX_SITE_USERS = 50
MAX_HOURLY_MESSAGE_LIMIT = 10_000
MESSAGE_LIMIT_WINDOW_SECONDS = 60 * 60
MAX_MEMBER_EXPIRY_DAYS = 3_650
MAX_MEMBER_EXPIRY_HOURS = MAX_MEMBER_EXPIRY_DAYS * 24
SESSION_RE = re.compile(r"^[a-f0-9]{32}$")
UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{32}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{2,32}$")
SAFE_FILE_RE = re.compile(r"[^\w.()\-\u4e00-\u9fff]+", re.UNICODE)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
}
IMAGE_CACHE_DIR = ".image-cache"
IMAGE_PREVIEW_MAX_EDGE = 960
IMAGE_PREVIEW_QUALITY = 76
IMAGE_COMPRESSED_MAX_EDGE = 2048
IMAGE_COMPRESSED_QUALITY = 88
IMAGE_DERIVATIVE_VERSION = "v1"
TRANSFER_URL_TTL_SECONDS = 15 * 60
TRANSFER_IMAGE_MAX_EDGE = 1600
TRANSFER_IMAGE_QUALITY = 82
TRANSFER_IMAGE_COMPRESS_MIN_BYTES = 256 * 1024
TRANSFER_CACHE_DIR = ".transfer-cache"
LONG_TASK_HEARTBEAT_SECONDS = 15.0
IMAGE_BRIDGE_REQUEST_TIMEOUT_SECONDS = 20 * 60
EXTERNAL_ASSET_DOWNLOAD_TIMEOUT_SECONDS = 3 * 60
EXTERNAL_ASSET_DOWNLOAD_ATTEMPTS = 3
EXTERNAL_ASSET_ATTEMPT_TIMEOUT_SECONDS = 55
EXTERNAL_RESPONSE_TIMEOUT_SECONDS = 20 * 60
EXTERNAL_ASSET_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504, 520, 522, 524}
EXTERNAL_ASSET_PATH_RE = re.compile(r"^/v1/assets/[A-Za-z0-9_-]{16,256}$")
EXTERNAL_ASSET_MARKDOWN_RE = re.compile(
    r"!\[([^\]\r\n]{0,120})\]\((https?://[^)\s]+)\)", re.IGNORECASE)
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".html", ".css", ".xml", ".yaml", ".yml", ".toml", ".ini", ".log", ".sql",
    ".sh", ".ps1", ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rs",
}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | OFFICE_EXTENSIONS | TEXT_EXTENSIONS | {".pdf"}
PROVIDER_ID_RE = re.compile(r"^[a-f0-9]{12}$")
CODEX_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
IMAGE_REQUEST_RE = re.compile(
    r"(?:"
    r"(?:^|[\s，。！？,.!?])(?:请|麻烦)?(?:帮我|给我)?"
    r"(?:生成|画|绘制|创建|制作|做)(?:一张|一个|几张|一些)?[^。！？\n]{0,100}"
    r"(?:图片|图像|插画|海报|壁纸|头像|照片|图|画)"
    r"|(?:^|[\s，。！？,.!?])(?:请|麻烦)?(?:帮我|给我)?"
    r"(?:用|使用|调用|让)\s*(?:gpt[\s_-]*)?image\s*[\s_-]*2(?:\s*[._-]\s*5)?"
    r"(?:[\s_-]*(?:sunburst|flare))?\s*(?:来|帮我|给我)?\s*"
    r"(?:生成|画|绘制|创建|制作|做)"
    r"|\b(?:generate|create|draw|make)\b.{0,100}\b(?:image|picture|illustration|poster|wallpaper|avatar)\b"
    r")",
    re.IGNORECASE,
)
IMAGE_DISCUSSION_RE = re.compile(
    r"(?:为什么|为何|怎么|如何|是否|能否|支不支持|可以吗|是什么).{0,24}(?:生成|画|绘制|制作).{0,24}(?:图片|图像|图|画)",
    re.IGNORECASE,
)
PROVIDER_PRESETS = {
    "openai": {"name": "OpenAI", "baseUrl": "https://api.openai.com/v1", "protocol": "openai"},
    "deepseek": {"name": "DeepSeek", "baseUrl": "https://api.deepseek.com", "protocol": "openai"},
    "kimi": {"name": "Kimi", "baseUrl": "https://api.moonshot.cn/v1", "protocol": "openai"},
    "glm": {"name": "智谱 GLM", "baseUrl": "https://open.bigmodel.cn/api/paas/v4", "protocol": "openai"},
    "grok": {"name": "xAI Grok", "baseUrl": "https://api.x.ai/v1", "protocol": "openai"},
    "claude": {"name": "Anthropic Claude", "baseUrl": "https://api.anthropic.com/v1", "protocol": "anthropic"},
    "custom": {"name": "自定义中转", "baseUrl": "", "protocol": "openai"},
}
EFFORT_LABELS = {
    "low": "轻度",
    "medium": "中",
    "high": "高",
    "xhigh": "极高",
    "max": "最高",
    "ultra": "Ultra",
}
CODEX_API_EQUIVALENT_PRICING_USD = {
    "gpt-6-astra": (10.0, 50.0),
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.6": (4.0, 20.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.2, 1.2),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.3-codex": (1.75, 14.0),
    "gpt-5.2-codex": (1.75, 14.0),
}
CODEX_LONG_CONTEXT_PRICING_MODELS = {
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6", "gpt-5.6-terra", "gpt-5.6-luna",
}


def _prepare_directories() -> None:
    for directory in (DATA_DIR, WORKSPACE_ROOT, CODEX_HOME, DATA_DIR / "home"):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    _init_conversations_db()


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS
    )
    salt_text = base64.urlsafe_b64encode(salt).decode().rstrip("=")
    hash_text = base64.urlsafe_b64encode(derived).decode().rstrip("=")
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt_text}${hash_text}"


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, expected_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        if not 100_000 <= iterations <= 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        supplied = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        supplied_text = base64.urlsafe_b64encode(supplied).decode().rstrip("=")
        return secrets.compare_digest(supplied_text, expected_text)
    except (TypeError, ValueError, binascii.Error):
        return False


def _normalized_username(value: str) -> str:
    username = value.strip()
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(400, "用户名需为 2～32 位中文、字母、数字、下划线或连字符")
    return username


@contextmanager
def _database_connection():
    database = sqlite3.connect(CONVERSATIONS_PATH, timeout=10)
    try:
        with database:
            yield database
    finally:
        database.close()


def _ensure_column(database: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in database.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        database.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_legacy_workspaces(database: sqlite3.Connection, owner_id: str) -> None:
    migrated = database.execute(
        "SELECT value FROM app_meta WHERE key = 'workspace_layout_v2'"
    ).fetchone()
    if migrated:
        return
    known_users = {str(row[0]) for row in database.execute("SELECT id FROM users")}
    owner_root = (WORKSPACE_ROOT / owner_id).resolve()
    owner_root.mkdir(parents=True, exist_ok=True)
    for child in list(WORKSPACE_ROOT.iterdir()):
        if not child.is_dir() or not SESSION_RE.fullmatch(child.name) or child.name in known_users:
            continue
        target = owner_root / child.name
        if not target.exists():
            shutil.move(str(child), str(target))
    database.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('workspace_layout_v2', '1')"
    )


def _init_conversations_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _database_connection() as database:
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=NORMAL")
        database.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                disabled INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )"""
        )
        _ensure_column(
            database,
            "users",
            "hourly_message_limit",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(database, "users", "expires_at", "INTEGER")
        database.execute(
            """CREATE TABLE IF NOT EXISTS site_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )"""
        )
        database.execute(
            """CREATE TABLE IF NOT EXISTS message_events (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )"""
        )
        database.execute(
            """CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        if not database.execute("SELECT 1 FROM users LIMIT 1").fetchone() and ACCESS_PASSWORD:
            _normalized_username(ADMIN_USERNAME)
            database.execute(
                """INSERT INTO users
                (id, username, password_hash, is_admin, disabled, created_at)
                VALUES (?, ?, ?, 1, 0, ?)""",
                (uuid.uuid4().hex, ADMIN_USERNAME, _password_hash(ACCESS_PASSWORD), int(time.time())),
            )
        owner_row = database.execute(
            "SELECT id FROM users ORDER BY is_admin DESC, created_at ASC LIMIT 1"
        ).fetchone()
        owner_id = str(owner_row[0]) if owner_row else ""
        database.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )"""
        )
        database.execute(
            """CREATE TABLE IF NOT EXISTS conversation_tombstones (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                deleted_at INTEGER NOT NULL
            )"""
        )
        database.execute(
            """CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )"""
        )
        database.execute(
            """CREATE TABLE IF NOT EXISTS project_tombstones (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                deleted_at INTEGER NOT NULL
            )"""
        )
        database.execute(
            """CREATE TABLE IF NOT EXISTS codex_thread_owners (
                thread_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )"""
        )
        for table in ("conversations", "conversation_tombstones", "projects", "project_tombstones"):
            _ensure_column(database, table, "user_id", "TEXT")
            if owner_id:
                database.execute(
                    f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL OR user_id = ''",
                    (owner_id,),
                )
        database.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user_updated ON conversations(user_id, updated_at DESC)")
        database.execute("CREATE INDEX IF NOT EXISTS idx_projects_user_updated ON projects(user_id, updated_at DESC)")
        database.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON site_sessions(user_id)")
        database.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_events_user_created "
            "ON message_events(user_id, created_at)"
        )
        database.execute("DELETE FROM site_sessions WHERE expires_at < ?", (int(time.time()),))
        if owner_id:
            for (payload,) in database.execute(
                "SELECT payload FROM conversations WHERE user_id = ?", (owner_id,)
            ):
                try:
                    conversation = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    continue
                thread_ids = list(conversation.get("codexThreadIds") or [])
                if conversation.get("threadId"):
                    thread_ids.append(conversation["threadId"])
                for thread_id in thread_ids:
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(thread_id)):
                        database.execute(
                            """INSERT OR IGNORE INTO codex_thread_owners
                            (thread_id, user_id, created_at) VALUES (?, ?, ?)""",
                            (str(thread_id), owner_id, int(time.time())),
                        )
            _migrate_legacy_workspaces(database, owner_id)
    CONVERSATIONS_PATH.chmod(0o600)


def _cloud_timestamp(value: Any, default: int = 0) -> int:
    try:
        timestamp = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, min(timestamp, int(time.time() * 1000) + 5 * 60 * 1000))


def _cloud_relative_path(value: Any) -> str | None:
    normalized = str(value or "").replace("\\", "/").strip()
    if not normalized or len(normalized) > 240:
        return None
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return str(relative)


def _cloud_size(value: Any, maximum: int = MAX_FILE_BYTES) -> int:
    try:
        size = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(size, maximum))


def _cloud_token_count(value: Any) -> int:
    try:
        count = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(count, 1_000_000_000))


def _cloud_cost(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "")
    if kind == "subscription":
        normalized: dict[str, Any] = {"kind": kind}
        if value.get("amount") is not None:
            try:
                amount = float(value.get("amount"))
            except (TypeError, ValueError, OverflowError):
                amount = -1
            if math.isfinite(amount) and 0 <= amount <= 1_000_000:
                normalized.update({
                    "amount": amount,
                    "currency": "USD",
                    "model": str(value.get("model") or "")[:100],
                    "usageEstimated": bool(value.get("usageEstimated")),
                })
        return normalized
    if kind in {"unconfigured", "unavailable"}:
        return {"kind": kind}
    if kind != "estimated":
        return None
    try:
        amount = float(value.get("amount"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(amount) or amount < 0 or amount > 1_000_000:
        return None
    currency = str(value.get("currency") or "CNY")
    if currency not in {"CNY", "USD"}:
        currency = "CNY"
    return {
        "kind": "estimated",
        "amount": amount,
        "currency": currency,
        "usageEstimated": bool(value.get("usageEstimated")),
    }


def _normalize_cloud_conversation(raw: dict[str, Any]) -> dict[str, Any]:
    conversation_id = str(raw.get("id") or "")
    workspace_id = str(raw.get("workspaceId") or "")
    if not SESSION_RE.fullmatch(conversation_id) or not SESSION_RE.fullmatch(workspace_id):
        raise HTTPException(400, "聊天记录标识无效")
    now_ms = int(time.time() * 1000)
    updated_at = _cloud_timestamp(raw.get("updatedAt"), now_ms) or now_ms
    title = re.sub(r"\s+", " ", str(raw.get("title") or "新对话")).strip()[:60] or "新对话"
    thread_id = str(raw.get("threadId") or "")
    if thread_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
        thread_id = ""
    thread_ids: list[str] = []
    for candidate in raw.get("codexThreadIds") if isinstance(raw.get("codexThreadIds"), list) else []:
        candidate = str(candidate)
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", candidate) and candidate not in thread_ids:
            thread_ids.append(candidate)
        if len(thread_ids) >= 50:
            break
    if thread_id and thread_id not in thread_ids:
        thread_ids.append(thread_id)

    messages: list[dict[str, Any]] = []
    total_content = 0
    raw_messages = raw.get("messages") if isinstance(raw.get("messages"), list) else []
    for index, item in enumerate(raw_messages[-MAX_CLOUD_MESSAGES:]):
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = str(item.get("content") or "")[:60_000]
        total_content += len(content)
        if total_content > MAX_CLOUD_CONTENT_CHARS:
            raise HTTPException(413, "单个聊天记录过大，请拆分为多个对话")
        message_id = str(item.get("id") or "")
        if not SESSION_RE.fullmatch(message_id):
            message_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"ai-chat:{conversation_id}:{index}:{item['role']}:{content[:256]}",
            ).hex
        message: dict[str, Any] = {
            "id": message_id,
            "role": item["role"],
            "content": content,
            "createdAt": _cloud_timestamp(item.get("createdAt"), updated_at) or updated_at,
        }
        if item.get("error"):
            message["error"] = True
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        input_tokens = _cloud_token_count(usage.get("inputTokens"))
        output_tokens = _cloud_token_count(usage.get("outputTokens"))
        total_tokens = _cloud_token_count(usage.get("totalTokens")) or input_tokens + output_tokens
        if total_tokens:
            message["usage"] = {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": total_tokens,
                "estimated": bool(usage.get("estimated")),
            }
        cost = _cloud_cost(item.get("cost"))
        if cost:
            message["cost"] = cost
        mode = str(item.get("mode") or "")
        if mode in {"image", "external"}:
            message["mode"] = mode
        attachments = []
        for attachment in item.get("attachments") if isinstance(item.get("attachments"), list) else []:
            if not isinstance(attachment, dict):
                continue
            attachment_id = str(attachment.get("id") or "")
            if not attachment_id or len(attachment_id) > 180 or "/" in attachment_id or "\\" in attachment_id:
                continue
            attachments.append({
                "id": attachment_id,
                "name": _safe_filename(str(attachment.get("name") or "file")),
                "size": _cloud_size(attachment.get("size")),
                "type": "image" if attachment.get("type") == "image" else "document",
                "source": (
                    "composer_text"
                    if attachment.get("source") == "composer_text"
                    else "file"
                ),
            })
            if len(attachments) >= MAX_FILES_PER_UPLOAD:
                break
        if attachments:
            message["attachments"] = attachments
        files = []
        for output in item.get("files") if isinstance(item.get("files"), list) else []:
            if not isinstance(output, dict):
                continue
            output_path = _cloud_relative_path(output.get("path"))
            if not output_path:
                continue
            normalized_output = {
                "name": _safe_filename(str(output.get("name") or Path(output_path).name)),
                "size": _cloud_size(output.get("size"), MAX_OUTPUT_FILE_BYTES),
                "path": output_path,
                "mediaType": str(output.get("mediaType") or "application/octet-stream")[:100],
                "inline": bool(output.get("inline")),
            }
            for dimension in ("width", "height"):
                try:
                    value = int(output.get(dimension) or 0)
                except (TypeError, ValueError, OverflowError):
                    value = 0
                if 0 < value <= 16_384:
                    normalized_output[dimension] = value
            for size_key in ("previewSize", "compressedSize"):
                value = _cloud_size(output.get(size_key), MAX_OUTPUT_FILE_BYTES)
                if value:
                    normalized_output[size_key] = value
            files.append(normalized_output)
            if len(files) >= 20:
                break
        if files:
            message["files"] = files
        messages.append(message)

    pinned = bool(raw.get("pinned"))
    backend_key = str(raw.get("backendKey") or "")[:240]
    external_conversation_id = str(raw.get("externalConversationId") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", external_conversation_id):
        external_conversation_id = ""
    external_context_key = str(raw.get("externalContextKey") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", external_context_key):
        external_context_key = ""
    project_id = str(raw.get("projectId") or "")
    if project_id and not SESSION_RE.fullmatch(project_id):
        project_id = ""
    return {
        "id": conversation_id,
        "workspaceId": workspace_id,
        "threadId": thread_id or None,
        "codexThreadIds": thread_ids,
        "title": title,
        "messages": messages,
        "updatedAt": updated_at,
        "pinned": pinned,
        "pinnedAt": _cloud_timestamp(raw.get("pinnedAt"), 0) if pinned else 0,
        "backendKey": backend_key or None,
        "externalConversationId": external_conversation_id or None,
        "externalContextKey": external_context_key or None,
        "projectId": project_id or None,
    }


def _normalize_cloud_project(raw: dict[str, Any]) -> dict[str, Any]:
    project_id = str(raw.get("id") or "")
    workspace_id = str(raw.get("workspaceId") or "")
    if not SESSION_RE.fullmatch(project_id) or not SESSION_RE.fullmatch(workspace_id):
        raise HTTPException(400, "项目记录标识无效")
    now_ms = int(time.time() * 1000)
    updated_at = _cloud_timestamp(raw.get("updatedAt"), now_ms) or now_ms
    created_at = _cloud_timestamp(raw.get("createdAt"), updated_at) or updated_at
    name = re.sub(r"\s+", " ", str(raw.get("name") or "新项目")).strip()[:60] or "新项目"
    instructions = str(raw.get("instructions") or "").strip()[:MAX_PROJECT_INSTRUCTIONS]
    files: list[dict[str, Any]] = []
    for item in raw.get("files") if isinstance(raw.get("files"), list) else []:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("id") or "")
        if not file_id or len(file_id) > 180 or "/" in file_id or "\\" in file_id:
            continue
        files.append({
            "id": file_id,
            "name": _safe_filename(str(item.get("name") or "file")),
            "size": _cloud_size(item.get("size")),
            "type": "image" if item.get("type") == "image" else "document",
        })
        if len(files) >= MAX_PROJECT_FILES:
            break
    return {
        "id": project_id,
        "workspaceId": workspace_id,
        "name": name,
        "instructions": instructions,
        "useContext": bool(raw.get("useContext")),
        "files": files,
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


def _cloud_state(database: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    rows = database.execute(
        "SELECT payload FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
        (user_id, MAX_CLOUD_CONVERSATIONS),
    ).fetchall()
    conversations = []
    for (payload,) in rows:
        try:
            item = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            conversations.append(item)
    deleted = [row[0] for row in database.execute(
        "SELECT id FROM conversation_tombstones WHERE user_id = ? ORDER BY deleted_at DESC LIMIT 2000",
        (user_id,),
    ).fetchall()]
    project_rows = database.execute(
        "SELECT payload FROM projects WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
        (user_id, MAX_CLOUD_PROJECTS),
    ).fetchall()
    projects = []
    for (payload,) in project_rows:
        try:
            item = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            projects.append(item)
    project_deleted = [row[0] for row in database.execute(
        "SELECT id FROM project_tombstones WHERE user_id = ? ORDER BY deleted_at DESC LIMIT 500",
        (user_id,),
    ).fetchall()]
    return {
        "conversations": conversations,
        "deleted": deleted,
        "projects": projects,
        "projectDeleted": project_deleted,
        "serverTime": int(time.time() * 1000),
    }


def _sync_cloud_conversations(
    user_id: str,
    raw_conversations: list[dict[str, Any]],
    raw_projects: list[dict[str, Any]],
) -> dict[str, Any]:
    _init_conversations_db()
    normalized = [_normalize_cloud_conversation(item) for item in raw_conversations]
    normalized_projects = [_normalize_cloud_project(item) for item in raw_projects]
    with _database_connection() as database:
        database.execute("BEGIN IMMEDIATE")
        tombstones = {row[0] for row in database.execute(
            "SELECT id FROM conversation_tombstones WHERE user_id = ?", (user_id,)
        )}
        for conversation in normalized:
            if conversation["id"] in tombstones:
                continue
            row = database.execute(
                "SELECT user_id, updated_at FROM conversations WHERE id = ?", (conversation["id"],)
            ).fetchone()
            if row is not None and str(row[0]) != user_id:
                raise HTTPException(409, "聊天记录标识已被占用，请新建对话")
            if row is None or conversation["updatedAt"] > row[1]:
                database.execute(
                    """INSERT INTO conversations (id, user_id, payload, updated_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
                    WHERE conversations.user_id = excluded.user_id""",
                    (
                        conversation["id"],
                        user_id,
                        json.dumps(conversation, ensure_ascii=False, separators=(",", ":")),
                        conversation["updatedAt"],
                    ),
                )
            for thread_id in conversation["codexThreadIds"]:
                _claim_codex_thread(database, user_id, thread_id)
        project_tombstones = {row[0] for row in database.execute(
            "SELECT id FROM project_tombstones WHERE user_id = ?", (user_id,)
        )}
        for project in normalized_projects:
            if project["id"] in project_tombstones:
                continue
            row = database.execute(
                "SELECT user_id, updated_at FROM projects WHERE id = ?", (project["id"],)
            ).fetchone()
            if row is not None and str(row[0]) != user_id:
                raise HTTPException(409, "项目标识已被占用，请新建项目")
            if row is None or project["updatedAt"] > row[1]:
                database.execute(
                    """INSERT INTO projects (id, user_id, payload, updated_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
                    WHERE projects.user_id = excluded.user_id""",
                    (
                        project["id"],
                        user_id,
                        json.dumps(project, ensure_ascii=False, separators=(",", ":")),
                        project["updatedAt"],
                    ),
                )
        state = _cloud_state(database, user_id)
        database.commit()
    return state


def _delete_cloud_conversation(user_id: str, conversation_id: str) -> None:
    if not SESSION_RE.fullmatch(conversation_id):
        raise HTTPException(400, "聊天记录标识无效")
    _init_conversations_db()
    with _database_connection() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT user_id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is not None and str(row[0]) != user_id:
            raise HTTPException(403, "无权删除此聊天")
        database.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)
        )
        tombstone = database.execute(
            "SELECT user_id FROM conversation_tombstones WHERE id = ?", (conversation_id,)
        ).fetchone()
        if tombstone is not None and str(tombstone[0]) != user_id:
            raise HTTPException(409, "聊天记录标识冲突")
        database.execute(
            """INSERT INTO conversation_tombstones (id, user_id, deleted_at) VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET deleted_at = excluded.deleted_at
            WHERE conversation_tombstones.user_id = excluded.user_id""",
            (conversation_id, user_id, int(time.time() * 1000)),
        )
        database.commit()


def _claim_codex_thread(database: sqlite3.Connection, user_id: str, thread_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
        raise HTTPException(400, "服务器会话标识无效")
    row = database.execute(
        "SELECT user_id FROM codex_thread_owners WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    if row is not None and str(row[0]) != user_id:
        raise HTTPException(403, "无权访问此服务器会话")
    database.execute(
        """INSERT OR IGNORE INTO codex_thread_owners
        (thread_id, user_id, created_at) VALUES (?, ?, ?)""",
        (thread_id, user_id, int(time.time())),
    )


def _require_codex_thread_owner(user_id: str, thread_id: str) -> None:
    _init_conversations_db()
    with _database_connection() as database:
        row = database.execute(
            "SELECT user_id FROM codex_thread_owners WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    if row is None or str(row[0]) != user_id:
        raise HTTPException(403, "无权访问此服务器会话")


def _register_codex_thread(user_id: str, thread_id: str) -> None:
    _init_conversations_db()
    with _database_connection() as database:
        _claim_codex_thread(database, user_id, thread_id)
        database.commit()


def _forget_codex_thread(user_id: str, thread_id: str) -> None:
    with _database_connection() as database:
        database.execute(
            "DELETE FROM codex_thread_owners WHERE thread_id = ? AND user_id = ?",
            (thread_id, user_id),
        )
        database.commit()


def _read_providers() -> list[dict[str, Any]]:
    if not PROVIDERS_PATH.is_file():
        return []
    try:
        data = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOG.exception("Failed to read provider settings")
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _write_providers(providers: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PROVIDERS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(providers, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, PROVIDERS_PATH)
    PROVIDERS_PATH.chmod(0o600)


def _read_disabled_codex_models() -> set[str]:
    if not CODEX_MODEL_SETTINGS_PATH.is_file():
        return set()
    try:
        data = json.loads(CODEX_MODEL_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOG.exception("Failed to read Codex model settings")
        return set()
    values = data.get("disabledModels", []) if isinstance(data, dict) else []
    return {
        value.strip()
        for value in values
        if isinstance(value, str) and CODEX_MODEL_ID_RE.fullmatch(value.strip())
    }


def _write_disabled_codex_models(models: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cleaned = sorted(
        {model.strip() for model in models if CODEX_MODEL_ID_RE.fullmatch(model.strip())},
        key=str.casefold,
    )
    temporary = CODEX_MODEL_SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"disabledModels": cleaned}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, CODEX_MODEL_SETTINGS_PATH)
    CODEX_MODEL_SETTINGS_PATH.chmod(0o600)


def _public_provider(provider: dict[str, Any]) -> dict[str, Any]:
    pricing = provider.get("pricing") if isinstance(provider.get("pricing"), dict) else {}
    configured = all(key in pricing for key in ("inputPerMillion", "outputPerMillion"))
    return {
        "id": provider.get("id"),
        "preset": provider.get("preset", "custom"),
        "name": provider.get("name", "API 连接"),
        "baseUrl": provider.get("baseUrl", ""),
        "protocol": provider.get("protocol", "openai"),
        "enabled": bool(provider.get("enabled", True)),
        "hasKey": bool(provider.get("apiKey")),
        "models": provider.get("models", []),
        "pricing": {
            "configured": configured,
            "currency": pricing.get("currency", "CNY") if configured else "CNY",
            "inputPerMillion": pricing.get("inputPerMillion") if configured else None,
            "outputPerMillion": pricing.get("outputPerMillion") if configured else None,
        },
        "updatedAt": provider.get("updatedAt"),
    }


def _provider_by_id(provider_id: str) -> dict[str, Any]:
    if not PROVIDER_ID_RE.fullmatch(provider_id):
        raise HTTPException(400, "API 连接标识无效")
    provider = next((item for item in _read_providers() if item.get("id") == provider_id), None)
    if not provider:
        raise HTTPException(404, "API 连接不存在")
    return provider


def _external_model_id(provider_id: str, model_id: str) -> str:
    encoded = base64.urlsafe_b64encode(model_id.encode()).decode().rstrip("=")
    return f"external.{provider_id}.{encoded}"


def _decode_external_model(value: str) -> tuple[str, str] | None:
    if not value.startswith("external."):
        return None
    parts = value.split(".", 2)
    if len(parts) != 3 or not PROVIDER_ID_RE.fullmatch(parts[1]):
        raise HTTPException(400, "外部模型标识无效")
    try:
        model_id = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4)).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "外部模型标识无效") from exc
    if not model_id or len(model_id) > 200:
        raise HTTPException(400, "外部模型标识无效")
    return parts[1], model_id


def _provider_endpoint(provider: dict[str, Any], suffix: str) -> str:
    return f"{str(provider['baseUrl']).rstrip('/')}/{suffix.lstrip('/')}"


def _private_http_address_allowed(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_link_local or address.is_multicast or address.is_unspecified:
        return False
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return address.is_private and not address.is_reserved


async def _validate_remote_base(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    try:
        parsed = urlparse(normalized)
    except ValueError as exc:
        raise HTTPException(400, "API 地址格式无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "API 地址必须是完整的 HTTP 或 HTTPS 地址")
    if parsed.query or parsed.fragment:
        raise HTTPException(400, "API 地址不能包含查询参数或片段")
    hostname = parsed.hostname.rstrip(".").lower()
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = await asyncio.to_thread(socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise HTTPException(400, "API 地址无法解析") from exc
    for address in {item[4][0].split("%", 1)[0] for item in addresses}:
        try:
            resolved = ipaddress.ip_address(address)
            if not resolved.is_global and not (
                parsed.scheme == "http" and _private_http_address_allowed(resolved)
            ):
                raise HTTPException(400, "该地址不可访问；私网地址仅支持 HTTP，且不能使用链路本地地址")
        except ValueError as exc:
            raise HTTPException(400, "API 地址解析结果无效") from exc
    return normalized


def _provider_headers(provider: dict[str, Any]) -> dict[str, str]:
    key = str(provider.get("apiKey", ""))
    if provider.get("protocol") == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    return {"authorization": f"Bearer {key}", "content-type": "application/json"}


def _upstream_error(payload: bytes, fallback: str) -> str:
    try:
        data = json.loads(payload[:64_000])
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:500]
        if isinstance(data, dict):
            return str(data.get("message") or data.get("detail") or fallback)[:500]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return fallback


def _workspace_path(user_id: str, session_id: str) -> Path:
    if not SESSION_RE.fullmatch(user_id) or not SESSION_RE.fullmatch(session_id):
        raise HTTPException(400, "会话标识无效")
    user_root = (WORKSPACE_ROOT / user_id).resolve()
    path = (user_root / session_id).resolve()
    if path.parent != user_root or user_root.parent != WORKSPACE_ROOT:
        raise HTTPException(400, "会话路径无效")
    return path


def _session_path(user_id: str, session_id: str) -> Path:
    path = _workspace_path(user_id, session_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "uploads").mkdir(exist_ok=True)
    (path / "outputs").mkdir(exist_ok=True)
    os.utime(path, None)
    return path


def _safe_filename(name: str) -> str:
    clean = SAFE_FILE_RE.sub("_", Path(name or "file").name).strip("._")
    return clean[:120] or "file"


def _file_media_type(target: Path) -> str:
    return (
        IMAGE_MEDIA_TYPES.get(target.suffix.lower())
        or mimetypes.guess_type(target.name)[0]
        or "application/octet-stream"
    )


def _transfer_secret_bytes() -> bytes:
    return TRANSFER_SECRET.encode("utf-8")


def _validated_transfer_base_url() -> str:
    if not TRANSFER_PUBLIC_BASE_URL:
        return ""
    parsed = urlparse(TRANSFER_PUBLIC_BASE_URL)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        LOG.warning("AI_CHAT_PUBLIC_BASE_URL must be an HTTPS origin without a path")
        return ""
    return TRANSFER_PUBLIC_BASE_URL



def _require_external_transfer_base_url() -> str:
    transfer_base_url = _validated_transfer_base_url()
    if not transfer_base_url or len(_transfer_secret_bytes()) < 32:
        raise HTTPException(
            503,
            "参考图临时传输服务未配置，已停止发送以避免使用 Base64 大请求",
        )
    return transfer_base_url


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _create_transfer_token(
    user_id: str,
    session_id: str,
    relative_path: str,
    *,
    now: int | None = None,
) -> str:
    secret = _transfer_secret_bytes()
    if len(secret) < 32:
        raise RuntimeError("AI_CHAT_TRANSFER_SECRET must contain at least 32 characters")
    issued_at = int(time.time()) if now is None else int(now)
    claims = {
        "v": 1,
        "u": user_id,
        "s": session_id,
        "p": relative_path,
        "e": issued_at + TRANSFER_URL_TTL_SECONDS,
    }
    encoded = _b64url_encode(
        json.dumps(claims, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    )
    signature = _b64url_encode(
        hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{encoded}.{signature}"


def _decode_transfer_token(token: str, *, now: int | None = None) -> dict[str, Any]:
    try:
        secret = _transfer_secret_bytes()
        if len(secret) < 32 or len(token) > 1200:
            raise ValueError("token too long")
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _b64url_encode(
            hmac.new(
                secret,
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("signature mismatch")
        claims = json.loads(_b64url_decode(encoded))
        current = int(time.time()) if now is None else int(now)
        expires_at = int(claims.get("e", 0))
        user_id = str(claims.get("u", ""))
        session_id = str(claims.get("s", ""))
        relative_path = str(claims.get("p", ""))
        relative = PurePosixPath(relative_path)
        if (
            claims.get("v") != 1
            or not SESSION_RE.fullmatch(user_id)
            or not SESSION_RE.fullmatch(session_id)
            or expires_at < current
            or expires_at > current + TRANSFER_URL_TTL_SECONDS + 60
            or relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or len(relative_path) > 260
        ):
            raise ValueError("invalid claims")
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error):
        raise HTTPException(404, "临时图片链接无效或已经过期") from None
    return {
        "user_id": user_id,
        "session_id": session_id,
        "relative_path": relative.as_posix(),
        "expires_at": expires_at,
    }


def _prepare_transfer_image(source: Path, upload_root: Path) -> Path:
    source = source.resolve()
    upload_root = upload_root.resolve()
    if source.parent != upload_root or not source.is_file():
        raise HTTPException(400, "参考图片不存在")
    try:
        with Image.open(source) as probe:
            image_format = str(probe.format or "").upper()
            width, height = probe.size
            if width <= 0 or height <= 0 or max(width, height) > 16_384 or width * height > 64_000_000:
                raise HTTPException(400, "参考图片尺寸过大")
            probe.verify()
    except HTTPException:
        raise
    except (OSError, SyntaxError) as exc:
        raise HTTPException(400, "参考图片文件无效或已损坏") from exc

    expected_format = {
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".webp": "WEBP",
        ".gif": "GIF",
        ".avif": "AVIF",
    }.get(source.suffix.lower())
    # AVIF is accepted at upload but converted to WebP for broad upstream API support.
    source_format_matches = image_format == expected_format and image_format != "AVIF"
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            width, height = image.size
            if width <= 0 or height <= 0 or max(width, height) > 16_384 or width * height > 64_000_000:
                raise HTTPException(400, "参考图片尺寸过大")
            if (
                source_format_matches
                and source.stat().st_size <= TRANSFER_IMAGE_COMPRESS_MIN_BYTES
                and max(width, height) <= TRANSFER_IMAGE_MAX_EDGE
            ):
                return source
            stat = source.stat()
            identity = f"{source.name}:{stat.st_size}:{stat.st_mtime_ns}:transfer-v2"
            key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            cache_root = (upload_root / TRANSFER_CACHE_DIR).resolve()
            cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            cache_root.chmod(0o700)
            target = cache_root / f"{key}.webp"
            if not target.is_file() or target.stat().st_size == 0:
                compressed = image.copy()
                compressed.thumbnail(
                    (TRANSFER_IMAGE_MAX_EDGE, TRANSFER_IMAGE_MAX_EDGE),
                    Image.Resampling.LANCZOS,
                )
                _save_webp(compressed, target, TRANSFER_IMAGE_QUALITY)
            if (
                source_format_matches
                and target.stat().st_size >= source.stat().st_size
                and max(width, height) <= TRANSFER_IMAGE_MAX_EDGE
            ):
                return source
            return target
    except HTTPException:
        raise
    except Exception as exc:
        LOG.warning("Unable to optimize transfer image %s: %s", source.name, exc)
        if source_format_matches:
            return source
        raise HTTPException(400, "参考图片扩展名与实际格式不一致，且转换失败") from exc


async def _prepare_transfer_images(
    sources: list[Path], upload_root: Path
) -> list[Path]:
    semaphore = asyncio.Semaphore(2)

    async def prepare(source: Path) -> Path:
        async with semaphore:
            return await asyncio.to_thread(_prepare_transfer_image, source, upload_root)

    return await asyncio.gather(*(prepare(source) for source in sources))


def _transfer_image_url(
    user_id: str,
    session_id: str,
    target: Path,
    upload_root: Path,
) -> str:
    relative_path = target.resolve().relative_to(upload_root.resolve()).as_posix()
    token = _create_transfer_token(user_id, session_id, relative_path)
    return f"{_validated_transfer_base_url()}/api/transfer-image/{token}"


def _natural_xml_key(name: str) -> list[int | str]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def _extract_office_text(target: Path) -> str:
    extension = target.suffix.lower()
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        if extension == ".docx":
            selected = [
                name for name in names
                if name == "word/document.xml"
                or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
            ]
        elif extension == ".pptx":
            selected = [name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)]
        else:
            selected = [
                name for name in names
                if name == "xl/sharedStrings.xml"
                or re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
            ]
        selected.sort(key=_natural_xml_key)
        total_size = sum(archive.getinfo(name).file_size for name in selected)
        if total_size > MAX_OFFICE_XML_BYTES:
            raise ValueError("Office 文档解压后内容过大")
        sections: list[str] = []
        for name in selected:
            root = ElementTree.fromstring(archive.read(name))
            values = [
                element.text.strip()
                for element in root.iter()
                if element.text and element.tag.rsplit("}", 1)[-1] in {"t", "v"} and element.text.strip()
            ]
            if values:
                sections.append("\n".join(values))
        return "\n\n".join(sections)


def _session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_site_user(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    user = {
        "id": str(row[0]),
        "username": str(row[1]),
        "isAdmin": bool(row[2]),
        "disabled": bool(row[3]),
        "createdAt": int(row[4]),
    }
    if len(row) > 5:
        user["hourlyMessageLimit"] = max(0, int(row[5] or 0))
    if len(row) > 6:
        user["expiresAt"] = int(row[6]) if row[6] is not None else None
    return user


def _expire_due_users(database: sqlite3.Connection, now: int | None = None) -> set[str]:
    current_time = int(time.time()) if now is None else int(now)
    rows = database.execute(
        """SELECT id FROM users
        WHERE is_admin = 0 AND disabled = 0
            AND expires_at IS NOT NULL AND expires_at <= ?""",
        (current_time,),
    ).fetchall()
    user_ids = {str(row[0]) for row in rows}
    if not user_ids:
        return set()
    database.executemany(
        "UPDATE users SET disabled = 1 WHERE id = ?",
        ((user_id,) for user_id in user_ids),
    )
    database.executemany(
        "DELETE FROM site_sessions WHERE user_id = ?",
        ((user_id,) for user_id in user_ids),
    )
    return user_ids


def _current_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get("ai_chat_session")
    if not token or len(token) > 200:
        return None
    _init_conversations_db()
    now = int(time.time())
    with _database_connection() as database:
        _expire_due_users(database, now)
        row = database.execute(
            """SELECT users.id, users.username, users.is_admin, users.disabled,
                users.created_at, users.hourly_message_limit, users.expires_at
            FROM site_sessions JOIN users ON users.id = site_sessions.user_id
            WHERE site_sessions.token_hash = ? AND site_sessions.expires_at >= ?""",
            (_session_token_hash(token), now),
        ).fetchone()
        if row is None or bool(row[3]):
            return None
        return _public_site_user(row)


def _message_limit_snapshot(
    database: sqlite3.Connection,
    user_id: str,
    hourly_limit: int,
    now: int,
) -> dict[str, int | None]:
    if hourly_limit <= 0:
        return {"messagesUsedLastHour": 0, "nextMessageAt": None}
    row = database.execute(
        """SELECT COUNT(*), MIN(created_at) FROM message_events
        WHERE user_id = ? AND created_at > ?""",
        (user_id, now - MESSAGE_LIMIT_WINDOW_SECONDS),
    ).fetchone()
    used = int(row[0] or 0)
    next_message_at = (
        int(row[1]) + MESSAGE_LIMIT_WINDOW_SECONDS
        if used >= hourly_limit and row[1] is not None
        else None
    )
    return {"messagesUsedLastHour": used, "nextMessageAt": next_message_at}


def _reserve_message_slot(user_id: str) -> None:
    now = int(time.time())
    cutoff = now - MESSAGE_LIMIT_WINDOW_SECONDS
    with _database_connection() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT is_admin, hourly_message_limit FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(401, "登录账户不存在")
        if bool(row[0]):
            return
        hourly_limit = max(0, int(row[1] or 0))
        if hourly_limit <= 0:
            return
        database.execute(
            "DELETE FROM message_events WHERE user_id = ? AND created_at <= ?",
            (user_id, cutoff),
        )
        usage = database.execute(
            "SELECT COUNT(*), MIN(created_at) FROM message_events WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        used = int(usage[0] or 0)
        if used >= hourly_limit:
            retry_after = max(
                1,
                int(usage[1] or now) + MESSAGE_LIMIT_WINDOW_SECONDS - now,
            )
            retry_minutes = max(1, math.ceil(retry_after / 60))
            raise HTTPException(
                status_code=429,
                detail=(
                    f"过去60分钟已发送 {used}/{hourly_limit} 条消息，"
                    f"请约 {retry_minutes} 分钟后再试"
                ),
                headers={"Retry-After": str(retry_after)},
            )
        database.execute(
            "INSERT INTO message_events (id, user_id, created_at) VALUES (?, ?, ?)",
            (uuid.uuid4().hex, user_id, now),
        )


def _create_site_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with _database_connection() as database:
        database.execute(
            """INSERT INTO site_sessions (token_hash, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)""",
            (_session_token_hash(token), user_id, now, now + SITE_SESSION_TTL_SECONDS),
        )
        database.commit()
    return token


def _delete_site_session(token: str | None) -> None:
    if not token:
        return
    with _database_connection() as database:
        database.execute(
            "DELETE FROM site_sessions WHERE token_hash = ?", (_session_token_hash(token),)
        )
        database.commit()


def _require_auth(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    if user is None:
        raise HTTPException(401, "请先登录")
    return user


def _require_admin(request: Request) -> dict[str, Any]:
    user = _require_auth(request)
    if not user["isAdmin"]:
        raise HTTPException(403, "仅管理员可以执行此操作")
    return user


def _normalize_rate_limits(result: dict[str, Any]) -> dict[str, Any]:
    """Return only the quota fields the browser needs."""
    buckets = result.get("rateLimitsByLimitId")
    bucket: dict[str, Any] | None = None
    limit_id = "codex"
    if isinstance(buckets, dict):
        candidate = buckets.get("codex")
        if isinstance(candidate, dict):
            bucket = candidate
        else:
            for key, value in buckets.items():
                if isinstance(value, dict):
                    limit_id = str(key)
                    bucket = value
                    break
    if bucket is None and isinstance(result.get("rateLimits"), dict):
        bucket = result["rateLimits"]
    if bucket is None:
        raise ValueError("Codex 暂未返回额度信息")

    windows: list[dict[str, Any]] = []
    for name in ("primary", "secondary"):
        window = bucket.get(name)
        if not isinstance(window, dict):
            continue
        try:
            used = float(window["usedPercent"])
        except (KeyError, TypeError, ValueError):
            continue
        remaining = max(0, min(100, round(100 - used)))
        reset_value = window.get("resetsAt")
        try:
            resets_at = int(float(reset_value)) if reset_value is not None else None
        except (TypeError, ValueError):
            resets_at = None
        windows.append({
            "name": name,
            "remainingPercent": remaining,
            "resetsAt": resets_at,
            "windowDurationMins": window.get("windowDurationMins"),
        })
    if not windows:
        raise ValueError("Codex 暂未返回可用的额度窗口")

    # The lowest remaining window is the one that can constrain requests first.
    selected = min(windows, key=lambda item: item["remainingPercent"])
    return {"ok": True, "limitId": limit_id, **selected}


class CodexProtocolError(RuntimeError):
    pass


class CodexAppServer:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.streams: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.backlog: dict[str, list[dict[str, Any]]] = {}
        self.loaded_threads: set[str] = set()
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def ensure_started(self) -> None:
        if self.process and self.process.returncode is None:
            return
        async with self._start_lock:
            if self.process and self.process.returncode is None:
                return
            env = os.environ.copy()
            env["CODEX_HOME"] = str(CODEX_HOME)
            env["HOME"] = str(DATA_DIR / "home")
            self.process = await asyncio.create_subprocess_exec(
                CODEX_BIN,
                "app-server",
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._reader_task = asyncio.create_task(self._reader())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            await self.request(
                "initialize",
                {
                    "clientInfo": {"name": "ai-chat", "title": "AI Chat", "version": "1.0.0"},
                    "capabilities": {"experimentalApi": True},
                },
                timeout=20,
            )
            await self.notify("initialized", {})

    async def stop(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            LOG.info("codex: %s", line.decode(errors="replace").rstrip())

    async def _reader(self) -> None:
        assert self.process and self.process.stdout
        while line := await self.process.stdout.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                LOG.warning("Ignoring malformed app-server output")
                continue
            if "id" in message and ("result" in message or "error" in message):
                future = self.pending.pop(message["id"], None)
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(CodexProtocolError(message["error"].get("message", "Codex 请求失败")))
                    else:
                        future.set_result(message.get("result"))
                continue
            if "id" in message and "method" in message:
                await self._decline_server_request(message)
                continue
            method = message.get("method", "")
            params = message.get("params") or {}
            turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
            if turn_id:
                event = {"method": method, "params": params}
                queue = self.streams.get(turn_id)
                if queue:
                    await queue.put(event)
                else:
                    self.backlog.setdefault(turn_id, []).append(event)

    async def _decline_server_request(self, message: dict[str, Any]) -> None:
        method = message.get("method", "")
        if "requestApproval" in method or "request_approval" in method:
            result: dict[str, Any] = {"decision": "decline"}
        else:
            result = {"error": "此私人聊天站未启用交互式工具"}
        await self._write({"id": message["id"], "result": result})

    async def _write(self, payload: dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            self.process.stdin.write(data)
            await self.process.stdin.drain()

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: int = 60) -> Any:
        if method != "initialize":
            await self.ensure_started()
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self._write({"id": request_id, "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write({"method": method, "params": params or {}})

    def register_stream(self, turn_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.streams[turn_id] = queue
        for event in self.backlog.pop(turn_id, []):
            queue.put_nowait(event)
        return queue

    def unregister_stream(self, turn_id: str) -> None:
        self.streams.pop(turn_id, None)
        self.backlog.pop(turn_id, None)


codex = CodexAppServer()
login_attempts: dict[str, dict[str, float | int]] = {}
provider_write_lock = asyncio.Lock()
codex_model_write_lock = asyncio.Lock()
conversation_write_lock = asyncio.Lock()
chunk_upload_locks: dict[str, asyncio.Lock] = {}
chunk_upload_lock_times: dict[str, float] = {}
external_turns: dict[str, tuple[str, asyncio.Task[Any]]] = {}
external_conversation_locks: dict[tuple[str, str], asyncio.Lock] = {}
external_conversation_states: dict[tuple[str, str], dict[str, str]] = {}
image_turns: dict[str, tuple[str, asyncio.Task[Any]]] = {}
image_variant_lock = asyncio.Lock()
attachment_extract_semaphore = asyncio.Semaphore(2)
image_variant_tasks: set[asyncio.Task[Any]] = set()


async def _clear_external_conversation_state(user_id: str, conversation_id: str | None) -> None:
    if not conversation_id:
        return
    key = (user_id, conversation_id)
    lock = external_conversation_locks.get(key)
    if lock is None:
        external_conversation_states.pop(key, None)
        return
    async with lock:
        external_conversation_states.pop(key, None)


def _login_client(request: Request) -> str:
    candidate = request.headers.get("x-real-ip", "").strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return request.client.host if request.client else "unknown"


def _active_login_attempt(client: str, now: float) -> dict[str, float | int] | None:
    attempt = login_attempts.get(client)
    if not attempt:
        return None
    locked_until = float(attempt.get("locked_until", 0))
    last_failure = float(attempt.get("last_failure", 0))
    if locked_until > now:
        return attempt
    if locked_until or now - last_failure >= LOGIN_LOCK_SECONDS:
        login_attempts.pop(client, None)
        return None
    return attempt


def _login_lock_remaining(client: str, now: float) -> int:
    attempt = _active_login_attempt(client, now)
    if not attempt:
        return 0
    return max(0, math.ceil(float(attempt.get("locked_until", 0)) - now))


def _record_login_failure(client: str, now: float) -> tuple[bool, int]:
    attempt = _active_login_attempt(client, now) or {
        "failures": 0,
        "last_failure": now,
        "locked_until": 0,
    }
    failures = int(attempt.get("failures", 0)) + 1
    attempt.update({"failures": failures, "last_failure": now})
    if failures >= LOGIN_MAX_FAILURES:
        attempt["locked_until"] = now + LOGIN_LOCK_SECONDS
        login_attempts[client] = attempt
        return True, 0
    login_attempts[client] = attempt
    return False, LOGIN_MAX_FAILURES - failures


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(60 * 60)
        cutoff = time.time() - SESSION_TTL_SECONDS
        for user_root in WORKSPACE_ROOT.iterdir():
            if not user_root.is_dir() or not SESSION_RE.fullmatch(user_root.name):
                continue
            for child in user_root.iterdir():
                if child.is_dir() and SESSION_RE.fullmatch(child.name) and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child)
        with _database_connection() as database:
            database.execute(
                "DELETE FROM message_events WHERE created_at <= ?",
                (int(time.time()) - MESSAGE_LIMIT_WINDOW_SECONDS,),
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    _prepare_directories()
    cleanup = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)
        current = asyncio.current_task()
        active_tasks = {
            task
            for _owner, task in [*external_turns.values(), *image_turns.values()]
            if task is not current
        }
        active_tasks.update(task for task in image_variant_tasks if task is not current)
        for task in active_tasks:
            if not task.done():
                task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        external_turns.clear()
        image_turns.clear()
        image_variant_tasks.clear()
        external_conversation_locks.clear()
        external_conversation_states.clear()
        chunk_upload_locks.clear()
        chunk_upload_lock_times.clear()
        await codex.stop()


app = FastAPI(title="AI Chat", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": None, "username": ""}
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    client = _login_client(request)
    now = time.time()
    lock_remaining = _login_lock_remaining(client, now)
    if lock_remaining:
        wait_minutes = max(1, math.ceil(lock_remaining / 60))
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": f"登录已锁定，请在 {wait_minutes} 分钟后再试",
                "username": username[:32],
            },
            status_code=429,
        )
    candidate = username.strip()
    row = None
    if USERNAME_RE.fullmatch(candidate):
        _init_conversations_db()
        with _database_connection() as database:
            _expire_due_users(database, int(now))
            row = database.execute(
                """SELECT id, password_hash, disabled FROM users
                WHERE username = ? COLLATE NOCASE""",
                (candidate,),
            ).fetchone()
    if row is None or bool(row[2]) or not _password_matches(password, str(row[1])):
        locked, attempts_left = _record_login_failure(client, now)
        error = "连续登录失败 3 次，已锁定 10 分钟" if locked else f"用户名或密码不正确，还可尝试 {attempts_left} 次"
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": error, "username": candidate[:32]},
            status_code=429 if locked else 401,
        )
    login_attempts.pop(client, None)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "ai_chat_session",
        _create_site_session(str(row[0])),
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=30 * 24 * 60 * 60,
    )
    return response


@app.post("/logout")
async def logout(request: Request):
    _require_auth(request)
    _delete_site_session(request.cookies.get("ai_chat_session"))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("ai_chat_session")
    return response


@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    site_user = _current_user(request)
    if site_user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request=request, name="index.html", context={"site_user": site_user}
    )


class SiteUserCreateRequest(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    is_admin: bool = False
    hourly_message_limit: int = Field(default=0, ge=0, le=MAX_HOURLY_MESSAGE_LIMIT)
    expires_in_days: int | None = Field(default=None, ge=1, le=MAX_MEMBER_EXPIRY_DAYS)
    expires_in_hours: int | None = Field(default=None, ge=1, le=MAX_MEMBER_EXPIRY_HOURS)


class SiteUserUpdateRequest(BaseModel):
    password: str | None = Field(default=None, min_length=8, max_length=128)
    disabled: bool | None = None
    is_admin: bool | None = None
    hourly_message_limit: int | None = Field(
        default=None, ge=0, le=MAX_HOURLY_MESSAGE_LIMIT
    )
    expires_in_days: int | None = Field(default=None, ge=1, le=MAX_MEMBER_EXPIRY_DAYS)
    expires_in_hours: int | None = Field(default=None, ge=1, le=MAX_MEMBER_EXPIRY_HOURS)


class CodexModelToggleRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    enabled: bool


@app.get("/api/site-account")
async def site_account(request: Request):
    return _require_auth(request)


@app.get("/api/admin/users")
async def list_site_users(request: Request):
    _require_admin(request)
    now = int(time.time())
    with _database_connection() as database:
        _expire_due_users(database, now)
        rows = database.execute(
            """SELECT id, username, is_admin, disabled, created_at,
                hourly_message_limit, expires_at
            FROM users ORDER BY is_admin DESC, created_at ASC"""
        ).fetchall()
        users = []
        for row in rows:
            user = _public_site_user(row)
            user.update(
                _message_limit_snapshot(database, str(row[0]), int(row[5] or 0), now)
            )
            users.append(user)
    return {"users": users}


@app.post("/api/admin/users")
async def create_site_user(request: Request, payload: SiteUserCreateRequest):
    _require_admin(request)
    username = _normalized_username(payload.username)
    if payload.expires_in_days is not None and payload.expires_in_hours is not None:
        raise HTTPException(400, "自动停用期限只能选择小时或天数其中一种")
    with _database_connection() as database:
        if int(database.execute("SELECT COUNT(*) FROM users").fetchone()[0]) >= MAX_SITE_USERS:
            raise HTTPException(409, f"最多可创建 {MAX_SITE_USERS} 个网站账户")
        try:
            user_id = uuid.uuid4().hex
            created_at = int(time.time())
            expires_at = (
                None
                if payload.is_admin
                or (payload.expires_in_days is None and payload.expires_in_hours is None)
                else created_at
                + (
                    payload.expires_in_hours * 60 * 60
                    if payload.expires_in_hours is not None
                    else payload.expires_in_days * 24 * 60 * 60
                )
            )
            database.execute(
                """INSERT INTO users
                (id, username, password_hash, is_admin, disabled, created_at,
                    hourly_message_limit, expires_at)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
                (
                    user_id,
                    username,
                    _password_hash(payload.password),
                    int(payload.is_admin),
                    created_at,
                    0 if payload.is_admin else payload.hourly_message_limit,
                    expires_at,
                ),
            )
            database.commit()
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "该用户名已经存在") from exc
    return {
        "user": {
            "id": user_id,
            "username": username,
            "isAdmin": payload.is_admin,
            "disabled": False,
            "createdAt": created_at,
            "hourlyMessageLimit": 0 if payload.is_admin else payload.hourly_message_limit,
            "expiresAt": expires_at,
            "messagesUsedLastHour": 0,
            "nextMessageAt": None,
        }
    }


@app.patch("/api/admin/users/{user_id}")
async def update_site_user(request: Request, user_id: str, payload: SiteUserUpdateRequest):
    current = _require_admin(request)
    if not SESSION_RE.fullmatch(user_id):
        raise HTTPException(400, "账户标识无效")
    if current["id"] == user_id and payload.disabled:
        raise HTTPException(400, "不能停用当前登录的账户")
    if current["id"] == user_id and payload.is_admin is False:
        raise HTTPException(400, "不能取消当前账户的管理员权限")
    expiry_fields = payload.model_fields_set.intersection(
        {"expires_in_days", "expires_in_hours"}
    )
    if len(expiry_fields) > 1:
        raise HTTPException(400, "自动停用期限只能选择小时或天数其中一种")
    expiry_requested = bool(expiry_fields)
    updates: list[str] = []
    values: list[Any] = []
    if payload.password is not None:
        updates.append("password_hash = ?")
        values.append(_password_hash(payload.password))
    if payload.disabled is not None:
        updates.append("disabled = ?")
        values.append(int(payload.disabled))
    if payload.is_admin is not None:
        updates.append("is_admin = ?")
        values.append(int(payload.is_admin))
        if payload.is_admin:
            updates.append("hourly_message_limit = 0")
            updates.append("expires_at = NULL")
    if payload.hourly_message_limit is not None:
        if payload.is_admin:
            raise HTTPException(400, "管理员账户不需要消息限额")
        updates.append("hourly_message_limit = ?")
        values.append(payload.hourly_message_limit)
    with _database_connection() as database:
        target = database.execute(
            "SELECT is_admin, expires_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not target:
            raise HTTPException(404, "账户不存在")
        if (
            payload.hourly_message_limit is not None
            and bool(target[0])
            and payload.is_admin is not False
        ):
            raise HTTPException(400, "管理员账户不需要消息限额")
        target_is_admin = bool(target[0])
        if "expires_in_hours" in expiry_fields:
            new_expiry = (
                None
                if payload.expires_in_hours is None
                else int(time.time()) + payload.expires_in_hours * 60 * 60
            )
        else:
            new_expiry = (
                None
                if payload.expires_in_days is None
                else int(time.time()) + payload.expires_in_days * 24 * 60 * 60
            )
        if expiry_requested:
            if payload.is_admin is True or (target_is_admin and payload.is_admin is not False):
                raise HTTPException(400, "管理员账户不能设置到期停用")
            updates.append("expires_at = ?")
            values.append(new_expiry)
        resulting_is_admin = target_is_admin if payload.is_admin is None else payload.is_admin
        resulting_expiry = target[1] if not expiry_requested else new_expiry
        if (
            payload.disabled is False
            and not resulting_is_admin
            and resulting_expiry is not None
            and int(resulting_expiry) <= int(time.time())
        ):
            raise HTTPException(400, "账户期限已到，请先设置新的停用倒计时或取消期限")
        if not updates:
            raise HTTPException(400, "没有需要保存的账户更改")
        values.append(user_id)
        database.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", values)
        if payload.password is not None or payload.disabled:
            database.execute("DELETE FROM site_sessions WHERE user_id = ?", (user_id,))
        database.commit()
        row = database.execute(
            """SELECT id, username, is_admin, disabled, created_at,
                hourly_message_limit, expires_at
            FROM users WHERE id = ?""",
            (user_id,),
        ).fetchone()
    return {"user": _public_site_user(row)}


@app.get("/api/admin/codex-models")
async def list_admin_codex_models(request: Request):
    _require_admin(request)
    disabled = _read_disabled_codex_models()
    try:
        result = await codex.request("model/list", {"includeHidden": False, "limit": 100}, timeout=30)
    except Exception as exc:
        raise HTTPException(503, "暂时无法读取 Codex 模型，请确认 Codex 登录状态") from exc
    models = []
    for item in result.get("data", []):
        model_id = str(item.get("model") or item.get("id") or "").strip()
        if not CODEX_MODEL_ID_RE.fullmatch(model_id):
            continue
        models.append({
            "id": model_id,
            "displayName": item.get("displayName") or model_id,
            "description": item.get("description", ""),
            "isDefault": bool(item.get("isDefault", False)),
            "enabled": model_id not in disabled,
        })
    return {"models": models, "disabledCount": sum(not item["enabled"] for item in models)}


@app.patch("/api/admin/codex-models")
async def toggle_codex_model(request: Request, payload: CodexModelToggleRequest):
    _require_admin(request)
    model_id = payload.model.strip()
    if not CODEX_MODEL_ID_RE.fullmatch(model_id):
        raise HTTPException(400, "Codex 模型标识无效")
    async with codex_model_write_lock:
        disabled = _read_disabled_codex_models()
        if payload.enabled:
            disabled.discard(model_id)
        else:
            disabled.add(model_id)
        _write_disabled_codex_models(disabled)
    return {"model": model_id, "enabled": payload.enabled}


@app.get("/healthz")
async def healthcheck():
    process_state = "running" if codex.process and codex.process.returncode is None else "idle"
    return {"status": "ok", "codex": process_state}


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _cliproxy_request_key(request: Request) -> str:
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    for header_name in ("x-api-key", "x-goog-api-key"):
        value = request.headers.get(header_name, "").strip()
        if value:
            return value
    return (request.query_params.get("key") or request.query_params.get("auth_token") or "").strip()


def _is_restricted_cliproxy_request(request: Request) -> bool:
    candidate = _cliproxy_request_key(request)
    return bool(
        RESTRICTED_API_KEY
        and candidate
        and secrets.compare_digest(candidate, RESTRICTED_API_KEY)
    )


def _cliproxy_forward_headers(request: Request) -> dict[str, str]:
    blocked = _HOP_BY_HOP_HEADERS | {"host", "content-length"}
    return {name: value for name, value in request.headers.items() if name.lower() not in blocked}


def _cliproxy_response_headers(headers: httpx.Headers) -> dict[str, str]:
    blocked = _HOP_BY_HOP_HEADERS | {"content-length"}
    return {name: value for name, value in headers.items() if name.lower() not in blocked}


def _model_not_allowed_response() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "message": f"This API key is restricted to model '{RESTRICTED_API_MODEL}'.",
                "type": "model_not_allowed",
                "code": "model_not_allowed",
            }
        },
    )


@app.api_route(
    "/v1/{proxy_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def cliproxy_gateway(request: Request, proxy_path: str):
    restricted = _is_restricted_cliproxy_request(request)
    normalized_path = proxy_path.strip("/")
    body = await request.body()

    if restricted:
        if normalized_path == "models" and request.method == "GET":
            url = f"{UPSTREAM_URL}/v1/models"
            if request.url.query:
                url = f"{url}?{request.url.query}"
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
                    upstream = await client.get(url, headers=_cliproxy_forward_headers(request))
            except httpx.RequestError:
                return JSONResponse(
                    status_code=502,
                    content={"error": {"message": "CLIProxyAPI is unavailable.", "type": "upstream_error"}},
                )
            try:
                payload = upstream.json()
            except ValueError:
                return JSONResponse(
                    status_code=upstream.status_code,
                    content={"error": {"message": "CLIProxyAPI returned an invalid model list.", "type": "upstream_error"}},
                )
            if upstream.is_success and isinstance(payload, dict) and isinstance(payload.get("data"), list):
                payload["data"] = [
                    item for item in payload["data"]
                    if isinstance(item, dict) and item.get("id") == RESTRICTED_API_MODEL
                ]
            return JSONResponse(
                status_code=upstream.status_code,
                content=payload,
                headers=_cliproxy_response_headers(upstream.headers),
            )

        if normalized_path.startswith("models/") and request.method == "GET":
            requested_model = normalized_path.removeprefix("models/")
            if requested_model != RESTRICTED_API_MODEL:
                return _model_not_allowed_response()
        elif request.method not in {"GET", "HEAD", "OPTIONS"}:
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                return JSONResponse(
                    status_code=400,
                    content={"error": {"message": "Request body must be valid JSON.", "type": "invalid_request_error"}},
                )
            if not isinstance(payload, dict) or payload.get("model") != RESTRICTED_API_MODEL:
                return _model_not_allowed_response()

    url = f"{UPSTREAM_URL}/v1/{proxy_path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    client = httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=10.0), follow_redirects=False)
    try:
        upstream_request = client.build_request(
            request.method,
            url,
            headers=_cliproxy_forward_headers(request),
            content=body,
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.RequestError:
        await client.aclose()
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "CLIProxyAPI is unavailable.", "type": "upstream_error"}},
        )

    async def stream_upstream() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_upstream(),
        status_code=upstream.status_code,
        headers=_cliproxy_response_headers(upstream.headers),
    )


@app.get("/api/status")
async def status(request: Request):
    _require_auth(request)
    try:
        result = await codex.request("account/read", {"refreshToken": False}, timeout=20)
        account = result.get("account") if isinstance(result, dict) else None
        public_account = None
        if isinstance(account, dict):
            public_account = {
                "type": account.get("type"),
                "planType": account.get("planType"),
            }
        return {"ok": True, "account": public_account}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)


@app.get("/api/rate-limits")
async def rate_limits(request: Request):
    _require_auth(request)
    try:
        result = await codex.request("account/rateLimits/read", {}, timeout=20)
        return _normalize_rate_limits(result)
    except Exception:
        return JSONResponse({"ok": False, "error": "Codex 额度暂不可用"}, status_code=503)


@app.post("/api/account/login")
async def account_login(request: Request):
    _require_admin(request)
    try:
        return await codex.request("account/login/start", {"type": "chatgptDeviceCode"}, timeout=30)
    except CodexProtocolError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/account/logout")
async def account_logout(request: Request):
    _require_admin(request)
    try:
        await codex.request("account/logout", {}, timeout=20)
        return {"ok": True}
    except CodexProtocolError as exc:
        raise HTTPException(502, str(exc)) from exc


class ProviderSaveRequest(BaseModel):
    id: str | None = Field(default=None, pattern=r"^[a-f0-9]{12}$")
    preset: str = Field(default="custom", max_length=30)
    name: str = Field(default="", max_length=80)
    base_url: str = Field(max_length=500)
    protocol: Literal["openai", "anthropic"] = "openai"
    api_key: str = Field(default="", max_length=1000)
    enabled: bool = True
    price_currency: Literal["CNY", "USD"] = "CNY"
    input_price_per_million: float | None = Field(default=None, ge=0, le=1_000_000)
    output_price_per_million: float | None = Field(default=None, ge=0, le=1_000_000)


class ProviderToggleRequest(BaseModel):
    enabled: bool


class ProviderModelsRequest(BaseModel):
    models: list[str] = Field(default_factory=list, max_length=200)


class ProviderModelDeleteRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)


def _clean_model_ids(values: list[Any]) -> list[str]:
    cleaned = {
        str(value).strip()
        for value in values
        if isinstance(value, str) and 0 < len(value.strip()) <= 200
    }
    return sorted(cleaned, key=str.casefold)[:200]


@app.get("/api/providers")
async def list_providers(request: Request):
    _require_auth(request)
    presets = [{"id": key, **value} for key, value in PROVIDER_PRESETS.items()]
    return {"presets": presets, "providers": [_public_provider(item) for item in _read_providers()]}


@app.post("/api/providers")
async def save_provider(request: Request, payload: ProviderSaveRequest):
    _require_admin(request)
    if payload.preset not in PROVIDER_PRESETS:
        raise HTTPException(400, "API 类型无效")
    preset = PROVIDER_PRESETS[payload.preset]
    if payload.preset == "custom":
        base_url = await _validate_remote_base(payload.base_url)
        protocol = payload.protocol
    else:
        base_url = await _validate_remote_base(str(preset["baseUrl"]))
        protocol = str(preset["protocol"])
    name = payload.name.strip() or str(preset["name"])
    api_key = payload.api_key.strip()
    has_input_price = payload.input_price_per_million is not None
    has_output_price = payload.output_price_per_million is not None
    if has_input_price != has_output_price:
        raise HTTPException(400, "请同时填写输入和输出单价，或全部留空")
    pricing = None
    if has_input_price and has_output_price:
        pricing = {
            "currency": payload.price_currency,
            "inputPerMillion": float(payload.input_price_per_million),
            "outputPerMillion": float(payload.output_price_per_million),
        }

    async with provider_write_lock:
        providers = _read_providers()
        current = next((item for item in providers if item.get("id") == payload.id), None)
        if current is None and payload.id:
            raise HTTPException(404, "API 连接不存在")
        if not api_key and current:
            api_key = str(current.get("apiKey", ""))
        if not api_key:
            raise HTTPException(400, "请输入 API 密钥")
        provider_id = str(current.get("id")) if current else uuid.uuid4().hex[:12]
        keep_models = bool(current and current.get("baseUrl") == base_url and current.get("protocol") == protocol)
        updated = {
            "id": provider_id,
            "preset": payload.preset,
            "name": name,
            "baseUrl": base_url,
            "protocol": protocol,
            "apiKey": api_key,
            "enabled": payload.enabled,
            "models": current.get("models", []) if keep_models and current else [],
            "updatedAt": int(time.time()),
        }
        if pricing is not None:
            updated["pricing"] = pricing
        if current:
            providers[providers.index(current)] = updated
        else:
            providers.append(updated)
        _write_providers(providers)
    return {"provider": _public_provider(updated)}


@app.patch("/api/providers/{provider_id}")
async def toggle_provider(request: Request, provider_id: str, payload: ProviderToggleRequest):
    _require_admin(request)
    async with provider_write_lock:
        providers = _read_providers()
        provider = next((item for item in providers if item.get("id") == provider_id), None)
        if not provider:
            raise HTTPException(404, "API 连接不存在")
        provider["enabled"] = payload.enabled
        provider["updatedAt"] = int(time.time())
        _write_providers(providers)
    return {"provider": _public_provider(provider)}


@app.delete("/api/providers/{provider_id}")
async def delete_provider(request: Request, provider_id: str):
    _require_admin(request)
    if not PROVIDER_ID_RE.fullmatch(provider_id):
        raise HTTPException(400, "API 连接标识无效")
    async with provider_write_lock:
        providers = _read_providers()
        remaining = [item for item in providers if item.get("id") != provider_id]
        if len(remaining) == len(providers):
            raise HTTPException(404, "API 连接不存在")
        _write_providers(remaining)
    return {"ok": True}


@app.put("/api/providers/{provider_id}/models")
async def import_provider_models(request: Request, provider_id: str, payload: ProviderModelsRequest):
    _require_admin(request)
    models = _clean_model_ids(payload.models)
    if not models:
        raise HTTPException(400, "请输入至少一个模型 ID")
    async with provider_write_lock:
        providers = _read_providers()
        provider = next((item for item in providers if item.get("id") == provider_id), None)
        if not provider:
            raise HTTPException(404, "API 连接不存在")
        provider["models"] = models
        provider["updatedAt"] = int(time.time())
        _write_providers(providers)
    return {"provider": _public_provider(provider)}


@app.delete("/api/providers/{provider_id}/models")
async def delete_provider_model(request: Request, provider_id: str, payload: ProviderModelDeleteRequest):
    _require_admin(request)
    if not PROVIDER_ID_RE.fullmatch(provider_id):
        raise HTTPException(400, "API 连接标识无效")
    model_id = payload.model.strip()
    if not model_id:
        raise HTTPException(400, "模型 ID 不能为空")
    async with provider_write_lock:
        providers = _read_providers()
        provider = next((item for item in providers if item.get("id") == provider_id), None)
        if not provider:
            raise HTTPException(404, "API 连接不存在")
        models = _clean_model_ids(provider.get("models", []))
        if model_id not in models:
            raise HTTPException(404, "模型不存在或已被移除")
        provider["models"] = [item for item in models if item != model_id]
        provider["updatedAt"] = int(time.time())
        _write_providers(providers)
    return {
        "provider": _public_provider(provider),
        "removed": model_id,
        "count": len(provider["models"]),
    }


@app.post("/api/providers/{provider_id}/detect")
async def detect_provider_models(request: Request, provider_id: str):
    _require_admin(request)
    provider = _provider_by_id(provider_id)
    provider["baseUrl"] = await _validate_remote_base(str(provider.get("baseUrl", "")))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0), follow_redirects=False) as client:
            response = await client.get(_provider_endpoint(provider, "models"), headers=_provider_headers(provider))
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "模型检测超时，请检查 API 地址") from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, "无法连接该 API，请检查地址和网络") from exc
    if response.status_code >= 400:
        raise HTTPException(response.status_code if response.status_code < 500 else 502, _upstream_error(response.content, "模型检测失败"))
    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(502, "API 返回的模型列表格式无效") from exc
    rows = result.get("data") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        rows = result.get("models") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise HTTPException(502, "API 没有返回可识别的模型列表")
    models = _clean_model_ids([item.get("id") if isinstance(item, dict) else item for item in rows])
    if not models:
        raise HTTPException(502, "API 返回的模型列表为空，可改用手动导入")
    async with provider_write_lock:
        providers = _read_providers()
        latest = next((item for item in providers if item.get("id") == provider_id), None)
        if not latest:
            raise HTTPException(404, "API 连接不存在")
        latest["models"] = models
        latest["updatedAt"] = int(time.time())
        _write_providers(providers)
    return {"provider": _public_provider(latest), "count": len(models)}


@app.get("/api/models")
async def models(request: Request, include_codex: bool = True):
    _require_auth(request)
    data: list[dict[str, Any]] = []
    warning = ""
    if include_codex:
        disabled_codex_models = _read_disabled_codex_models()
        try:
            result = await codex.request("model/list", {"includeHidden": False, "limit": 100}, timeout=30)
            for model in result.get("data", []):
                model_id = str(model.get("model") or model.get("id") or "").strip()
                if not model_id or model_id in disabled_codex_models:
                    continue
                efforts = []
                for option in model.get("supportedReasoningEfforts", []):
                    effort = option.get("reasoningEffort") or option.get("effort") or option.get("id")
                    if effort:
                        efforts.append({
                            "id": effort,
                            "label": EFFORT_LABELS.get(effort, option.get("description") or effort),
                        })
                data.append({
                    "id": model_id,
                    "displayName": model.get("displayName") or model_id,
                    "description": model.get("description", ""),
                    "isDefault": model.get("isDefault", False),
                    "defaultEffort": model.get("defaultReasoningEffort"),
                    "efforts": efforts,
                    "inputModalities": model.get("inputModalities", ["text"]),
                    "serviceTiers": model.get("serviceTiers", []),
                    "source": "codex",
                    "providerName": "Codex",
                })
        except Exception:
            warning = "Codex 模型暂不可用"

    for provider in _read_providers():
        if not provider.get("enabled", True):
            continue
        provider_id = str(provider.get("id", ""))
        if not PROVIDER_ID_RE.fullmatch(provider_id):
            continue
        for model_id in _clean_model_ids(provider.get("models", [])):
            data.append({
                "id": _external_model_id(provider_id, model_id),
                "rawModel": model_id,
                "displayName": model_id,
                "description": f"通过 {provider.get('name', '外部 API')} 调用",
                "isDefault": False,
                "defaultEffort": "default",
                "efforts": [{"id": "default", "label": "默认"}],
                "inputModalities": ["text", "image"],
                "serviceTiers": [],
                "source": "external",
                "providerId": provider_id,
                "providerName": provider.get("name", "外部 API"),
            })
    return {"data": data, "warning": warning}
class ConversationSyncRequest(BaseModel):
    conversations: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_CLOUD_CONVERSATIONS)
    projects: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_CLOUD_PROJECTS)


class ConversationDeleteRequest(BaseModel):
    conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    workspace_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    thread_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    thread_ids: list[str] = Field(default_factory=list, max_length=20)
    delete_workspace: bool = True


class ChunkUploadInitRequest(BaseModel):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0, le=MAX_FILE_BYTES)
    content_type: str = Field(default="", max_length=200)
    source: Literal["file", "composer_text"] = "file"


class ChunkUploadCompleteRequest(BaseModel):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def _upload_staging_root(upload_root: Path) -> Path:
    upload_root = upload_root.resolve()
    staging_root = (upload_root / UPLOAD_STAGING_DIR).resolve()
    if staging_root.parent != upload_root:
        raise HTTPException(400, "上传暂存路径无效")
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return staging_root


def _chunk_upload_paths(upload_root: Path, upload_id: str) -> tuple[Path, Path]:
    if not UPLOAD_ID_RE.fullmatch(upload_id):
        raise HTTPException(404, "分片上传不存在或已经失效")
    staging_root = _upload_staging_root(upload_root)
    part_path = (staging_root / f"{upload_id}.part").resolve()
    metadata_path = (staging_root / f"{upload_id}.json").resolve()
    if part_path.parent != staging_root or metadata_path.parent != staging_root:
        raise HTTPException(400, "分片上传路径无效")
    return part_path, metadata_path


def _remove_paths(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOG.warning("Unable to remove temporary upload %s", path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cleanup_stale_uploads(upload_root: Path, *, now: float | None = None) -> None:
    staging_root = _upload_staging_root(upload_root)
    cutoff = (time.time() if now is None else now) - UPLOAD_STAGING_TTL_SECONDS
    receipt_paths: dict[str, list[Path]] = {}
    for candidate in staging_root.iterdir():
        try:
            if not candidate.is_file():
                continue
            matched = re.fullmatch(r"([a-f0-9]{32})\.(?:part|json)", candidate.name)
            if matched:
                receipt_paths.setdefault(matched.group(1), []).append(candidate)
            elif candidate.stat().st_mtime < cutoff:
                candidate.unlink(missing_ok=True)
        except OSError:
            LOG.warning("Unable to clean stale upload %s", candidate)
    for upload_id, candidates in receipt_paths.items():
        try:
            if any(candidate.stat().st_mtime >= cutoff for candidate in candidates):
                continue
        except OSError:
            continue
        lock = chunk_upload_locks.get(upload_id)
        if lock is not None and lock.locked():
            continue
        _remove_paths(*candidates)
        chunk_upload_locks.pop(upload_id, None)
        chunk_upload_lock_times.pop(upload_id, None)


def _chunk_upload_lock(upload_id: str) -> asyncio.Lock:
    now = time.monotonic()
    existing = chunk_upload_locks.get(upload_id)
    if existing is not None:
        chunk_upload_lock_times[upload_id] = now
        return existing
    if len(chunk_upload_locks) >= MAX_CHUNK_UPLOAD_LOCKS:
        for candidate, last_used in sorted(
            chunk_upload_lock_times.items(),
            key=lambda item: item[1],
        ):
            lock = chunk_upload_locks.get(candidate)
            waiters = getattr(lock, "_waiters", None) if lock is not None else None
            if (
                lock is not None
                and not lock.locked()
                and not any(not waiter.done() for waiter in (waiters or ()))
            ):
                chunk_upload_locks.pop(candidate, None)
                chunk_upload_lock_times.pop(candidate, None)
            if len(chunk_upload_locks) < MAX_CHUNK_UPLOAD_LOCKS:
                break
    if len(chunk_upload_locks) >= MAX_CHUNK_UPLOAD_LOCKS:
        raise HTTPException(503, "同时进行的分片上传过多，请稍后重试")
    lock = asyncio.Lock()
    chunk_upload_locks[upload_id] = lock
    chunk_upload_lock_times[upload_id] = now
    return lock


def _upload_file_result(stored_name: str, original: str, extension: str, size: int) -> dict[str, Any]:
    return {
        "id": stored_name,
        "name": original,
        "size": size,
        "type": "image" if extension in IMAGE_EXTENSIONS else "document",
    }


def _write_upload_block(target: Path, block: bytes, create: bool) -> None:
    with target.open("xb" if create else "ab") as handle:
        if block and handle.write(block) != len(block):
            raise OSError("上传分片未完整写入")


def _sync_upload_file(target: Path) -> None:
    with target.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _truncate_upload_file(target: Path, size: int) -> None:
    with target.open("r+b") as handle:
        handle.truncate(size)
        handle.flush()
        os.fsync(handle.fileno())


def _upload_block_matches(target: Path, offset: int, block: bytes) -> bool:
    with target.open("rb") as handle:
        handle.seek(offset)
        return handle.read(len(block)) == block


def _write_chunk_block(target: Path, offset: int, block: bytes) -> None:
    try:
        with target.open("r+b") as handle:
            handle.seek(offset)
            if handle.write(block) != len(block):
                raise OSError("分片未完整写入")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            _truncate_upload_file(target, offset)
        except OSError:
            LOG.exception("Unable to roll back partial upload %s", target.name)
        raise


async def _await_upload_io(function, *args) -> Any:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except BaseException:
        await asyncio.gather(task, return_exceptions=True)
        raise


def _composer_text_preview(target: Path, original: str) -> str:
    try:
        edge = 1_600
        byte_window = edge * 4 + 4
        size = target.stat().st_size
        if size <= edge * 8 + 8:
            text = target.read_bytes().decode("utf-8", errors="replace")
            if len(text) <= edge * 2:
                return text
            head = text[:edge]
            tail = text[-edge:]
        else:
            with target.open("rb") as handle:
                head = handle.read(byte_window).decode("utf-8", errors="replace")[:edge]
                handle.seek(max(0, size - byte_window))
                tail_bytes = handle.read(byte_window)
            if size > byte_window:
                boundary = 0
                while boundary < len(tail_bytes) and tail_bytes[boundary] & 0xC0 == 0x80:
                    boundary += 1
                tail_bytes = tail_bytes[boundary:]
            tail = tail_bytes.decode("utf-8", errors="replace")[-edge:]
    except OSError as exc:
        raise HTTPException(400, f"长文字附件读取失败：{exc}") from exc
    return (
        head
        + f"\n\n……（中间内容已省略，完整原文已作为附件“{original}”上传，请以附件为准。）……\n\n"
        + tail
    )


def _load_chunk_upload(upload_root: Path, upload_id: str) -> tuple[dict[str, Any], Path, Path, Path]:
    part_path, metadata_path = _chunk_upload_paths(upload_root, upload_id)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        original = str(metadata["original"])
        extension = str(metadata["extension"])
        expected_size = int(metadata["expected_size"])
        stored_name = str(metadata["stored_name"])
        source = str(metadata.get("source") or "file")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(404, "分片上传不存在或已经失效") from None
    if (
        metadata.get("upload_id") != upload_id
        or original != _safe_filename(original)
        or extension != Path(original).suffix.lower()
        or extension not in ALLOWED_EXTENSIONS
        or not 0 <= expected_size <= MAX_FILE_BYTES
        or stored_name != _safe_filename(stored_name)
        or source not in {"file", "composer_text"}
        or (source == "composer_text" and extension != ".txt")
    ):
        raise HTTPException(400, "分片上传记录无效")
    final_path = (upload_root.resolve() / stored_name).resolve()
    if final_path.parent != upload_root.resolve():
        raise HTTPException(400, "分片上传目标无效")
    metadata.update({
        "original": original,
        "extension": extension,
        "expected_size": expected_size,
        "stored_name": stored_name,
        "source": source,
    })
    return metadata, part_path, metadata_path, final_path


async def _stream_upload_to_path(
    upload: UploadFile,
    target: Path,
    batch_size: int,
) -> tuple[int, int]:
    written = 0
    created = False
    try:
        while True:
            block = await upload.read(UPLOAD_STREAM_BYTES)
            if not block:
                break
            written += len(block)
            if written > MAX_FILE_BYTES:
                raise HTTPException(413, f"{_safe_filename(upload.filename or 'file')} 超过 30 MB")
            if batch_size + written > MAX_UPLOAD_BATCH_BYTES:
                raise HTTPException(413, "单次上传的文件合计超过 30 MB")
            await _await_upload_io(_write_upload_block, target, block, not created)
            created = True
        if not created:
            await _await_upload_io(_write_upload_block, target, b"", True)
        await _await_upload_io(_sync_upload_file, target)
    except BaseException:
        await _await_upload_io(_remove_paths, target)
        raise
    return written, batch_size + written


@app.post("/api/conversations/sync")
async def sync_conversations(request: Request, payload: ConversationSyncRequest):
    user = _require_auth(request)
    async with conversation_write_lock:
        return await asyncio.to_thread(
            _sync_cloud_conversations, user["id"], payload.conversations, payload.projects
        )


@app.post("/api/uploads")
async def upload_files(
    request: Request,
    session_id: str = Form(...),
    files: list[UploadFile] = File(...),
):
    user = _require_auth(request)
    if not 1 <= len(files) <= MAX_FILES_PER_UPLOAD:
        raise HTTPException(400, f"一次最多上传 {MAX_FILES_PER_UPLOAD} 个文件")
    root = _session_path(user["id"], session_id)
    upload_root = (root / "uploads").resolve()
    staging_root = _upload_staging_root(upload_root)
    _cleanup_stale_uploads(upload_root)
    validated = []
    for upload in files:
        original = _safe_filename(upload.filename or "file")
        extension = Path(original).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(415, f"暂不支持 {extension or '无扩展名'} 文件")
        validated.append((upload, original, extension))
    staged: list[tuple[Path, Path, dict[str, Any]]] = []
    committed: list[Path] = []
    batch_size = 0
    try:
        for upload, original, extension in validated:
            temporary = (staging_root / f"legacy-{uuid.uuid4().hex}.part").resolve()
            if temporary.parent != staging_root:
                raise HTTPException(400, "上传暂存路径无效")
            size, batch_size = await _stream_upload_to_path(upload, temporary, batch_size)
            while True:
                stored_name = f"{uuid.uuid4().hex[:10]}-{original}"
                target = (upload_root / stored_name).resolve()
                if target.parent == upload_root and not target.exists():
                    break
            staged.append((
                temporary,
                target,
                _upload_file_result(stored_name, original, extension, size),
            ))
        for temporary, target, _result in staged:
            os.replace(temporary, target)
            committed.append(target)
    except BaseException:
        _remove_paths(*(item[0] for item in staged), *committed)
        raise
    return {"files": [item[2] for item in staged]}


@app.post("/api/uploads/init")
async def init_chunk_upload(request: Request, payload: ChunkUploadInitRequest):
    user = _require_auth(request)
    original = _safe_filename(payload.name)
    extension = Path(original).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, f"暂不支持 {extension or '无扩展名'} 文件")
    if payload.source == "composer_text" and extension != ".txt":
        raise HTTPException(415, "长文字附件必须使用 TXT 格式")
    root = _session_path(user["id"], payload.session_id)
    upload_root = (root / "uploads").resolve()
    _cleanup_stale_uploads(upload_root)
    while True:
        upload_id = uuid.uuid4().hex
        part_path, metadata_path = _chunk_upload_paths(upload_root, upload_id)
        stored_name = f"{uuid.uuid4().hex[:10]}-{original}"
        final_path = (upload_root / stored_name).resolve()
        if not part_path.exists() and not metadata_path.exists() and not final_path.exists():
            break
    metadata = {
        "upload_id": upload_id,
        "original": original,
        "extension": extension,
        "expected_size": payload.size,
        "stored_name": stored_name,
        "source": payload.source,
        "created_at": int(time.time()),
    }
    try:
        part_path.touch(mode=0o600, exist_ok=False)
        _atomic_write_json(metadata_path, metadata)
    except Exception:
        _remove_paths(part_path, metadata_path)
        raise
    return {
        "uploadId": upload_id,
        "chunkSize": UPLOAD_CHUNK_BYTES,
        "nextOffset": 0,
    }


@app.post("/api/uploads/{upload_id}/chunk")
async def upload_chunk(
    request: Request,
    upload_id: str,
    session_id: str = Form(...),
    offset: int = Form(...),
    chunk: UploadFile = File(...),
):
    user = _require_auth(request)
    root = _session_path(user["id"], session_id)
    upload_root = (root / "uploads").resolve()
    block = await chunk.read(UPLOAD_CHUNK_BYTES + 1)
    if len(block) > UPLOAD_CHUNK_BYTES:
        raise HTTPException(413, "单个分片超过 3 MB")
    if offset < 0:
        raise HTTPException(400, "分片偏移量无效")
    _load_chunk_upload(upload_root, upload_id)
    lock = _chunk_upload_lock(upload_id)
    async with lock:
        metadata, part_path, metadata_path, final_path = _load_chunk_upload(upload_root, upload_id)
        expected_size = int(metadata["expected_size"])
        if not block and expected_size:
            raise HTTPException(400, "分片内容不能为空")
        if offset + len(block) > expected_size:
            raise HTTPException(413, "分片内容超过声明的文件大小")
        source = part_path if part_path.is_file() else final_path
        if not source.is_file():
            raise HTTPException(404, "分片上传不存在或已经失效")
        current_size = source.stat().st_size
        if (
            source == part_path
            and offset < current_size < offset + len(block)
            and current_size < expected_size
            and offset % UPLOAD_CHUNK_BYTES == 0
        ):
            await _await_upload_io(_truncate_upload_file, part_path, offset)
            current_size = offset
        if offset < current_size:
            if offset + len(block) > current_size:
                raise HTTPException(409, f"分片偏移冲突，应从 {current_size} 字节继续")
            if not await _await_upload_io(_upload_block_matches, source, offset, block):
                raise HTTPException(409, "重复分片内容不一致")
            if source == part_path:
                os.utime(part_path, None)
            os.utime(metadata_path, None)
            return {
                "uploadId": upload_id,
                "nextOffset": current_size,
                "complete": current_size == expected_size,
            }
        if offset != current_size:
            raise HTTPException(409, f"分片顺序错误，应从 {current_size} 字节继续")
        if len(block) != UPLOAD_CHUNK_BYTES and offset + len(block) < expected_size:
            raise HTTPException(400, "除最后一片外，每个分片必须为 3 MB")
        if source == final_path:
            raise HTTPException(409, "文件已经完成，不能继续追加分片")
        if block:
            await _await_upload_io(_write_chunk_block, part_path, current_size, block)
        os.utime(metadata_path, None)
        next_offset = offset + len(block)
        return {
            "uploadId": upload_id,
            "nextOffset": next_offset,
            "complete": next_offset == expected_size,
        }


@app.post("/api/uploads/{upload_id}/complete")
async def complete_chunk_upload(
    request: Request,
    upload_id: str,
    payload: ChunkUploadCompleteRequest,
):
    user = _require_auth(request)
    root = _session_path(user["id"], payload.session_id)
    upload_root = (root / "uploads").resolve()
    _load_chunk_upload(upload_root, upload_id)
    lock = _chunk_upload_lock(upload_id)
    async with lock:
        metadata, part_path, metadata_path, final_path = _load_chunk_upload(upload_root, upload_id)
        expected_size = int(metadata["expected_size"])
        if final_path.is_file() and not part_path.exists():
            if final_path.stat().st_size != expected_size:
                raise HTTPException(409, "已完成文件大小与声明不一致")
        else:
            if not part_path.is_file():
                raise HTTPException(404, "分片上传不存在或已经失效")
            if part_path.stat().st_size != expected_size:
                raise HTTPException(
                    409,
                    f"文件尚未上传完整，当前 {part_path.stat().st_size} / {expected_size} 字节",
                )
            if final_path.exists():
                raise HTTPException(409, "上传目标已存在，请重新开始上传")
            os.replace(part_path, final_path)
        os.utime(metadata_path, None)
        result = _upload_file_result(
            str(metadata["stored_name"]),
            str(metadata["original"]),
            str(metadata["extension"]),
            expected_size,
        )
        if metadata["source"] == "composer_text":
            result["source"] = "composer_text"
            result["preview"] = _composer_text_preview(final_path, str(metadata["original"]))
    return {"files": [result]}

@app.get("/api/transfer-image/{token}")
async def transfer_image(token: str):
    claims = _decode_transfer_token(token)
    workspace = _workspace_path(claims["user_id"], claims["session_id"])
    upload_root = (workspace / "uploads").resolve()
    relative = PurePosixPath(claims["relative_path"])
    target = upload_root.joinpath(*relative.parts).resolve()
    if (
        upload_root not in target.parents
        or not target.is_file()
        or target.suffix.lower() not in IMAGE_EXTENSIONS
        or target.stat().st_size > MAX_FILE_BYTES
    ):
        raise HTTPException(404, "临时图片不存在或已经失效")
    return FileResponse(
        target,
        media_type=_file_media_type(target),
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        },
    )



@app.post("/api/conversations/delete")
async def delete_conversation(request: Request, payload: ConversationDeleteRequest):
    user = _require_auth(request)
    thread_ids: list[str] = []
    for thread_id in ([payload.thread_id] if payload.thread_id else []) + payload.thread_ids:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
            raise HTTPException(400, "服务器会话标识无效")
        if thread_id not in thread_ids:
            thread_ids.append(thread_id)
    for thread_id in thread_ids:
        _require_codex_thread_owner(user["id"], thread_id)
        try:
            await codex.request("thread/delete", {"threadId": thread_id}, timeout=30)
        except CodexProtocolError as exc:
            raise HTTPException(502, f"服务器会话删除失败：{exc}") from exc
        codex.loaded_threads.discard(thread_id)
        _forget_codex_thread(user["id"], thread_id)

    workspace_deleted = False
    if payload.delete_workspace:
        workspace = _workspace_path(user["id"], payload.workspace_id)
        if workspace.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, workspace)
                workspace_deleted = True
            except OSError as exc:
                LOG.exception("Failed to remove conversation workspace %s", workspace)
                raise HTTPException(500, "会话记录已删除，但临时文件清理失败，请重试") from exc
    cloud_deleted = False
    if payload.conversation_id:
        async with conversation_write_lock:
            await asyncio.to_thread(_delete_cloud_conversation, user["id"], payload.conversation_id)
        await _clear_external_conversation_state(user["id"], payload.conversation_id)
        cloud_deleted = True
    return {
        "ok": True,
        "threadsDeleted": len(thread_ids),
        "workspaceDeleted": workspace_deleted,
        "cloudDeleted": cloud_deleted,
    }


class AttachmentRef(BaseModel):
    id: str = Field(min_length=1, max_length=180)
    name: str = Field(min_length=1, max_length=255)
    source: Literal["file", "composer_text"] = "file"


class HistoryMessage(BaseModel):
    id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    role: Literal["user", "assistant"]
    content: str = Field(max_length=60_000)


class TurnRequest(BaseModel):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    client_conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    thread_id: str | None = None
    external_conversation_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
    )
    external_context_key: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    message: str = Field(max_length=60_000)
    model: str
    effort: str
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=5)
    project_attachments: list[AttachmentRef] = Field(default_factory=list, max_length=MAX_PROJECT_FILES)
    project_instructions: str = Field(default="", max_length=MAX_PROJECT_INSTRUCTIONS)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=60)


def _validate_user_content_size(payload: TurnRequest, workspace: Path) -> int:
    composer_texts = [
        attachment for attachment in payload.attachments
        if attachment.source == "composer_text"
    ]
    if len(composer_texts) > 1:
        raise HTTPException(400, "每轮最多包含一个长文字附件")
    message_bytes = len(payload.message.encode("utf-8"))
    total = message_bytes
    upload_root = (workspace / "uploads").resolve()
    for attachment in payload.attachments:
        target = (upload_root / attachment.id).resolve()
        if target.parent != upload_root or not target.is_file():
            raise HTTPException(400, f"附件 {attachment.name} 不存在")
        if attachment.source == "composer_text" and target.suffix.lower() != ".txt":
            raise HTTPException(400, "长文字附件必须是 TXT 文件")
        if attachment.source == "composer_text":
            expected_preview = _composer_text_preview(target, attachment.name)
            if payload.message != expected_preview:
                raise HTTPException(400, "长文字附件的预览已改变，请移除后重新发送")
            if message_bytes > MAX_INLINE_MESSAGE_BYTES:
                raise HTTPException(413, "长文字附件的预览不能超过 60 KB")
            total -= message_bytes
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            raise HTTPException(413, f"{attachment.name} 超过 30 MB")
        total += size
        if total > MAX_USER_CONTENT_BYTES:
            raise HTTPException(413, "本轮文字与当前附件原始大小合计超过 30 MB")
    if total > MAX_USER_CONTENT_BYTES:
        raise HTTPException(413, "本轮文字与当前附件原始大小合计超过 30 MB")
    return total


def _normalize_context_text(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def _clip_context_text(value: Any, limit: int) -> str:
    text = _normalize_context_text(value)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker_template = "\n…[中间省略 {count} 字]…\n"
    marker = marker_template.format(count=max(0, len(text) - limit))
    if len(marker) >= limit:
        return text[:limit]
    available = limit - len(marker)
    head = max(1, int(available * 0.7))
    tail = max(0, available - head)
    omitted = max(0, len(text) - head - tail)
    marker = marker_template.format(count=omitted)
    while head + tail + len(marker) > limit and head > 1:
        head -= 1
    return text[:head] + marker + (text[-tail:] if tail else "")


def _cloud_context_history(user_id: str, conversation_id: str | None) -> list[HistoryMessage]:
    if not conversation_id:
        return []
    _init_conversations_db()
    with _database_connection() as database:
        row = database.execute(
            "SELECT payload FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
    if not row:
        return []
    try:
        raw = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return []
    result: list[HistoryMessage] = []
    for item in raw.get("messages") if isinstance(raw, dict) and isinstance(raw.get("messages"), list) else []:
        if (
            not isinstance(item, dict)
            or item.get("role") not in {"user", "assistant"}
            or item.get("error")
        ):
            continue
        content = _normalize_context_text(item.get("content"))
        if not content:
            continue
        message_id = str(item.get("id") or "")
        result.append(HistoryMessage(
            id=message_id if SESSION_RE.fullmatch(message_id) else None,
            role=item["role"],
            content=content[:60_000],
        ))
    return result[-MAX_CLOUD_MESSAGES:]


def _merged_context_history(
    cloud_history: list[HistoryMessage],
    client_history: list[HistoryMessage],
) -> list[HistoryMessage]:
    client = [
        HistoryMessage(id=item.id, role=item.role, content=_normalize_context_text(item.content))
        for item in client_history
        if _normalize_context_text(item.content)
    ]
    if not cloud_history:
        return client
    if not client:
        return cloud_history

    client_by_id = {item.id: item for item in client if item.id}
    cloud = [client_by_id.get(item.id, item) if item.id else item for item in cloud_history]

    def same(left: HistoryMessage, right: HistoryMessage) -> bool:
        if left.id and right.id:
            return left.id == right.id
        return left.role == right.role and left.content == right.content

    maximum_overlap = min(len(cloud), len(client))
    for size in range(maximum_overlap, 0, -1):
        if all(same(cloud[-size + index], client[index]) for index in range(size)):
            return [*cloud, *client[size:]][-MAX_CLOUD_MESSAGES:]

    if len(client) <= len(cloud):
        for start in range(len(cloud) - len(client) + 1):
            if all(same(cloud[start + index], client[index]) for index in range(len(client))):
                return cloud

    cloud_ids = {item.id for item in cloud if item.id}
    if cloud_ids and any(item.id in cloud_ids for item in client if item.id):
        additions = [item for item in client if not item.id or item.id not in cloud_ids]
        return [*cloud, *additions][-MAX_CLOUD_MESSAGES:]
    return [*cloud, *client][-MAX_CLOUD_MESSAGES:]


def _history_turns(history: list[HistoryMessage]) -> list[tuple[str, str]]:
    turns: list[dict[str, list[str]]] = []
    for item in history:
        content = _normalize_context_text(item.content)
        if not content:
            continue
        if item.role == "user":
            turns.append({"user": [content], "assistant": []})
        elif turns:
            turns[-1]["assistant"].append(content)
    return [
        ("\n\n".join(turn["user"]), "\n\n".join(turn["assistant"]))
        for turn in turns
        if turn["user"]
    ]


def _older_context_excerpt(turns: list[tuple[str, str]], limit: int) -> str:
    if not turns or limit <= 0:
        return ""
    rendered: list[tuple[int, str]] = []
    for index, (user_text, assistant_text) in enumerate(turns, start=1):
        parts = [f"第 {index} 轮", f"用户：{_clip_context_text(user_text, 480)}"]
        if assistant_text:
            parts.append(f"助手：{_clip_context_text(assistant_text, 320)}")
        rendered.append((index, "\n".join(parts)))
    selected: dict[int, str] = {}
    used = 0
    for index, text in rendered[:2]:
        addition = len(text) + (2 if selected else 0)
        if used + addition > limit:
            break
        selected[index] = text
        used += addition
    for index, text in reversed(rendered[2:]):
        addition = len(text) + (2 if selected else 0)
        if used + addition > limit:
            continue
        selected[index] = text
        used += addition
    ordered = [selected[index] for index in sorted(selected)]
    omitted = len(rendered) - len(ordered)
    if omitted:
        marker = f"…另有 {omitted} 个较早轮次已压缩省略…"
        insertion = min(2, len(ordered))
        ordered.insert(insertion, marker)
    return _clip_context_text("\n\n".join(ordered), limit)


def _fit_context_window(
    history: list[HistoryMessage],
    available_chars: int,
) -> tuple[list[HistoryMessage], str]:
    turns = _history_turns(history)
    recent_turns = turns[-MAX_RECENT_HISTORY_TURNS:]
    older_turns = turns[:-MAX_RECENT_HISTORY_TURNS]
    digest_limit = min(MAX_OLDER_CONTEXT_CHARS, max(0, available_chars // 5))
    digest = _older_context_excerpt(older_turns, digest_limit)
    digest_block_length = len(digest) + 96 if digest else 0
    recent_budget = max(0, available_chars - digest_block_length)
    kept: list[tuple[str, str]] = []
    used = 0
    for user_text, assistant_text in reversed(recent_turns):
        cost = len(user_text) + len(assistant_text) + 24
        if used + cost <= recent_budget:
            kept.append((user_text, assistant_text))
            used += cost
            continue
        remaining = recent_budget - used
        if remaining >= 160:
            user_limit = max(80, int(remaining * (0.65 if assistant_text else 1.0)))
            clipped_user = _clip_context_text(user_text, user_limit)
            assistant_limit = max(0, remaining - len(clipped_user) - 24)
            clipped_assistant = _clip_context_text(assistant_text, assistant_limit) if assistant_limit else ""
            kept.append((clipped_user, clipped_assistant))
        break
    recent: list[HistoryMessage] = []
    for user_text, assistant_text in reversed(kept):
        recent.append(HistoryMessage(role="user", content=user_text))
        if assistant_text:
            recent.append(HistoryMessage(role="assistant", content=assistant_text))
    return recent, digest


def _external_context_key(
    payload: TurnRequest,
    provider_id: str,
    provider: dict[str, Any],
    model_id: str,
) -> str:
    provider_address = str(provider.get("baseUrl") or "").strip().rstrip("/")
    address_digest = hashlib.sha256(provider_address.encode("utf-8")).hexdigest()[:20]
    project_files = sorted(
        ({"id": item.id, "name": item.name} for item in payload.project_attachments),
        key=lambda item: (item["id"], item["name"]),
    )
    material = {
        "version": EXTERNAL_SYSTEM_VERSION,
        "provider": provider_id,
        "protocol": str(provider.get("protocol") or "openai"),
        "model": model_id,
        "providerAddress": address_digest,
        "projectInstructions": _normalize_context_text(payload.project_instructions),
        "projectFiles": project_files,
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _extract_attachment_text(
    target: Path,
    query: str = "",
    *,
    query_terms: list[str] | None = None,
) -> str:
    extension = target.suffix.lower()
    try:
        if extension == ".pdf":
            reader = PdfReader(str(target))
            extracted = _stream_relevant_attachment_excerpt(
                ((page.extract_text() or "") + "\n\n" for page in reader.pages),
                query,
                MAX_EXTRACTED_CHARS,
                query_terms=query_terms,
            )
            if not extracted.strip():
                extracted = "该 PDF 没有可提取的文本，可能是扫描件。"
        elif extension in OFFICE_EXTENSIONS:
            extracted = _extract_office_text(target)
            if not extracted.strip():
                extracted = "该 Office 文件没有可提取的文字。"
        else:
            with target.open("r", encoding="utf-8", errors="replace") as handle:
                extracted = _stream_relevant_attachment_excerpt(
                    iter(lambda: handle.read(64 * 1024), ""),
                    query,
                    MAX_EXTRACTED_CHARS,
                    query_terms=query_terms,
                )
    except Exception as exc:
        extracted = f"附件读取失败：{exc}"
    return _relevant_attachment_excerpt(
        extracted,
        query,
        MAX_EXTRACTED_CHARS,
        query_terms=query_terms,
    )


def _sample_context_query(query: str) -> str:
    if len(query) <= MAX_CONTEXT_QUERY_SCAN_CHARS:
        return query
    window_count = MAX_CONTEXT_QUERY_SAMPLE_WINDOWS
    window_chars = max(1, (MAX_CONTEXT_QUERY_SCAN_CHARS - window_count + 1) // window_count)
    last_start = len(query) - window_chars
    starts = sorted({
        round(index * last_start / (window_count - 1))
        for index in range(window_count)
    })
    return " ".join(query[start:start + window_chars] for start in starts)


def _sampled_regex_matches(pattern: re.Pattern[str], text: str, limit: int) -> list[str]:
    first_matches: list[str] = []
    match_count = 0
    for match in pattern.finditer(text):
        if match_count < limit:
            first_matches.append(match.group())
        match_count += 1
    if match_count <= limit:
        return first_matches

    wanted = {
        round(index * (match_count - 1) / (limit - 1))
        for index in range(limit)
    }
    return [
        match.group()
        for index, match in enumerate(pattern.finditer(text))
        if index in wanted
    ]


def _context_query_terms(query: str) -> list[str]:
    normalized = _normalize_context_text(_sample_context_query(query)).lower()
    terms: list[str] = []
    max_terms = 32
    ignored = {
        "请问", "帮我", "告诉我", "是什么", "什么意思", "怎么样", "怎么办",
        "为什么", "有哪些", "有没有", "是否", "如何", "什么", "怎么",
        "附件", "文档", "文件", "内容", "问题", "一下",
        "what", "which", "where", "when", "why", "how", "the", "is", "are",
        "does", "do", "please", "find", "tell", "about",
    }

    def add(candidate: str) -> None:
        candidate = candidate.strip()
        if 2 <= len(candidate) <= 64 and candidate not in terms and candidate not in ignored:
            terms.append(candidate)

    # Keep exact Latin identifiers first: model names, codes and hyphenated terms
    # are usually more selective than natural-language filler.
    latin_tokens = [
        token
        for token in _sampled_regex_matches(
            re.compile(r"[a-z0-9_.-]{2,}"), normalized, 12
        )
        if token not in ignored
    ]
    for token in latin_tokens:
        if len(token) <= 64:
            add(token)
        else:
            add(token[:32])
            add(token[-32:])

    prefixes = (
        "请问一下", "请告诉我", "请帮我查找", "请帮我查询", "帮我查找",
        "帮我查询", "麻烦查找", "麻烦查询", "请查找", "请查询", "帮我看看",
        "我想知道", "想知道", "请问", "请",
    )
    suffixes = (
        "分别是什么", "是什么意思", "是什么东西", "是什么", "有哪些内容",
        "有哪些", "怎么样", "怎么办", "如何处理", "如何", "请回答", "吗", "呢",
    )
    separators = re.compile(
        r"(?:请问一下|请告诉我|请帮我|帮我|麻烦|请|查找|查询|搜索|概括|总结|"
        r"根据|关于|告诉我|说明|回答|是什么意思|是什么|怎么样|怎么办|如何|"
        r"是否|有没有|有哪些|附件|文档|文件|内容|问题|一下)"
    )

    variants: list[str] = []
    chinese_tokens = _sampled_regex_matches(
        re.compile(r"[\u4e00-\u9fff]{2,}"),
        normalized,
        MAX_CONTEXT_QUERY_SAMPLE_WINDOWS,
    )
    for token in chinese_tokens:
        core = token
        changed = True
        while changed and len(core) >= 2:
            changed = False
            for prefix in prefixes:
                if core.startswith(prefix) and len(core) - len(prefix) >= 2:
                    core = core[len(prefix):]
                    changed = True
                    break
            for suffix in suffixes:
                if core.endswith(suffix) and len(core) - len(suffix) >= 2:
                    core = core[:-len(suffix)]
                    changed = True
                    break

        split_limit = max(1, 64 - len(variants))
        for variant in (*separators.split(core, maxsplit=split_limit), core, token):
            if len(variant) >= 2 and variant not in variants:
                variants.append(variant)
        if len(variants) >= 64:
            break

    # Add the concise subjects from every clause before n-grams so a long opening
    # clause cannot consume the whole bounded term budget.
    for variant in variants:
        if len(variant) <= 12:
            add(variant)
            if len(terms) >= max_terms:
                return terms

    for width in (4, 3, 2):
        windows: list[tuple[str, list[int]]] = []
        for variant in variants:
            if len(variant) >= width:
                window_count = len(variant) - width + 1
                if window_count <= 12:
                    starts = list(range(window_count))
                else:
                    starts = sorted({round(index * (window_count - 1) / 11) for index in range(12)})
                windows.append((variant, starts))
        # Round-robin across clauses keeps both the start and end of a long
        # question represented without creating an unbounded combination list.
        for rank in range(12):
            for variant, starts in windows:
                if rank < len(starts):
                    start = starts[rank]
                    add(variant[start:start + width])
                    if len(terms) >= max_terms:
                        return terms
    return terms


def _relevant_attachment_excerpt(
    text: str,
    query: str,
    limit: int,
    *,
    query_terms: list[str] | None = None,
) -> str:
    normalized = _normalize_context_text(text)
    if len(normalized) <= limit:
        return normalized
    chunk_size = 1_200
    if limit < chunk_size * 2 + 5:
        return _clip_context_text(normalized, limit)
    chunk_count = math.ceil(len(normalized) / chunk_size)
    terms = query_terms if query_terms is not None else _context_query_terms(query)
    selected: dict[int, str] = {
        0: normalized[:chunk_size],
        chunk_count - 1: normalized[(chunk_count - 1) * chunk_size:],
    }
    mandatory_size = sum(len(chunk) for chunk in selected.values()) + 5 * (len(selected) - 1)
    candidate_slots = max(0, (limit - mandatory_size) // (chunk_size + 5))
    candidates: list[tuple[int, int, int, str]] = []
    for index in range(1, chunk_count - 1):
        chunk = normalized[index * chunk_size:(index + 1) * chunk_size]
        lowered = chunk.lower()
        score = sum(len(term) * len(term) for term in terms if term in lowered)
        entry = (score, -index, index, chunk)
        if len(candidates) < candidate_slots:
            heapq.heappush(candidates, entry)
        elif candidate_slots and entry[:2] > candidates[0][:2]:
            heapq.heapreplace(candidates, entry)
    selected.update({index: chunk for _score, _reverse, index, chunk in candidates})
    result = "\n\n…\n\n".join(selected[index] for index in sorted(selected))
    return _clip_context_text(result, limit)


def _stream_relevant_attachment_excerpt(
    parts,
    query: str,
    limit: int,
    *,
    query_terms: list[str] | None = None,
) -> str:
    """Select a bounded relevant excerpt without loading the source in memory."""
    if limit <= 0:
        return ""
    chunk_size = 1_200
    terms = query_terms if query_terms is not None else _context_query_terms(query)
    rank_capacity = max(2, math.ceil(limit / chunk_size) + 2)
    ranked: list[tuple[int, int, int, str]] = []
    small_chunks: list[str] = []
    buffered = ""
    first = ""
    last = ""
    last_index = -1
    total_chars = 0

    def consume(raw_chunk: str) -> None:
        nonlocal first, last, last_index, total_chars
        if not raw_chunk:
            return
        chunk = unicodedata.normalize("NFC", raw_chunk)
        index = last_index + 1
        last_index = index
        total_chars += len(chunk)
        if index == 0:
            first = chunk
        last = chunk
        if total_chars <= limit:
            small_chunks.append(chunk)
        if index > 0:
            lowered = chunk.lower()
            score = sum(len(term) * len(term) for term in terms if term in lowered)
            entry = (score, -index, index, chunk)
            if len(ranked) < rank_capacity:
                heapq.heappush(ranked, entry)
            elif entry[:2] > ranked[0][:2]:
                heapq.heapreplace(ranked, entry)

    for part in parts:
        if not part:
            continue
        buffered += str(part)
        while len(buffered) >= chunk_size:
            consume(buffered[:chunk_size])
            buffered = buffered[chunk_size:]
    consume(buffered)

    if last_index < 0:
        return ""
    if total_chars <= limit:
        return _normalize_context_text("".join(small_chunks))
    selected: dict[int, str] = {0: first}
    if last_index:
        selected[last_index] = last
    mandatory_size = sum(len(chunk) for chunk in selected.values()) + 5 * (len(selected) - 1)
    candidate_slots = max(0, (limit - mandatory_size) // (chunk_size + 5))
    candidates = [entry for entry in ranked if entry[2] != last_index]
    for _score, _reverse, index, chunk in sorted(candidates, reverse=True)[:candidate_slots]:
        selected[index] = chunk
    result = "\n\n…\n\n".join(selected[index] for index in sorted(selected))
    return _clip_context_text(result, limit)


def _attachment_inputs(
    payload: TurnRequest,
    workspace: Path,
    include_history: bool = True,
    *,
    include_project_context: bool = True,
    include_project_instructions: bool | None = None,
    include_project_images: bool = True,
    max_text_chars: int | None = None,
    external_limits: bool = False,
) -> list[dict[str, Any]]:
    message = payload.message
    if include_project_instructions is None:
        include_project_instructions = include_project_context
    if include_project_instructions and payload.project_instructions.strip():
        message = (
            "请在整个回答中遵循以下项目说明：\n"
            f"{payload.project_instructions.strip()}\n\n"
            f"当前问题：\n{message}"
        )
    if include_history and not payload.thread_id and payload.history:
        context_limit = max_text_chars if max_text_chars is not None else MAX_CONTEXT_TEXT_CHARS
        current_ids = {item.id for item in payload.attachments}
        has_current_documents = any(
            Path(item.id).suffix.lower() not in IMAGE_EXTENSIONS
            for item in payload.attachments
        )
        has_project_documents = include_project_context and any(
            item.id not in current_ids
            and Path(item.id).suffix.lower() not in IMAGE_EXTENSIONS
            for item in payload.project_attachments
        )
        attachment_reserve = min(
            context_limit,
            (MAX_CURRENT_ATTACHMENT_CONTEXT_CHARS if has_current_documents else 0)
            + (MAX_PROJECT_ATTACHMENT_CONTEXT_CHARS if has_project_documents else 0),
        )
        history_budget = max(0, context_limit - len(message) - attachment_reserve - 96)
        recent_history, older_excerpt = _fit_context_window(payload.history, history_budget)
        transcript_parts = []
        if older_excerpt:
            transcript_parts.append(f"较早对话摘录：\n{older_excerpt}")
        transcript_parts.extend(f"{item.role}: {item.content}" for item in recent_history)
        transcript = "\n".join(transcript_parts)
        message = f"以下是网页中已有的最近对话，请在此基础上继续：\n{transcript}\n\n当前问题：\n{message}"
    if max_text_chars is not None:
        message = _clip_context_text(message, max_text_chars)
    inputs: list[dict[str, Any]] = [{"type": "text", "text": message}]
    used_text_chars = len(message)
    upload_root = (workspace / "uploads").resolve()
    current_ids = {item.id for item in payload.attachments}
    project_attachments = [
        item for item in payload.project_attachments
        if include_project_context and item.id not in current_ids
    ]
    all_attachments = [*payload.attachments, *project_attachments]
    prepared: list[tuple[AttachmentRef, Path, bool, str | None]] = []
    query_terms: list[str] | None = None
    selected_images = 0
    selected_image_bytes = 0
    selected_project_bytes = 0
    omitted_project_images = 0
    omitted_project_files = 0
    for attachment in all_attachments:
        target = (upload_root / attachment.id).resolve()
        if target.parent != upload_root or not target.is_file():
            raise HTTPException(400, f"附件 {attachment.name} 不存在")
        extension = target.suffix.lower()
        is_project = attachment.id not in current_ids
        if is_project and extension in IMAGE_EXTENSIONS and not include_project_images:
            continue
        size = target.stat().st_size
        if is_project and selected_project_bytes + size > MAX_PROJECT_CONTEXT_SOURCE_BYTES:
            omitted_project_files += 1
            continue
        if extension in IMAGE_EXTENSIONS:
            exceeds = (
                selected_images >= MAX_EXTERNAL_IMAGES
                or selected_image_bytes + size > MAX_EXTERNAL_IMAGE_BYTES
            )
            if exceeds and is_project:
                omitted_project_images += 1
                continue
            if exceeds:
                raise HTTPException(413, "当前消息中的图片超过单次 8 张或合计 30 MiB")
            if is_project:
                selected_project_bytes += size
            selected_images += 1
            selected_image_bytes += size
            prepared.append((attachment, target, is_project, None))
            continue
        if is_project:
            selected_project_bytes += size
        if query_terms is None:
            query_terms = _context_query_terms(payload.message)
        prepared.append((
            attachment,
            target,
            is_project,
            _extract_attachment_text(target, payload.message, query_terms=query_terms),
        ))

    current_documents = sum(1 for _attachment, _target, is_project, text in prepared if text is not None and not is_project)
    project_documents = sum(1 for _attachment, _target, is_project, text in prepared if text is not None and is_project)
    current_document_budget = MAX_CURRENT_ATTACHMENT_CONTEXT_CHARS
    project_document_budget = MAX_PROJECT_ATTACHMENT_CONTEXT_CHARS
    for attachment, target, is_project, extracted in prepared:
        if extracted is None:
            inputs.append({"type": "localImage", "path": str(target)})
            continue
        if is_project:
            per_file = min(
                MAX_PROJECT_FILE_EXCERPT_CHARS,
                max(1, MAX_PROJECT_ATTACHMENT_CONTEXT_CHARS // max(1, project_documents)),
                project_document_budget,
            )
            excerpt = _relevant_attachment_excerpt(
                extracted, payload.message, per_file, query_terms=query_terms
            )
            project_document_budget -= len(excerpt)
            attachment_label = "项目共享文件"
        else:
            per_file = max(1, MAX_CURRENT_ATTACHMENT_CONTEXT_CHARS // max(1, current_documents))
            per_file = min(per_file, current_document_budget)
            excerpt = _relevant_attachment_excerpt(
                extracted, payload.message, per_file, query_terms=query_terms
            )
            current_document_budget -= len(excerpt)
            attachment_label = "附件"
        block = f"\n【{attachment_label}：{attachment.name}】\n{excerpt}\n【文件结束】"
        if max_text_chars is not None:
            remaining = max_text_chars - used_text_chars
            if remaining <= 0:
                continue
            block = _clip_context_text(block, remaining)
        if block:
            inputs.append({"type": "text", "text": block})
            used_text_chars += len(block)
    if omitted_project_images:
        notice = f"\n【提示：另有 {omitted_project_images} 张项目共享图片因单次最多 8 张、原始文件合计 30 MiB 而未附加。】"
        if max_text_chars is None or used_text_chars + len(notice) <= max_text_chars:
            inputs[0]["text"] += notice
            used_text_chars += len(notice)
    if omitted_project_files:
        notice = f"\n【提示：另有 {omitted_project_files} 个项目共享文件因本轮项目文件原始大小最多 30 MiB 而未读取。】"
        if max_text_chars is None or used_text_chars + len(notice) <= max_text_chars:
            inputs[0]["text"] += notice
            used_text_chars += len(notice)
    return inputs


async def _attachment_inputs_async(
    payload: TurnRequest,
    workspace: Path,
    **options: Any,
) -> list[dict[str, Any]]:
    async with attachment_extract_semaphore:
        return await asyncio.to_thread(
            _attachment_inputs,
            payload,
            workspace,
            **options,
        )


def _payload_has_images(payload: TurnRequest, workspace: Path) -> bool:
    upload_root = (workspace / "uploads").resolve()
    found = False
    for attachment in [*payload.attachments, *payload.project_attachments]:
        target = (upload_root / attachment.id).resolve()
        if target.parent != upload_root or not target.is_file():
            raise HTTPException(400, f"附件 {attachment.name} 不存在")
        found = found or target.suffix.lower() in IMAGE_EXTENSIONS
    return found


def _developer_instructions(workspace: Path, project_instructions: str = "") -> str:
    output_dir = workspace / "outputs"
    instructions = (
        "你是这个私人网页中的通用中文 AI 助手。直接回答用户问题，默认使用用户的语言，保持自然、准确、实用。"
        "不要自称编程代理，不要为了普通问答读取服务器文件或执行命令。"
        "只有用户明确要求制作可下载文件时，才可以创建文件，并且只能写入 " + str(output_dir) + "。"
        "不得读取或修改该会话目录之外的内容，不得启动后台服务，不得请求扩大权限。"
        "用户上传的文档文本会直接出现在消息中，图片会作为图像输入提供。"
    )
    if project_instructions.strip():
        instructions += "该聊天属于一个项目，还必须遵循以下项目专属说明：" + project_instructions.strip()
    return instructions


async def _ensure_thread(payload: TurnRequest, workspace: Path, user_id: str) -> str:
    if payload.thread_id:
        _require_codex_thread_owner(user_id, payload.thread_id)
        if payload.thread_id not in codex.loaded_threads:
            await codex.request(
                "thread/resume",
                {
                    "threadId": payload.thread_id,
                    "cwd": str(workspace),
                    "approvalPolicy": "never",
                    "approvalsReviewer": "user",
                    "developerInstructions": _developer_instructions(workspace, payload.project_instructions),
                    "sandbox": "workspace-write",
                    "runtimeWorkspaceRoots": [str(workspace)],
                },
                timeout=30,
            )
            codex.loaded_threads.add(payload.thread_id)
        return payload.thread_id
    result = await codex.request(
        "thread/start",
        {
            "cwd": str(workspace),
            "model": payload.model,
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "developerInstructions": _developer_instructions(workspace, payload.project_instructions),
            "sandbox": "workspace-write",
            "runtimeWorkspaceRoots": [str(workspace)],
            "historyMode": "legacy",
        },
        timeout=45,
    )
    thread_id = result["thread"]["id"]
    _register_codex_thread(user_id, thread_id)
    codex.loaded_threads.add(thread_id)
    return thread_id


def _output_file_snapshot(workspace: Path) -> dict[str, tuple[int, int]]:
    output_root = (workspace / "outputs").resolve()
    if not output_root.is_dir():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    for path in output_root.rglob("*"):
        relative = path.relative_to(output_root)
        if IMAGE_CACHE_DIR in relative.parts or not path.is_file():
            continue
        stat = path.stat()
        snapshot[relative.as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def _output_files(
    workspace: Path,
    baseline: dict[str, tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    output_root = (workspace / "outputs").resolve()
    if not output_root.is_dir():
        return []
    found = []
    for path in sorted(output_root.rglob("*")):
        relative = path.relative_to(output_root)
        if IMAGE_CACHE_DIR in relative.parts:
            continue
        if not path.is_file():
            continue
        stat = path.stat()
        relative_path = relative.as_posix()
        if stat.st_size > MAX_OUTPUT_FILE_BYTES:
            continue
        if baseline is not None and baseline.get(relative_path) == (stat.st_size, stat.st_mtime_ns):
            continue
        found.append({"name": path.name, "size": stat.st_size, "path": relative_path})
    return found[-20:]


def _image_variant_paths(source: Path, output_root: Path) -> tuple[Path, Path]:
    stat = source.stat()
    relative = source.relative_to(output_root).as_posix()
    identity = f"{relative}:{stat.st_size}:{stat.st_mtime_ns}:{IMAGE_DERIVATIVE_VERSION}"
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    cache_root = (output_root / IMAGE_CACHE_DIR).resolve()
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache_root.chmod(0o700)
    return cache_root / f"{key}-preview.webp", cache_root / f"{key}-高清.webp"


def _webp_image(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        return image.convert("RGBA")
    return image.convert("RGB")


def _save_webp(image: Image.Image, target: Path, quality: int) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    try:
        _webp_image(image).save(temporary, format="WEBP", quality=quality, method=5)
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _prepare_image_variants(
    source: Path,
    output_root: Path,
    include_compressed: bool = True,
) -> dict[str, Any]:
    preview_path, compressed_path = _image_variant_paths(source, output_root)
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        image.load()
        width, height = image.size
        if max(width, height) > 16_384 or width * height > 64_000_000:
            raise ValueError("图片尺寸过大，无法创建预览")
        if not preview_path.is_file() or preview_path.stat().st_size == 0:
            preview = image.copy()
            preview.thumbnail(
                (IMAGE_PREVIEW_MAX_EDGE, IMAGE_PREVIEW_MAX_EDGE),
                Image.Resampling.LANCZOS,
            )
            _save_webp(preview, preview_path, IMAGE_PREVIEW_QUALITY)
        if include_compressed and (
            not compressed_path.is_file() or compressed_path.stat().st_size == 0
        ):
            compressed = image.copy()
            compressed.thumbnail(
                (IMAGE_COMPRESSED_MAX_EDGE, IMAGE_COMPRESSED_MAX_EDGE),
                Image.Resampling.LANCZOS,
            )
            _save_webp(compressed, compressed_path, IMAGE_COMPRESSED_QUALITY)
    result = {
        "width": width,
        "height": height,
        "previewSize": preview_path.stat().st_size,
        "previewPath": preview_path,
        "compressedPath": compressed_path,
    }
    if compressed_path.is_file() and compressed_path.stat().st_size > 0:
        result["compressedSize"] = compressed_path.stat().st_size
    return result


async def _prepare_image_variants_in_background(source: Path, output_root: Path) -> None:
    try:
        async with image_variant_lock:
            await asyncio.to_thread(_prepare_image_variants, source, output_root)
    except Exception:
        LOG.exception("Unable to prepare background image variants for %s", source.name)


def _schedule_image_variants(source: Path, output_root: Path) -> None:
    task = asyncio.create_task(_prepare_image_variants_in_background(source, output_root))
    image_variant_tasks.add(task)
    task.add_done_callback(image_variant_tasks.discard)


def _inspect_image_file(target: Path) -> tuple[str, int, int]:
    with Image.open(target) as opened:
        image_format = str(opened.format or "").upper()
        width, height = opened.size
        if (
            width <= 0
            or height <= 0
            or max(width, height) > 16_384
            or width * height > 64_000_000
        ):
            raise RuntimeError("生成图片尺寸无效")
        opened.verify()
    return image_format, width, height


def _is_image_generation_request(message: str) -> bool:
    text = message.strip()
    if not text or IMAGE_DISCUSSION_RE.search(text):
        return False
    return bool(IMAGE_REQUEST_RE.search(text))


async def _generate_image(prompt: str, workspace: Path) -> dict[str, Any]:
    if not IMAGE_BRIDGE_TOKEN:
        raise RuntimeError("图片生成服务尚未配置")
    request_id = uuid.uuid4().hex
    timeout = httpx.Timeout(IMAGE_BRIDGE_REQUEST_TIMEOUT_SECONDS, connect=5)
    output_root = (workspace / "outputs").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    target: Path | None = None
    temporary: Path | None = None
    media_type = ""
    size = 0
    width = 0
    height = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{IMAGE_BRIDGE_URL}/generate",
            headers={"Authorization": f"Bearer {IMAGE_BRIDGE_TOKEN}"},
            json={"requestId": request_id, "prompt": prompt},
        ) as response:
            if response.status_code != 200:
                detail = "图片生成失败，请稍后重试"
                try:
                    data = json.loads((await response.aread()).decode("utf-8"))
                    if isinstance(data, dict) and data.get("error"):
                        detail = str(data["error"])[:500]
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
                raise RuntimeError(detail)
            media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            extensions = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/webp": ".webp",
                "image/avif": ".avif",
            }
            extension = extensions.get(media_type)
            if not extension:
                raise RuntimeError("图片服务返回了不支持的格式")
            try:
                content_length = int(response.headers.get("content-length") or 0)
            except (TypeError, ValueError):
                content_length = 0
            if content_length > MAX_OUTPUT_FILE_BYTES:
                raise RuntimeError("生成图片超过 30 MB")
            filename = f"image-{int(time.time())}-{request_id[:8]}{extension}"
            target = (output_root / filename).resolve()
            if target.parent != output_root:
                raise RuntimeError("图片输出路径无效")
            temporary = target.with_suffix(f"{target.suffix}.part")
            try:
                with temporary.open("wb") as output:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > MAX_OUTPUT_FILE_BYTES:
                            raise RuntimeError("生成图片超过 30 MB")
                        output.write(chunk)
                if size == 0:
                    raise RuntimeError("生成图片为空")
                try:
                    image_format, width, height = await asyncio.to_thread(
                        _inspect_image_file, temporary
                    )
                except (OSError, SyntaxError) as exc:
                    raise RuntimeError("图片服务返回的图片文件无效") from exc
                actual_media_type = {
                    "PNG": "image/png",
                    "JPEG": "image/jpeg",
                    "WEBP": "image/webp",
                    "AVIF": "image/avif",
                }.get(image_format)
                if actual_media_type != media_type:
                    raise RuntimeError("图片服务返回的格式与文件内容不一致")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    if target is None:
        raise RuntimeError("图片服务没有返回文件")
    variants: dict[str, Any] = {}
    try:
        async with image_variant_lock:
            variants = await asyncio.to_thread(
                _prepare_image_variants, target, output_root, False
            )
    except Exception:
        LOG.exception("Unable to prepare generated image variants for %s", target.name)
    _schedule_image_variants(target, output_root)
    result = {
        "name": filename,
        "size": size,
        "path": filename,
        "mediaType": media_type,
        "inline": True,
        "width": width,
        "height": height,
    }
    for key in ("width", "height", "previewSize", "compressedSize"):
        if variants.get(key):
            result[key] = variants[key]
    return result


async def _image_stream(payload: TurnRequest, workspace: Path, user_id: str) -> AsyncIterator[bytes]:
    turn_id = uuid.uuid4().hex
    if payload.attachments or payload.project_attachments:
        yield _ndjson({"type": "started", "threadId": "image", "turnId": turn_id, "mode": "image"})
        yield _ndjson({"type": "error", "message": "当前仅支持文字描述生成图片，暂不支持参考图编辑"})
        return
    task = asyncio.create_task(_generate_image(payload.message, workspace))
    image_turns[turn_id] = (user_id, task)
    try:
        yield _ndjson({"type": "started", "threadId": "image", "turnId": turn_id, "mode": "image"})
        yield _ndjson({"type": "replace", "text": "正在生成图片，请稍候…"})
        async with asyncio.timeout(IMAGE_BRIDGE_REQUEST_TIMEOUT_SECONDS):
            while not task.done():
                done, _ = await asyncio.wait(
                    {task}, timeout=LONG_TASK_HEARTBEAT_SECONDS
                )
                if not done:
                    yield _ndjson({"type": "ping"})
            generated = await task
        yield _ndjson({"type": "replace", "text": "图片已生成。"})
        yield _ndjson({"type": "done", "files": [generated], "cost": {"kind": "unavailable"}})
    except TimeoutError:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        yield _ndjson({"type": "error", "message": "图片桥接超过 20 分钟，已自动停止"})
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        yield _ndjson({"type": "error", "message": "已停止生成图片"})
    except (httpx.TimeoutException, httpx.RequestError):
        yield _ndjson({"type": "error", "message": "图片生成服务暂时无法连接，请稍后重试"})
    except Exception as exc:
        LOG.warning("Image generation failed: %s", exc)
        yield _ndjson({"type": "error", "message": str(exc)[:500]})
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        image_turns.pop(turn_id, None)


def _ndjson(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


async def _iter_with_heartbeat(
    source: AsyncIterator[Any], interval: float | None = None
) -> AsyncIterator[Any | None]:
    """Yield ``None`` while an async stream is temporarily silent.

    The pending ``__anext__`` task stays alive across heartbeat intervals.  This
    is important for HTTP response streams: cancelling and recreating it every
    15 seconds can close the upstream response or lose the next chunk.
    """
    heartbeat_interval = interval or LONG_TASK_HEARTBEAT_SECONDS
    iterator = source.__aiter__()
    pending: asyncio.Task[Any] | None = asyncio.create_task(anext(iterator))
    try:
        while pending is not None:
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_interval)
            if not done:
                yield None
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                pending = None
                break
            pending = asyncio.create_task(anext(iterator))
            yield item
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


def _safe_token_count(value: Any) -> int:
    try:
        count = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(count, 1_000_000_000))


def _extract_token_usage(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    for key in ("usage", "tokenUsage", "token_usage"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.insert(0, nested)
    message = payload.get("message")
    if isinstance(message, dict):
        candidates.append(message)
        for key in ("usage", "tokenUsage", "token_usage"):
            nested = message.get(key)
            if isinstance(nested, dict):
                candidates.insert(0, nested)
    input_keys = ("input_tokens", "prompt_tokens", "inputTokens", "promptTokens")
    output_keys = ("output_tokens", "completion_tokens", "outputTokens", "completionTokens")
    total_keys = ("total_tokens", "totalTokens")
    for candidate in candidates:
        input_tokens = next((_safe_token_count(candidate.get(key)) for key in input_keys if candidate.get(key) is not None), 0)
        output_tokens = next((_safe_token_count(candidate.get(key)) for key in output_keys if candidate.get(key) is not None), 0)
        total_tokens = next((_safe_token_count(candidate.get(key)) for key in total_keys if candidate.get(key) is not None), 0)
        total_tokens = total_tokens or input_tokens + output_tokens
        if total_tokens:
            return {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": total_tokens,
                "estimated": False,
            }
    return None


def _merge_token_usage(current: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any] | None:
    if not incoming:
        return current
    if not current:
        return incoming
    input_tokens = max(_safe_token_count(current.get("inputTokens")), _safe_token_count(incoming.get("inputTokens")))
    output_tokens = max(_safe_token_count(current.get("outputTokens")), _safe_token_count(incoming.get("outputTokens")))
    total_tokens = max(
        _safe_token_count(current.get("totalTokens")),
        _safe_token_count(incoming.get("totalTokens")),
        input_tokens + output_tokens,
    )
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": total_tokens,
        "estimated": False,
    }


def _estimate_token_count(text: str) -> int:
    compact = str(text or "")
    if not compact:
        return 0
    ascii_count = sum(1 for character in compact if ord(character) < 128)
    non_ascii_count = len(compact) - ascii_count
    return max(1, (ascii_count + 3) // 4 + non_ascii_count)


def _turn_usage(
    payload: TurnRequest,
    response_text: str,
    exact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if exact:
        return exact
    input_text = "\n".join([
        *(item.content for item in payload.history if item.content),
        payload.message,
    ])
    input_tokens = _estimate_token_count(input_text)
    output_tokens = _estimate_token_count(response_text)
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": input_tokens + output_tokens,
        "estimated": True,
    }


def _turn_cost(
    usage: dict[str, Any] | None,
    provider: dict[str, Any] | None = None,
    *,
    subscription: bool = False,
    model_id: str = "",
) -> dict[str, Any]:
    if subscription:
        normalized_model = str(model_id or "").strip().lower()
        pricing = CODEX_API_EQUIVALENT_PRICING_USD.get(normalized_model)
        if not pricing or not usage:
            return {"kind": "subscription"}
        input_tokens = _safe_token_count(usage.get("inputTokens"))
        output_tokens = _safe_token_count(usage.get("outputTokens"))
        if not input_tokens and not output_tokens:
            return {"kind": "subscription"}
        input_price, output_price = pricing
        if normalized_model in CODEX_LONG_CONTEXT_PRICING_MODELS and input_tokens > 272_000:
            input_price *= 2
            output_price *= 1.5
        amount = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
        return {
            "kind": "subscription",
            "amount": round(max(0.0, amount), 8),
            "currency": "USD",
            "model": normalized_model,
            "usageEstimated": bool(usage.get("estimated")),
        }
    pricing = provider.get("pricing") if provider and isinstance(provider.get("pricing"), dict) else None
    if not pricing or not all(key in pricing for key in ("inputPerMillion", "outputPerMillion")):
        return {"kind": "unconfigured"}
    if not usage:
        return {"kind": "unavailable"}
    input_tokens = _safe_token_count(usage.get("inputTokens"))
    output_tokens = _safe_token_count(usage.get("outputTokens"))
    if not input_tokens and not output_tokens and _safe_token_count(usage.get("totalTokens")):
        return {"kind": "unavailable"}
    try:
        input_price = float(pricing.get("inputPerMillion"))
        output_price = float(pricing.get("outputPerMillion"))
    except (TypeError, ValueError, OverflowError):
        return {"kind": "unconfigured"}
    amount = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
    return {
        "kind": "estimated",
        "amount": round(max(0.0, amount), 8),
        "currency": pricing.get("currency") if pricing.get("currency") in {"CNY", "USD"} else "CNY",
        "usageEstimated": bool(usage.get("estimated")),
    }


async def _external_current_content(
    payload: TurnRequest,
    workspace: Path,
    protocol: str,
    user_id: str,
    *,
    include_project_context: bool = True,
    include_project_instructions: bool | None = None,
    include_project_images: bool = True,
    max_text_chars: int = MAX_CONTEXT_TEXT_CHARS,
) -> str | list[dict[str, Any]]:
    inputs = await _attachment_inputs_async(
        payload,
        workspace,
        include_history=False,
        include_project_context=include_project_context,
        include_project_instructions=include_project_instructions,
        include_project_images=include_project_images,
        max_text_chars=max_text_chars,
        external_limits=True,
    )
    text = "".join(str(item.get("text", "")) for item in inputs if item.get("type") == "text")
    image_paths = [Path(str(item["path"])) for item in inputs if item.get("type") == "localImage"]
    if not image_paths:
        return text

    if protocol == "anthropic":
        upload_root = (workspace / "uploads").resolve()
        optimized_paths = await _prepare_transfer_images(image_paths, upload_root)
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for path in optimized_paths:
            media_type = _file_media_type(path)
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(path.read_bytes()).decode(),
                },
            })
        return content

    transfer_base_url = _require_external_transfer_base_url()
    if transfer_base_url:
        upload_root = (workspace / "uploads").resolve()
        optimized_paths = await _prepare_transfer_images(image_paths, upload_root)
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for target in optimized_paths:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": _transfer_image_url(
                        user_id,
                        payload.session_id,
                        target,
                        upload_root,
                    )
                },
            })
        return content

def _external_messages(
    history: list[HistoryMessage],
    current_content: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    messages = [
        {"role": item.role, "content": item.content}
        for item in history
        if item.content.strip()
    ]
    messages.append({"role": "user", "content": current_content})
    return messages


def _external_content_text_length(content: str | list[dict[str, Any]]) -> int:
    if isinstance(content, str):
        return len(content)
    return sum(
        len(str(item.get("text") or ""))
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def _prepend_older_context(
    content: str | list[dict[str, Any]],
    excerpt: str,
) -> str | list[dict[str, Any]]:
    if not excerpt:
        return content
    prefix = (
        "【较早对话摘录】\n"
        "以下内容只用于恢复事实与偏好；若与当前问题冲突，以当前问题为准。\n"
        f"{excerpt}\n"
        "【较早对话摘录结束】\n\n"
        "【当前问题】\n"
    )
    if isinstance(content, str):
        return prefix + content
    copied = [dict(item) for item in content]
    for item in copied:
        if item.get("type") == "text":
            item["text"] = prefix + str(item.get("text") or "")
            return copied
    copied.insert(0, {"type": "text", "text": prefix})
    return copied


def _valid_external_conversation_id(value: Any) -> str:
    candidate = str(value or "")
    return candidate if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", candidate) else ""


def _openai_finish_reason(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    return str(choices[0].get("finish_reason") or "")


def _openai_stream_error(data: dict[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "API 回答失败")[:500]
    if error:
        return str(error)[:500]
    if data.get("type") == "error" or _openai_finish_reason(data).lower() == "error":
        return "API 回答失败"
    return ""


def _openai_message_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
    content = delta.get("content") if delta else message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
        )
    return ""

def _external_asset_url_matches_provider(url: str, provider_base: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
        provider = urlparse(str(provider_base or "").strip())
        parsed_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        provider_port = provider.port or (443 if provider.scheme == "https" else 80)
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.hostname
        and provider.hostname
        and parsed.scheme == provider.scheme
        and parsed.hostname.rstrip(".").lower()
        == provider.hostname.rstrip(".").lower()
        and parsed_port == provider_port
        and EXTERNAL_ASSET_PATH_RE.fullmatch(parsed.path)
    )


def _openai_image_candidates(
    data: dict[str, Any], provider_base: str
) -> list[dict[str, str]]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    choice = choices[0] if isinstance(choices[0], dict) else {}
    containers = [
        value for value in (choice.get("delta"), choice.get("message"))
        if isinstance(value, dict)
    ]
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_candidate(url_value: Any, name_value: Any = "image") -> None:
        url = str(url_value or "").strip()
        if (
            not url
            or url in seen
            or not _external_asset_url_matches_provider(url, provider_base)
        ):
            return
        seen.add(url)
        candidates.append({
            "url": url,
            "name": _safe_filename(str(name_value or "image")),
        })

    for container in containers:
        attachments = container.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict) or attachment.get("type") != "image":
                    continue
                add_candidate(attachment.get("url"), attachment.get("name"))
        content = container.get("content")
        if isinstance(content, str):
            for match in EXTERNAL_ASSET_MARKDOWN_RE.finditer(content):
                add_candidate(match.group(2), match.group(1))
    return candidates


def _strip_external_asset_markdown(
    text: str, candidates: list[dict[str, str]]
) -> str:
    cleaned = str(text or "")
    for candidate in candidates:
        url = str(candidate.get("url") or "")
        if not url:
            continue
        pattern = re.compile(
            r"!?\[[^\]\r\n]{0,120}\]\(\s*" + re.escape(url) + r"\s*\)"
        )
        cleaned = pattern.sub("", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


async def _validated_external_asset_url(url: str, provider_base: str) -> str:
    candidate = str(url or "").strip()
    try:
        parsed = urlparse(candidate)
        provider = urlparse(provider_base)
        parsed_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        provider_port = provider.port or (443 if provider.scheme == "https" else 80)
    except ValueError as exc:
        raise RuntimeError("图片下载地址无效") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
        or not provider.hostname
        or parsed.scheme != provider.scheme
        or parsed.hostname.rstrip(".").lower()
        != provider.hostname.rstrip(".").lower()
        or parsed_port != provider_port
        or not EXTERNAL_ASSET_PATH_RE.fullmatch(parsed.path)
    ):
        raise RuntimeError("图片下载地址不属于当前 API 通道")
    try:
        return await _validate_remote_base(candidate)
    except HTTPException as exc:
        raise RuntimeError(str(exc.detail)) from exc


async def _persist_external_image(
    client: httpx.AsyncClient,
    candidate: dict[str, str],
    provider_base: str,
    workspace: Path,
) -> dict[str, Any]:
    url = await _validated_external_asset_url(candidate["url"], provider_base)
    output_root = (workspace / "outputs").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = output_root / f".external-{uuid.uuid4().hex}.part"
    last_error: Exception | None = None
    try:
        for attempt in range(EXTERNAL_ASSET_DOWNLOAD_ATTEMPTS):
            temporary.unlink(missing_ok=True)
            try:
                async with client.stream(
                    "GET",
                    url,
                    headers={"accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.1"},
                ) as response:
                    if response.status_code != 200:
                        if response.status_code in EXTERNAL_ASSET_RETRYABLE_STATUS_CODES:
                            last_error = RuntimeError(
                                f"图片下载服务暂时不可用（{response.status_code}）"
                            )
                            if attempt + 1 < EXTERNAL_ASSET_DOWNLOAD_ATTEMPTS:
                                await asyncio.sleep(0.5 * (2 ** attempt))
                                continue
                        raise RuntimeError(f"图片下载失败（{response.status_code}）")
                    try:
                        content_length = int(response.headers.get("content-length") or 0)
                    except (TypeError, ValueError):
                        content_length = 0
                    if content_length > MAX_OUTPUT_FILE_BYTES:
                        raise RuntimeError("生成图片超过 30 MB")
                    size = 0
                    with temporary.open("wb") as output:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            size += len(chunk)
                            if size > MAX_OUTPUT_FILE_BYTES:
                                raise RuntimeError("生成图片超过 30 MB")
                            output.write(chunk)
                if size == 0:
                    last_error = RuntimeError("生成图片为空")
                    if attempt + 1 < EXTERNAL_ASSET_DOWNLOAD_ATTEMPTS:
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    break
                try:
                    image_format, width, height = await asyncio.to_thread(
                        _inspect_image_file, temporary
                    )
                except (OSError, SyntaxError) as exc:
                    last_error = exc
                    if attempt + 1 < EXTERNAL_ASSET_DOWNLOAD_ATTEMPTS:
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    break
                formats = {
                    "PNG": (".png", "image/png"),
                    "JPEG": (".jpg", "image/jpeg"),
                    "WEBP": (".webp", "image/webp"),
                    "GIF": (".gif", "image/gif"),
                    "AVIF": (".avif", "image/avif"),
                }
                if image_format not in formats:
                    raise RuntimeError("图片服务返回了不支持的格式")
                extension, media_type = formats[image_format]
                stem = Path(_safe_filename(candidate.get("name") or "image")).stem[:80]
                filename = f"{stem or 'image'}-{uuid.uuid4().hex[:10]}{extension}"
                target = (output_root / filename).resolve()
                if target.parent != output_root:
                    raise RuntimeError("图片输出路径无效")
                os.replace(temporary, target)
                target.chmod(0o600)
                _schedule_image_variants(target, output_root)
                return {
                    "name": filename,
                    "size": size,
                    "path": filename,
                    "mediaType": media_type,
                    "inline": True,
                    "width": width,
                    "height": height,
                }
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                if attempt + 1 < EXTERNAL_ASSET_DOWNLOAD_ATTEMPTS:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                break
        raise RuntimeError("生成图片下载失败，请稍后重试") from last_error
    finally:
        temporary.unlink(missing_ok=True)


async def _persist_external_images(
    candidates: list[dict[str, str]], provider_base: str, workspace: Path
) -> tuple[list[dict[str, Any]], int]:
    selected = candidates[:MAX_FILES_PER_UPLOAD]
    results: list[dict[str, Any] | None] = [None] * len(selected)
    semaphore = asyncio.Semaphore(2)
    timeout = httpx.Timeout(
        EXTERNAL_ASSET_ATTEMPT_TIMEOUT_SECONDS, connect=15.0
    )

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        async def save_one(index: int, candidate: dict[str, str]) -> None:
            try:
                async with semaphore:
                    results[index] = await _persist_external_image(
                        client, candidate, provider_base, workspace
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Unable to persist external image: %s", exc)

        tasks = [
            asyncio.create_task(save_one(index, candidate))
            for index, candidate in enumerate(selected)
        ]
        try:
            async with asyncio.timeout(EXTERNAL_ASSET_DOWNLOAD_TIMEOUT_SECONDS):
                await asyncio.gather(*tasks)
        except TimeoutError:
            LOG.warning(
                "External image batch exceeded %s seconds",
                EXTERNAL_ASSET_DOWNLOAD_TIMEOUT_SECONDS,
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    saved = [result for result in results if result is not None]
    failed = len(candidates) - len(saved)
    return saved, failed


def _anthropic_message_text(data: dict[str, Any]) -> str:
    delta = data.get("delta")
    if isinstance(delta, dict) and delta.get("type") == "text_delta":
        return str(delta.get("text", ""))
    content = data.get("content")
    if isinstance(content, list):
        return "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


async def _external_response_stream(
    payload: TurnRequest,
    workspace: Path,
    provider: dict[str, Any],
    model_id: str,
    user_id: str,
    *,
    context_history: list[HistoryMessage] | None = None,
    reuse_conversation_id: str = "",
    external_context_key: str = "",
    completion_state: dict[str, str] | None = None,
) -> AsyncIterator[bytes]:
    try:
        provider["baseUrl"] = await _validate_remote_base(str(provider.get("baseUrl", "")))
        protocol = str(provider.get("protocol", "openai"))
        is_continuation = bool(reuse_conversation_id)
        current_limit = MAX_CONTEXT_TEXT_CHARS - (0 if is_continuation else len(EXTERNAL_SYSTEM_PROMPT))
        # The upstream conversation already retains stable instructions and images.
        # Re-excerpt project documents so a follow-up can ask about a different section.
        current_content = await _external_current_content(
            payload,
            workspace,
            protocol,
            user_id,
            include_project_context=True,
            include_project_instructions=not is_continuation,
            include_project_images=not is_continuation,
            max_text_chars=max(1, current_limit),
        )
        if is_continuation:
            history: list[HistoryMessage] = []
            messages = _external_messages(history, current_content)
        else:
            available = max(
                0,
                MAX_CONTEXT_TEXT_CHARS
                - len(EXTERNAL_SYSTEM_PROMPT)
                - _external_content_text_length(current_content),
            )
            history, older_excerpt = _fit_context_window(
                context_history if context_history is not None else payload.history,
                available,
            )
            current_content = _prepend_older_context(current_content, older_excerpt)
            messages = _external_messages(history, current_content)
        if protocol == "anthropic":
            url = _provider_endpoint(provider, "messages")
            body = {
                "model": model_id,
                "max_tokens": 8192,
                "stream": True,
                "messages": messages,
            }
            if not is_continuation:
                body["system"] = EXTERNAL_SYSTEM_PROMPT
            text_reader = _anthropic_message_text
        else:
            url = _provider_endpoint(provider, "chat/completions")
            body = {
                "model": model_id,
                "stream": True,
                "messages": messages,
            }
            if not is_continuation:
                body["messages"] = [{"role": "system", "content": EXTERNAL_SYSTEM_PROMPT}, *messages]
            if is_continuation:
                body["conversation_id"] = reuse_conversation_id
            if provider.get("preset") == "openai":
                body["stream_options"] = {"include_usage": True}
            text_reader = _openai_message_text

        delivered = False
        response_parts: list[str] = []
        asset_candidates: dict[str, dict[str, str]] = {}
        token_usage: dict[str, Any] | None = None
        captured_conversation_id = ""
        terminal_success = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(920.0, connect=15.0), follow_redirects=False) as client:
            async with client.stream("POST", url, headers=_provider_headers(provider), json=body) as response:
                if response.status_code >= 400:
                    raw = await response.aread()
                    yield _ndjson({"type": "error", "message": _upstream_error(raw, f"API 请求失败（{response.status_code}）")})
                    return
                content_type = response.headers.get("content-type", "").lower()
                if "application/json" in content_type:
                    raw = await response.aread()
                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        yield _ndjson({"type": "error", "message": "API 返回的回答格式无效"})
                        return
                    if isinstance(data, dict):
                        stream_error = _openai_stream_error(data) if protocol != "anthropic" else ""
                        if stream_error:
                            yield _ndjson({"type": "error", "message": stream_error})
                            return
                        if protocol != "anthropic":
                            finish_reason = _openai_finish_reason(data).lower()
                            if finish_reason not in {"", "error"}:
                                terminal_success = True
                                captured_conversation_id = _valid_external_conversation_id(data.get("conversation_id"))
                        else:
                            delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
                            terminal_success = bool(
                                data.get("type") in {"message", "message_stop"}
                                or data.get("stop_reason")
                                or delta.get("stop_reason")
                            )
                    token_usage = _merge_token_usage(token_usage, _extract_token_usage(data))
                    incoming = (
                        _openai_image_candidates(data, str(provider["baseUrl"])) if isinstance(data, dict) else []
                    )
                    for candidate in incoming:
                        asset_candidates.setdefault(candidate["url"], candidate)
                    text = text_reader(data) if isinstance(data, dict) else ""
                    if text:
                        response_parts.append(text)
                        visible_text = _strip_external_asset_markdown(text, incoming) if incoming else text
                        if visible_text:
                            delivered = True
                            yield _ndjson({"type": "delta", "text": visible_text})
                else:
                    async for line in response.aiter_lines():
                        if line.startswith(":"):
                            yield _ndjson({"type": "ping"})
                            continue
                        if not line.startswith("data:"):
                            continue
                        raw_data = line[5:].strip()
                        if not raw_data:
                            continue
                        if raw_data == "[DONE]":
                            if protocol != "anthropic":
                                terminal_success = True
                            break
                        try:
                            data = json.loads(raw_data)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(data, dict):
                            stream_error = _openai_stream_error(data) if protocol != "anthropic" else ""
                            if data.get("type") == "error" and protocol == "anthropic":
                                error = data.get("error") if isinstance(data.get("error"), dict) else {}
                                stream_error = str(error.get("message") or "API 回答失败")[:500]
                            if stream_error:
                                yield _ndjson({"type": "error", "message": stream_error})
                                return
                            if protocol != "anthropic":
                                incoming_conversation_id = _valid_external_conversation_id(
                                    data.get("conversation_id")
                                )
                                if incoming_conversation_id:
                                    captured_conversation_id = incoming_conversation_id
                                finish_reason = _openai_finish_reason(data).lower()
                                if finish_reason not in {"", "error"}:
                                    terminal_success = True
                            else:
                                delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
                                if (
                                    data.get("type") == "message_stop"
                                    or data.get("stop_reason")
                                    or delta.get("stop_reason")
                                ):
                                    terminal_success = True
                        token_usage = _merge_token_usage(token_usage, _extract_token_usage(data))
                        incoming = (
                            _openai_image_candidates(data, str(provider["baseUrl"])) if isinstance(data, dict) else []
                        )
                        for candidate in incoming:
                            asset_candidates.setdefault(candidate["url"], candidate)
                        text = text_reader(data) if isinstance(data, dict) else ""
                        if text:
                            response_parts.append(text)
                            visible_text = _strip_external_asset_markdown(text, incoming) if incoming else text
                            if visible_text:
                                delivered = True
                                yield _ndjson({"type": "delta", "text": visible_text})
                if not terminal_success:
                    yield _ndjson({"type": "error", "message": "API 回答连接意外中断，请重试"})
                    return
                if protocol != "anthropic" and response_parts:
                    complete_response = {
                        "choices": [{"message": {"content": "".join(response_parts)}}]
                    }
                    for candidate in _openai_image_candidates(
                        complete_response, str(provider["baseUrl"])
                    ):
                        asset_candidates.setdefault(candidate["url"], candidate)
                candidates = list(asset_candidates.values())
                files: list[dict[str, Any]] = []
                failed_assets = 0
                final_text = "".join(response_parts)
                if candidates:
                    final_text = _strip_external_asset_markdown(
                        final_text, candidates
                    )
                    files, failed_assets = await _persist_external_images(
                        candidates, str(provider["baseUrl"]), workspace
                    )
                    if files and not final_text:
                        final_text = "图片已生成。"
                    if failed_assets:
                        notice = (
                            "部分图片保存失败，请稍后重试。"
                            if files
                            else "图片已生成，但下载到网站失败，请稍后重试。"
                        )
                        final_text = "\n\n".join(
                            part for part in (final_text, notice) if part
                        )
                    yield _ndjson({"type": "replace", "text": final_text})
                    delivered = bool(final_text)
                if delivered or files:
                    usage = _turn_usage(payload, final_text, token_usage)
                    event = {
                        "type": "done",
                        "files": files,
                        "usage": usage,
                        "cost": _turn_cost(usage, provider),
                    }
                    if files:
                        event["mode"] = "image"
                    committed_conversation_id = (
                        captured_conversation_id or _valid_external_conversation_id(reuse_conversation_id)
                    )
                    if committed_conversation_id and external_context_key:
                        event["externalConversationId"] = committed_conversation_id
                        event["externalContextKey"] = external_context_key
                        if completion_state is not None:
                            completion_state["external_conversation_id"] = committed_conversation_id
                            completion_state["external_context_key"] = external_context_key
                    yield _ndjson(event)
                else:
                    yield _ndjson({"type": "error", "message": "API 没有返回可显示的文字"})
    except httpx.TimeoutException:
        yield _ndjson({"type": "error", "message": "API 回答超时，请稍后重试"})
    except httpx.RequestError:
        yield _ndjson({"type": "error", "message": "无法连接该 API，请检查地址和网络"})
    except HTTPException as exc:
        yield _ndjson({"type": "error", "message": str(exc.detail)})
    except Exception:
        LOG.exception("External provider turn failed")
        yield _ndjson({"type": "error", "message": "外部 API 暂时无法回答"})


async def _external_stream(
    payload: TurnRequest,
    workspace: Path,
    provider: dict[str, Any],
    model_id: str,
    user_id: str,
    provider_id: str = "",
    context_history: list[HistoryMessage] | None = None,
) -> AsyncIterator[bytes]:
    turn_id = uuid.uuid4().hex
    task = asyncio.current_task()
    if task:
        external_turns[turn_id] = (user_id, task)
    yield _ndjson({"type": "started", "threadId": "external", "turnId": turn_id, "mode": "external"})
    site_key = (
        (user_id, payload.client_conversation_id)
        if payload.client_conversation_id
        else None
    )
    lock = external_conversation_locks.setdefault(site_key, asyncio.Lock()) if site_key else None
    completion_state: dict[str, str] = {}

    async def run_locked() -> AsyncIterator[bytes]:
        context_key = _external_context_key(payload, provider_id, provider, model_id)
        reuse_id = ""
        if site_key:
            state = external_conversation_states.get(site_key)
            if state and hmac.compare_digest(state.get("external_context_key", ""), context_key):
                reuse_id = _valid_external_conversation_id(state.get("external_conversation_id"))
            elif (
                payload.external_conversation_id
                and payload.external_context_key
                and hmac.compare_digest(payload.external_context_key, context_key)
            ):
                reuse_id = _valid_external_conversation_id(payload.external_conversation_id)
            external_conversation_states[site_key] = {
                "external_conversation_id": "",
                "external_context_key": context_key,
            }
        source = _external_response_stream(
            payload,
            workspace,
            provider,
            model_id,
            user_id,
            context_history=context_history,
            reuse_conversation_id=reuse_id,
            external_context_key=context_key,
            completion_state=completion_state,
        )
        try:
            async with asyncio.timeout(EXTERNAL_RESPONSE_TIMEOUT_SECONDS):
                async for event in _iter_with_heartbeat(source):
                    if event is None:
                        yield _ndjson({"type": "ping"})
                    else:
                        if site_key and completion_state.get("external_conversation_id"):
                            external_conversation_states[site_key] = {
                                "external_conversation_id": completion_state["external_conversation_id"],
                                "external_context_key": completion_state["external_context_key"],
                            }
                        yield event
        except TimeoutError:
            yield _ndjson({"type": "error", "message": "API 回答超过 20 分钟，已自动停止"})

    try:
        if lock:
            async with lock:
                async for event in run_locked():
                    yield event
        else:
            async for event in run_locked():
                yield event
    except asyncio.CancelledError:
        yield _ndjson({"type": "done", "files": []})
    finally:
        external_turns.pop(turn_id, None)


@app.post("/api/turn")
async def turn(request: Request, payload: TurnRequest):
    user = _require_auth(request)
    workspace = _session_path(user["id"], payload.session_id)
    _validate_user_content_size(payload, workspace)
    external = _decode_external_model(payload.model)
    if external is None and _is_image_generation_request(payload.message):
        await _clear_external_conversation_state(user["id"], payload.client_conversation_id)
        if not payload.attachments and not payload.project_attachments:
            _reserve_message_slot(user["id"])
        return StreamingResponse(
            _image_stream(payload, workspace, user["id"]),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    if external:
        provider_id, model_id = external
        provider = _provider_by_id(provider_id)
        if not provider.get("enabled", True):
            raise HTTPException(400, "该 API 连接已停用")
        if model_id not in _clean_model_ids(provider.get("models", [])):
            raise HTTPException(400, "该模型尚未导入或已被移除")
        if (
            str(provider.get("protocol", "openai")) != "anthropic"
            and _payload_has_images(payload, workspace)
        ):
            _require_external_transfer_base_url()

        cloud_history = await asyncio.to_thread(
            _cloud_context_history,
            user["id"],
            payload.client_conversation_id,
        )
        context_history = _merged_context_history(cloud_history, payload.history)
        _reserve_message_slot(user["id"])
        return StreamingResponse(
            _external_stream(
                payload,
                workspace,
                provider,
                model_id,
                user["id"],
                provider_id,
                context_history,
            ),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    if payload.model in _read_disabled_codex_models():
        raise HTTPException(400, "该 Codex 模型已被管理员关闭，请选择其他模型")
    await _clear_external_conversation_state(user["id"], payload.client_conversation_id)
    inputs = await _attachment_inputs_async(
        payload,
        workspace,
        max_text_chars=MAX_CONTEXT_TEXT_CHARS,
    )
    _reserve_message_slot(user["id"])
    async def stream() -> AsyncIterator[bytes]:
        turn_id = ""
        output_baseline = _output_file_snapshot(workspace)
        response_parts: list[str] = []
        try:
            thread_id = await _ensure_thread(payload, workspace, user["id"])
            result = await codex.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": inputs,
                    "model": payload.model,
                    "effort": payload.effort,
                    "approvalPolicy": "never",
                    "sandboxPolicy": {
                        "type": "workspaceWrite",
                        "writableRoots": [str(workspace)],
                        "networkAccess": False,
                        "excludeSlashTmp": True,
                        "excludeTmpdirEnvVar": True,
                    },
                    "runtimeWorkspaceRoots": [str(workspace)],
                },
                timeout=45,
            )
            turn_id = result["turn"]["id"]
            queue = codex.register_stream(turn_id)
            yield _ndjson({"type": "started", "threadId": thread_id, "turnId": turn_id})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield _ndjson({"type": "ping"})
                    continue
                method = event["method"]
                params = event["params"]
                if method == "item/agentMessage/delta":
                    delta = str(params.get("delta", ""))
                    response_parts.append(delta)
                    yield _ndjson({"type": "delta", "text": delta})
                elif method == "turn/completed":
                    turn_data = params.get("turn") or {}
                    if turn_data.get("status") == "failed":
                        error = turn_data.get("error") or {}
                        yield _ndjson({"type": "error", "message": error.get("message", "回答失败")})
                    else:
                        usage = _turn_usage(
                            payload,
                            "".join(response_parts),
                            _extract_token_usage(turn_data),
                        )
                        yield _ndjson({
                            "type": "done",
                            "files": _output_files(workspace, output_baseline),
                            "usage": usage,
                            "cost": _turn_cost(usage, subscription=True, model_id=payload.model),
                        })
                    break
        except (CodexProtocolError, HTTPException) as exc:
            yield _ndjson({"type": "error", "message": getattr(exc, "detail", str(exc))})
        except Exception:
            LOG.exception("Turn failed")
            yield _ndjson({"type": "error", "message": "服务暂时无法回答，请稍后重试"})
        finally:
            if turn_id:
                codex.unregister_stream(turn_id)

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/api/turn/{thread_id}/{turn_id}/interrupt")
async def interrupt(request: Request, thread_id: str, turn_id: str):
    user = _require_auth(request)
    if thread_id == "image":
        owned_task = image_turns.get(turn_id)
        if owned_task and owned_task[0] != user["id"]:
            raise HTTPException(403, "无权停止此生成任务")
        if owned_task and not owned_task[1].done():
            owned_task[1].cancel()
        return {"ok": True}
    if thread_id == "external":
        owned_task = external_turns.get(turn_id)
        if owned_task and owned_task[0] != user["id"]:
            raise HTTPException(403, "无权停止此回答任务")
        if owned_task and not owned_task[1].done():
            owned_task[1].cancel()
        return {"ok": True}
    _require_codex_thread_owner(user["id"], thread_id)
    await codex.request(
        "turn/interrupt",
        {"threadId": thread_id, "turnId": turn_id},
        timeout=15,
    )
    return {"ok": True}


@app.get("/api/files/{session_id}/{file_path:path}")
async def download_file(
    request: Request,
    session_id: str,
    file_path: str,
    inline: bool = False,
    variant: Literal["original", "preview", "compressed"] = "original",
):
    user = _require_auth(request)
    workspace = _session_path(user["id"], session_id)
    output_root = (workspace / "outputs").resolve()
    target = (output_root / file_path).resolve()
    if output_root not in target.parents or not target.is_file() or target.stat().st_size > MAX_OUTPUT_FILE_BYTES:
        raise HTTPException(404, "文件不存在")
    is_image = target.suffix.lower() in IMAGE_EXTENSIONS
    selected = target
    download_name = target.name
    media_type = _file_media_type(target)
    if variant != "original":
        if not is_image:
            raise HTTPException(415, "该文件没有图片预览版本")
        try:
            async with image_variant_lock:
                variants = await asyncio.to_thread(_prepare_image_variants, target, output_root)
            selected = variants["previewPath" if variant == "preview" else "compressedPath"]
            media_type = "image/webp"
            if variant == "compressed":
                download_name = f"{target.stem}-高清.webp"
        except Exception as exc:
            LOG.warning("Image variant failed for %s: %s", target.name, exc)
            if variant == "compressed":
                raise HTTPException(500, "高清压缩版生成失败，请下载原图") from exc
    response_inline = variant == "preview" or (inline and is_image)
    headers = {
        "Cache-Control": "private, max-age=31536000, immutable",
        "Vary": "Cookie",
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(
        selected,
        filename=download_name,
        media_type=media_type,
        headers=headers,
        content_disposition_type="inline" if response_inline else "attachment",
    )
