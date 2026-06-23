"""Unified Playwright actions — driven by selectors from config."""

from __future__ import annotations

import time
from typing import Any

from playwright.sync_api import Locator, Page

def split_selectors(selector: str | None) -> list[str]:
    if not selector:
        return []
    return [s.strip() for s in selector.split(",") if s.strip()]


def resolve_message_frame(model_config: dict[str, Any]) -> str | None:
    """Return the selector for the last assistant message on the page."""
    selectors = model_config.get("selectors") or {}
    message_frame = selectors.get("message_frame")
    if isinstance(message_frame, str):
        return message_frame.strip() or None
    return None


def _page_blocker_hint(page: Page) -> str | None:
    """Return a human-readable hint when the chat UI is blocked (login, captcha, etc.)."""
    try:
        url = (page.url or "").lower()
        body = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        return None

    if (
        "just a moment" in body
        or "checking your browser" in body
        or "cloudflare" in body
        or "performing security verification" in body
        or "security service" in body
        or "malicious bots" in body
    ):
        return (
            "Cloudflare đang xác minh — hoàn thành thủ công trong cửa sổ browser, "
            "đăng nhập rồi gửi prompt lại."
        )
    if "log in" in body or "sign in" in body or "/login" in url:
        return "Chưa đăng nhập — đăng nhập trong cửa sổ browser rồi thử lại."
    if "unusual activity" in body or "automated" in body or "bot" in body:
        return (
            "Trang phát hiện automation — đăng nhập thủ công trong cửa sổ browser, "
            "hoặc thử tắt VPN / đổi mạng."
        )
    return None


def _is_security_challenge(page: Page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=2000).lower()
    except Exception:
        return False
    markers = (
        "performing security verification",
        "security service",
        "just a moment",
        "checking your browser",
        "cloudflare",
    )
    return any(m in body for m in markers)


def wait_for_input(page: Page, selectors: dict[str, str], timeout_ms: int = 90000) -> Locator:
    deadline = time.time() + timeout_ms / 1000
    last_hint: str | None = None
    while time.time() < deadline:
        if _is_security_challenge(page):
            # Give user time to pass Cloudflare manually.
            deadline = max(deadline, time.time() + 30)

        hint = _page_blocker_hint(page)
        if hint and hint != last_hint:
            print(f"[Playwright] {hint}")
            last_hint = hint

        for sel in split_selectors(selectors.get("input")):
            loc = page.locator(sel).first
            try:
                loc.wait_for(state="visible", timeout=2000)
                if loc.is_enabled():
                    return loc
            except Exception:
                pass
        page.wait_for_timeout(500)

    hint = _page_blocker_hint(page)
    extra = f" — {hint}" if hint else ""
    raise TimeoutError(
        f"Input not found within {timeout_ms}ms: {selectors.get('input')}{extra}"
    )


_PASTE_CHAR_THRESHOLD = 200


def _clear_input_field(page: Page) -> None:
    page.keyboard.press("Control+a")
    page.keyboard.press("Backspace")


def _fill_via_keyboard(page: Page, text: str) -> None:
    """Type into rich editors — avoids Trusted Types / innerHTML restrictions (Gemini, Claude, etc.)."""
    _clear_input_field(page)
    page.keyboard.type(text, delay=15)


def _fill_via_paste(page: Page, text: str) -> None:
    """Paste long prompts into rich editors — faster than typing character by character."""
    try:
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    except Exception:
        pass
    page.evaluate(
        """async (text) => {
            await navigator.clipboard.writeText(text);
        }""",
        text,
    )
    _clear_input_field(page)
    page.keyboard.press("Control+v")


def fill_prompt(page: Page, selectors: dict[str, str], text: str) -> Locator:
    inp = wait_for_input(page, selectors)
    inp.click()
    inp.focus()

    input_kind = inp.evaluate(
        """(el) => {
            const tag = el.tagName.toLowerCase();
            if (tag === 'textarea' || tag === 'input') return 'native';
            if (el.isContentEditable) return 'contenteditable';
            return 'other';
        }"""
    )

    if input_kind == "native":
        inp.fill(text)
        inp.dispatch_event("input")
        inp.dispatch_event("change")
    else:
        if len(text) > _PASTE_CHAR_THRESHOLD:
            _fill_via_paste(page, text)
        else:
            _fill_via_keyboard(page, text)

    return inp


def click_send(page: Page, selectors: dict[str, str]) -> bool:
    for sel in split_selectors(selectors.get("send_button")):
        btn = page.locator(sel).first
        try:
            btn.wait_for(state="visible", timeout=5000)
            disabled = btn.evaluate(
                """(el) => el.disabled || el.getAttribute('aria-disabled') === 'true'"""
            )
            if not disabled:
                btn.click()
                return True
        except Exception:
            pass
    raise RuntimeError(f"Send button not found or disabled: {selectors.get('send_button')}")


def _is_generating(page: Page, selectors: dict[str, str]) -> bool:
    stop = selectors.get("stop_button")
    if not stop:
        return False
    for sel in split_selectors(stop):
        if page.locator(sel).count() > 0:
            return True
    return False


def _read_last_response(page: Page, message_frame: str | None) -> str:
    """Pick the last message frame for this AI and read its text."""
    for frame_sel in split_selectors(message_frame):
        turns = page.locator(frame_sel)
        try:
            count = turns.count()
        except Exception:
            continue
        if count > 0:
            try:
                text = turns.nth(count - 1).inner_text().strip()
                if text:
                    return text
            except Exception:
                pass

    return ""


def send_and_wait_response(page: Page, model_config: dict[str, Any], text: str) -> str:
    selectors = model_config.get("selectors") or {}
    rs = model_config.get("response_settings") or {}
    stability_ms = rs.get("stability_ms", 800)
    streaming_grace_ms = rs.get("streaming_grace_ms", 2500)
    max_wait_ms = rs.get("max_wait_ms", 300000)
    message_frame = resolve_message_frame(model_config)
    if not message_frame:
        raise RuntimeError(
            f"selectors.message_frame is required for model "
            f"{model_config.get('model') or model_config.get('key')}"
        )

    baseline = _read_last_response(page, message_frame)

    fill_prompt(page, selectors, text)
    page.wait_for_timeout(300)
    click_send(page, selectors)

    started = time.time() * 1000
    last_candidate = ""
    stable_since = 0.0
    was_generating = False

    while (time.time() * 1000) - started < max_wait_ms:
        if _is_generating(page, selectors):
            was_generating = True
            stable_since = 0.0
            page.wait_for_timeout(300)
            continue

        current = _read_last_response(page, message_frame)
        if not current or current == baseline:
            page.wait_for_timeout(300)
            continue

        if was_generating:
            was_generating = False
            last_candidate = current
            stable_since = time.time() * 1000
        elif (time.time() * 1000) - started < streaming_grace_ms:
            page.wait_for_timeout(300)
            continue

        if current != last_candidate:
            last_candidate = current
            stable_since = time.time() * 1000
        elif stable_since and (time.time() * 1000) - stable_since >= stability_ms:
            return current

        page.wait_for_timeout(200)

    if last_candidate:
        return last_candidate
    raise TimeoutError("Response timeout")
