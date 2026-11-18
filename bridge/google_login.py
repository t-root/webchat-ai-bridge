"""Kiểm tra trình duyệt đã đăng nhập Google chưa; nếu chưa thì tự đăng nhập từ login-gg.txt."""

from __future__ import annotations

import os
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page

from .config import PROJECT_ROOT

LOGIN_FILE = os.path.join(PROJECT_ROOT, "login-gg.txt")
CHECK_URL = (
    "https://accounts.google.com/ServiceLogin?hl=en"
    "&continue=https%3A%2F%2Fmyaccount.google.com%2F"
)
STEP_TIMEOUT_MS = 20000
MANUAL_WAIT_MS = 120000  # chờ người dùng xử lý 2FA / captcha


def _read_credentials() -> tuple[str, str] | None:
    """Đọc 2 dòng `account=...` và `password=...` từ login-gg.txt."""
    if not os.path.isfile(LOGIN_FILE):
        return None
    values: dict[str, str] = {}
    with open(LOGIN_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip().lower()] = value.strip()
    account = values.get("account", "")
    password = values.get("password", "")
    if not account or not password:
        return None
    return account, password


def _is_logged_in(page: Page) -> bool:
    """Vào myaccount.google.com: nếu chưa đăng nhập sẽ bị chuyển sang accounts.google.com."""
    page.goto(CHECK_URL, wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS * 2)
    page.wait_for_timeout(1500)
    host = urlparse(page.url).netloc
    return host == "myaccount.google.com"


def _login(page: Page, account: str, password: str) -> bool:
    # Trang "chọn tài khoản" → bấm "Use another account"
    another = page.get_by_text("Use another account")
    if another.count() and another.first.is_visible():
        another.first.click()

    email_box = page.locator(
        'input#identifierId, input[name="identifier"], input[type="email"]'
    ).first
    try:
        email_box.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    except Exception:
        print(f"[Google] Không thấy ô email. URL hiện tại: {page.url}")
        raise
    email_box.fill(account)
    page.keyboard.press("Enter")

    pass_box = page.locator('input[type="password"]:visible')
    pass_box.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    pass_box.fill(password)
    page.keyboard.press("Enter")

    # Chờ về myaccount (đăng nhập xong). Nếu dính 2FA/captcha, để người dùng tự xử lý trong cửa sổ.
    try:
        page.wait_for_url("https://myaccount.google.com/**", timeout=MANUAL_WAIT_MS)
        return True
    except Exception:
        return False


def ensure_google_login(context: BrowserContext) -> bool:
    """Trả về True nếu đã đăng nhập. Không bao giờ ném lỗi — đây chỉ là bước phụ trợ."""
    page: Page | None = None
    try:
        # Dùng luôn tab đầu tiên của Chrome làm tab kiểm tra/đăng nhập (không mở tab thừa).
        page = context.pages[0] if context.pages else context.new_page()
        if _is_logged_in(page):
            print("[Google] Đã đăng nhập Google — bỏ qua.")
            return True

        creds = _read_credentials()
        if not creds:
            print("[Google] Chưa đăng nhập, và login-gg.txt chưa có account/password — bỏ qua.")
            return False

        print("[Google] Chưa đăng nhập — đang đăng nhập từ login-gg.txt...")
        if _login(page, *creds):
            print("[Google] Đăng nhập Google thành công.")
            return True
        else:
            print(
                "[Google] Chưa đăng nhập được (có thể cần xác minh 2 bước/captcha, "
                "hoặc sai tài khoản/mật khẩu)."
            )
    except Exception as exc:
        print(f"[Google] Lỗi khi kiểm tra/đăng nhập Google: {exc}")
    return False
