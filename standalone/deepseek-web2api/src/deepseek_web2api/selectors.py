"""Central, editable DOM selector catalog for chat.deepseek.com.

The page is a private implementation detail of DeepSeek and may change.  All
CSS and accessible-name candidates live here so adapting to a harmless markup
change doesn't require touching the driver state machine.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SelectorCatalog:
    composer: tuple[str, ...]
    send_button: tuple[str, ...]
    stop_button: tuple[str, ...]
    primary_action: tuple[str, ...]
    message_block: tuple[str, ...]
    user: tuple[str, ...]
    assistant: tuple[str, ...]
    reasoning_content: tuple[str, ...]
    final_content: tuple[str, ...]
    login: tuple[str, ...]
    verification: tuple[str, ...]
    rate_limit: tuple[str, ...]
    reasoning_button: tuple[str, ...]
    search_button: tuple[str, ...]
    send_labels: tuple[str, ...]
    stop_labels: tuple[str, ...]
    reasoning_labels: tuple[str, ...]
    search_labels: tuple[str, ...]


SELECTORS = SelectorCatalog(
    composer=(
        "textarea#chat-input",
        "textarea[data-testid='chat-input']",
        "textarea[placeholder*='DeepSeek' i]",
        "textarea[placeholder*='message' i]",
        "textarea[placeholder*='消息']",
        "[contenteditable='true'][role='textbox']",
        "div[contenteditable='true'][data-lexical-editor='true']",
    ),
    send_button=(
        "button[data-testid='send-button']",
        "button[aria-label*='send' i]",
        "button[title*='send' i]",
        "button[aria-label*='发送']",
        "button[title*='发送']",
        "div[role='button'].ds-button--primary.ds-button--filled.ds-button--circle:not(.ds-button--disabled):not([aria-disabled='true']) .ds-icon svg[viewBox='0 0 14 16']",
    ),
    stop_button=(
        "button[data-testid='stop-button']",
        "button[aria-label*='stop' i]",
        "button[title*='stop' i]",
        "button[aria-label*='停止']",
        "button[title*='停止']",
    ),
    primary_action=(
        "div[role='button'].ds-button--primary.ds-button--filled.ds-button--circle:not(.ds-button--disabled):not([aria-disabled='true'])",
        "button.ds-button--primary.ds-button--filled.ds-button--circle:not(:disabled):not([aria-disabled='true'])",
    ),
    # The hashed classes are last-resort adapters for the 2025/2026 web UI.
    # They remain isolated here and are preceded by semantic candidates.
    message_block=("div.dad65929", "div._4f9bf79"),
    user=(
        "[data-message-author-role='user']",
        "[data-role='user']",
        "[data-testid='user-message']",
        "article[data-author='user']",
        "div[class*='message'][class*='user']",
        ".fbb737a4",
    ),
    assistant=(
        "[data-message-author-role='assistant']",
        "[data-role='assistant']",
        "[data-testid='assistant-message']",
        "article[data-author='assistant']",
        ".ds-markdown",
        "div[class*='markdown'][class*='response']",
    ),
    reasoning_content=(
        "[data-testid='reasoning-content']",
        "[data-role='reasoning']",
        "details[class*='reason']",
        "div[class*='reasoning']",
        "div[class*='thinking']",
    ),
    final_content=(
        "[data-testid='message-content']",
        "[data-role='final']",
        ".ds-markdown",
        "div[class*='markdown']",
    ),
    login=(
        "a[href*='/sign_in']",
        "a[href*='/login']",
        "button[data-testid='login-button']",
        "form[action*='login']",
        "input[type='password']",
    ),
    verification=(
        "iframe[src*='challenge']",
        "iframe[src*='captcha']",
        "[data-testid*='captcha']",
        "[class*='captcha']",
        "[id*='captcha']",
        "[class*='challenge']",
    ),
    rate_limit=(
        "[data-testid='rate-limit-error']",
        "[data-testid='toast-error']",
        "[role='alert']",
        "div[class*='error']",
    ),
    reasoning_button=(
        "button[data-testid='deepthink-button']",
        "button[aria-label*='DeepThink' i]",
        "button[title*='DeepThink' i]",
        "button[aria-label*='深度思考']",
        "button[title*='深度思考']",
        "div.ds-toggle-button[aria-pressed][tabindex]",
        ".ds-toggle-button",
    ),
    search_button=(
        "[data-testid='web-search-button']",
        "[aria-label*='web search' i]",
        "[aria-label*='联网搜索']",
        "div.ds-toggle-button[aria-pressed][tabindex]",
        ".ds-toggle-button",
    ),
    send_labels=("send", "发送"),
    stop_labels=("stop", "停止生成", "停止"),
    reasoning_labels=("deepthink", "deep think", "深度思考", "r1"),
    search_labels=("web search", "联网搜索", "搜索"),
)
