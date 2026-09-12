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
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote_to_bytes, urljoin, urlparse

import aiohttp

logger = logging.getLogger(__name__)

# ``ConnectionTimeoutError`` was split out from ``ServerTimeoutError`` after
# aiohttp 3.9, which remains our minimum supported version.  On 3.9 the empty
# tuple simply matches nothing and retains the legacy retry behavior.
_CONNECTION_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = tuple(
    error_type
    for error_type in (getattr(aiohttp, "ConnectionTimeoutError", None),)
    if isinstance(error_type, type) and issubclass(error_type, BaseException)
)


MAX_IMAGE_COUNT = 8
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 30 * 1024 * 1024
MAX_REDIRECTS = 3
IMAGE_DOWNLOAD_ATTEMPTS = 3
IMAGE_DOWNLOAD_RETRY_DELAYS = (0.5, 1.5)
IMAGE_DOWNLOAD_CONCURRENCY = 2
IMAGE_RETRY_AFTER_MAX_SECONDS = 30.0
IMAGE_PREPARE_TIMEOUT_SECONDS = 5 * 60
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
_ZH_IMAGE_NOUN = (
    r"(?:图片|图像|照片|相片|参考图|原图|底图|素材图|附件图|新图|成图|"
    r"(?:这|那)(?:一|两|三|四|五|六|七|八|九|十|几|\d+)?张图|"
    r"(?:这些|那些)图|第(?:一|二|三|四|五|六|七|八|九|十|\d+)张图|"
    r"(?:一|两|三|四|五|六|七|八|九|十|几|\d+)张图)"
)
_ZH_COMPOSITE_VERB = r"(?:合并|拼(?:接)?|融合|组合|叠加|合成)"
_ZH_EDIT_VERB = r"(?:编辑|修改|裁(?:剪|成)|旋转|缩放|调色|美化|修图)"
_ZH_CREATE_VERB = r"(?:生成|画|绘制|创作|制作|做)"
_EN_IMAGE_NOUN = r"(?:images?|pictures?|photos?|photographs?|reference\s+images?|artworks?)"
_EN_COMPOSITE_VERB = (
    r"(?:merg(?:e|ed|es|ing)|combin(?:e|ed|es|ing)|"
    r"stitch(?:ed|es|ing)?|composit(?:e|ed|es|ing)|"
    r"overlay(?:s|ed|ing)?|overlaid|blend(?:ed|s|ing)?|fus(?:e|ed|es|ing))"
)
_EN_EDIT_VERB = (
    r"(?:edit(?:ed|s|ing)?|modif(?:y|ied|ies|ying)|"
    r"crop(?:ped|s|ping)?|rotat(?:e|ed|es|ing)|resiz(?:e|ed|es|ing)|retouch(?:ed|es|ing)?)"
)
_EN_CREATE_VERB = (
    r"(?:generat(?:e|ed|es|ing)|creat(?:e|ed|es|ing)|"
    r"draw(?:n|s|ing)?|mak(?:e|es|ing)|render(?:ed|s|ing)?)"
)
_IMAGE_ACTION_CLAUSE_SPLIT_RE = re.compile(
    r"[。！？.!?，,；;\r\n]+|而是|但是|\bbut\b",
    re.IGNORECASE,
)
_IMAGE_EDIT_PATTERNS = (
    re.compile(
        rf"(?:{_ZH_COMPOSITE_VERB}|{_ZH_EDIT_VERB})"
        rf"[^。！？\n]{{0,32}}{_ZH_IMAGE_NOUN}"
    ),
    re.compile(
        rf"{_ZH_IMAGE_NOUN}[^。！？\n]{{0,16}}{_ZH_COMPOSITE_VERB}"
    ),
    re.compile(
        rf"(?:把|将)[^。！？\n]{{0,16}}{_ZH_IMAGE_NOUN}"
        rf"[^。！？\n]{{0,24}}{_ZH_EDIT_VERB}"
    ),
    re.compile(
        rf"{_ZH_IMAGE_NOUN}(?:进行|做|需要|需|请|帮我|给我|来)?"
        rf"(?:一下|下)?{_ZH_EDIT_VERB}"
    ),
    re.compile(
        r"(?:去(?:除|掉)?|移除|删除|抠除|替换|更换|换)"
        r"[^。！？\n]{0,12}(?:背景|底色)"
    ),
    re.compile(
        r"(?:背景|底色)[^。！？\n]{0,12}"
        r"(?:去掉|去除|移除|删除|抠除|替换|更换)"
    ),
    re.compile(r"抠图"),
    re.compile(
        rf"\b(?:{_EN_COMPOSITE_VERB}|{_EN_EDIT_VERB})\b"
        rf"[^.!?\n]{{0,60}}\b{_EN_IMAGE_NOUN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_EN_IMAGE_NOUN}\b[^.!?\n]{{0,30}}"
        rf"\b{_EN_COMPOSITE_VERB}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_EN_IMAGE_NOUN}\b\s*"
        rf"(?:needs?\s+to\s+be|should\s+be|to\s+be)?\s*"
        rf"\b{_EN_EDIT_VERB}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:把|将)?(?:它们|这俩|这两张|这几张|这些|两张)"
        rf"[^。！？\n]{{0,10}}{_ZH_COMPOSITE_VERB}"
    ),
    re.compile(
        r"(?:把|将)?第(?:一|二|三|四|五|六|七|八|九|十|\d+)张"
        r"(?:图|图片|照片)?(?:和|与|及)"
        r"第(?:一|二|三|四|五|六|七|八|九|十|\d+)张"
        r"(?:图|图片|照片)?[^。！？\n]{0,10}"
        rf"{_ZH_COMPOSITE_VERB}"
    ),
    re.compile(
        rf"{_ZH_COMPOSITE_VERB}[^。！？\n]{{0,10}}"
        r"(?:它们|这俩|这两张|这几张|这些|两张)"
    ),
    re.compile(
        rf"(?:把|将)?{_ZH_IMAGE_NOUN}[^。！？\n]{{0,12}}"
        r"(?:放|摆|排)(?:在|到)?一起"
    ),
    re.compile(
        rf"{_ZH_IMAGE_NOUN}[^。！？\n]{{0,10}}合(?:在|到)?一起"
    ),
    re.compile(
        rf"\b{_EN_COMPOSITE_VERB}\b[^.!?\n]{{0,16}}"
        r"\b(?:them|both|these|those|the\s+two)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:去掉|去除|移除|删除|抠除)[^。！？\n]{0,20}"
        rf"{_ZH_IMAGE_NOUN}(?:中|里|上|内|的)"
    ),
    re.compile(
        rf"\b(?:remove|erase|delete)\b[^.!?\n]{{0,24}}"
        r"\b(?:watermarks?|logos?|objects?|people|person)\b"
        rf"[^.!?\n]{{0,40}}\b{_EN_IMAGE_NOUN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:给|为)(?:图中|照片中|画面中)?"
        r"(?:人物|人像|角色|主体|模特|人)[^。！？\n]{0,12}"
        r"(?:加上?|添加|戴上|换上)[^。！？\n]{0,20}"
        r"(?:帽子|眼镜|衣服|服装|配饰|饰品|物件)"
    ),
    re.compile(
        r"(?:去掉|去除|移除|删除|抠除)[^。！？\n]{0,8}"
        r"(?:水印|标志|logo)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:水印|标志|logo)[^。！？\n]{0,8}"
        r"(?:去掉|去除|移除|删除|抠除)",
        re.IGNORECASE,
    ),
    re.compile(rf"(?:把|将)?(?:它|其)[^。！？\n]{{0,12}}{_ZH_EDIT_VERB}"),
    re.compile(
        rf"\b{_EN_EDIT_VERB}\b[^.!?\n]{{0,12}}\b(?:it|them)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:make|crop)\b[^.!?\n]{0,12}"
        r"\b(?:it|them|this|these|those)\b[^.!?\n]{0,16}"
        r"\b(?:square|portrait|landscape)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:remove|erase|delete)\b[^.!?\n]{0,24}"
        r"\b(?:watermarks?|logos?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:remove|erase|replace|change)\b.{0,40}\b(?:background|backdrop)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bbackground\s+(?:removal|replacement)\b", re.IGNORECASE),
)
_IMAGE_PROMPT_NEGATION_PATTERNS = (
    re.compile(
        rf"(?:不要|不用|无需|不需要|不必|不|别|切勿|禁止)"
        rf"[^。！？，,；;\n]{{0,16}}"
        rf"(?:生成|画|绘制|创作|制作|做|{_ZH_COMPOSITE_VERB}|"
        rf"{_ZH_EDIT_VERB}|去掉|去除|移除|删除|添加)"
    ),
    re.compile(
        rf"\b(?:do\s+not|don['’]t|never|no\s+need\s+to)\b"
        rf"[^.!?,;\n]{{0,16}}\b(?:{_EN_CREATE_VERB}|"
        rf"{_EN_COMPOSITE_VERB}|{_EN_EDIT_VERB})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*(?:was|were|is|are|has|have)\b[^.!?\n]{{0,60}}"
        rf"\b{_EN_IMAGE_NOUN}\b[^.!?\n]{{0,24}}"
        rf"\b(?:been\s+)?{_EN_EDIT_VERB}\b[^.!?\n]*\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_ZH_IMAGE_NOUN}[^。！？\n]{{0,12}}(?:被|是否)"
        rf"[^。！？\n]{{0,12}}{_ZH_EDIT_VERB}[^。！？\n]{{0,8}}(?:吗|过吗)"
    ),
    re.compile(
        rf"{_ZH_IMAGE_NOUN}[^。！？\n]{{0,20}}(?:是|是否|是不是)"
        r"[^。！？\n]{0,12}(?:AI|人工智能)?[^。！？\n]{0,8}"
        rf"{_ZH_CREATE_VERB}[^。！？\n]{{0,8}}(?:吗|么)"
    ),
    re.compile(
        rf"^\s*did\b[^.!?\n]{{0,32}}"
        r"\b(?:ai|artificial\s+intelligence)\b"
        rf"[^.!?\n]{{0,24}}\b{_EN_CREATE_VERB}\b"
        rf"[^.!?\n]{{0,40}}\b{_EN_IMAGE_NOUN}\b[^.!?\n]*\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*did\s+(?:you|we|they|he|she|it)\b[^.!?\n]{{0,40}}"
        rf"\b{_EN_CREATE_VERB}\b[^.!?\n]{{0,40}}"
        rf"\b{_EN_IMAGE_NOUN}\b[^.!?\n]*\s*$",
        re.IGNORECASE,
    ),
)
_IMAGE_TEXT_ONLY_INTENT_PATTERNS = (
    re.compile(
        rf"(?:解释|说明|讲解|描述|告诉(?:我)?)[^。！？\n]{{0,60}}"
        rf"(?:如何|怎么|怎样)[^。！？\n]{{0,40}}"
        rf"(?:{_ZH_CREATE_VERB}|{_ZH_COMPOSITE_VERB}|{_ZH_EDIT_VERB})"
    ),
    re.compile(
        rf"(?:如何|怎么|怎样)[^。！？\n]{{0,40}}{_ZH_CREATE_VERB}"
        rf"[^。！？\n]{{0,40}}{_ZH_IMAGE_NOUN}"
    ),
    re.compile(
        r"(?:比较|对比)[^。！？\n]{0,80}(?:差异|区别|不同|异同|有什么变化)"
    ),
    re.compile(
        rf"(?:分析|描述|说明|解释|比较|对比|识别)"
        rf"[^。！？\n]{{0,60}}{_ZH_IMAGE_NOUN}"
    ),
    re.compile(
        r"(?:合并|组合|汇总|整合)[^。！？\n]{0,24}"
        r"(?:文字|文本|文案|内容)[^。！？\n]{0,40}"
        r"(?:摘要|总结|文档|段落|回答|答复)"
    ),
    re.compile(
        rf"{_ZH_IMAGE_NOUN}[^。！？\n]{{0,24}}"
        r"(?:提取|读取|识别|转写)[^。！？\n]{0,16}"
        r"(?:文字|文本|文案|内容)[^。！？\n]{0,40}"
        r"(?:摘要|总结|文档|段落|回答|答复)"
    ),
    re.compile(
        rf"{_ZH_IMAGE_NOUN}[^。！？\n]{{0,24}}"
        r"(?:文字|文本|文案|内容)[^。！？\n]{0,36}"
        r"(?:合并|组合|汇总|整合)[^。！？\n]{0,36}"
        r"(?:一段(?:话)?|摘要|总结|文档|段落)"
    ),
    re.compile(
        rf"\b(?:explain|describe|show|tell)\b[^.!?\n]{{0,40}}"
        rf"\bhow(?:\s+to)?\b[^.!?\n]{{0,40}}"
        rf"\b(?:{_EN_CREATE_VERB}|{_EN_COMPOSITE_VERB}|{_EN_EDIT_VERB})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bhow(?:\s+(?:do|can|should|would)\s+(?:i|we|you)|\s+to)\b"
        rf"[^.!?\n]{{0,40}}\b{_EN_CREATE_VERB}\b"
        rf"[^.!?\n]{{0,40}}\b{_EN_IMAGE_NOUN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:compare|contrast)\b[^.!?\n]{0,80}"
        r"\b(?:differences?|changes?|before\s+and\s+after)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:analyze|analyse|describe|explain|compare|identify)\b"
        rf"[^.!?\n]{{0,60}}\b{_EN_IMAGE_NOUN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:merge|combine)\b[^.!?\n]{0,32}"
        r"\b(?:text|words|captions?|content|information)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:extract|read|recognize|transcribe)\b[^.!?\n]{0,80}"
        r"\b(?:text|words|captions?|content)\b[^.!?\n]{0,80}"
        rf"\b{_EN_COMPOSITE_VERB}\b[^.!?\n]{{0,40}}"
        r"\b(?:paragraph|summary|document)\b",
        re.IGNORECASE,
    ),
)
_IMAGE_TRANSFORM_AFTER_TEXT_PATTERNS = (
    re.compile(
        r"(?:先|首先)[^。！？\n]{0,40}"
        r"(?:比较|对比|分析|描述|识别)[^。！？\n]{0,40}"
        r"(?:再|然后|接着|随后)[^。！？\n]{0,40}"
        rf"(?:{_ZH_COMPOSITE_VERB}|{_ZH_EDIT_VERB})"
    ),
    re.compile(
        r"\b(?:first|initially)\b[^.!?\n]{0,60}"
        r"\b(?:compare|analyze|analyse|describe|identify)\b[^.!?\n]{0,60}"
        r"\b(?:then|next|afterwards)\b[^.!?\n]{0,60}"
        rf"\b(?:{_EN_COMPOSITE_VERB}|{_EN_EDIT_VERB})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:analyze|analyse|inspect|review|compare|contrast|describe)\b"
        rf"[^.!?\n]{{0,60}}"
        rf"\b{_EN_IMAGE_NOUN}\b[^.!?\n]{{0,40}}\b(?:and|then)\b"
        rf"[^.!?\n]{{0,20}}\b(?:{_EN_EDIT_VERB}|{_EN_COMPOSITE_VERB})\b",
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


def wants_image_output(
    payload: dict,
    latest_user_text: str,
    has_image_inputs: bool = False,
) -> bool:
    """Detect an image-output request from explicit OpenAI fields or prompt text.

    Chat Completions clients do not all expose the same image-output switch, so
    explicit ``modalities``/``response_format`` signals take precedence and a
    conservative multilingual prompt fallback covers ordinary chat clients.
    Broad editing verbs are considered image-output intent only when the
    current request actually includes image inputs. This keeps image-analysis,
    comparison, and no-attachment tutorial prompts on the text-response path.
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
    if not text:
        return False
    clauses = (
        clause.strip()
        for clause in _IMAGE_ACTION_CLAUSE_SPLIT_RE.split(text)
        if clause.strip()
    )
    for clause in clauses:
        if any(
            pattern.search(clause)
            for pattern in _IMAGE_PROMPT_NEGATION_PATTERNS
        ):
            continue
        is_text_only = any(
            pattern.search(clause)
            for pattern in _IMAGE_TEXT_ONLY_INTENT_PATTERNS
        )
        is_explicit_sequence = any(
            pattern.search(clause)
            for pattern in _IMAGE_TRANSFORM_AFTER_TEXT_PATTERNS
        )
        if is_text_only and not is_explicit_sequence:
            continue
        if any(pattern.search(clause) for pattern in _IMAGE_OUTPUT_PATTERNS):
            return True
        if has_image_inputs and any(
            pattern.search(clause) for pattern in _IMAGE_EDIT_PATTERNS
        ):
            return True
    return False


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


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write one prepared image without exposing a partial destination file."""
    temporary = path.with_name(f".{path.name}.part")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


async def _write_bytes_cancellation_safe(path: Path, data: bytes) -> None:
    """Do not report cancellation until the underlying worker thread has stopped."""
    write_task = asyncio.create_task(asyncio.to_thread(_write_bytes_atomic, path, data))
    try:
        await asyncio.shield(write_task)
    except asyncio.CancelledError:
        # asyncio.to_thread cannot stop a running thread. Keep the wrapper task
        # alive until the real write/replace has completed so callers may safely
        # clean up their TemporaryDirectory after our cancellation propagates.
        while not write_task.done():
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                continue
        error = write_task.exception()
        if error is not None:
            logger.warning("Image write failed while cancellation was pending: %s", error)
        raise


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
    """Transient remote-download failure with an optional server delay."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class _ImageByteBudget:
    """Concurrency-safe request budget charged while response chunks arrive."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self._lock = asyncio.Lock()
        self._by_task: dict[asyncio.Task, int] = {}

    async def reserve(self, size: int) -> None:
        if size <= 0:
            return
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - all callers run in asyncio tasks
            raise RuntimeError("Image byte budget requires an asyncio task")
        async with self._lock:
            if self.total + size > self.limit:
                raise ImageInputError("Combined images exceed the 30 MiB limit")
            self.total += size
            self._by_task[task] = self._by_task.get(task, 0) + size

    async def release(self, size: int) -> None:
        if size <= 0:
            return
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - all callers run in asyncio tasks
            return
        async with self._lock:
            reserved = self._by_task.get(task, 0)
            released = min(size, reserved)
            self.total -= released
            remaining = reserved - released
            if remaining:
                self._by_task[task] = remaining
            else:
                self._by_task.pop(task, None)

    async def reserved_by_current_task(self) -> int:
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - all callers run in asyncio tasks
            return 0
        async with self._lock:
            return self._by_task.get(task, 0)


_ACTIVE_IMAGE_BYTE_BUDGET: ContextVar[_ImageByteBudget | None] = ContextVar(
    "web2api_image_byte_budget", default=None
)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse an HTTP Retry-After value and cap any wait at 30 seconds."""
    if not value:
        return None
    raw = value.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(max(0.0, seconds), IMAGE_RETRY_AFTER_MAX_SECONDS)


async def _download_remote_asset_once(
    url: str, *, require_image: bool
) -> tuple[bytes, str, str]:
    timeout = aiohttp.ClientTimeout(total=180, connect=20, sock_read=60)
    current = url
    budget = _ACTIVE_IMAGE_BYTE_BUDGET.get()
    reserved = 0
    completed = False
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for redirect_count in range(MAX_REDIRECTS + 1):
                await _validate_public_url(current)
                async with session.get(
                    current,
                    allow_redirects=False,
                    headers={"User-Agent": "ChatGPT-Web2API/0.2 asset-fetch"},
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        if redirect_count >= MAX_REDIRECTS:
                            raise ImageInputError("Too many image URL redirects")
                        location = response.headers.get("Location")
                        if not location:
                            raise ImageInputError("Image redirect has no destination")
                        current = urljoin(current, location)
                        continue
                    if response.status in {
                        408, 425, 429, 500, 502, 503, 504, 520, 522, 524
                    }:
                        retry_after = (
                            _parse_retry_after(response.headers.get("Retry-After"))
                            if response.status == 429
                            else None
                        )
                        raise _RetryableImageDownload(
                            f"HTTP {response.status}", retry_after=retry_after
                        )
                    if response.status != 200:
                        raise ImageInputError(
                            f"Image URL returned HTTP {response.status}"
                        )
                    if response.content_length and response.content_length > MAX_IMAGE_BYTES:
                        raise ImageInputError("Image exceeds the 30 MiB limit")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        chunk_size = len(chunk)
                        size += chunk_size
                        if size > MAX_IMAGE_BYTES:
                            raise ImageInputError("Image exceeds the 30 MiB limit")
                        if budget is not None:
                            await budget.reserve(chunk_size)
                            reserved += chunk_size
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if require_image:
                        content_type = _sniff_image_type(data)
                        if content_type is None:
                            raise ImageInputError(
                                "Image URL did not return PNG, JPEG, WebP, or GIF"
                            )
                    else:
                        content_type = (
                            str(response.headers.get("Content-Type") or "")
                            .split(";", 1)[0]
                            .strip()
                            or "application/octet-stream"
                        )
                    completed = True
                    return data, content_type, current
        raise ImageInputError("Image download failed")
    finally:
        if budget is not None and reserved and not completed:
            await budget.release(reserved)


async def _download_remote_image_once(url: str) -> tuple[bytes, str, str]:
    return await _download_remote_asset_once(url, require_image=True)


async def _download_remote_file_once(url: str) -> tuple[bytes, str, str]:
    return await _download_remote_asset_once(url, require_image=False)


async def _download_remote_with_retries(
    url: str,
    download_once,
    *,
    retry_connection_timeouts: bool,
) -> tuple[bytes, str, str]:
    """Retry a bounded remote asset download, honoring capped Retry-After."""
    last_error: Exception | None = None
    retry_after: float | None = None
    for attempt in range(IMAGE_DOWNLOAD_ATTEMPTS):
        retry_after = None
        try:
            return await download_once(url)
        except ImageInputError:
            raise
        except _CONNECTION_TIMEOUT_ERRORS as exc:
            # A response asset can use the authenticated browser as a second
            # transport. Let that caller opt out of repeating the same long
            # direct-connect timeout before it switches to its fallback.
            if not retry_connection_timeouts:
                raise ImageInputError("Image download connection timed out") from exc
            last_error = exc
        except _RetryableImageDownload as exc:
            last_error = exc
            retry_after = exc.retry_after
        except (TimeoutError, aiohttp.ClientError) as exc:
            last_error = exc
        if attempt + 1 >= IMAGE_DOWNLOAD_ATTEMPTS:
            break
        delay = (
            retry_after
            if retry_after is not None
            else IMAGE_DOWNLOAD_RETRY_DELAYS[attempt]
        )
        logger.warning(
            "Reference image download attempt %d/%d failed (%s): %s; retrying in %.1fs",
            attempt + 1,
            IMAGE_DOWNLOAD_ATTEMPTS,
            type(last_error).__name__,
            last_error,
            delay,
        )
        await asyncio.sleep(delay)
    raise ImageInputError(
        f"Image download failed after {IMAGE_DOWNLOAD_ATTEMPTS} attempts"
    ) from last_error


async def _download_remote_image(
    url: str, *, retry_connection_timeouts: bool = True
) -> tuple[bytes, str, str]:
    return await _download_remote_with_retries(
        url,
        _download_remote_image_once,
        retry_connection_timeouts=retry_connection_timeouts,
    )


async def _download_remote_file(
    url: str, *, retry_connection_timeouts: bool = True
) -> tuple[bytes, str, str]:
    """Download a generic response file without applying image signatures."""
    return await _download_remote_with_retries(
        url,
        _download_remote_file_once,
        retry_connection_timeouts=retry_connection_timeouts,
    )


async def prepare_image_files(
    references: list[ImageReference], directory: str | Path
) -> list[str]:
    """Download reference images under one byte budget and hard deadline."""
    if len(references) > MAX_IMAGE_COUNT:
        raise ImageInputError(f"At most {MAX_IMAGE_COUNT} images are allowed per request")
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(IMAGE_DOWNLOAD_CONCURRENCY)
    byte_budget = _ImageByteBudget(MAX_TOTAL_IMAGE_BYTES)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + IMAGE_PREPARE_TIMEOUT_SECONDS

    async def load(reference: ImageReference) -> tuple[bytes, str, str]:
        async with semaphore:
            if reference.url.lower().startswith("data:"):
                data, mime_type = _decode_data_url(reference.url)
                await byte_budget.reserve(len(data))
                return data, mime_type, ""
            token = _ACTIVE_IMAGE_BYTE_BUDGET.set(byte_budget)
            try:
                item = await _download_remote_image(reference.url)
            finally:
                _ACTIVE_IMAGE_BYTE_BUDGET.reset(token)
            # Test doubles and alternate transports may return complete bytes
            # without using the chunk-aware downloader. Charge them here while
            # avoiding a double charge for the normal streaming path.
            if not await byte_budget.reserved_by_current_task():
                await byte_budget.reserve(len(item[0]))
            return item

    async def save(index: int, item: tuple[bytes, str, str]) -> str:
        data, mime_type, final_url = item
        path = target / _safe_filename(index, mime_type, final_url)
        await _write_bytes_cancellation_safe(path, data)
        return str(path.resolve())

    async def run_tasks(coroutines) -> list:
        tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
        if not tasks:
            return []
        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            return list(
                await asyncio.wait_for(
                    asyncio.gather(*tasks),
                    timeout=remaining,
                )
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        loaded = await run_tasks(load(reference) for reference in references)
        return await run_tasks(
            save(index, item) for index, item in enumerate(loaded, start=1)
        )
    except TimeoutError as exc:
        raise ImageInputError(
            "Timed out preparing reference images after 5 minutes"
        ) from exc


def _asset_from_dict(value: dict) -> dict | None:
    pointer = value.get("asset_pointer") or value.get("file_id")
    content_type = str(value.get("content_type") or value.get("type") or "")
    mime_type = str(value.get("mime_type") or "")
    content_type_lower = content_type.lower()
    raw_download_url = value.get("download_url")
    direct_url = raw_download_url or value.get("image_url")
    if isinstance(direct_url, dict):
        direct_url = direct_url.get("url")

    file_id = ""
    if isinstance(pointer, str):
        match = _FILE_ID_RE.search(pointer)
        if match:
            file_id = match.group(1).replace("file-", "file_")
    # An arbitrary citation/website record may contain image_url for a
    # favicon or preview. It is not a generated attachment. A bare image URL
    # is accepted only when its enclosing object explicitly declares an asset
    # content type; download_url remains an explicit attachment signal.
    explicit_asset_types = {
        "image_asset_pointer",
        "file_asset_pointer",
        "file",
        "image",
    }
    effective_mime_type = (
        mime_type
        or (content_type if "/" in content_type else "")
    )
    citation_or_favicon = (
        content_type_lower in {
            "citation",
            "webpage",
            "website",
            "web_result",
            "link",
        }
        or any(
            key in value
            for key in ("citation", "citations", "citation_id", "favicon")
        )
    )
    explicit_download = (
        raw_download_url is not None and isinstance(direct_url, str)
    )
    explicit_image_mime = (
        isinstance(direct_url, str)
        and effective_mime_type.lower().startswith("image/")
        and not citation_or_favicon
    )
    is_asset = bool(
        file_id
        or explicit_download
        or explicit_image_mime
        or content_type_lower in explicit_asset_types
    )
    if not is_asset:
        return None
    kind = (
        "image"
        if content_type_lower in {"image_asset_pointer", "image"}
        or effective_mime_type.lower().startswith("image/")
        else "file"
    )
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
        "mime_type": effective_mime_type or mimetypes.guess_type(str(name))[0]
        or ("image/png" if kind == "image" else "application/octet-stream"),
    }
    if file_id:
        result["file_id"] = file_id
    if isinstance(direct_url, str) and direct_url.startswith(("http://", "https://")):
        result["source_url"] = direct_url
    return result if result.get("file_id") or result.get("source_url") else None


def _asset_identity_keys(asset: dict) -> set[str]:
    """Return stable cross-shape identities for one response asset.

    The same ChatGPT file can be represented as an ``asset_pointer``/``file_id``
    on one node and as an estuary or files URL on another. Keep exact values
    while also extracting the conservative file identifiers used by those URLs.
    """
    keys: set[str] = set()

    def remember_id(value) -> None:
        candidate = str(value or "").strip()
        if not candidate:
            return
        match = _FILE_ID_RE.search(candidate)
        if match:
            candidate = match.group(1).replace("file-", "file_")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,255}", candidate):
            keys.add(f"id:{candidate}")

    for field in ("file_id", "asset_pointer", "identity"):
        value = str(asset.get(field) or "").strip()
        if value:
            keys.add(f"raw:{value}")
            remember_id(value)

    source_url = str(asset.get("source_url") or "").strip()
    if source_url:
        keys.add(f"raw:{source_url}")
        try:
            parsed = urlparse(source_url)
            for value in parse_qs(parsed.query).get("id", []):
                remember_id(value)
            match = re.search(r"/files/([^/?#]+)", parsed.path)
            if match:
                remember_id(match.group(1))
        except ValueError:
            pass
        remember_id(source_url)
    return keys


def extract_response_assets(conversation: dict, anchor) -> list[dict]:
    """Extract file/image descriptors from the assistant turn matching *anchor*."""
    from .turn_anchor import _resolve_user_node

    mapping = conversation.get("mapping") or {}
    if not mapping:
        return []
    user_nid, _ = _resolve_user_node({"mapping": mapping}, anchor)
    if not user_nid:
        return []

    # ChatGPT can repeat the user's uploaded reference images inside descendant
    # tool/assistant metadata. Those records are inputs, not generated output.
    # Remember every stable identity present on the anchored user node so the
    # descendant walk cannot publish the references back to the API caller.
    input_asset_identities: set[str] = set()

    def remember_input_assets(value) -> None:
        if isinstance(value, dict):
            asset = _asset_from_dict(value)
            if asset:
                input_asset_identities.update(_asset_identity_keys(asset))
            for child in value.values():
                remember_input_assets(child)
        elif isinstance(value, list):
            for child in value:
                remember_input_assets(child)

    user_message = (mapping.get(user_nid) or {}).get("message") or {}
    remember_input_assets(user_message.get("content") or {})
    remember_input_assets(user_message.get("metadata") or {})

    # Generated images are often stored on a tool node before the final assistant.
    descendant_ids: list[str] = []
    seen_descendant_ids: set[str] = set()
    queue = list((mapping.get(user_nid) or {}).get("children") or [])
    while queue:
        node_id = queue.pop(0)
        if node_id in seen_descendant_ids:
            continue
        seen_descendant_ids.add(node_id)
        descendant_ids.append(node_id)
        queue.extend((mapping.get(node_id) or {}).get("children") or [])
    if not descendant_ids:
        for node_id, node in mapping.items():
            parent = node.get("parent")
            seen_parents: set[str] = set()
            while parent and parent not in seen_parents:
                if parent == user_nid:
                    if node_id not in seen_descendant_ids:
                        seen_descendant_ids.add(node_id)
                        descendant_ids.append(node_id)
                    break
                seen_parents.add(parent)
                parent = (mapping.get(parent) or {}).get("parent")

    descendants = [mapping[node_id] for node_id in descendant_ids if node_id in mapping]
    assets: list[dict] = []
    seen_asset_identities: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            asset = _asset_from_dict(value)
            if asset:
                identities = _asset_identity_keys(asset)
                if (
                    identities
                    and identities.isdisjoint(seen_asset_identities)
                    and identities.isdisjoint(input_asset_identities)
                ):
                    seen_asset_identities.update(identities)
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
