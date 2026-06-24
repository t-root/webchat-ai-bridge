"""Interactive client for the local OpenAI-compatible API — settings from ai_models_config.json."""

from __future__ import annotations

import json
import sys
from typing import Any

import requests

from bridge.config import get_client_settings, list_models


def _settings() -> dict[str, Any]:
    return get_client_settings()


def _auth_headers(cfg: dict[str, Any]) -> dict[str, str]:
    api_key = cfg.get("api_key", "")
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def call_chat_completions(
    messages: list[dict[str, str]],
    model: str | None = None,
    stream: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    cfg = _settings()
    url = f"{cfg['api_base_url']}/v1/chat/completions"

    payload: dict[str, Any] = {
        "messages": messages,
        "model": model or cfg["default_model"],
        "stream": stream,
        "temperature": cfg["temperature"] if temperature is None else temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    print(f"📤 Calling {url}")
    print(f"📝 Payload: {json.dumps(payload, indent=2, ensure_ascii=False)}")

    response = requests.post(
        url,
        json=payload,
        headers=_auth_headers(cfg),
        timeout=cfg["request_timeout"],
    )
    response.raise_for_status()
    return response.json()


def health_check() -> dict[str, Any]:
    url = f"{_settings()['api_base_url']}/health"
    response = requests.get(url, headers=_auth_headers(_settings()), timeout=10)
    response.raise_for_status()
    return response.json()


def select_model_interactive() -> str:
    models = list_models()
    cfg = _settings()

    if not models:
        print("❌ No models in ai_models_config.json")
        sys.exit(1)

    print("Select model:")
    choice_to_model: dict[str, str] = {}
    default_idx = 1

    for idx, entry in enumerate(models, 1):
        name = entry.get("name") or entry["key"]
        model_id = entry["model"]
        marker = " (default)" if model_id == cfg["default_model"] else ""
        print(f"{idx}. {name} ({model_id}){marker}")
        choice_to_model[str(idx)] = model_id
        if model_id == cfg["default_model"]:
            default_idx = idx

    print()
    prompt = f"Choose (1-{len(models)}) [default {default_idx}]: "
    choice = input(prompt).strip() or str(default_idx)
    selected = choice_to_model.get(choice, cfg["default_model"])
    print(f"\n✓ Using model: {selected}\n")
    return selected


def display_response(response: dict[str, Any], show_full: bool = False) -> str | None:
    try:
        model = response.get("model", "unknown")
        choices = response.get("choices", [])
        if not choices:
            print("❌ No choices in response")
            return None

        first = choices[0]
        content = (first.get("message") or {}).get("content", "").strip()
        usage = response.get("usage") or {}

        if show_full:
            print("\n" + "=" * 60)
            print("📦 FULL API RESPONSE")
            print("=" * 60)
            print(f"Model: {model}")
            print(f"Request ID: {response.get('id', 'N/A')}")
            print(f"Finish Reason: {first.get('finish_reason', 'unknown')}")
            print("-" * 60)
            print("💬 ASSISTANT MESSAGE:")
            print(content)
            print("-" * 60)
            print("📊 USAGE STATS:")
            print(f"  • Prompt tokens: {usage.get('prompt_tokens', 0)}")
            print(f"  • Completion tokens: {usage.get('completion_tokens', 0)}")
            print(f"  • Total tokens: {usage.get('total_tokens', 0)}")
            print("-" * 60)
            print(json.dumps(response, indent=2, ensure_ascii=False))
        else:
            print(f"\n🤖 Assistant ({model}):")
            print(content)
            print(
                f"\n📊 [{usage.get('prompt_tokens', 0)} → "
                f"{usage.get('completion_tokens', 0)} tokens]"
            )

        return content or None
    except Exception as exc:
        print(f"❌ Error displaying response: {exc}")
        print(json.dumps(response, indent=2, ensure_ascii=False))
        return None


def main() -> None:
    cfg = _settings()

    try:
        print("🏥 Checking server health...\n")
        health = health_check()
        print(f"✓ Server is running: {health}\n")
    except Exception as exc:
        print(f"❌ Server is not running: {exc}")
        print(f"   API: {cfg['api_base_url']}")
        print("   Start with: python server.py")
        sys.exit(1)

    print("=" * 60)
    print("Chat Completions API Client")
    print("=" * 60)
    print(f"API: {cfg['api_base_url']}")
    print(f"Default model: {cfg['default_model']}")
    print("\nSelect display mode:")
    print("1. Full API response (JSON + token stats)")
    print("2. Assistant reply only (concise)\n")

    while True:
        mode_choice = input("Choose (1 or 2): ").strip()
        if mode_choice in ("1", "2"):
            show_full = mode_choice == "1"
            break
        print("❌ Please choose 1 or 2\n")

    print("\n" + "=" * 60)
    print("Type a message to chat. Enter 'exit' or 'quit' to stop.\n")

    selected_model = select_model_interactive()
    conversation: list[dict[str, str]] = []

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("exit", "quit"):
            print("Goodbye!")
            break
        if not user_input:
            continue

        conversation.append({"role": "user", "content": user_input})

        try:
            print("\n⏳ Waiting for response...")
            response = call_chat_completions(messages=conversation, model=selected_model)
            assistant_message = display_response(response, show_full=show_full)
            if assistant_message:
                conversation.append({"role": "assistant", "content": assistant_message})
            print()
        except Exception as exc:
            print(f"❌ Error: {exc}\n")


if __name__ == "__main__":
    main()
