"""Unified Playwright actions — driven by selectors from config."""

from __future__ import annotations

import time
from typing import Any, Callable

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


def resolve_stream_frame(model_config: dict[str, Any]) -> str | None:
    """Selector for the in-progress assistant message; defaults to message_frame."""
    selectors = model_config.get("selectors") or {}
    stream_frame = selectors.get("stream_frame")
    if isinstance(stream_frame, str) and stream_frame.strip():
        return stream_frame.strip()
    return resolve_message_frame(model_config)


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


def _clear_input_field(page: Page) -> None:
    page.keyboard.press("Control+a")
    page.keyboard.press("Backspace")


def _fill_via_paste(page: Page, text: str) -> None:
    """Paste into rich editors — avoids Trusted Types / innerHTML restrictions (Gemini, Claude, etc.)."""
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
        _fill_via_paste(page, text)

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
        loc = page.locator(sel)
        try:
            count = loc.count()
            for i in range(count):
                if loc.nth(i).is_visible():
                    return True
        except Exception:
            pass
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


def _frame_counts(page: Page, frame: str | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for frame_sel in split_selectors(frame):
        try:
            counts[frame_sel] = page.locator(frame_sel).count()
        except Exception:
            counts[frame_sel] = 0
    return counts


def _read_new_response(page: Page, frame: str | None, baseline_counts: dict[str, int]) -> str:
    """Like _read_last_response, but only from frames added after baseline_counts was taken.

    Old replies can change after sending (e.g. ChatGPT drops their suggestion chips),
    so comparing text alone would stream the previous answer.
    """
    for frame_sel in split_selectors(frame):
        turns = page.locator(frame_sel)
        try:
            count = turns.count()
        except Exception:
            continue
        if count > baseline_counts.get(frame_sel, 0):
            try:
                text = turns.nth(count - 1).inner_text().strip()
                if text:
                    return text
            except Exception:
                pass

    return ""


def send_and_wait_response(
    page: Page,
    model_config: dict[str, Any],
    text: str,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """Send a prompt and wait for the full reply.

    on_progress (optional) receives the partial reply text each time it changes
    while the model is still generating — used for real-time streaming.
    """
    selectors = model_config.get("selectors") or {}
    rs = model_config.get("response_settings") or {}
    stability_ms = rs.get("stability_ms", 800)
    streaming_grace_ms = rs.get("streaming_grace_ms", 2500)
    force_complete_ms = rs.get("force_complete_ms", 12000)
    max_wait_ms = rs.get("max_wait_ms", 300000)
    message_frame = resolve_message_frame(model_config)
    if not message_frame:
        raise RuntimeError(
            f"selectors.message_frame is required for model "
            f"{model_config.get('model') or model_config.get('key')}"
        )

    baseline = _read_last_response(page, message_frame)
    stream_frame = resolve_stream_frame(model_config) if on_progress else None
    stream_baseline_counts = _frame_counts(page, stream_frame) if stream_frame else {}
    last_progress = ""

    def report_progress() -> None:
        nonlocal last_progress
        partial = _read_new_response(page, stream_frame, stream_baseline_counts)
        if not partial or partial == last_progress:
            return
        last_progress = partial
        try:
            on_progress(partial)
        except Exception as exc:
            print(f"[Playwright] Stream callback error: {exc}")

    fill_prompt(page, selectors, text)
    page.wait_for_timeout(300)
    click_send(page, selectors)

    started = time.time() * 1000
    last_candidate = ""
    last_change_ms = 0.0
    first_nonempty_ms = 0.0
    last_log_ms = started
    saw_generating = False

    while (time.time() * 1000) - started < max_wait_ms:
        now = time.time() * 1000
        generating = _is_generating(page, selectors)
        if on_progress:
            report_progress()

        if generating:
            saw_generating = True
            last_change_ms = now
            page.wait_for_timeout(300)
            continue

        current = _read_last_response(page, message_frame)
        if not current or current == baseline:
            page.wait_for_timeout(300)
            continue

        if current != last_candidate:
            last_candidate = current
            last_change_ms = now
            if not first_nonempty_ms:
                first_nonempty_ms = now

        idle_ms = now - last_change_ms
        grace = 0 if saw_generating else streaming_grace_ms
        if idle_ms >= grace + stability_ms:
            return last_candidate

        if (
            last_candidate
            and first_nonempty_ms
            and (now - first_nonempty_ms) >= force_complete_ms
            and idle_ms >= 300
        ):
            print("[Playwright] Force-completing response after UI tail updates")
            return last_candidate

        if now - last_log_ms >= 30000:
            print(
                f"[Playwright] Waiting for stable response "
                f"({int(idle_ms)}ms idle, {len(last_candidate)} chars)..."
            )
            last_log_ms = now

        page.wait_for_timeout(200)

    if last_candidate:
        return last_candidate
    raise TimeoutError("Response timeout")
