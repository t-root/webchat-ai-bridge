
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue, Empty
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime
from decimal import Decimal
import json
import os
import sys
import threading
import time
import uuid


# ANSI Color codes
class Colors:
    RESET = "\033[0m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BOLD = "\033[1m"


MODEL_ID = os.environ.get("MODEL_ID", "claude")
SERVER_HOST = os.environ.get("SERVER_HOST", "127.0.0.1")
PORT_EXTENSION = int(os.environ.get("PORT_EXTENSION", "3000"))
PORT_API = int(os.environ.get("PORT_API", "5000"))
UPSTREAM_CHAT_COMPLETIONS_URL = os.environ.get("UPSTREAM_CHAT_COMPLETIONS_URL", "").strip()
UPSTREAM_API_KEY = os.environ.get("UPSTREAM_API_KEY", "").strip()
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "60"))


def _parse_wait_seconds(value) -> float | None:
    """0 / None / 'unlimited' → wait forever (returns None). Positive number → timeout in seconds."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("", "none", "unlimited", "inf", "infinite", "0"):
            return None
        return float(v)
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return float(value)
    return None


def load_timeout_settings() -> tuple[float | None, float | None]:
    """Load timeout from ai_models_config.json; env vars can override."""
    bridge_wait = None
    api_bridge = None

    try:
        config_path = os.path.join(os.path.dirname(__file__), "ai_models_config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        timeouts = config.get("timeouts", {})
        bridge_wait = _parse_wait_seconds(timeouts.get("bridge_wait_seconds"))
        api_bridge = _parse_wait_seconds(timeouts.get("api_call_timeout_seconds"))
    except Exception:
        pass

    if os.environ.get("BRIDGE_WAIT_SECONDS") is not None:
        bridge_wait = _parse_wait_seconds(os.environ.get("BRIDGE_WAIT_SECONDS"))

    if os.environ.get("API_BRIDGE_TIMEOUT") is not None:
        api_bridge = _parse_wait_seconds(os.environ.get("API_BRIDGE_TIMEOUT"))

    if api_bridge is None and bridge_wait is not None:
        api_bridge = bridge_wait + 5

    return bridge_wait, api_bridge


def _format_wait_label(seconds: float | None) -> str:
    return "unlimited" if seconds is None else f"{int(seconds)}s"


BRIDGE_WAIT_SECONDS, API_BRIDGE_TIMEOUT = load_timeout_settings()


def load_api_key() -> str:
    """API key for public OpenAI-compatible endpoints; env API_KEY overrides JSON."""
    default = "trungdeptrai"
    try:
        config_path = os.path.join(os.path.dirname(__file__), "ai_models_config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        client = config.get("client") or {}
        key = client.get("api_key")
        if isinstance(key, str) and key.strip():
            default = key.strip()
    except Exception:
        pass
    return os.environ.get("API_KEY", default).strip()


API_KEY = load_api_key()

# Store messages from extension / terminal
messages = []
response_queue: Queue = Queue()
latest_response = None

# API request tracking
pending_api_requests = {}  # request_id -> threading.Event
api_responses = {}         # request_id -> response_text


def json_dumps(data) -> str:
    """Serialize JSON with sane defaults."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    """Read and parse request JSON body."""
    content_length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(content_length) if content_length > 0 else b""
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc


def write_json(handler: BaseHTTPRequestHandler, status_code: int, payload: dict, extra_headers: dict | None = None):
    """Send a JSON response."""
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    if extra_headers:
        for k, v in extra_headers.items():
            handler.send_header(k, str(v))
    handler.end_headers()
    handler.wfile.write(json_dumps(payload).encode("utf-8"))


def extract_api_key(handler: BaseHTTPRequestHandler) -> str | None:
    """Read API key from Authorization Bearer or api-key / x-api-key headers."""
    auth = handler.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for header in ("api-key", "x-api-key"):
        value = handler.headers.get(header)
        if value:
            return value.strip()
    return None


def require_api_key(handler: BaseHTTPRequestHandler) -> bool:
    """Return False and send 401 if the request API key is missing or invalid."""
    token = extract_api_key(handler)
    if token and token == API_KEY:
        return True
    write_json(
        handler,
        401,
        {
            "error": {
                "message": "Incorrect API key provided",
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_api_key",
            }
        },
    )
    return False


def sanitize_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool, Decimal)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def extract_last_user_message(messages_list) -> str | None:
    """Return the last user message content from a chat-completions messages array."""
    if not isinstance(messages_list, list):
        return None
    for msg in reversed(messages_list):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                text = content.strip()
                if text:
                    return text
            elif isinstance(content, list):
                # Multimodal content array, flatten text parts.
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        txt = part.get("text", "")
                        if txt:
                            parts.append(txt)
                text = "\n".join(parts).strip()
                if text:
                    return text
            else:
                text = sanitize_text(content).strip()
                if text:
                    return text
    return None


def build_chat_completion_response(model: str, content: str, request_id: str | None = None, extra: dict | None = None) -> dict:
    """Build a valid OpenAI Chat Completions response."""
    resp = {
        "id": f"chatcmpl-{request_id or uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": max(1, len(content.split())),   # placeholder estimation
            "completion_tokens": max(1, len(content.split())),  # placeholder estimation
            "total_tokens": max(2, len(content.split()) * 2),
        },
    }
    if extra and isinstance(extra, dict):
        resp.update(extra)
    return resp


def build_chat_completion_chunk(model: str, content: str, request_id: str | None = None, final: bool = False) -> dict:
    return {
        "id": f"chatcmpl-{request_id or uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": ({"role": "assistant", "content": content} if not final else {}),
                "finish_reason": ("stop" if final else None),
            }
        ],
    }


def upstream_chat_completions(data: dict) -> tuple[int, dict]:
    """Proxy request to upstream OpenAI-compatible chat-completions endpoint."""
    if not UPSTREAM_CHAT_COMPLETIONS_URL:
        raise RuntimeError("UPSTREAM_CHAT_COMPLETIONS_URL is not configured")

    headers = {
        "Content-Type": "application/json",
    }
    if UPSTREAM_API_KEY:
        headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"

    req = Request(
        UPSTREAM_CHAT_COMPLETIONS_URL,
        data=json_dumps(data).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {"raw": raw}
            return resp.status, payload
    except HTTPError as e:
        raw = e.read().decode("utf-8") if hasattr(e, "read") else ""
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"error": raw or str(e)}
        return e.code, payload
    except URLError as e:
        raise RuntimeError(f"Upstream connection error: {e}") from e


class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP request handler for AI model bridge (Port 3000)."""

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            write_json(self, 200, {
                "status": "ok",
                "messages_received": len(messages),
                "timestamp": datetime.now().isoformat(),
            })
        elif path == "/api/ai/config":
            # Return available models configuration
            try:
                config_path = os.path.join(os.path.dirname(__file__), "ai_models_config.json")
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    # Extract just the models info
                    models_info = {}
                    for platform_key, platform_config in config.get("models", {}).items():
                        models_info[platform_key] = {
                            "name": platform_config.get("name"),
                            "model": platform_config.get("model", platform_key),
                            "url": platform_config.get("url"),
                        }
                    write_json(self, 200, {
                        "models": models_info,
                        "status": "ok"
                    })
            except Exception as e:
                write_json(self, 500, {"error": f"Failed to load config: {str(e)}"})
        elif path == "/api/ai/user-input":
            try:
                user_input = response_queue.get(timeout=0.5)
                write_json(self, 200, {"input": user_input})
            except Exception:
                write_json(self, 200, {"input": None})
        else:
            write_json(self, 404, {"error": "Not found"})

    def do_POST(self):
        global latest_response
        path = urlparse(self.path).path

        try:
            data = read_json_body(self)
        except ValueError as exc:
            write_json(self, 400, {"error": str(exc)})
            return

        if path == "/api/ai/message":
            # Always acknowledge first so the sender does not hang.
            write_json(self, 200, {"ok": True})

            message = {
                "role": data.get("role", "unknown"),
                "content": data.get("content", ""),
                "timestamp": data.get("timestamp", datetime.now().isoformat()),
                "request_id": data.get("request_id"),
            }
            messages.append(message)

            role = message["role"]
            content = sanitize_text(message["content"])

            if role == "user":
                print(f"{Colors.CYAN}👤 [USER]{Colors.RESET} {content}", flush=True)
            elif role == "assistant":
                print(f"\n{Colors.GREEN}🤖 [MODEL RESPONSE]{Colors.RESET}", flush=True)
                print(content, flush=True)
                print(f"{Colors.YELLOW}{'-' * 50}{Colors.RESET}", flush=True)
            else:
                print(f"✓ [{str(role).upper()}] {content}", flush=True)

            request_id = data.get("request_id")
            if request_id:
                pending_ids = list(pending_api_requests.keys())
                print(f"{Colors.CYAN}🔍 [Bridge] Response arrived for {request_id}{Colors.RESET}", flush=True)
                print(f"{Colors.CYAN}   Pending requests: {pending_ids}{Colors.RESET}", flush=True)
                
                if request_id in pending_api_requests:
                    api_responses[request_id] = content
                    pending_api_requests[request_id].set()
                    print(f"{Colors.GREEN}✓ [Bridge] Response matched with request {request_id}{Colors.RESET}", flush=True)
                else:
                    pending_ids = list(pending_api_requests.keys())
                    print(f"{Colors.YELLOW}❌ [Bridge] Request {request_id} NOT found in pending (waiting: {pending_ids}){Colors.RESET}", flush=True)
            else:
                print(f"{Colors.YELLOW}⚠ [Bridge] No request_id in response payload{Colors.RESET}", flush=True)

            if data.get("role") == "assistant":
                latest_response = content
            return

        elif path == "/api/ai/chat-request":
            # Port 5000 API is requesting AI response through Bridge
            # This endpoint receives message and returns raw text response
            
            user_message = data.get("message", "").strip()
            request_id = data.get("request_id", "")
            model = data.get("model", "auto")  # Preferred model from API
            
            if not user_message:
                write_json(self, 400, {"error": "message is required"})
                return
            
            print(f"{Colors.CYAN}📥 [Bridge] Received chat request{Colors.RESET}", flush=True)
            print(f"{Colors.CYAN}   Message: {user_message}{Colors.RESET}", flush=True)
            print(f"{Colors.CYAN}   Request ID: {request_id}{Colors.RESET}", flush=True)
            print(f"{Colors.CYAN}   Preferred model: {model}{Colors.RESET}", flush=True)
            
            # Store the message for background.js or other consumers
            latest_response = None
            response_queue.put({
                "message": user_message,
                "request_id": request_id,
                "model": model,  # Pass preferred model
                "is_api_request": True,
            })
            
            # Wait for response from backend/background.js
            print(f"{Colors.YELLOW}⏳ [Bridge] Waiting for model response...{Colors.RESET}", flush=True)
            
            response_event = threading.Event()
            pending_api_requests[request_id] = response_event
            print(f"{Colors.YELLOW}   → Added to pending_api_requests (total: {len(pending_api_requests)}){Colors.RESET}", flush=True)
            
            wait_label = _format_wait_label(BRIDGE_WAIT_SECONDS)
            print(f"{Colors.YELLOW}   → Waiting up to {wait_label} for AI response...{Colors.RESET}", flush=True)

            if response_event.wait(timeout=BRIDGE_WAIT_SECONDS):
                ai_response = api_responses.pop(request_id, None)
                pending_api_requests.pop(request_id, None)
                print(f"{Colors.YELLOW}   → Event signaled! Removed from pending (remaining: {len(pending_api_requests)}){Colors.RESET}", flush=True)
                
                if ai_response:
                    cleaned = sanitize_text(ai_response).strip()
                    print(f"{Colors.GREEN}✓ [Bridge] Got model response, sending back to Port 5000{Colors.RESET}", flush=True)
                    write_json(self, 200, {"content": cleaned})
                    return
            
            # Timeout (only when BRIDGE_WAIT_SECONDS > 0)
            pending_api_requests.pop(request_id, None)
            api_responses.pop(request_id, None)
            print(f"{Colors.YELLOW}⚠ [Bridge] Model response timeout after {wait_label}{Colors.RESET}", flush=True)
            write_json(self, 504, {"error": f"Backend timeout - no model response received within {wait_label}"})
            return

        write_json(self, 404, {"error": "Not found"})


    def log_message(self, format, *args):
        pass


class APIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for public API (Port 5000)."""

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            write_json(self, 200, {
                "status": "ok",
                "model_id": MODEL_ID,
                "timestamp": datetime.now().isoformat(),
                "upstream_configured": bool(UPSTREAM_CHAT_COMPLETIONS_URL),
            })
            return

        if path == "/v1/models":
            if not require_api_key(self):
                return
            models_data = []
            try:
                config_path = os.path.join(os.path.dirname(__file__), "ai_models_config.json")
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                for platform_key, platform_config in cfg.get("models", {}).items():
                    models_data.append({
                        "id": platform_config.get("model", platform_key),
                        "object": "model",
                        "owned_by": "local",
                    })
            except Exception:
                models_data = [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]
            write_json(self, 200, {"object": "list", "data": models_data})
            return

        write_json(self, 404, {"error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/v1/chat/completions":
            if not require_api_key(self):
                return
            self.handle_openai_chat_completions()
            return

        write_json(self, 404, {"error": "Not found"})

    def handle_openai_chat_completions(self):
        """Handle OpenAI-compatible /v1/chat/completions endpoint."""
        try:
            data = read_json_body(self)
        except ValueError as exc:
            write_json(self, 400, {"error": str(exc)})
            return

        messages_list = data.get("messages", [])
        if not isinstance(messages_list, list) or not messages_list:
            write_json(self, 400, {"error": "messages is required"})
            return

        model = sanitize_text(data.get("model") or MODEL_ID)

        if data.get("stream") is True:
            self._handle_chat_stream(model, messages_list, data)
            return

        response_text = self._generate_assistant_response(messages_list, data)
        completion_response = build_chat_completion_response(model, response_text)
        write_json(self, 200, completion_response)

    def _handle_chat_stream(self, model: str, messages_list, data: dict):
        """Minimal SSE streaming support."""
        response_text = self._generate_assistant_response(messages_list, data)
        request_id = uuid.uuid4().hex

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        chunk = build_chat_completion_chunk(model, response_text, request_id=request_id, final=False)
        self.wfile.write(f"data: {json_dumps(chunk)}\n\n".encode("utf-8"))
        self.wfile.flush()

        final_chunk = build_chat_completion_chunk(model, "", request_id=request_id, final=True)
        self.wfile.write(f"data: {json_dumps(final_chunk)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _generate_assistant_response(self, messages_list, data: dict) -> str:
        """
        Response strategy:
        1) If an upstream OpenAI-compatible model is configured, proxy to it.
        2) Else, send request to Port 3000 (Bridge) and wait for raw text response.
        3) Else, return a simple fallback response so clients always work.
        """
        # 1) Proxy mode (external API like OpenRouter)
        if UPSTREAM_CHAT_COMPLETIONS_URL:
            payload = dict(data)
            payload["model"] = payload.get("model") or MODEL_ID
            status, upstream_payload = upstream_chat_completions(payload)

            # Normalize upstream responses.
            if isinstance(upstream_payload, dict):
                choices = upstream_payload.get("choices")
                if isinstance(choices, list) and choices:
                    first = choices[0] if isinstance(choices[0], dict) else {}
                    message = first.get("message") or {}
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        # Clean up "json\n" prefix if present
                        cleaned = content.strip()
                        if cleaned.lower().startswith('json'):
                            cleaned = cleaned[4:].lstrip()
                        return cleaned
                    # Some APIs return delta or text in other fields
                    if isinstance(first.get("text"), str) and first["text"].strip():
                        return first["text"]
                    if isinstance(upstream_payload.get("output_text"), str) and upstream_payload["output_text"].strip():
                        return upstream_payload["output_text"]

                # If upstream returns an error object, surface a readable fallback.
                if status >= 400:
                    err = upstream_payload.get("error") if isinstance(upstream_payload.get("error"), str) else json.dumps(upstream_payload, ensure_ascii=False)
                    return f"Error from upstream service ({status}): {err}"
            return "Upstream service returned an unrecognized response format."

        # 2) Bridge mode: Send request to Port 3000, get raw text response back
        user_message = extract_last_user_message(messages_list) or ""
        if user_message:
            request_id = str(uuid.uuid4())
            response_event = threading.Event()
            pending_api_requests[request_id] = response_event
            api_responses[request_id] = None

            # Send request to Port 3000 bridge
            print(f"{Colors.YELLOW}📡 [API → Bridge] Sending message to Port 3000...{Colors.RESET}", flush=True)
            print(f"{Colors.YELLOW}   Request ID: {request_id}{Colors.RESET}", flush=True)
            print(f"{Colors.YELLOW}   Message: {user_message}{Colors.RESET}", flush=True)
            
            preferred_model = sanitize_text(data.get("model") or MODEL_ID)
            bridge_response = self._send_to_bridge(user_message, request_id, model=preferred_model)
            
            if bridge_response:
                cleaned = sanitize_text(bridge_response).strip()
                # Clean up "json\n" prefix if present
                if cleaned.lower().startswith('json'):
                    cleaned = cleaned[4:].lstrip()
                print(f"{Colors.GREEN}✓ [Bridge] Received model response{Colors.RESET}", flush=True)
                return cleaned
            
            # Fallback if bridge doesn't respond
            pending_api_requests.pop(request_id, None)
            api_responses.pop(request_id, None)
            echo_response = f"[Fallback] You asked: {user_message}"
            print(f"{Colors.GREEN}🔄 [Bridge Timeout] Returning fallback response{Colors.RESET}", flush=True)
            return echo_response

        return "The request does not contain valid message content."

    def _send_to_bridge(
        self,
        message: str,
        request_id: str,
        model: str | None = None,
        timeout: float | None = None,
    ) -> str | None:
        """Send request to Bridge and wait for model response (timeout=None → wait forever)."""
        if timeout is None:
            timeout = API_BRIDGE_TIMEOUT
        try:
            # Build request to bridge
            bridge_url = f"http://{SERVER_HOST}:{PORT_EXTENSION}/api/ai/chat-request"
            payload = {
                "message": message,
                "request_id": request_id,
                "model": model or MODEL_ID,
            }
            
            req = Request(
                bridge_url,
                data=json_dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            
            print(f"{Colors.YELLOW}   → Calling {bridge_url}{Colors.RESET}", flush=True)
            
            with urlopen(req, timeout=timeout) as resp:
                response_data = json.loads(resp.read().decode("utf-8"))
                content = response_data.get("content", "").strip()
                if content:
                    return content
        except Exception as e:
            print(f"{Colors.YELLOW}⚠ [API] Bridge request failed: {e}{Colors.RESET}", flush=True)
        
        return None



    def log_message(self, format, *args):
        pass





def start_playwright_bridge() -> threading.Thread | None:
    """Start Playwright bridge (bridge/worker.py) unless AUTO_START_BRIDGE=0."""
    if os.environ.get("AUTO_START_BRIDGE", "1").strip() in ("0", "false", "no"):
        return None

    try:
        from bridge import run_bridge_loop, wait_browser_ready
    except ImportError as exc:
        print(
            f"{Colors.YELLOW}⚠ Playwright bridge import failed: {exc}{Colors.RESET}",
            flush=True,
        )
        print(
            f"{Colors.YELLOW}⚠ Run: pip install -r requirements.txt{Colors.RESET}",
            flush=True,
        )
        return None

    os.environ.setdefault("BRIDGE_SERVER_URL", f"http://{SERVER_HOST}:{PORT_EXTENSION}")

    def _run_bridge():
        try:
            run_bridge_loop()
        except Exception as exc:
            print(f"{Colors.YELLOW}⚠ Playwright bridge stopped: {exc}{Colors.RESET}", flush=True)

    def _notify_when_ready(worker: threading.Thread) -> None:
        if wait_browser_ready(timeout=120):
            print(
                f"{Colors.GREEN}✓ Playwright bridge ready — browser window open{Colors.RESET}",
                flush=True,
            )
        elif worker.is_alive():
            print(
                f"{Colors.YELLOW}⚠ Browser chưa sẵn sàng sau 120s — bridge vẫn đang thử...{Colors.RESET}",
                flush=True,
            )
        else:
            print(
                f"{Colors.YELLOW}⚠ Playwright bridge exited during startup — check logs above{Colors.RESET}",
                flush=True,
            )

    thread = threading.Thread(target=_run_bridge, daemon=True, name="playwright-bridge")
    thread.start()

    print(
        f"{Colors.YELLOW}⏳ Playwright browser starting in background...{Colors.RESET}",
        flush=True,
    )
    threading.Thread(
        target=_notify_when_ready,
        args=(thread,),
        daemon=True,
        name="playwright-ready-notifier",
    ).start()
    print(f"{Colors.GREEN}✓ Playwright bridge thread started{Colors.RESET}", flush=True)
    return thread


def run_server(port_extension: int = PORT_EXTENSION, port_api: int = PORT_API):
    """Start bridge + API servers and optional Playwright worker."""
    bridge_server = ThreadingHTTPServer((SERVER_HOST, port_extension), BridgeHandler)
    api_server = ThreadingHTTPServer((SERVER_HOST, port_api), APIHandler)

    bridge_thread = threading.Thread(target=bridge_server.serve_forever, daemon=True)
    api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
    bridge_thread.start()
    api_thread.start()

    playwright_thread = start_playwright_bridge()

    print(f"\n{Colors.BOLD}{Colors.YELLOW}{'=' * 58}{Colors.RESET}", flush=True)
    print(f"{Colors.BOLD}  Local OpenAI-Compatible API Server (Playwright){Colors.RESET}", flush=True)
    print(f"{Colors.BOLD}{Colors.YELLOW}{'=' * 58}{Colors.RESET}", flush=True)
    print(f"{Colors.YELLOW}  🌐 OpenAI-compatible API (Port {port_api}):{Colors.RESET}", flush=True)
    print(f"{Colors.YELLOW}     - GET  /health{Colors.RESET}", flush=True)
    print(f"{Colors.YELLOW}     - GET  /v1/models{Colors.RESET}", flush=True)
    print(f"{Colors.YELLOW}     - POST /v1/chat/completions{Colors.RESET}", flush=True)
    print(f"{Colors.YELLOW}  🔗 Bridge (Port {port_extension}) — Playwright worker{Colors.RESET}", flush=True)
    if UPSTREAM_CHAT_COMPLETIONS_URL:
        print(f"{Colors.YELLOW}  ↪ Upstream proxy: {UPSTREAM_CHAT_COMPLETIONS_URL}{Colors.RESET}", flush=True)
    else:
        print(f"{Colors.YELLOW}  ↪ Upstream proxy: disabled{Colors.RESET}", flush=True)
    print(f"{Colors.YELLOW}  ⏱ Bridge wait: {_format_wait_label(BRIDGE_WAIT_SECONDS)}{Colors.RESET}", flush=True)
    print(f"{Colors.YELLOW}  ⏱ API→Bridge timeout: {_format_wait_label(API_BRIDGE_TIMEOUT)}{Colors.RESET}", flush=True)
    print(f"{Colors.YELLOW}  🔑 API key: required (Bearer token on /v1/*){Colors.RESET}", flush=True)
    print(f"{Colors.BOLD}{Colors.YELLOW}{'=' * 58}{Colors.RESET}", flush=True)
    print(f"\n{Colors.BOLD}🚀 Server running. Use client_test.py to call the API.{Colors.RESET}\n", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n{Colors.GREEN}✓ Server stopped{Colors.RESET}", flush=True)
        if playwright_thread and playwright_thread.is_alive():
            try:
                from bridge import request_stop
                request_stop()
                playwright_thread.join(timeout=5)
            except Exception:
                pass
        bridge_server.shutdown()
        api_server.shutdown()
        sys.exit(0)


if __name__ == "__main__":
    run_server()
