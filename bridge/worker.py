"""
Playwright bridge — poll server port 3000, control browser via selectors in config.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from .actions import send_and_wait_response, wait_for_input
from .config import (
    PROJECT_ROOT,
    get_model_by_id,
    get_retry_settings,
    is_auto_model,
    load_config,
    pick_random_model,
)
from .setup import ensure_chromium_installed

SERVER_URL = os.environ.get("BRIDGE_SERVER_URL", "http://127.0.0.1:3000").rstrip("/")
POLL_MS = int(os.environ.get("BRIDGE_POLL_MS", "2000"))
LAUNCH_RETRIES = int(os.environ.get("BROWSER_LAUNCH_RETRIES", "3"))
LAUNCH_RETRY_DELAY_SEC = float(os.environ.get("BROWSER_LAUNCH_RETRY_DELAY", "2"))

_STEALTH_INIT_SCRIPT = """
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true });
  } catch (_) {}

  if (!window.chrome) {
    window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}) };
  }

  const originalQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
  window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters)
  );
})();
"""

_stop_event = threading.Event()
_browser_ready = threading.Event()
_pages: dict[str, Page] = {}
_context_lock = threading.Lock()


def wait_browser_ready(timeout: float | None = 120) -> bool:
    """Block until the browser window is launched (or timeout)."""
    return _browser_ready.wait(timeout)


def request_stop() -> None:
    _stop_event.set()


def _url_origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _fetch_user_input() -> dict[str, Any] | None:
    try:
        res = requests.get(f"{SERVER_URL}/api/ai/user-input", timeout=5)
        if not res.ok:
            return None
        data = res.json()
        return data.get("input")
    except Exception:
        return None


def _post_message(payload: dict[str, Any]) -> bool:
    try:
        res = requests.post(
            f"{SERVER_URL}/api/ai/message",
            json=payload,
            timeout=10,
        )
        return res.ok
    except Exception:
        return False


def _is_launch_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    markers = (
        "executable doesn't exist",
        "already in use",
        "singletonlock",
        "profile",
        "locked",
        "browser closed",
        "connection closed",
        "target closed",
        "failed to launch",
    )
    return any(m in msg for m in markers)


def _close_context_safe(context: BrowserContext | None) -> None:
    if context is None:
        return
    try:
        context.close()
    except Exception:
        pass
    _pages.clear()


def _warm_up_window(context: BrowserContext, browser_cfg: dict[str, Any]) -> None:
    try:
        page = context.pages[0] if context.pages else context.new_page()
        startup_url = (browser_cfg.get("startup_url") or "").strip()
        if startup_url:
            timeout = browser_cfg.get("page_load_timeout_ms", 60000)
            print(f"[Playwright] Opening startup URL: {startup_url}")
            page.goto(startup_url, wait_until="domcontentloaded", timeout=timeout)
        page.bring_to_front()
        print("[Playwright] Browser window ready.")
    except Exception as exc:
        print(f"[Playwright] Warning: could not show browser window: {exc}")


def _context_is_alive(context: BrowserContext | None) -> bool:
    if context is None:
        return False
    try:
        browser = context.browser
        if browser is not None and not browser.is_connected():
            return False
        _ = context.pages
        return True
    except Exception:
        return False


def _profile_path_from_config(browser_cfg: dict[str, Any]) -> str:
    user_data_dir = browser_cfg.get("user_data_dir", "browser-profile")
    profile_path = (
        user_data_dir
        if os.path.isabs(user_data_dir)
        else os.path.join(PROJECT_ROOT, user_data_dir)
    )
    os.makedirs(profile_path, exist_ok=True)
    return profile_path


def _release_profile_lock(profile_path: str, force_kill: bool = False) -> None:
    """Clear stale profile locks when a previous browser session didn't exit cleanly."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"):
        lock_path = os.path.join(profile_path, name)
        try:
            if os.path.lexists(lock_path):
                os.remove(lock_path)
        except OSError:
            pass

    if not force_kill or sys.platform != "win32":
        return

    taskkill = os.path.join(
        os.environ.get("WINDIR", r"C:\Windows"),
        "System32",
        "taskkill.exe",
    )
    if not os.path.isfile(taskkill):
        return

    print("[Playwright] Đang đóng Chrome/Chromium cũ đang giữ profile...")
    for image in ("chrome.exe", "chromium.exe"):
        subprocess.run(
            [taskkill, "/F", "/IM", image, "/T"],
            capture_output=True,
            timeout=15,
        )
    time.sleep(1)


_browser_hidden = False


def configure_browser(*, hidden: bool = False) -> None:
    global _browser_hidden
    _browser_hidden = hidden


