"""OpenAI-compatible image input and ChatGPT asset extraction helpers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import logging
import mimetypes
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_to_bytes, urljoin, urlparse

import aiohttp

logger = logging.getLogger(__name__)


MAX_IMAGE_COUNT = 8
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 30 * 1024 * 1024
MAX_REDIRECTS = 3
IMAGE_DOWNLOAD_ATTEMPTS = 3
IMAGE_DOWNLOAD_RETRY_DELAYS = (0.5, 1.5)
IMAGE_DOWNLOAD_CONCURRENCY = 2
ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_FILE_ID_RE = re.compile(r"(file[_-][A-Za-z0-9_-]+)")
_IMAGE_OUTPUT_PATTERNS = (
    re.compile(
        r"(?:生成|画|绘制|创作|制作|做)(?:一|1)?(?:张|幅|个)?"
        r"[^。！？\n]{0,40}(?:图片|图像|照片|海报|插画|头像|壁纸)"
    ),
    re.compile(
        r"(?:图片|图像|照片|海报|插画|头像|壁纸)"
        r"[^。！？\n]{0,20}(?:生成|画|绘制|创作|制作)"
    ),
    re.compile(
        r"\b(?:generate|create|draw|make|render)\b.{0,60}"
        r"\b(?:image|picture|photo|illustration|poster|wallpaper)\b",
        re.IGNORECASE,
    ),
)


class ImageInputError(ValueError):
    """Raised when an OpenAI image input is invalid or unsafe."""


@dataclass(frozen=True)
class ImageReference:
    """One image URL extracted from an OpenAI content part."""

    url: str
    detail: str | None = None


def parse_content_parts(content) -> tuple[str, list[ImageReference]]:
    """Return text plus image references from OpenAI Chat/Responses content."""
    if not isinstance(content, list):
        return str(content or ""), []

    text_parts: list[str] = []
    images: list[ImageReference] = []
    for part in content:
        if not isinstance(part, dict):
            text_parts.append(str(part))
            continue
        part_type = str(part.get("type") or "")
        if part_type in {"text", "input_text", "output_text"}:
            text_parts.append(str(part.get("text") or ""))
            continue
        if part_type not in {"image_url", "input_image"}:
            continue

        image_value = part.get("image_url")
        detail = part.get("detail")
        if isinstance(image_value, dict):
            detail = image_value.get("detail", detail)
            image_value = image_value.get("url")
        if not image_value and part_type == "input_image":
            image_value = part.get("url")
        if isinstance(image_value, str) and image_value.strip():
            images.append(ImageReference(image_value.strip(), str(detail) if detail else None))
    return "\n".join(p for p in text_parts if p), images


def wants_image_output(payload: dict, latest_user_text: str) -> bool:
    """Detect an image-output request from explicit OpenAI fields or prompt text.

    Chat Completions clients do not all expose the same image-output switch, so
    explicit ``modalities``/``response_format`` signals take precedence and a
    conservative multilingual prompt fallback covers ordinary chat clients.
    The result only changes completion observation and timeout behavior; it
    does not rewrite the user's prompt.
    """
    modalities = payload.get("modalities") or []
    if isinstance(modalities, str):
        modalities = [modalities]
    if any(str(item).lower() in {"image", "images"} for item in modalities):
        return True

    response_format = payload.get("response_format") or {}
    if isinstance(response_format, str):
        response_type = response_format
    elif isinstance(response_format, dict):
        response_type = response_format.get("type", "")
    else:
        response_type = ""
    if str(response_type).lower() in {"image", "image_url", "b64_json"}:
        return True

    model = str(payload.get("model", "")).lower()
    if model.startswith(("gpt-image", "dall-e")):
        return True

    text = latest_user_text.strip()
    return bool(text and any(pattern.search(text) for pattern in _IMAGE_OUTPUT_PATTERNS))


def _sniff_image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _safe_filename(index: int, mime_type: str, source_url: str = "") -> str:
    parsed_name = Path(urlparse(source_url).path).name if source_url else ""
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", parsed_name)[:80]
    if stem and Path(stem).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return f"{index:02d}_{stem}"
    return f"{index:02d}_image{ALLOWED_IMAGE_TYPES[mime_type]}"


def _decode_data_url(url: str) -> tuple[bytes, str]:
    try:
        header, payload = url.split(",", 1)
    except ValueError as exc:
        raise ImageInputError("Malformed image data URL") from exc
    match = re.fullmatch(r"data:([^;,]+)(;base64)?", header, flags=re.IGNORECASE)
    if not match:
        raise ImageInputError("Malformed image data URL header")
    declared_type = match.group(1).lower()
    if declared_type not in ALLOWED_IMAGE_TYPES:
        raise ImageInputError(f"Unsupported image type: {declared_type}")
    try:
        data = (
            base64.b64decode(payload, validate=True)
            if match.group(2)
            else unquote_to_bytes(payload)
        )
    except (binascii.Error, ValueError) as exc:
        raise ImageInputError("Invalid base64 image data") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageInputError("Image exceeds the 30 MiB limit")
    actual_type = _sniff_image_type(data)
    if actual_type is None:
        raise ImageInputError("Image data is not PNG, JPEG, WebP, or GIF")
    return data, actual_type


def _is_public_ip(value: str) -> bool:
    try:
        return bool(ipaddress.ip_address(value).is_global)
    except ValueError:
        return False


async def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ImageInputError("Image URL must use http or https")
    if parsed.username or parsed.password:
        raise ImageInputError("Image URL must not contain credentials")
    if _is_public_ip(parsed.hostname):
        return
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise ImageInputError("Private or local image URLs are not allowed")

    loop = asyncio.get_running_loop()
    try:
        addresses = await loop.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ImageInputError("Image URL hostname could not be resolved") from exc
    resolved = {item[4][0].split("%", 1)[0] for item in addresses}
    if not resolved or any(not _is_public_ip(ip) for ip in resolved):
        raise ImageInputError("Private or local image URLs are not allowed")


class _RetryableImageDownload(Exception):
    pass


async def _download_remote_image_once(url: str) -> tuple[bytes, str, str]:
    timeout = aiohttp.ClientTimeout(total=180, connect=20, sock_read=60)
    current = url
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for redirect_count in range(MAX_REDIRECTS + 1):
            await _validate_public_url(current)
            async with session.get(
                current,
                allow_redirects=False,
                headers={"User-Agent": "ChatGPT-Web2API/0.2 image-fetch"},
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    if redirect_count >= MAX_REDIRECTS:
                        raise ImageInputError("Too many image URL redirects")
                    location = response.headers.get("Location")
                    if not location:
                        raise ImageInputError("Image redirect has no destination")
                    current = urljoin(current, location)
                    continue
                if response.status in {408, 425, 429, 500, 502, 503, 504, 520, 522, 524}:
                    raise _RetryableImageDownload(f"HTTP {response.status}")
                if response.status != 200:
                    raise ImageInputError(f"Image URL returned HTTP {response.status}")
                if response.content_length and response.content_length > MAX_IMAGE_BYTES:
                    raise ImageInputError("Image exceeds the 30 MiB limit")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise ImageInputError("Image exceeds the 30 MiB limit")
                    chunks.append(chunk)
                data = b"".join(chunks)
                actual_type = _sniff_image_type(data)
                if actual_type is None:
                    raise ImageInputError("Image URL did not return PNG, JPEG, WebP, or GIF")
                return data, actual_type, current
    raise ImageInputError("Image download failed")


async def _download_remote_image(url: str) -> tuple[bytes, str, str]:
    last_error: Exception | None = None
    for attempt in range(IMAGE_DOWNLOAD_ATTEMPTS):
        try:
            return await _download_remote_image_once(url)
        except ImageInputError:
            raise
        except (TimeoutError, _RetryableImageDownload, aiohttp.ClientError) as exc:
            last_error = exc
            if attempt + 1 >= IMAGE_DOWNLOAD_ATTEMPTS:
                break
            logger.warning(
                "Reference image download attempt %d/%d failed (%s): %s",
                attempt + 1,
                IMAGE_DOWNLOAD_ATTEMPTS,
                type(exc).__name__,
                exc,
            )
            await asyncio.sleep(IMAGE_DOWNLOAD_RETRY_DELAYS[attempt])
    raise ImageInputError(
        f"Image download failed after {IMAGE_DOWNLOAD_ATTEMPTS} attempts"
    ) from last_error


async def prepare_image_files(
    references: list[ImageReference], directory: str | Path
) -> list[str]:
    """Download reference images concurrently and materialize local upload files."""
    if len(references) > MAX_IMAGE_COUNT:
        raise ImageInputError(f"At most {MAX_IMAGE_COUNT} images are allowed per request")
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(IMAGE_DOWNLOAD_CONCURRENCY)

    async def load(reference: ImageReference) -> tuple[bytes, str, str]:
        async with semaphore:
            if reference.url.lower().startswith("data:"):
                data, mime_type = _decode_data_url(reference.url)
                return data, mime_type, ""
            return await _download_remote_image(reference.url)

    loaded = await asyncio.gather(*(load(reference) for reference in references))
    if sum(len(data) for data, _mime_type, _url in loaded) > MAX_TOTAL_IMAGE_BYTES:
        raise ImageInputError("Combined images exceed the 30 MiB limit")

    async def save(index: int, item: tuple[bytes, str, str]) -> str:
        data, mime_type, final_url = item
        path = target / _safe_filename(index, mime_type, final_url)
        await asyncio.to_thread(path.write_bytes, data)
        return str(path.resolve())

    return list(await asyncio.gather(*(
        save(index, item) for index, item in enumerate(loaded, start=1)
    )))


def _asset_from_dict(value: dict) -> dict | None:
    pointer = value.get("asset_pointer") or value.get("file_id")
    content_type = str(value.get("content_type") or value.get("type") or "")
    mime_type = str(value.get("mime_type") or "")
    direct_url = value.get("download_url") or value.get("image_url")
    if isinstance(direct_url, dict):
        direct_url = direct_url.get("url")

    file_id = ""
    if isinstance(pointer, str):
        match = _FILE_ID_RE.search(pointer)
        if match:
            file_id = match.group(1).replace("file-", "file_")
    is_asset = bool(
        file_id
        or isinstance(direct_url, str)
        or content_type in {"image_asset_pointer", "file", "image"}
    )
    if not is_asset:
        return None
    kind = "image" if (
        content_type in {"image_asset_pointer", "image"} or mime_type.startswith("image/")
    ) else "file"
    name = (
        value.get("name")
        or value.get("filename")
        or value.get("file_name")
        or (f"{file_id}.png" if kind == "image" and file_id else file_id)
        or "attachment"
    )
    result = {
        "type": kind,
        "name": str(name),
        "mime_type": mime_type or mimetypes.guess_type(str(name))[0]
        or ("image/png" if kind == "image" else "application/octet-stream"),
    }
    if file_id:
        result["file_id"] = file_id
    if isinstance(direct_url, str) and direct_url.startswith(("http://", "https://")):
        result["source_url"] = direct_url
    return result if result.get("file_id") or result.get("source_url") else None


def extract_response_assets(conversation: dict, anchor) -> list[dict]:
    """Extract file/image descriptors from the assistant turn matching *anchor*."""
    from .turn_anchor import _resolve_user_node

    mapping = conversation.get("mapping") or {}
    if not mapping:
        return []
    user_nid, _ = _resolve_user_node({"mapping": mapping}, anchor)
    if not user_nid:
        return []

    # Generated images are often stored on a tool node before the final assistant.
    descendant_ids: set[str] = set()
    queue = list((mapping.get(user_nid) or {}).get("children") or [])
    while queue:
        node_id = queue.pop(0)
        if node_id in descendant_ids:
            continue
        descendant_ids.add(node_id)
        queue.extend((mapping.get(node_id) or {}).get("children") or [])
    if not descendant_ids:
        for node_id, node in mapping.items():
            parent = node.get("parent")
            seen_parents: set[str] = set()
            while parent and parent not in seen_parents:
                if parent == user_nid:
                    descendant_ids.add(node_id)
                    break
                seen_parents.add(parent)
                parent = (mapping.get(parent) or {}).get("parent")

    descendants = [mapping[node_id] for node_id in descendant_ids if node_id in mapping]
    assets: list[dict] = []
    seen: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            asset = _asset_from_dict(value)
            if asset:
                identity = str(asset.get("file_id") or asset.get("source_url"))
                if identity not in seen:
                    seen.add(identity)
                    assets.append(asset)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for node in descendants:
        message = node.get("message") or {}
        content = message.get("content") or {}
        if message.get("end_turn") or content.get("content_type") != "text":
            walk(content)
            walk(message.get("metadata") or {})
    return assets
