from __future__ import annotations

import json

import pytest

from deepseek_web2api.deepseek_sse import (
    DeepSeekSSELimitError,
    DeepSeekSSEParseError,
    DeepSeekSSEParser,
    DeepSeekSSEProtocolError,
    parse_deepseek_sse,
)


def frame(event: str, payload: object | None = None, *, crlf: bool = False) -> bytes:
    newline = "\r\n" if crlf else "\n"
    lines = [f"event: {event}"]
    if payload is not None:
        lines.append("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return (newline.join(lines) + newline * 2).encode()


def complete_stream(*, content: str = "D") -> bytes:
    return b"".join(
        [
            frame("ready", {"ok": True}),
            frame(
                "update_session",
                {
                    "v": {
                        "response": {
                            "status": "WIP",
                            "thinking_enabled": False,
                            "fragments": [{"type": "RESPONSE", "content": content}],
                        }
                    }
                },
            ),
            frame(
                "update_session",
                {"p": "response/status", "o": "SET", "v": "FINISHED"},
            ),
            frame("close", {}),
        ]
    )


def test_incremental_utf8_sse_and_patch_inheritance() -> None:
    body = b"".join(
        [
            b"\xef\xbb\xbf" + frame("ready", {"ok": True}, crlf=True),
            frame("update_session", {"updated_at": 1789056000}, crlf=True),
            frame(
                "update_session",
                {
                    "v": {
                        "response": {
                            "status": "WIP",
                            "thinking_enabled": False,
                            "fragments": [{"type": "RESPONSE", "content": "D你"}],
                        }
                    }
                },
                crlf=True,
            ),
            frame(
                "update_session",
                {
                    "p": "response/fragments/-1/content",
                    "o": "APPEND",
                    "v": "EEP",
                },
                crlf=True,
            ),
            frame("update_session", {"v": "SEE"}, crlf=True),
            frame(
                "update_session",
                {
                    "p": "response",
                    "o": "BATCH",
                    "v": [
                        {"p": "accumulated_token_usage", "v": 64},
                        {"p": "quasi_status", "v": "FINISHED"},
                    ],
                },
                crlf=True,
            ),
            frame("title", {"title": "private title"}, crlf=True),
            frame("update_session", {"updated_at": 1789056001}, crlf=True),
            frame("close", {}, crlf=True),
        ]
    )

    events = parse_deepseek_sse(bytes([byte]) for byte in body)
    assert [(item.kind, item.source_event) for item in events] == [
        ("ack", "ready"),
        ("content_delta", "update_session"),
        ("content_delta", "update_session"),
        ("content_delta", "update_session"),
        ("finished", "update_session"),
    ]
    assert "".join(item.text for item in events if item.kind == "content_delta") == "D你EEPSEE"


def test_reasoning_and_appended_response_fragment_are_separate() -> None:
    body = b"".join(
        [
            frame(
                "update_session",
                {
                    "v": {
                        "response": {
                            "status": "WIP",
                            "fragments": [{"type": "THINKING", "content": "思"}],
                        }
                    }
                },
            ),
            frame(
                "update_session",
                {
                    "p": "response/fragments",
                    "o": "APPEND",
                    "v": {"type": "RESPONSE", "content": "答"},
                },
            ),
            frame(
                "update_session",
                {"p": "response/status", "o": "SET", "v": "FINISHED"},
            ),
            frame("close", {}),
        ]
    )
    events = parse_deepseek_sse(body)
    assert [(item.kind, item.text) for item in events] == [
        ("reasoning_delta", "思"),
        ("content_delta", "答"),
        ("finished", ""),
    ]


def test_business_error_is_structured_and_may_close_without_content() -> None:
    events = parse_deepseek_sse(
        frame("error", {"code": "busy", "message": "Please retry later"})
        + frame("close", {})
    )
    assert len(events) == 1
    assert events[0].kind == "error"
    assert events[0].code == "busy"
    assert events[0].message == "Please retry later"


def test_multiline_data_and_comments_follow_sse_rules() -> None:
    response = {
        "response": {
            "status": "FINISHED",
            "fragments": [{"type": "RESPONSE", "content": "ok"}],
        }
    }
    encoded = json.dumps({"v": response}, separators=(",", ":"))
    split_at = encoded.index('"response"')
    body = (
        ": heartbeat\n"
        "event: update_session\n"
        f"data: {encoded[:split_at]}\n"
        f"data: {encoded[split_at:]}\n\n"
        "event: close\n"
        "data: {}\n\n"
    ).encode()
    events = parse_deepseek_sse(body)
    assert [item.kind for item in events] == ["content_delta", "finished"]


def test_live_metadata_sequence_is_silent_and_only_ready_is_ack() -> None:
    body = b"".join(
        [
            frame("ready", {"request_message_id": "opaque"}),
            frame("update_session", {"updated_at": 1789056000}),
            frame(
                "update_session",
                {
                    "v": {
                        "response": {
                            "status": "WIP",
                            "fragments": [{"type": "RESPONSE", "content": "ok"}],
                        }
                    }
                },
            ),
            frame("title", {"title": "ignored"}),
            frame(
                "update_session",
                {"p": "response/status", "o": "SET", "v": "FINISHED"},
            ),
            frame("update_session", {"updated_at": 1789056001}),
            frame("close", {}),
        ]
    )
    events = parse_deepseek_sse(body)
    assert [item.kind for item in events] == ["ack", "content_delta", "finished"]


def test_unknown_json_metadata_never_acknowledges_a_send() -> None:
    body = frame("future_metadata", {"sequence": 1}) + complete_stream()
    events = parse_deepseek_sse(body)
    assert [item.kind for item in events].count("ack") == 1
    assert all(
        item.source_event != "future_metadata"
        for item in events
    )


def test_invalid_json_is_an_explicit_error_without_echoing_body() -> None:
    parser = DeepSeekSSEParser()
    with pytest.raises(DeepSeekSSEParseError, match="invalid JSON") as caught:
        parser.feed(b"event: update_session\ndata: {secret-token\n\n")
    assert "secret-token" not in str(caught.value)


def test_invalid_utf8_is_an_explicit_error() -> None:
    parser = DeepSeekSSEParser()
    with pytest.raises(DeepSeekSSEParseError, match="UTF-8"):
        parser.feed(b"\xff")


def test_body_size_is_bounded() -> None:
    parser = DeepSeekSSEParser(max_body_bytes=8)
    with pytest.raises(DeepSeekSSELimitError, match="exceeded 8 bytes"):
        parser.feed(b"123456789")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (frame("close", {}), "without response text"),
        (
            frame(
                "update_session",
                {
                    "v": {
                        "response": {
                            "status": "WIP",
                            "fragments": [{"type": "RESPONSE", "content": "partial"}],
                        }
                    }
                },
            )
            + frame("close", {}),
            "before completion",
        ),
    ],
)
def test_close_requires_body_and_finished_state(body: bytes, message: str) -> None:
    with pytest.raises(DeepSeekSSEProtocolError, match=message):
        parse_deepseek_sse(body)


def test_eof_without_close_is_rejected() -> None:
    without_close = complete_stream().removesuffix(frame("close", {}))
    with pytest.raises(DeepSeekSSEProtocolError, match="without a close event"):
        parse_deepseek_sse(without_close)


def test_feed_after_close_is_rejected() -> None:
    parser = DeepSeekSSEParser()
    parser.feed(complete_stream())
    with pytest.raises(DeepSeekSSEProtocolError, match="after the close"):
        parser.feed(b"x")


def test_unknown_operator_and_incompatible_rewrite_are_explicit() -> None:
    parser = DeepSeekSSEParser()
    parser.feed(
        frame(
            "update_session",
            {
                "v": {
                    "response": {
                        "status": "WIP",
                        "fragments": [{"type": "RESPONSE", "content": "old"}],
                    }
                }
            },
        )
    )
    with pytest.raises(DeepSeekSSEProtocolError, match="rewritten"):
        parser.feed(
            frame(
                "update_session",
                {
                    "p": "response/fragments/-1/content",
                    "o": "SET",
                    "v": "different",
                },
            )
        )