def _launch_browser(pw: Playwright) -> BrowserContext:
    config = load_config()
    browser_cfg = config.get("browser") or {}

    user_data_dir = browser_cfg.get("user_data_dir", "browser-profile")
    profile_path = _profile_path_from_config(browser_cfg)

    print("[Playwright] Launching browser (persistent profile)...")
    print(f"[Playwright] Profile: {profile_path}")
    _release_profile_lock(profile_path, force_kill=False)

    channel = (browser_cfg.get("channel") or "").strip()
    headless = _browser_hidden
    if headless:
        print("[Playwright] Browser mode: hidden (headless)")
    else:
        print("[Playwright] Browser mode: visible")
    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "viewport": None,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
        ],
        "ignore_default_args": [
            "--enable-automation",
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
        ],
        "locale": browser_cfg.get("locale", "en-US"),
    }
    if channel:
        launch_kwargs["channel"] = channel
        print(f"[Playwright] Using system browser channel: {channel}")

    last_exc: Exception | None = None
    for attempt in range(1, LAUNCH_RETRIES + 1):
        if attempt > 1:
            _release_profile_lock(profile_path, force_kill=True)
        try:
            context = pw.chromium.launch_persistent_context(profile_path, **launch_kwargs)
            if browser_cfg.get("stealth") is not False:
                context.add_init_script(_STEALTH_INIT_SCRIPT)
            _warm_up_window(context, browser_cfg)
            return context
        except Exception as exc:
            last_exc = exc
            if channel and ("channel" in str(exc).lower() or "chrome" in str(exc).lower()):
                print(
                    f"[Playwright] Không mở được Chrome ({channel}) — "
                    "cài Google Chrome hoặc bỏ browser.channel trong config."
                )
                launch_kwargs.pop("channel", None)
                channel = ""
                continue
            if "Executable doesn't exist" in str(exc):
                ensure_chromium_installed()

            if attempt < LAUNCH_RETRIES and _is_launch_retryable(exc):
                print(
                    f"[Playwright] Launch failed (attempt {attempt}/{LAUNCH_RETRIES}): {exc}"
                )
                if "in use" in str(exc).lower() or "singleton" in str(exc).lower():
                    print(
                        "[Playwright] Profile đang bị giữ — sẽ thử mở khóa và đóng Chrome cũ..."
                    )
                    _release_profile_lock(profile_path, force_kill=True)
                time.sleep(LAUNCH_RETRY_DELAY_SEC)
                continue
            break

    raise RuntimeError(f"Could not launch browser after {LAUNCH_RETRIES} attempts: {last_exc}")


def _ensure_browser(pw: Playwright, context: BrowserContext | None) -> BrowserContext:
    with _context_lock:
        if _context_is_alive(context):
            return context
        if context is not None:
            print("[Playwright] Browser context lost — relaunching...")
            _close_context_safe(context)
        return _launch_browser(pw)


def _get_page_for_model(context: BrowserContext, model_config: dict[str, Any]) -> Page:
    key = model_config["key"]
    url = model_config["url"]

    page = _pages.get(key)
    if page and not page.is_closed():
        try:
            page.bring_to_front()
            return page
        except Exception:
            _pages.pop(key, None)

    origin = _url_origin(url) if url else ""
    existing = None
    for p in context.pages:
        try:
            if p.url.startswith(origin):
                existing = p
                break
        except Exception:
            pass

    if existing:
        page = existing
    else:
        print(f"[Playwright] Opening {model_config.get('name')}: {url}")
        page = context.new_page()
        config = load_config()
        timeout = (config.get("browser") or {}).get("page_load_timeout_ms", 60000)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        page.bring_to_front()

    _pages[key] = page
    config = load_config()
    wait_ms = (config.get("browser") or {}).get("wait_for_input_ms", 90000)
    wait_for_input(page, model_config.get("selectors") or {}, wait_ms)
    return page


