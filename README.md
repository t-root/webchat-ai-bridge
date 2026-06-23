# WebChat AI Bridge

**Kết nối Claude, ChatGPT, Gemini, DeepSeek, Grok, Copilot với terminal / script qua AI API OpenAI-compatible.**

Dự án **100% Python** — server API + Playwright bridge. Playwright tự động gửi prompt lên giao diện web của các nền tảng AI, đọc phản hồi, rồi trả về cho ứng dụng qua endpoint `/v1/chat/completions`.

> **4 selector cho mỗi AI.** Mỗi nền tảng khai báo `input`, `send_button`, `stop_button`, `message_frame` trong `ai_models_config.json`. Toàn bộ cấu hình (model, browser, client, timeout) nằm trong một file JSON — không hardcode trong code.

---

## Mục lục

- [Tính năng](#tính-năng)
- [Kiến trúc](#kiến-trúc)
- [Yêu cầu hệ thống](#yêu-cầu-hệ-thống)
- [Cài đặt](#cài-đặt)
- [Hướng dẫn sử dụng](#hướng-dẫn-sử-dụng)
- [API Reference](#api-reference) · [API.md đầy đủ](./API.md)
- [Cấu hình](#cấu-hình)
- [Thêm nền tảng AI mới](#thêm-nền-tảng-ai-mới)
- [Cấu trúc thư mục](#cấu-trúc-thư-mục)
- [Xử lý sự cố](#xử-lý-sự-cố)

---

## Tính năng

| Tính năng | Mô tả |
|-----------|--------|
| **Đa nền tảng** | Claude, ChatGPT, Gemini, DeepSeek, Grok, Copilot (cấu hình qua JSON) |
| **API OpenAI-compatible** | `POST /v1/chat/completions`, `GET /v1/models`, streaming SSE cơ bản |
| **Playwright** | Điều khiển Chromium/Chrome trực tiếp — không cần Chrome Extension |
| **4 selector / AI** | `input`, `send_button`, `stop_button`, `message_frame` — khai báo trong JSON |
| **Action chung** | fill → gửi → chờ stop biến mất → đọc tin assistant cuối qua `message_frame` |
| **Điền prompt thông minh** | Prompt dài (>200 ký tự) vào editor rich-text được paste (Ctrl+V); ngắn hơn thì gõ từng ký tự |
| **Cấu hình tập trung** | Model, browser, client test, timeout — tất cả trong `ai_models_config.json` |
| **Profile lưu đăng nhập** | Session đăng nhập giữ trong `browser-profile/` |
| **Upstream proxy** | Tùy chọn chuyển tiếp sang API bên ngoài (OpenRouter, v.v.) |

---

## Kiến trúc

```
┌─────────────────┐     POST /v1/chat/completions      ┌──────────────────┐
│  client_test.py │ ─────────────────────────────────► │  server.py       │
│  (hoặc app khác)│                                    │  Port 5000 (API) │
└─────────────────┘                                    └────────┬─────────┘
                                                                  │
                                                     POST /api/ai/chat-request
                                                                  ▼
┌─────────────────┐     GET /api/ai/user-input         ┌──────────────────┐
│  Playwright     │ ◄────────────────────────────────► │  server.py       │
│  python -m bridge│    POST /api/ai/message          │  Port 3000       │
│  (Chromium)     │                                    │  (Bridge)        │
└────────┬────────┘                                    └──────────────────┘
         │ fill input / click send / đọc message_frame
         ▼
┌─────────────────┐
│  claude.ai      │
│  chatgpt.com    │
│  gemini.google  │
│  chat.deepseek  │
└─────────────────┘
```

**Luồng hoạt động:**

1. App gọi `POST http://127.0.0.1:5000/v1/chat/completions` với `messages` và `model`.
2. API server chuyển tin sang Bridge (port 3000).
3. Playwright bridge poll `/api/ai/user-input`, mở tab AI tương ứng.
4. Action chung: điền prompt → bấm gửi → chờ nút stop biến mất → lấy phần tử **cuối cùng** khớp `message_frame` → `inner_text()`.
5. Phản hồi POST về Bridge → API trả JSON OpenAI-compatible.

---

## Yêu cầu hệ thống

- **Python** 3.10+
- Tài khoản đã **đăng nhập** trên ít nhất một nền tảng AI
- Không cần Node.js

---

## Cài đặt

### 1. Clone / tải project

```bash
git clone https://github.com/t-root/webchat-ai-bridge.git
cd webchat-ai-bridge
```

### 2. Cài dependencies Python

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 3. Khởi động server

```bash
python server.py
```

Server tự khởi động Playwright bridge. Khi chạy thành công:

- **Port 3000** — Bridge (giao tiếp với Playwright)
- **Port 5000** — API OpenAI-compatible

Lần đầu chạy, Chromium mở ra — **đăng nhập** các nền tảng AI cần dùng. Session được lưu trong `browser-profile/`.

### 4. Chạy bridge thủ công (tùy chọn)

```bash
python -m bridge
```

Hoặc tắt auto-start: `set AUTO_START_BRIDGE=0` rồi chạy `python -m bridge` trong terminal riêng.

### 5. Mở Chrome thủ công (Windows, tùy chọn)

Nếu cần đăng nhập / vượt Cloudflare trước:

```powershell
.\start_chrome.ps1
```

Script mở Chrome với profile `browser-profile/`, sau đó chạy `python server.py`.

---

## Hướng dẫn sử dụng

### Chat từ terminal

```bash
python client_test.py
```

`client_test.py` đọc host, port, model mặc định từ `ai_models_config.json` → section `client`. Danh sách model lấy từ section `models` — không hardcode trong code.

### Gọi API bằng curl

```bash
curl -X POST http://127.0.0.1:5000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude",
    "messages": [
      {"role": "user", "content": "Xin chào!"}
    ]
  }'
```

**Model ID** (theo `model` trong config):

| `model` | Nền tảng |
|---------|----------|
| `claude` | Claude |
| `gpt` | ChatGPT |
| `gemini` | Gemini |
| `deepseek` | DeepSeek |
| `grok` | Grok |
| `copilot` | Copilot |

### Stream mode

Gửi `"stream": true` → server trả `text/event-stream` (SSE). Lưu ý: bridge vẫn **chờ web AI trả lời xong** rồi mới gửi toàn bộ nội dung trong một chunk — không stream token-by-token. Chi tiết: [API.md](./API.md).

---

## API Reference

Hai server chạy song song:

| Server | URL | Ai gọi |
|--------|-----|--------|
| **Public API** | `http://127.0.0.1:5000` | App, script |
| **Bridge** | `http://127.0.0.1:3000` | Playwright bridge |

Chi tiết đầy đủ: [API.md](./API.md)

---

## Cấu hình

Mọi cấu hình chính nằm trong **`ai_models_config.json`**.

### Biến môi trường

Env override giá trị trong JSON khi được set:

| Biến | Override | Mô tả |
|------|----------|--------|
| `SERVER_HOST` | `client.host`, bind server | Host |
| `PORT_EXTENSION` | `client.bridge_port` | Port Bridge |
| `PORT_API` | `client.api_port` | Port API |
| `MODEL_ID` | `client.default_model` | Model mặc định |
| `CLIENT_TEMPERATURE` | `client.temperature` | Temperature cho client test |
| `CLIENT_REQUEST_TIMEOUT` | `client.request_timeout` | Timeout HTTP client (`null` = không giới hạn) |
| `BRIDGE_WAIT_SECONDS` | `timeouts.bridge_wait_seconds` | Thời gian chờ response từ web AI |
| `API_BRIDGE_TIMEOUT` | `timeouts.api_call_timeout_seconds` | Timeout API → Bridge |
| `AUTO_START_BRIDGE` | — | `0` = không tự chạy Playwright |
| `BRIDGE_SERVER_URL` | — | URL bridge cho Playwright worker |
| `UPSTREAM_CHAT_COMPLETIONS_URL` | — | Proxy upstream (bỏ qua Playwright) |

### Cấu trúc `ai_models_config.json`

```json
{
  "models": {
    "chatgpt": {
      "name": "ChatGPT",
      "model": "gpt",
      "url": "https://chatgpt.com",
      "selectors": {
        "input": "#prompt-textarea",
        "send_button": "#composer-submit-button",
        "stop_button": "button[data-testid=\"stop-button\"]",
        "message_frame": "[data-message-author-role=\"assistant\"] .markdown"
      },
      "response_settings": {
        "stability_ms": 800,
        "streaming_grace_ms": 2500
      }
    }
  },
  "browser": {
    "headless": false,
    "channel": "chrome",
    "user_data_dir": "browser-profile",
    "page_load_timeout_ms": 60000,
    "wait_for_input_ms": 300000
  },
  "timeouts": {
    "bridge_wait_seconds": 0,
    "api_call_timeout_seconds": 0
  },
  "client": {
    "host": "127.0.0.1",
    "api_port": 5000,
    "bridge_port": 3000,
    "default_model": "gpt",
    "temperature": 0.7,
    "request_timeout": null
  }
}
```

### 4 selector — cùng cấp trong `selectors`

| Key | Mô tả |
|-----|--------|
| `input` | Ô nhập prompt (textarea hoặc contenteditable) |
| `send_button` | Nút gửi |
| `stop_button` | Nút dừng generate — bridge biết AI còn đang trả lời hay đã xong |
| `message_frame` | Khung tin assistant — lấy phần tử **cuối cùng** khớp selector, đọc `inner_text()` |

**Cách chọn `message_frame`:** trỏ vào phần tử chứa **text trả lời** (ví dụ `.markdown`, `.ds-markdown`). Nếu selector quá rộng (bao gồm nút Copy, Regenerate…), `inner_text()` sẽ lẫn text UI — thu hẹp selector.

Nhiều selector trong một key (cách nhau bằng dấu phẩy) — thử lần lượt cho đến khi khớp (hữu ích cho đa ngôn ngữ UI).

**`response_settings` (tùy chọn):**

| Key | Mặc định | Mô tả |
|-----|----------|--------|
| `stability_ms` | `800` | Đợi text ổn định trước khi trả về |
| `streaming_grace_ms` | `2500` | Grace period sau khi nút stop biến mất |
| `max_wait_ms` | `300000` | Timeout tối đa chờ response |

**`timeouts`:** `0` hoặc `null` = chờ không giới hạn cho đến khi web AI trả lời xong.

---

## Thêm nền tảng AI mới

1. Mở trang web AI, F12 → copy selector cho **input**, **nút gửi**, **nút stop** (khi AI đang generate), **khung tin assistant** (text/markdown).
2. Thêm block mới vào `ai_models_config.json`:

```json
"newai": {
  "name": "New AI",
  "model": "newai",
  "url": "https://newai.com",
  "selectors": {
    "input": "#chat-input",
    "send_button": "button.send",
    "stop_button": "button[aria-label*=\"Stop\"]",
    "message_frame": ".assistant-reply .markdown"
  },
  "response_settings": {
    "stability_ms": 800,
    "streaming_grace_ms": 2500
  }
}
```

3. Khởi động lại server. Lần đầu mở tab, đăng nhập nếu cần.

Action chung trong `bridge/actions.py` tự xử lý:

- `textarea`/`input` → `fill()` (điền thẳng, mọi độ dài)
- `contenteditable` → gõ từng ký tự nếu prompt **≤ 200 ký tự**; nếu **> 200 ký tự** thì copy vào clipboard rồi **paste (Ctrl+V)** để tránh chờ quá lâu
- Click send → chờ `stop_button` biến mất → `page.locator(message_frame).last` → `inner_text()`

Không cần sửa code bridge nếu chỉ thêm model mới với 4 selector đúng.

---

## Cấu trúc thư mục

```
webchat-ai-bridge/
├── ai_models_config.json   # models + browser + timeouts + client
├── requirements.txt
├── server.py               # Bridge (3000) + API (5000)
├── bridge/
│   ├── worker.py           # Poll bridge, quản lý browser
│   ├── actions.py          # fill, click, đọc message_frame
│   └── config.py           # Load config, list_models, get_client_settings
├── client_test.py          # Client mẫu — load config từ JSON
├── start_chrome.ps1        # Mở Chrome + profile (Windows)
├── browser-profile/        # Session Chromium (tự tạo)
├── API.md
└── README.md
```

---

## Xử lý sự cố

### Playwright không khởi động

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### API trả `[Fallback] You asked: ...`

- Playwright bridge chưa chạy hoặc chưa nhận response
- Kiểm tra đã đăng nhập trong `browser-profile`
- Xem log terminal khi chạy `python server.py`

### Prompt không gửi được

- Selector lỗi thời — test trong DevTools: `document.querySelector('...')`
- Tăng `browser.wait_for_input_ms` trong config
- Prompt rất dài trên editor rich-text: bridge tự paste khi >200 ký tự; nếu vẫn lỗi, thử rút gọn prompt hoặc đăng nhập lại trang AI

### Phản hồi bị cắt hoặc đọc sai

- Kiểm tra `stop_button` — selector phải khớp khi AI đang generate
- Tăng `stability_ms` hoặc `streaming_grace_ms` trong `response_settings`
- Kiểm tra `message_frame` — trỏ vào phần text thuần, không bao gồm nút UI

### Cloudflare / chưa đăng nhập

- Hoàn thành xác minh thủ công trong cửa sổ browser
- Windows: chạy `start_chrome.ps1` trước, đăng nhập, rồi `python server.py`

---

## Lưu ý pháp lý

Chỉ dùng cho mục đích cá nhân / nghiên cứu. Tự động hóa giao diện web có thể vi phạm Terms of Service của các nền tảng AI.

---

## Tóm tắt nhanh

```bash
git clone https://github.com/t-root/webchat-ai-bridge.git
cd webchat-ai-bridge
pip install -r requirements.txt
python -m playwright install chromium
python server.py
# Terminal khác:
python client_test.py
```

**Nhớ:** mỗi AI = `url` + **4 selector** (`input`, `send_button`, `stop_button`, `message_frame`). Cấu hình client/browser/timeout cũng trong `ai_models_config.json`.
