import json
from types import SimpleNamespace

import pytest

from sieve.actions import (
    MAX_ACTIONS,
    MAX_SCROLL,
    MAX_WAIT_MS,
    _validate_actions,
    build_page_action,
    ActionResult,
)
from sieve.public_output import safe_public_json
from sieve.security import SecurityError


def test_validate_actions_normalizes_all_action_forms_and_bounds():
    actions = _validate_actions([
        {"click": "  button.next  "},
        {"fill": {"selector": "input[name=q]", "text": "hello"}},
        {"press": "  Enter  "},
        {"press": {"selector": "input", "key": "  Tab  "}},
        {"wait": MAX_WAIT_MS + 1},
        {"scroll": MAX_SCROLL + 1},
        {"wait_selector": ".loaded"},
    ])

    assert actions == [
        {"click": "  button.next  "},
        {"fill": {"selector": "input[name=q]", "text": "hello"}},
        {"press": {"selector": None, "key": "Enter"}},
        {"press": {"selector": "input", "key": "Tab"}},
        {"wait": MAX_WAIT_MS},
        {"scroll": MAX_SCROLL},
        {"wait_selector": ".loaded"},
    ]


def test_validate_actions_rejects_malformed_payloads_and_limits():
    cases = [
        [],
        [{"click": ""}],
        [{"fill": {"selector": "input"}}],
        [{"fill": {"selector": "input", "text": 1}}],
        [{"press": {"selector": "", "key": "Enter"}}],
        [{"press": "   "}],
        [{"wait": True}],
        [{"scroll": 1.5}],
        [{"unknown": "value"}],
        [{"click": "a", "wait": 1}],
    ]
    for actions in cases:
        with pytest.raises((ValueError, SecurityError)):
            _validate_actions(actions)

    with pytest.raises(ValueError, match="Too many actions"):
        _validate_actions([{"wait": 0}] * (MAX_ACTIONS + 1))


def test_validate_actions_rejects_unsafe_css_and_caps_press_key():
    with pytest.raises(SecurityError):
        _validate_actions([{"click": "javascript:alert(1)"}])

    normalized = _validate_actions([{"press": {"key": "x" * 100}}])
    assert normalized[0]["press"]["key"] == "x" * 50


class _Locator:
    def __init__(self, calls, selector):
        self.calls = calls
        self.selector = selector
        self.first = self

    async def click(self, **kwargs):
        self.calls.append(("click", self.selector, kwargs))

    async def fill(self, text, **kwargs):
        self.calls.append(("fill", self.selector, text, kwargs))

    async def press(self, key, **kwargs):
        self.calls.append(("press", self.selector, key, kwargs))

    async def wait_for(self, **kwargs):
        self.calls.append(("wait_selector", self.selector, kwargs))


class _Page:
    def __init__(self):
        self.calls = []
        self.keyboard = _Locator(self.calls, "keyboard")

    def locator(self, selector):
        return _Locator(self.calls, selector)

    async def wait_for_timeout(self, milliseconds):
        self.calls.append(("wait", milliseconds))

    async def evaluate(self, script):
        self.calls.append(("evaluate", script))


@pytest.mark.asyncio
@pytest.mark.parametrize("error,category", [(RuntimeError, "execution_error"), (TimeoutError, "timeout")])
async def test_page_action_isolates_execution_errors_and_continues(caplog, error, category):
    page = _Page()
    original_locator = page.locator

    def failing_locator(selector):
        if selector == ".private-selector":
            raise error("private-exception private-fill-value")
        return original_locator(selector)

    page.locator = failing_locator
    action = build_page_action([
        {"press": "Enter"},
        {"fill": {"selector": ".private-selector", "text": "private-fill-value"}},
        {"wait": 1},
    ])

    reports = await action(page)
    assert [item.model_dump() for item in reports] == [
        {"index": 0, "type": "press", "status": "ok", "category": ""},
        {"index": 1, "type": "fill", "status": "error", "category": category},
        {"index": 2, "type": "wait", "status": "ok", "category": ""},
    ]
    assert len(reports) <= MAX_ACTIONS
    for secret in ("private-selector", "private-fill-value", "private-exception"):
        assert secret not in safe_public_json(reports)
        assert secret not in caplog.text

    assert ("press", "keyboard", "Enter", {}) in page.calls
    assert ("wait", 1) in page.calls