def _reload_page_for_model(context: BrowserContext, model_config: dict[str, Any]) -> Page:
    """Reload model page from scratch — clears stuck UI / in-progress generation."""
    key = model_config["key"]
    url = model_config["url"]
    config = load_config()
    browser_cfg = config.get("browser") or {}
    page_load_timeout = browser_cfg.get("page_load_timeout_ms", 60000)
    wait_ms = browser_cfg.get("wait_for_input_ms", 90000)
    selectors = model_config.get("selectors") or {}
    platform = model_config.get("name") or key

    page = _pages.get(key)
    if page and not page.is_closed():
        print(f"[Playwright] Reloading {platform} page (retry after timeout)...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=page_load_timeout)
            page.bring_to_front()
            wait_for_input(page, selectors, wait_ms)
            _pages[key] = page
            return page
        except Exception as exc:
            print(f"[Playwright] Reload failed ({exc}) — opening fresh tab")
            _pages.pop(key, None)
            try:
                page.close()
            except Exception:
                pass

    _pages.pop(key, None)
    return _get_page_for_model(context, model_config)


def _resolve_model_for_request(model_id: str | None) -> dict[str, Any] | None:
    if is_auto_model(model_id):
        picked = pick_random_model()
        if picked:
            platform = picked.get("name") or picked.get("key")
            print(f"[Playwright] Auto model → randomly selected {platform}")
        return picked

    resolved = get_model_by_id(model_id)
    if resolved:
        return resolved

    cfg = load_config()
    models = cfg.get("models") or {}
    first_key = next(iter(models), None)
    if not first_key:
        return None
    print(
        f"[Playwright] Unknown model '{model_id}' — "
        f"falling back to {models[first_key].get('name', first_key)}"
    )
    return {"key": first_key, **models[first_key]}


def _send_with_retry(
    context: BrowserContext,
    model_config: dict[str, Any],
    message: str,
) -> str:
    retry = get_retry_settings()
    max_attempts = retry["max_attempts"]
    reload_on_failure = retry["reload_on_failure"]
    platform = model_config.get("name") or model_config.get("key")
    last_err: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            if attempt > 1 and reload_on_failure:
                page = _reload_page_for_model(context, model_config)
            else:
                page = _get_page_for_model(context, model_config)

            response = send_and_wait_response(page, model_config, message)
            if (response or "").strip():
                return response
            raise TimeoutError("Response timeout (empty result)")
        except TimeoutError as err:
            last_err = err
            if attempt < max_attempts and reload_on_failure:
                print(
                    f"[Playwright] {platform} timeout on attempt {attempt}/{max_attempts} "
                    f"— will reload and retry"
                )
                continue
            raise
        except Exception as err:
            last_err = err
            if attempt < max_attempts and reload_on_failure:
                print(
                    f"[Playwright] {platform} error on attempt {attempt}/{max_attempts}: {err} "
                    f"— will reload and retry"
                )
                continue
            raise

    if last_err:
        raise last_err
    raise TimeoutError("Response timeout")


def _handle_request(context: BrowserContext, input_data: dict[str, Any] | str) -> None:
    if isinstance(input_data, str):
        message = input_data
        request_id = None
        model_id = None
    else:
        message = input_data.get("message", "")
        request_id = input_data.get("request_id")
        model_id = input_data.get("model")

    if not (message or "").strip():
        return

    resolved = _resolve_model_for_request(model_id)
    if not resolved:
        print("[Playwright] No model configured")
        _post_message({
            "role": "assistant",
            "content": "Error: no AI model configured in ai_models_config.json",
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return

    platform = resolved.get("name") or resolved.get("key")
    auto_label = " (auto)" if is_auto_model(model_id) else ""
    print(f"[Playwright] Request: model={model_id or 'default'}{auto_label} → {platform}, id={request_id}")

    try:
        response = _send_with_retry(context, resolved, message.strip())
        _post_message({
            "role": "assistant",
            "content": response,
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        print(f"[Playwright] Response sent ({len(response)} chars)")
    except Exception as err:
        print(f"[Playwright] Error: {err}")
        _post_message({
            "role": "assistant",
            "content": f"Error: {err}",
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


def _run_bridge_session() -> None:
    browser_cfg = load_config().get("browser") or {}
    if not (browser_cfg.get("channel") or "").strip():
        ensure_chromium_installed()
    print(f"[Playwright] Bridge started — polling {SERVER_URL} every {POLL_MS}ms")

    with sync_playwright() as pw:
        context: BrowserContext | None = None
        try:
            context = _ensure_browser(pw, None)
            _browser_ready.set()
            while not _stop_event.is_set():
                try:
                    context = _ensure_browser(pw, context)
                    user_input = _fetch_user_input()
                    if user_input:
                        _handle_request(context, user_input)
                except Exception as err:
                    print(f"[Playwright] Poll error: {err}")
                    if not _context_is_alive(context):
                        context = None
                _stop_event.wait(POLL_MS / 1000)
        finally:
            print("[Playwright] Shutting down...")
            _close_context_safe(context)


def run_bridge_loop() -> None:
    """Poll bridge server and drive Playwright until request_stop() is called."""
    restart_delay = float(os.environ.get("BRIDGE_RESTART_DELAY", "5"))

    while not _stop_event.is_set():
        try:
            _run_bridge_session()
            break
        except Exception as exc:
            if _stop_event.is_set():
                break
            print(f"[Playwright] Bridge crashed: {exc}")
            print(f"[Playwright] Restarting in {restart_delay:.0f}s...")
            _stop_event.wait(restart_delay)
