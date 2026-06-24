"""Load ai_models_config.json for the Playwright bridge."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "ai_models_config.json")


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_model_by_id(model_id: str | None) -> dict[str, Any] | None:
    config = load_config()
    needle = (model_id or "").lower().strip()
    if not needle:
        return None

    models = config.get("models") or {}

    def _match(resolved: str) -> dict[str, Any] | None:
        for key, model in models.items():
            mid = (model.get("model") or key).lower()
            if mid == resolved or key.lower() == resolved:
                return {"key": key, **model}
        return None

    hit = _match(needle)
    if hit:
        return hit

    # OpenAI-style ids (gpt-4, gpt-4o-mini, …) → platform id in config.
    if needle.startswith("gpt") or needle in ("chatgpt", "openai"):
        return _match("gpt")
    if needle.startswith("claude"):
        return _match("claude")
    if needle.startswith("gemini"):
        return _match("gemini")
    if needle.startswith("deepseek"):
        return _match("deepseek")
    if needle.startswith("grok"):
        return _match("grok")
    if needle in ("copilot", "microsoft-copilot"):
        return _match("copilot")

    return None


def list_models() -> list[dict[str, Any]]:
    config = load_config()
    return [
        {
            "key": key,
            "name": model.get("name"),
            "model": model.get("model") or key,
            "url": model.get("url"),
        }
        for key, model in (config.get("models") or {}).items()
    ]


def get_client_settings() -> dict[str, Any]:
    """Client defaults from ai_models_config.json; env vars override when set."""
    config = load_config()
    client = config.get("client") or {}

    host = os.environ.get("SERVER_HOST", client.get("host", "127.0.0.1"))
    api_port = int(os.environ.get("PORT_API", client.get("api_port", 5000)))
    bridge_port = int(os.environ.get("PORT_EXTENSION", client.get("bridge_port", 3000)))

    default_model = os.environ.get("MODEL_ID", client.get("default_model"))
    if not default_model:
        models = list_models()
        default_model = models[0]["model"] if models else ""

    timeout_raw = os.environ.get("CLIENT_REQUEST_TIMEOUT", client.get("request_timeout"))
    request_timeout: float | None
    if timeout_raw is None or str(timeout_raw).strip().lower() in ("", "none", "null", "unlimited"):
        request_timeout = None
    else:
        request_timeout = float(timeout_raw)

    temperature = float(os.environ.get("CLIENT_TEMPERATURE", client.get("temperature", 0.7)))

    api_key = os.environ.get("API_KEY", client.get("api_key", "trungdeptrai")).strip()

    base = f"http://{host}"
    return {
        "host": host,
        "api_port": api_port,
        "bridge_port": bridge_port,
        "default_model": default_model,
        "temperature": temperature,
        "request_timeout": request_timeout,
        "api_key": api_key,
        "api_base_url": f"{base}:{api_port}",
        "bridge_base_url": f"{base}:{bridge_port}",
    }
