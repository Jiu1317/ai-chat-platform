"""Tests for ChatGPT rate-limit detection.

When ChatGPT shows its "Too many requests" pop-up, no assistant message
appears, so ``send_and_stream`` currently spins until a 60s timeout. These
tests guard a dedicated ``RateLimitError`` plus a DOM-text detector so the
failure surfaces immediately and clearly instead.
"""

import pytest

from chatgpt_web2api.cdp_driver import (
    RateLimitError,
    is_rate_limited_text,
)


def test_rate_limit_exception_is_runtime_subclass():
    """RateLimitError should be catchable by generic handlers (RuntimeError)."""
    assert issubclass(RateLimitError, RuntimeError)


@pytest.mark.parametrize("text,expected", [
    # The exact pop-up the user reported
    ("Too many requests", True),
    ("You're making requests too quickly. We've temporarily limited "
     "access to your conversations to protect your data.", True),
    # Case / whitespace insensitive
    ("  too many requests  ", True),
    ("TOO MANY REQUESTS", True),
    # Common phrasings ChatGPT uses for the same condition
    ("You've reached the rate limit. Please try again later.", True),
    # Negative cases — must not false-positive on normal UI text
    ("4", False),
    ("Here is your answer about too many things.", False),
    ("", False),
    ("How do I avoid too many API requests in my code?", False),
])
def test_is_rate_limited_text(text, expected):
    assert is_rate_limited_text(text) is expected
