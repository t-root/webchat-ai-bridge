"""Ensure Playwright Chromium browser is installed."""

from __future__ import annotations

import os
import subprocess
import sys
import threading

_install_lock = threading.Lock()
_chromium_ready = False


def _chromium_executable_path() -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
            return path if path and os.path.isfile(path) else None
    except Exception:
        return None


def _verify_chromium_launch() -> bool:
    """Quick launch test — catches corrupt or incomplete browser downloads."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception as exc:
        print(f"[Playwright] Chromium launch test failed: {exc}")
        return False


def _playwright_cli(*args: str) -> tuple[list[str], dict[str, str] | None]:
    """Command for the Playwright CLI.

    In a frozen exe sys.executable is the exe itself, so `-m playwright` won't work —
    call the bundled Node driver directly instead.
    """
    if getattr(sys, "frozen", False):
        from playwright._impl._driver import compute_driver_executable, get_driver_env

        return [*compute_driver_executable(), *args], get_driver_env()
    return [sys.executable, "-m", "playwright", *args], None


def install_chromium() -> None:
    """Download Chromium via `python -m playwright install chromium`."""
    print("[Playwright] Chromium chưa có — đang tải tự động (lần đầu có thể mất vài phút)...")
    cmd, env = _playwright_cli("install", "chromium")
    subprocess.run(cmd, check=True, env=env)

    if sys.platform == "linux":
        print("[Playwright] Installing Linux system dependencies...")
        cmd, env = _playwright_cli("install-deps", "chromium")
        subprocess.run(cmd, check=False, env=env)

    print("[Playwright] Chromium đã cài xong.")


def ensure_chromium_installed() -> None:
    """Install Chromium if missing or broken."""
    global _chromium_ready

    if _chromium_ready:
        return

    with _install_lock:
        if _chromium_ready:
            return

        if _chromium_executable_path() and _verify_chromium_launch():
            _chromium_ready = True
            return

        if _chromium_executable_path():
            print("[Playwright] Chromium binary found but launch failed — reinstalling...")

        install_chromium()

        if not _chromium_executable_path():
            raise RuntimeError(
                "Chromium vẫn chưa sẵn sàng sau khi cài. "
                "Thử chạy thủ công: python -m playwright install chromium"
            )

        if not _verify_chromium_launch():
            raise RuntimeError(
                "Chromium đã tải nhưng không khởi động được. "
                "Đóng mọi cửa sổ Chromium cũ rồi thử lại."
            )

        _chromium_ready = True
