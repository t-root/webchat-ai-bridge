"""Load ai_models_config.json for the Playwright bridge."""

from __future__ import annotations

import json
import os
import random
from functools import lru_cache
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "ai_models_config.json")

AUTO_MODEL_IDS = frozenset({"auto", "random"})


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_model_lookup(models: dict[str, Any]) -> tuple[dict[str, tuple[str, dict[str, Any]]], list[tuple[str, str, dict[str, Any]]]]:
    """Build alias lookup tables from models section in JSON."""
    exact: dict[str, tuple[str, dict[str, Any]]] = {}
    prefixes: list[tuple[str, str, dict[str, Any]]] = []

    for key, model in models.items():
        entry = (key, model)
        exact[key.lower()] = entry
        mid = (model.get("model") or key).lower()
        exact[mid] = entry

        for alias in model.get("aliases") or []:
            alias_key = str(alias).lower().strip()
            if alias_key:
                exact[alias_key] = entry

        for prefix in model.get("alias_prefixes") or []:
            prefix_key = str(prefix).lower().strip()
            if prefix_key:
                prefixes.append((prefix_key, key, model))

    return exact, prefixes


def is_auto_model(model_id: str | None) -> bool:
    return (model_id or "").lower().strip() in AUTO_MODEL_IDS


def pick_random_model(exclude_keys: set[str] | None = None) -> dict[str, Any] | None:
    """Pick a random configured model (for model=auto requests)."""
    config = load_config()
    models = config.get("models") or {}
    excluded = exclude_keys or set()
    candidates = [
        (key, model)
        for key, model in models.items()
        if key not in excluded
    ]
    if not candidates:
        return None
    key, model = random.choice(candidates)
    return {"key": key, **model}


def get_retry_settings() -> dict[str, Any]:
    config = load_config()
    retry = config.get("retry") or {}
    return {
        "max_attempts": max(1, int(retry.get("max_attempts", 2))),
        "reload_on_failure": retry.get("reload_on_failure", True) is not False,
    }


def get_model_by_id(model_id: str | None) -> dict[str, Any] | None:
    config = load_config()
    needle = (model_id or "").lower().strip()
    if not needle:
        return None

    models = config.get("models") or {}
    exact, prefixes = _build_model_lookup(models)

    hit = exact.get(needle)
    if hit:
        key, model = hit
        return {"key": key, **model}

    for prefix, key, model in prefixes:
        if needle.startswith(prefix):
            return {"key": key, **model}

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
