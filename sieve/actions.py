"""Page interaction for smart_fetch (the `actions` param).

When the agent passes `actions=[...]`, smart_fetch forces the stealthy browser
tier and runs the actions on the page after navigation, before content
extraction. This reaches content behind a click, a search form, a "load more"
button, or infinite scroll — cases a plain fetch can't.

Implementation: patchright's stealthy fetch accepts a `page_action` callable that
receives the Playwright AsyncPage after goto and is awaited. We build that
callable from a validated list of action dicts and thread it through
smart_fetch -> _force_fetch -> stealthy_fetch -> session.fetch(page_action=...).

Action schema (one key per dict):
  {"click": "css-selector"}                 click the first match
  {"fill": {"selector": "css", "text": "x"}}  clear + fill an input
  {"press": "Enter"}                        press a keyboard key on the page
  {"press": {"selector": "css", "key": "Enter"}}  press on a specific element
  {"wait": 500}                             wait milliseconds
  {"scroll": 3}                             scroll down N viewport-heights
  {"wait_selector": "css"}                  wait for a selector to appear

Validation is strict (CSS selectors validated, counts/ms capped) so a bad action
fails fast instead of hanging the browser. Per-action errors are caught so one
failing step doesn't abort the rest; the agent gets the page in whatever state
it reached.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("master-fetch.actions")

MAX_ACTIONS = 20
MAX_WAIT_MS = 30_000
MAX_SCROLL = 50
_VALID_KEYS = {"click", "fill", "press", "wait", "scroll", "wait_selector"}


class ActionResult(BaseModel):
    """Value-free report for one validated browser action."""
    index: int = Field(ge=0, lt=MAX_ACTIONS)
    type: Literal["click", "fill", "press", "wait", "scroll", "wait_selector", "callback"]
    status: Literal["ok", "error"]
    category: Literal["", "timeout", "execution_error"] = ""


def _action_error_category(error: Exception) -> Literal["timeout", "execution_error"]:
    """Classify builtin and browser timeouts without inspecting their messages."""
    return "timeout" if isinstance(error, TimeoutError) or type(error).__name__ == "TimeoutError" else "execution_error"


def _validate_selector_action(index: int, key: str, value: Any, validate_css_selector) -> dict:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"action {index} {key!r} must be a non-empty CSS selector string")
    validate_css_selector(value)
    return {key: value}


def _validate_fill_action(index: int, value: Any, validate_css_selector) -> dict:
    if not isinstance(value, dict) or "selector" not in value or "text" not in value:
        raise ValueError(f"action {index} 'fill' must be {{selector, text}}")
    selector = value["selector"]
    if not isinstance(selector, str) or not selector.strip():
        raise ValueError(f"action {index} 'fill.selector' must be a non-empty CSS selector")
    validate_css_selector(selector)
    text = value["text"]
    if not isinstance(text, str):
        raise ValueError(f"action {index} 'fill.text' must be a string")
    if len(text) > 5000:
        raise ValueError(f"action {index} 'fill.text' too long (max 5000 chars)")
    return {"fill": {"selector": selector, "text": text}}


def _validate_press_action(index: int, value: Any, validate_css_selector) -> dict:
    if isinstance(value, str):
        key = value.strip()
        if not key:
            raise ValueError(f"action {index} 'press' key is empty")
        return {"press": {"selector": None, "key": key[:50]}}
    if isinstance(value, dict) and "key" in value:
        selector = value.get("selector")
        key = str(value.get("key", "")).strip()
        if not key:
            raise ValueError(f"action {index} 'press.key' is empty")
        if selector is not None and (not isinstance(selector, str) or not selector.strip()):
            raise ValueError(f"action {index} 'press.selector' must be a non-empty CSS selector")
        if selector:
            validate_css_selector(selector)
        return {"press": {"selector": selector, "key": key[:50]}}
    raise ValueError(f"action {index} 'press' must be a key string or {{selector, key}}")


def _validate_bounded_int_action(index: int, key: str, value: Any, maximum: int, label: str) -> dict:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"action {index} {key!r} must be an int ({label})")
    return {key: max(0, min(value, maximum))}


def _validate_actions(actions) -> list[dict]:
    """Validate + normalize the actions list. Raises ValueError on bad input."""
    from sieve.security import validate_css_selector

    if not isinstance(actions, list) or not actions:
        raise ValueError("actions must be a non-empty list of action dicts")
    if len(actions) > MAX_ACTIONS:
        raise ValueError(f"Too many actions ({len(actions)}). Maximum is {MAX_ACTIONS}.")
    out: list[dict] = []
    for i, a in enumerate(actions):
        if not isinstance(a, dict) or len(a) != 1:
            raise ValueError(f"action {i} must be a dict with exactly one key {sorted(_VALID_KEYS)}")
        (key, val), = a.items()
        if key not in _VALID_KEYS:
            raise ValueError(f"action {i} has unknown key {key!r}; valid: {sorted(_VALID_KEYS)}")
        if key in ("click", "wait_selector"):
            out.append(_validate_selector_action(i, key, val, validate_css_selector))
        elif key == "fill":
            out.append(_validate_fill_action(i, val, validate_css_selector))
        elif key == "press":
            out.append(_validate_press_action(i, val, validate_css_selector))
        elif key == "wait":
            out.append(_validate_bounded_int_action(i, key, val, MAX_WAIT_MS, "ms"))
        elif key == "scroll":
            out.append(_validate_bounded_int_action(i, key, val, MAX_SCROLL, "viewport steps"))
    return out


def build_page_action(actions) -> Optional[Callable]:
    """Validate `actions` and return an async page_action(page) callable, or None
    if `actions` is None/empty."""
    if not actions:
        return None
    validated = _validate_actions(actions)

    async def page_action(page) -> list[ActionResult]:
        reports = []
        for index, a in enumerate(validated):
            action_type = next(iter(a))
            category = ""
            try:
                if "click" in a:
                    await page.locator(a["click"]).first.click(timeout=10_000)
                elif "fill" in a:
                    f = a["fill"]
                    await page.locator(f["selector"]).first.fill(f["text"], timeout=10_000)
                elif "press" in a:
                    p = a["press"]
                    if p["selector"]:
                        await page.locator(p["selector"]).first.press(p["key"], timeout=10_000)
                    else:
                        await page.keyboard.press(p["key"])
                elif "wait" in a:
                    await page.wait_for_timeout(a["wait"])
                elif "scroll" in a:
                    # Jump to the current bottom each step, not a fixed viewport
                    # delta. Infinite-scroll loaders fire near the bottom and
                    # grow the page; a fixed scrollBy stops re-reaching the new
                    # bottom once content extends past it, so page 2+ never load.
                    # scrollTo(scrollHeight) re-triggers on every step and still
                    # reveals lazy-loaded content progressively.
                    for _ in range(a["scroll"]):
                        try:
                            await page.evaluate(
                                "() => window.scrollTo(0, document.body.scrollHeight)"
                            )
                        except Exception:
                            await page.mouse.wheel(0, 20000)
                        await page.wait_for_timeout(700)
                elif "wait_selector" in a:
                    await page.locator(a["wait_selector"]).first.wait_for(
                        state="attached", timeout=10_000
                    )
            except Exception as e:
                category = _action_error_category(e)
                logger.warning("page action index=%d type=%s category=%s failed", index, action_type, category)
            reports.append(ActionResult(index=index, type=action_type,
                                        status="error" if category else "ok", category=category))
        return reports

    return page_action
