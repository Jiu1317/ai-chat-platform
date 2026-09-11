"""Incremental parser for DeepSeek's web completion SSE stream.

The parser is deliberately independent from CDP and HTTP code.  It accepts raw
bytes, keeps only protocol state, and never logs payloads (which can contain a
user's prompt or account-specific identifiers).
"""

from __future__ import annotations

import codecs
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024

EventKind = Literal[
    "ack",
    "content_delta",
    "reasoning_delta",
    "finished",
    "error",
]


class DeepSeekSSEParseError(ValueError):
    """Base class for malformed or incomplete DeepSeek SSE responses."""


class DeepSeekSSELimitError(DeepSeekSSEParseError):
    """The response exceeded the parser's configured byte limit."""


class DeepSeekSSEProtocolError(DeepSeekSSEParseError):
    """The decoded response did not follow the expected patch protocol."""


@dataclass(frozen=True, slots=True)
class DeepSeekSSEEvent:
    """A normalized event produced by :class:`DeepSeekSSEParser`.

    ``text`` is populated for content/reasoning deltas, while ``code`` and
    ``message`` are populated for business errors. ``source_event`` identifies
    the original SSE event without exposing its body.
    """

    kind: EventKind
    text: str = ""
    code: str | None = None
    message: str | None = None
    source_event: str | None = None

    @property
    def delta(self) -> str:
        """Alias useful to streaming callers."""

        return self.text