def test_build_page_action_returns_none_for_empty_input():
    assert build_page_action(None) is None
    assert build_page_action([]) is None


@pytest.mark.asyncio
async def test_action_reports_are_bounded_and_request_local():
    callback = build_page_action([{"wait": 0}] * MAX_ACTIONS)
    first, second = await callback(_Page()), await callback(_Page())
    assert len(first) == len(second) == MAX_ACTIONS
    assert first is not second
    assert [item.index for item in first] == list(range(MAX_ACTIONS))
    assert all(item.status == "ok" for item in first)
    with pytest.raises(ValueError, match="Too many actions"):
        build_page_action([{"wait": 0}] * (MAX_ACTIONS + 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_kind", ["actions", "failure", "unrelated", "subclass", "invalid_report"])
async def test_browser_action_reports_reach_public_output_and_reset(monkeypatch, caplog, callback_kind):
    from sieve import browser, fetcher
    from sieve.browser import DynamicBrowser
    from sieve.command_router import CapabilityRouter
    from sieve.response_translation import translate_response
    from sieve.server import ResponseModel

    page = _Page()
    page.set_default_navigation_timeout = lambda *a: None
    page.set_default_timeout = lambda *a: None
    page.on = page.remove_listener = lambda *a: None

    async def noop(*a, **kw):
        pass

    async def goto(*a, **kw):
        return SimpleNamespace(status=200)

    page.unroute_all = page.set_extra_http_headers = noop
    page.goto = goto
    session = DynamicBrowser()
    session._is_alive = True
    session._init_script = ""

    async def acquire():
        return page

    async def native_response(*a, **kw):
        return fetcher.Response("https://example.test", b'{"answer":42}', 200,
                                headers={"content-type": "application/json"})

    monkeypatch.setattr(session, "_acquire_page", acquire)
    monkeypatch.setattr(session, "_release_page", noop)
    monkeypatch.setattr(session, "_wait_for_stability", noop)
    monkeypatch.setattr(browser, "resolve_and_check", lambda *a: None)
    monkeypatch.setattr(fetcher, "response_from_browser_page", native_response)
    if callback_kind == "actions":
        callback = build_page_action([{"wait": 0}])
        expected = [ActionResult(index=0, type="wait", status="ok").model_dump()]
    elif callback_kind == "failure":
        async def callback(page):
            raise TimeoutError("private-exception private-fill-value .private-selector")
        expected = [ActionResult(index=0, type="callback", status="error", category="timeout").model_dump()]
    elif callback_kind == "subclass":
        class CallbackResult(ActionResult):
            selector: str = ".private-selector"
            value: str = "private-fill-value"

        async def callback(page):
            return [CallbackResult(index=0, type="wait", status="ok")]
        expected = [ActionResult(index=0, type="wait", status="ok").model_dump()]
    elif callback_kind == "invalid_report":
        async def callback(page):
            return [ActionResult.model_construct(index=0, type=".private-selector", status="ok", category="")]
        expected = [ActionResult(index=0, type="callback", status="error", category="execution_error").model_dump()]
    else:
        async def callback(page):
            return [{"selector": ".private-selector", "value": "private-fill-value"}]
        expected = []

    native = await session.fetch("https://example.test", page_action=callback,
                                 solve_cloudflare=False, wait=0, retries=1)
    result, _ = translate_response(native, "text", None, True, False, "dynamic", 0,
                                   ResponseModel, maximum_bytes=1024)

    class Server:
        async def smart_fetch(self, **kw):
            return result

    content, structured = await CapabilityRouter(Server()).dispatch("smart_fetch", {"url": "https://example.test"})
    assert structured["action_results"] == expected
    assert structured == json.loads(content[0].text)
    for secret in ("private-selector", "private-fill-value", "private-exception"):
        assert secret not in content[0].text
        assert secret not in caplog.text
    next_native = await session.fetch("https://example.test", solve_cloudflare=False, wait=0, retries=1)
    assert next_native.action_results == []
