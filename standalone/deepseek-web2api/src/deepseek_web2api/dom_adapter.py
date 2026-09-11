"""The only layer that knows DeepSeek's private DOM structure."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .exceptions import (
    AuthenticationRequired,
    CDPProtocolError,
    DOMElementNotFound,
    GenerationRejected,
    HumanVerificationRequired,
    UnsupportedModel,
)
from .selectors import SELECTORS


class PageSession(Protocol):
    @property
    def connected(self) -> bool: ...

    async def command(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 15
    ) -> dict[str, Any]: ...

    async def evaluate(self, expression: str, *, timeout: float = 15) -> Any: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TurnSnapshot:
    identity: str
    text: str
    reasoning: str = ""
    content: str = ""


@dataclass(frozen=True, slots=True)
class DOMSnapshot:
    url: str
    title: str
    document_ready: bool
    composer_found: bool
    composer_identity: str
    composer_empty: bool
    composer_text: str
    send_found: bool
    stop_visible: bool
    logged_in: bool
    verification_required: bool
    login_visible: bool
    rate_limited: bool
    rate_limit_text: str
    reasoning_available: bool
    reasoning_enabled: bool
    search_available: bool
    search_enabled: bool
    users: tuple[TurnSnapshot, ...]
    assistants: tuple[TurnSnapshot, ...]


def _catalog_json() -> str:
    return json.dumps(asdict(SELECTORS), ensure_ascii=False)


_PROBE_SCRIPT = rf"""/*deepseek-web2api:probe*/
(() => {{
  const S = {_catalog_json()};
  const visible = (el) => !!el && !el.disabled &&
    el.getAttribute('aria-hidden') !== 'true' &&
    (el.getClientRects().length > 0 || el === document.activeElement);
  const firstVisible = (selectors) => {{
    for (const selector of selectors) {{
      const found = [...document.querySelectorAll(selector)].find(visible);
      if (found) return found;
    }}
    return null;
  }};
  const firstGroup = (selectors) => {{
    for (const selector of selectors) {{
      const found = [...document.querySelectorAll(selector)].filter(visible);
      if (found.length) return found;
    }}
    return [];
  }};
  const allVisible = (selectors) => [...new Set(selectors.flatMap((selector) =>
    [...document.querySelectorAll(selector)].filter(visible)))];
  const label = (el) => [el?.getAttribute('aria-label'), el?.title,
    el?.innerText, el?.textContent].filter(Boolean).join(' ').trim().toLowerCase();
  const normalizeButton = (el) => el?.closest('button,[role="button"]') || el || null;
  const semanticButton = (selectors, labels) => normalizeButton(firstVisible(selectors)) ||
    [...document.querySelectorAll('button,[role="button"]')].filter(visible).find((el) =>
      labels.some((item) => label(el).includes(item.toLowerCase()))) || null;
  const labelledControl = (selectors, labels) => {{
    for (const selector of selectors) {{
      const found = [...document.querySelectorAll(selector)].filter(visible).find((el) =>
        labels.some((item) => label(el).includes(item.toLowerCase())));
      if (found) return normalizeButton(found);
    }}
    return [...document.querySelectorAll('button,[role="button"],.ds-toggle-button')]
      .filter(visible).find((el) =>
        labels.some((item) => label(el).includes(item.toLowerCase()))) || null;
  }};
  const toggleEnabled = (el) => !!el && (
    el.getAttribute('aria-pressed') === 'true' || el.getAttribute('data-state') === 'on' ||
    /(^|\s)(active|selected|pressed|ds-toggle-button--selected)(\s|$)/i
      .test(el.className || ''));
  const nodeStore = window.__deepseekWeb2ApiNodeStore ||
    Object.defineProperty(window, '__deepseekWeb2ApiNodeStore', {{
      value: {{map: new WeakMap(), next: 1}}, configurable: true
    }}).__deepseekWeb2ApiNodeStore;
  const nodeId = (el, role) => {{
    const explicit = el.getAttribute('data-message-id') || el.getAttribute('data-id') ||
      el.closest('[data-message-id]')?.getAttribute('data-message-id');
    if (explicit) return `${{role}}:explicit:${{explicit}}`;
    if (!nodeStore.map.has(el)) nodeStore.map.set(el, nodeStore.next++);
    return `${{role}}:node:${{nodeStore.map.get(el)}}`;
  }};
  const clean = (value) => String(value || '').replace(/\u00a0/g, ' ').trim();
  let userElements = firstGroup(S.user);
  if (!userElements.length) {{
    userElements = firstGroup(S.message_block).filter((el) =>
      !S.assistant.some((selector) => el.matches(selector) || el.querySelector(selector)));
  }}
  const users = userElements.map((el) => ({{
    identity: nodeId(el, 'user'), text: clean(el.innerText || el.textContent)
  }}));
  const assistants = firstGroup(S.assistant).map((el) => {{
    const reasoningNode = S.reasoning_content.map((x) => el.querySelector(x)).find(Boolean);
    const finalNode = S.final_content.map((x) => el.matches(x) ? el : el.querySelector(x))
      .find((x) => x && (!reasoningNode || (x !== reasoningNode && !reasoningNode.contains(x))));
    const full = clean(el.innerText || el.textContent);
    const reasoning = clean(reasoningNode?.innerText || reasoningNode?.textContent);
    let content = clean(finalNode?.innerText || finalNode?.textContent || full);
    if (reasoning && content === full && full.startsWith(reasoning))
      content = clean(full.slice(reasoning.length));
    return {{identity: nodeId(el, 'assistant'), text: full, reasoning, content}};
  }});
  const composer = firstVisible(S.composer);
  let send = semanticButton(S.send_button, S.send_labels);
  let stop = semanticButton(S.stop_button, S.stop_labels);
  const pathIsLogin = /^\/(sign_in|login)(\/|$)/i.test(location.pathname);
  const login = firstVisible(S.login);
  const verificationElement = firstVisible(S.verification);
  const pageSignal = `${{document.title}} ${{location.pathname}}`.toLowerCase();
  const verificationRequired = !!verificationElement ||
    /verify you are human|security verification|安全验证|人机验证/.test(pageSignal);
  const errorElements = allVisible(S.rate_limit);
  const rateLimitElement = errorElements.find((el) =>
    /rate limit|too many requests|频繁|请求过多|稍后再试/.test(label(el)));
  const reasoningButton = labelledControl(S.reasoning_button, S.reasoning_labels);
  const searchButton = labelledControl(S.search_button, S.search_labels);
  const reasoningEnabled = toggleEnabled(reasoningButton);
  const searchEnabled = toggleEnabled(searchButton);
  const composerValue = composer ?
    (composer.value ?? composer.innerText ?? composer.textContent ?? '') : '';
  const composerEmpty = !clean(composerValue);
  const primaryCandidates = allVisible(S.primary_action)
    .map(normalizeButton).filter(Boolean);
  let composerRoot = composer?.parentElement || null;
  while (composerRoot && !primaryCandidates.some((item) => composerRoot.contains(item)))
    composerRoot = composerRoot.parentElement;
  const primaryAction = primaryCandidates.find((item) =>
    !composerRoot || composerRoot.contains(item)) || null;
  if (!send && primaryAction && !composerEmpty) send = primaryAction;
  if (!stop && primaryAction && composerEmpty) stop = primaryAction;
  return {{
    url: location.href, title: document.title,
    documentReady: document.readyState === 'complete',
    composerFound: !!composer,
    composerIdentity: composer ? nodeId(composer, 'composer') : '',
    composerEmpty, composerText: String(composerValue || '').trim(),
    sendFound: !!send, stopVisible: !!stop,
    loginVisible: pathIsLogin || !!login, verificationRequired,
    loggedIn: !!composer && !pathIsLogin && !login && !verificationRequired,
    rateLimited: !!rateLimitElement,
    rateLimitText: clean(rateLimitElement?.innerText || rateLimitElement?.textContent),
    reasoningAvailable: !!reasoningButton, reasoningEnabled,
    searchAvailable: !!searchButton, searchEnabled,
    users, assistants
  }};
}})()
"""


def _button_script(
    marker: str,
    selectors: tuple[str, ...],
    labels: tuple[str, ...],
    *,
    structural: tuple[str, ...] = (),
    structural_when_empty: bool | None = None,
    require_label: bool = False,
) -> str:
    return rf"""/*deepseek-web2api:{marker}*/