class DeepSeekSSEParser:
    """Incrementally decode and normalize one DeepSeek completion stream."""

    def __init__(self, *, max_body_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.max_body_bytes = max_body_bytes
        self._received_bytes = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._text_buffer = ""
        self._event_name = "message"
        self._data_lines: list[str] = []
        self._has_fields = False
        self._first_line = True

        self._root: dict[str, Any] = {}
        self._last_path: str | None = None
        self._last_op: str | None = None
        self._content = ""
        self._reasoning = ""
        self._finished = False
        self._errored = False
        self._closed = False
        self._eof = False

    @property
    def closed(self) -> bool:
        return self._closed

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[DeepSeekSSEEvent]:
        """Consume a raw byte chunk and return all newly completed events."""

        if self._eof:
            raise DeepSeekSSEProtocolError("cannot feed data after stream finalization")
        raw = bytes(chunk)
        if self._closed and raw:
            raise DeepSeekSSEProtocolError("received data after the close event")
        self._received_bytes += len(raw)
        if self._received_bytes > self.max_body_bytes:
            raise DeepSeekSSELimitError(
                f"SSE body exceeded {self.max_body_bytes} bytes"
            )
        try:
            decoded = self._decoder.decode(raw, final=False)
        except UnicodeDecodeError as exc:
            raise DeepSeekSSEParseError("SSE body is not valid UTF-8") from exc
        return self._consume_text(decoded, final=False)

    def finish(self) -> list[DeepSeekSSEEvent]:
        """Finalize UTF-8/SSE decoding and require a valid terminal close event."""

        if self._eof:
            return []
        self._eof = True
        try:
            tail = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise DeepSeekSSEParseError("SSE body ended inside a UTF-8 sequence") from exc
        events = self._consume_text(tail, final=True)
        if not self._closed:
            raise DeepSeekSSEProtocolError("SSE stream ended without a close event")
        return events

    # ``finalize`` is a familiar spelling for callers using codec-style APIs.
    finalize = finish

    def _consume_text(self, text: str, *, final: bool) -> list[DeepSeekSSEEvent]:
        self._text_buffer += text
        events: list[DeepSeekSSEEvent] = []
        while True:
            boundary = self._next_line_boundary(final=final)
            if boundary is None:
                break
            line, consumed = boundary
            self._text_buffer = self._text_buffer[consumed:]
            events.extend(self._consume_line(line))
        if final and self._text_buffer:
            line = self._text_buffer
            self._text_buffer = ""
            events.extend(self._consume_line(line))
        if final and self._has_fields:
            events.extend(self._dispatch_event())
        return events

    def _next_line_boundary(self, *, final: bool) -> tuple[str, int] | None:
        for index, char in enumerate(self._text_buffer):
            if char == "\n":
                return self._text_buffer[:index], index + 1
            if char == "\r":
                if index + 1 == len(self._text_buffer) and not final:
                    return None
                width = 2 if self._text_buffer[index + 1 : index + 2] == "\n" else 1
                return self._text_buffer[:index], index + width
        return None

    def _consume_line(self, line: str) -> list[DeepSeekSSEEvent]:
        if self._first_line:
            self._first_line = False
            line = line.removeprefix("\ufeff")
        if not line:
            return self._dispatch_event() if self._has_fields else []
        if line.startswith(":"):
            return []

        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        self._has_fields = True
        if field == "event":
            self._event_name = value or "message"
        elif field == "data":
            self._data_lines.append(value)
        # id/retry/extension fields are intentionally ignored.
        return []

    def _dispatch_event(self) -> list[DeepSeekSSEEvent]:
        source_event = self._event_name
        data_text = "\n".join(self._data_lines)
        self._event_name = "message"
        self._data_lines = []
        self._has_fields = False

        if source_event == "close" or data_text.strip() == "[DONE]":
            return self._handle_close()
        if source_event in {"ready", "title"}:
            if data_text.strip():
                self._decode_json(data_text, source_event)
            if source_event == "ready":
                return [DeepSeekSSEEvent("ack", source_event=source_event)]
            return []
        if not data_text.strip():
            raise DeepSeekSSEProtocolError(
                f"{source_event!r} SSE event did not contain data"
            )

        payload = self._decode_json(data_text, source_event)
        business_error = self._extract_business_error(payload, source_event)
        if business_error is not None:
            self._errored = True
            return [business_error]

        if source_event not in {"message", "update_session"}:
            # Future metadata events are accepted after validating their JSON.
            return []
        if not isinstance(payload, dict):
            raise DeepSeekSSEProtocolError("DeepSeek patch data must be a JSON object")
        if source_event == "update_session" and not (
            {"p", "o", "v", "response"} & payload.keys()
        ):
            # The live endpoint interleaves metadata such as ``updated_at``
            # before and after response patches. It carries no model output.
            return []
        return self._apply_payload(payload, source_event=source_event)

    @staticmethod
    def _decode_json(data_text: str, source_event: str) -> Any:
        try:
            return json.loads(data_text)
        except json.JSONDecodeError as exc:
            # Never include the response body in the exception: it may contain a prompt.
            raise DeepSeekSSEParseError(
                f"invalid JSON in {source_event!r} SSE event at character {exc.pos}"
            ) from exc

    @staticmethod
    def _extract_business_error(
        payload: Any, source_event: str
    ) -> DeepSeekSSEEvent | None:
        if not isinstance(payload, dict):
            if source_event != "error":
                return None
            return DeepSeekSSEEvent(
                "error",
                code="deepseek_error",
                message=str(payload),
                source_event=source_event,
            )

        error: Any = payload.get("error")
        if source_event != "error" and error in (None, False):
            return None
        if isinstance(error, dict):
            code = error.get("code") or payload.get("code") or "deepseek_error"
            message = (
                error.get("message")
                or error.get("msg")
                or payload.get("message")
                or "DeepSeek returned an error"
            )
        elif isinstance(error, str):
            code = payload.get("code") or "deepseek_error"
            message = error
        else:
            code = payload.get("code") or "deepseek_error"
            message = payload.get("message") or payload.get("msg") or "DeepSeek returned an error"
        return DeepSeekSSEEvent(
            "error",
            code=str(code),
            message=str(message),
            source_event=source_event,
        )

    def _apply_payload(
        self, payload: dict[str, Any], *, source_event: str
    ) -> list[DeepSeekSSEEvent]:
        before_content, before_reasoning = self._extract_text()

        explicit_path = payload.get("p")
        explicit_op = payload.get("o")
        if explicit_path is None and explicit_op is None and "v" in payload:
            value = payload["v"]
            if not isinstance(value, dict):
                if self._last_path is None or self._last_op is None:
                    raise DeepSeekSSEProtocolError(
                        "patch omitted path/operator before either was established"
                    )
                self._apply_operation(self._last_path, self._last_op, value)
            elif "response" in value:
                self._root = value
            elif self._last_path is not None and self._last_op is not None:
                self._apply_operation(self._last_path, self._last_op, value)
            else:
                self._root = value
        elif "response" in payload and explicit_path is None and explicit_op is None:
            self._root = payload
        else:
            path = explicit_path if explicit_path is not None else self._last_path
            op = explicit_op if explicit_op is not None else self._last_op
            if not isinstance(path, str) or not path:
                raise DeepSeekSSEProtocolError("patch path is missing or invalid")
            if not isinstance(op, str) or not op:
                raise DeepSeekSSEProtocolError("patch operator is missing or invalid")
            normalized_op = op.upper()
            if normalized_op == "BATCH":
                self._apply_batch(path, payload.get("v"))
            else:
                if "v" not in payload:
                    raise DeepSeekSSEProtocolError("patch value is missing")
                self._apply_operation(path, normalized_op, payload["v"])
                self._last_path = path
                self._last_op = normalized_op

        after_content, after_reasoning = self._extract_text()
        events: list[DeepSeekSSEEvent] = []
        content_delta = self._suffix(before_content, after_content, "response")
        reasoning_delta = self._suffix(before_reasoning, after_reasoning, "reasoning")
        self._content = after_content
        self._reasoning = after_reasoning
        if reasoning_delta:
            events.append(
                DeepSeekSSEEvent(
                    "reasoning_delta", text=reasoning_delta, source_event=source_event
                )
            )
        if content_delta:
            events.append(
                DeepSeekSSEEvent(
                    "content_delta", text=content_delta, source_event=source_event
                )
            )

        status = self._response_status()
        if status in {"FAILED", "ERROR"} and not self._errored:
            response = self._root.get("response", {})
            self._errored = True
            events.append(
                DeepSeekSSEEvent(
                    "error",
                    code=str(response.get("code") or "deepseek_error"),
                    message=str(
                        response.get("message")
                        or response.get("msg")
                        or "DeepSeek generation failed"
                    ),
                    source_event=source_event,
                )
            )
        elif status in {"FINISHED", "COMPLETED"} and not self._finished:
            self._finished = True
            events.append(DeepSeekSSEEvent("finished", source_event=source_event))
        return events

    def _apply_batch(self, base_path: str, value: Any) -> None:
        if not isinstance(value, list):
            raise DeepSeekSSEProtocolError("BATCH patch value must be a list")
        for item in value:
            if not isinstance(item, dict):
                raise DeepSeekSSEProtocolError("BATCH entries must be objects")
            child_path = item.get("p")
            if not isinstance(child_path, str) or not child_path:
                raise DeepSeekSSEProtocolError("BATCH entry path is missing or invalid")
            path = f"{base_path.rstrip('/')}/{child_path.lstrip('/')}"
            op = str(item.get("o", "SET")).upper()
            if op == "BATCH":
                self._apply_batch(path, item.get("v"))
            else:
                if "v" not in item:
                    raise DeepSeekSSEProtocolError("BATCH entry value is missing")
                self._apply_operation(path, op, item["v"])

    def _apply_operation(self, path: str, op: str, value: Any) -> None:
        parts = [part for part in path.strip("/").split("/") if part]
        if not parts:
            if op != "SET" or not isinstance(value, dict):
                raise DeepSeekSSEProtocolError("root patch must SET a JSON object")
            self._root = value
            return
        parent, key = self._resolve_parent(parts, create=op in {"SET", "APPEND"})
        if op == "SET":
            self._assign(parent, key, value)
            return
        if op != "APPEND":
            raise DeepSeekSSEProtocolError(f"unsupported patch operator {op!r}")

        current = self._lookup(parent, key, missing=None)
        if current is None:
            self._assign(parent, key, value)
        elif isinstance(current, str) and isinstance(value, str):
            self._assign(parent, key, current + value)
        elif isinstance(current, list):
            if isinstance(value, list):
                current.extend(value)
            else:
                current.append(value)
        else:
            raise DeepSeekSSEProtocolError(
                "APPEND target and value have incompatible types"
            )

    def _resolve_parent(
        self, parts: list[str], *, create: bool
    ) -> tuple[dict[str, Any] | list[Any], str]:
        current: Any = self._root
        for index, part in enumerate(parts[:-1]):
            next_part = parts[index + 1]
            if isinstance(current, dict):
                if part not in current:
                    if not create:
                        raise DeepSeekSSEProtocolError("patch path does not exist")
                    current[part] = [] if self._is_list_part(next_part) else {}
                current = current[part]
            elif isinstance(current, list):
                position = self._list_index(part, current, allow_append=create)
                if position == len(current):
                    current.append([] if self._is_list_part(next_part) else {})
                current = current[position]
            else:
                raise DeepSeekSSEProtocolError("patch traversed a scalar value")
        if not isinstance(current, (dict, list)):
            raise DeepSeekSSEProtocolError("patch parent is not a container")
        return current, parts[-1]

    @staticmethod
    def _is_list_part(part: str) -> bool:
        return part in {"-", "-1"} or part.isdigit()

    @staticmethod
    def _list_index(part: str, value: list[Any], *, allow_append: bool) -> int:
        if part == "-":
            if allow_append:
                return len(value)
            raise DeepSeekSSEProtocolError("append index is not readable")
        try:
            position = int(part)
        except ValueError as exc:
            raise DeepSeekSSEProtocolError("list patch index is invalid") from exc
        if position == -1:
            if not value:
                raise DeepSeekSSEProtocolError("last-item patch used on an empty list")
            return len(value) - 1
        if position < 0 or position > len(value) or (
            position == len(value) and not allow_append
        ):
            raise DeepSeekSSEProtocolError("list patch index is out of range")
        return position

    def _lookup(
        self, parent: dict[str, Any] | list[Any], key: str, *, missing: Any
    ) -> Any:
        if isinstance(parent, dict):
            return parent.get(key, missing)
        position = self._list_index(key, parent, allow_append=True)
        return missing if position == len(parent) else parent[position]

    def _assign(
        self, parent: dict[str, Any] | list[Any], key: str, value: Any
    ) -> None:
        if isinstance(parent, dict):
            parent[key] = value
            return
        position = self._list_index(key, parent, allow_append=True)
        if position == len(parent):
            parent.append(value)
        else:
            parent[position] = value

    def _extract_text(self) -> tuple[str, str]:
        response = self._root.get("response")
        if not isinstance(response, dict):
            return "", ""
        fragments = response.get("fragments", [])
        if not isinstance(fragments, list):
            raise DeepSeekSSEProtocolError("response fragments must be a list")
        content: list[str] = []
        reasoning: list[str] = []
        for fragment in fragments:
            if not isinstance(fragment, dict):
                continue
            value = fragment.get("content", "")
            if not isinstance(value, str):
                raise DeepSeekSSEProtocolError("fragment content must be text")
            fragment_type = str(fragment.get("type", "")).upper()
            if "THINK" in fragment_type or "REASON" in fragment_type:
                reasoning.append(value)
            elif fragment_type == "RESPONSE":
                content.append(value)
        return "".join(content), "".join(reasoning)

    @staticmethod
    def _suffix(before: str, after: str, channel: str) -> str:
        if after.startswith(before):
            return after[len(before) :]
        raise DeepSeekSSEProtocolError(
            f"{channel} text was rewritten instead of appended"
        )

    def _response_status(self) -> str:
        response = self._root.get("response")
        if not isinstance(response, dict):
            return ""
        status = response.get("quasi_status") or response.get("status") or ""
        return str(status).upper()

    def _handle_close(self) -> list[DeepSeekSSEEvent]:
        if self._closed:
            raise DeepSeekSSEProtocolError("received more than one close event")
        if self._errored:
            self._closed = True
            return []
        if not self._content:
            raise DeepSeekSSEProtocolError("DeepSeek closed the stream without response text")
        if not self._finished:
            raise DeepSeekSSEProtocolError("DeepSeek closed the stream before completion")
        self._closed = True
        return []


def parse_deepseek_sse(
    chunks: Iterable[bytes] | bytes,
    *,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> list[DeepSeekSSEEvent]:
    """Parse a complete iterable of SSE byte chunks."""

    parser = DeepSeekSSEParser(max_body_bytes=max_body_bytes)
    source = (chunks,) if isinstance(chunks, bytes) else chunks
    events: list[DeepSeekSSEEvent] = []
    for chunk in source:
        events.extend(parser.feed(chunk))
    events.extend(parser.finish())
    return events