(() => {{
  const selectors = {json.dumps(selectors, ensure_ascii=False)};
  const labels = {json.dumps(labels, ensure_ascii=False)};
  const structural = {json.dumps(structural, ensure_ascii=False)};
  const structuralWhenEmpty = {json.dumps(structural_when_empty)};
  const requireLabel = {json.dumps(require_label)};
  const composerSelectors = {json.dumps(SELECTORS.composer, ensure_ascii=False)};
  const visible = (el) => !!el && !el.disabled && el.getAttribute('aria-hidden') !== 'true' &&
    (el.getClientRects().length > 0 || el === document.activeElement);
  const label = (el) => [el?.getAttribute('aria-label'), el?.title,
    el?.innerText, el?.textContent].filter(Boolean).join(' ').trim().toLowerCase();
  let button = null;
  for (const selector of selectors) {{
    button = [...document.querySelectorAll(selector)].filter(visible).find((el) =>
      !requireLabel || labels.some((item) => label(el).includes(item.toLowerCase())));
    if (button) break;
  }}
  button = button?.closest('button,[role="button"]') || button;
  button ||= [...document.querySelectorAll('button,[role="button"]')].filter(visible)
    .find((el) => labels.some((item) => label(el).includes(item.toLowerCase())));
  if (!button && structural.length && structuralWhenEmpty !== null) {{
    let composer = null;
    for (const selector of composerSelectors) {{
      composer = [...document.querySelectorAll(selector)].find(visible);
      if (composer) break;
    }}
    const value = String(composer?.value ?? composer?.innerText ?? '').trim();
    if (!!composer && (!value) === structuralWhenEmpty) {{
      const candidates = structural.flatMap((selector) =>
        [...document.querySelectorAll(selector)].filter(visible));
      let root = composer.parentElement;
      while (root && !candidates.some((item) => root.contains(item))) root = root.parentElement;
      button = candidates.find((item) => !root || root.contains(item)) || null;
    }}
  }}
  if (!button) return false;
  button.scrollIntoView({{block: 'nearest', inline: 'nearest'}});
  button.focus();
  button.click();
  return true;
}})()
"""


_GET_COMPOSER_SCRIPT = f"""/*deepseek-web2api:get-composer*/
(() => {{
  const selectors = {json.dumps(SELECTORS.composer, ensure_ascii=False)};
  const visible = (el) => !!el && !el.disabled && el.getAttribute('aria-hidden') !== 'true' &&
    (el.getClientRects().length > 0 || el === document.activeElement);
  let editor = null;
  for (const selector of selectors) {{
    editor = [...document.querySelectorAll(selector)].find(visible);
    if (editor) break;
  }}
  return editor;
}})()
"""


_SET_COMPOSER_VALUE_FUNCTION = r"""
function(value) {
  this.focus();
  if ('value' in this) {
    const proto = this.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype :
      HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (!setter) return false;
    setter.call(this, value);
  } else {
    this.textContent = value;
  }
  this.dispatchEvent(new InputEvent('input', {
    bubbles: true, composed: true, data: value, inputType: 'insertText'
  }));
  this.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
  return ('value' in this ? this.value : this.textContent) === value;
}
"""


class DeepSeekDOMAdapter:
    """Semantic operations over the mutable DeepSeek page DOM."""

    def __init__(self, page: PageSession) -> None:
        self._page = page

    async def probe(self) -> DOMSnapshot:
        raw = await self._page.evaluate(_PROBE_SCRIPT)
        if not isinstance(raw, dict):
            raise DOMElementNotFound("DeepSeek page probe returned an unexpected value")

        def turns(key: str) -> tuple[TurnSnapshot, ...]:
            values = raw.get(key, [])
            if not isinstance(values, list):
                return ()
            return tuple(
                TurnSnapshot(
                    identity=str(item.get("identity", "")),
                    text=str(item.get("text", "")),
                    reasoning=str(item.get("reasoning", "")),
                    content=str(item.get("content", item.get("text", ""))),
                )
                for item in values
                if isinstance(item, dict) and item.get("identity")
            )

        return DOMSnapshot(
            url=str(raw.get("url", "")), title=str(raw.get("title", "")),
            document_ready=bool(raw.get("documentReady")),
            composer_found=bool(raw.get("composerFound")),
            composer_identity=str(raw.get("composerIdentity", "")),
            composer_empty=bool(raw.get("composerEmpty")),
            composer_text=str(raw.get("composerText", "")),
            send_found=bool(raw.get("sendFound")), stop_visible=bool(raw.get("stopVisible")),
            logged_in=bool(raw.get("loggedIn")),
            verification_required=bool(raw.get("verificationRequired")),
            login_visible=bool(raw.get("loginVisible")),
            rate_limited=bool(raw.get("rateLimited")),
            rate_limit_text=str(raw.get("rateLimitText", "")),
            reasoning_available=bool(raw.get("reasoningAvailable")),
            reasoning_enabled=bool(raw.get("reasoningEnabled")),
            search_available=bool(raw.get("searchAvailable")),
            search_enabled=bool(raw.get("searchEnabled")),
            users=turns("users"), assistants=turns("assistants"),
        )

    @staticmethod
    def ensure_page_state(state: DOMSnapshot) -> None:
        if state.verification_required:
            raise HumanVerificationRequired(
                "DeepSeek requires manual verification in the dedicated browser"
            )
        if state.login_visible or not state.logged_in:
            raise AuthenticationRequired("Sign in to DeepSeek manually in the dedicated browser")
        if state.rate_limited:
            raise GenerationRejected(state.rate_limit_text or "DeepSeek is rate limited")

    @classmethod
    def ensure_ready_to_compose(cls, state: DOMSnapshot) -> None:
        cls.ensure_page_state(state)
        if not state.composer_found:
            raise DOMElementNotFound(
                "DeepSeek composer was not found; selectors may need updating"
            )

    @classmethod
    def ensure_ready_to_send(cls, state: DOMSnapshot) -> None:
        cls.ensure_ready_to_compose(state)
        if state.composer_empty or not state.send_found:
            raise DOMElementNotFound(
                "DeepSeek send button was not found after typing; selectors may need updating"
            )

    async def type_prompt(self, prompt: str) -> None:
        handle_result = await self._page.command(
            "Runtime.evaluate",
            {
                "expression": _GET_COMPOSER_SCRIPT,
                "awaitPromise": True,
                "returnByValue": False,
            },
        )
        if "exceptionDetails" in handle_result:
            raise CDPProtocolError("DeepSeek composer lookup failed")
        remote = handle_result.get("result", {})
        object_id = remote.get("objectId") if isinstance(remote, dict) else None
        if not isinstance(object_id, str) or not object_id:
            raise DOMElementNotFound("DeepSeek composer was not found")
        try:
            call_result = await self._page.command(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": _SET_COMPOSER_VALUE_FUNCTION,
                    "arguments": [{"value": prompt}],
                    "awaitPromise": True,
                    "returnByValue": True,
                    "userGesture": True,
                },
                timeout=30,
            )
            if "exceptionDetails" in call_result:
                raise CDPProtocolError("DeepSeek rejected the composer input event")
            result = call_result.get("result", {})
            if not isinstance(result, dict) or result.get("value") is not True:
                raise DOMElementNotFound("DeepSeek composer did not accept the prompt")
        finally:
            await self._page.command(
                "Runtime.releaseObject", {"objectId": object_id}
            )

    async def click_send(self) -> None:
        clicked = await self._page.evaluate(
            _button_script(
                "click-send",
                SELECTORS.send_button,
                SELECTORS.send_labels,
                structural=SELECTORS.primary_action,
                structural_when_empty=False,
            )
        )
        if clicked is not True:
            raise DOMElementNotFound("DeepSeek send button was not found")

    async def click_stop(self) -> bool:
        clicked = await self._page.evaluate(
            _button_script(
                "click-stop",
                SELECTORS.stop_button,
                SELECTORS.stop_labels,
                structural=SELECTORS.primary_action,
                structural_when_empty=True,
            )
        )
        return clicked is True

    async def _set_toggle(
        self,
        *,
        kind: str,
        enabled: bool,
        selectors: tuple[str, ...],
        labels: tuple[str, ...],
        available_attr: str,
        enabled_attr: str,
        state: DOMSnapshot | None,
        required: bool,
    ) -> None:
        current = state or await self.probe()
        if not getattr(current, available_attr):
            if enabled and required:
                raise UnsupportedModel(f"DeepSeek {kind} control was not found")
            return
        if getattr(current, enabled_attr) == enabled:
            return
        clicked = await self._page.evaluate(
            _button_script(
                f"toggle-{kind}",
                selectors,
                labels,
                require_label=True,
            )
        )
        if clicked is not True:
            raise DOMElementNotFound(f"DeepSeek {kind} control disappeared")
        stable_count = 0
        for _ in range(30):
            updated = await self.probe()
            self.ensure_page_state(updated)
            if getattr(updated, enabled_attr) == enabled:
                stable_count += 1
                if stable_count >= 2:
                    return
            else:
                stable_count = 0
            await asyncio.sleep(0.1)
        raise DOMElementNotFound(f"DeepSeek {kind} control did not change state")

    async def set_reasoning(self, enabled: bool, state: DOMSnapshot | None = None) -> None:
        await self._set_toggle(
            kind="reasoning",
            enabled=enabled,
            selectors=SELECTORS.reasoning_button,
            labels=SELECTORS.reasoning_labels,
            available_attr="reasoning_available",
            enabled_attr="reasoning_enabled",
            state=state,
            required=True,
        )

    async def disable_web_search(self, state: DOMSnapshot | None = None) -> None:
        await self._set_toggle(
            kind="web-search",
            enabled=False,
            selectors=SELECTORS.search_button,
            labels=SELECTORS.search_labels,
            available_attr="search_available",
            enabled_attr="search_enabled",
            state=state,
            required=False,
        )
