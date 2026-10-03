# API Reference

AI Terminal Bridge runs **two HTTP servers** on localhost:

| Server | Default port | Role | Consumers |
|--------|--------------|------|-----------|
| **Public API** | `5000` | OpenAI-compatible endpoints | Apps, scripts, `client.py` |
| **Bridge** | `3000` | Playwright ↔ server messaging | Playwright bridge (`bridge_worker.py`), internal API calls |

**Base URLs**

```
Public API : http://127.0.0.1:5000
Bridge     : http://127.0.0.1:3000
```

Override ports via env: `PORT_API`, `PORT_EXTENSION`.

**Common behavior**

- `Content-Type`: `application/json; charset=utf-8` for JSON bodies/responses
- CORS: `Access-Control-Allow-Origin: *`
- `OPTIONS` → `204 No Content` (preflight supported on all routes)

---

## Model IDs

| `model` value | Platform | Web URL |
|---------------|----------|---------|
| `claude` | Claude | https://claude.ai |
| `gpt` | ChatGPT | https://chatgpt.com |
| `gemini` | Gemini | https://gemini.google.com |
| `deepseek` | DeepSeek | https://chat.deepseek.com |
| `grok` | Grok | https://grok.com |
| `copilot` | Copilot | https://copilot.microsoft.com |
| `kimi` | Kimi | https://www.kimi.ai |
| `qwen` | Qwen | https://chat.qwen.ai |
| `meta` | Meta AI | https://www.meta.ai |

Default model when omitted: `claude` (`MODEL_ID` env).

---

# Port 5000 — Public API (OpenAI-compatible)

## GET `/health`

Health check for the public API server.

**Request**

```http
GET /health HTTP/1.1
Host: 127.0.0.1:5000
```

**Response `200`**

```json
{
  "status": "ok",
  "model_id": "claude",
  "timestamp": "2026-06-09T12:00:00.000000",
  "upstream_configured": false,
  "stream_enabled": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `"ok"` when server is up |
| `model_id` | string | Default model (`MODEL_ID` env) |
| `timestamp` | string | ISO 8601 local time |
| `upstream_configured` | boolean | `true` if `UPSTREAM_CHAT_COMPLETIONS_URL` is set |
| `stream_enabled` | boolean | `true` if real-time streaming was enabled at startup |

---

## GET `/v1/models`

List models (OpenAI-compatible shape).

**Request**

```http
GET /v1/models HTTP/1.1
Host: 127.0.0.1:5000
```

**Response `200`**

```json
{
  "object": "list",
  "data": [
    {
      "id": "claude",
      "object": "model",
      "owned_by": "local"
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `object` | string | `"list"` |
| `data` | array | Single entry using current `MODEL_ID` |
| `data[].id` | string | Model identifier |
| `data[].object` | string | `"model"` |
| `data[].owned_by` | string | `"local"` |

---

## POST `/v1/chat/completions`

Chat completions (OpenAI-compatible). Routes to web AI via Bridge unless upstream proxy is configured.

**Request**

```http
POST /v1/chat/completions HTTP/1.1
Host: 127.0.0.1:5000
Content-Type: application/json
```

**Body**

```json
{
  "model": "gpt",
  "messages": [
    { "role": "system", "content": "You are a helpful assistant." },
    { "role": "user", "content": "Hello!" }
  ],
  "stream": false,
  "temperature": 0.7,
  "max_tokens": 2048
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `messages` | array | **yes** | Chat history; last `user` message is sent to the web AI |
| `model` | string | no | `claude`, `gpt`, `gemini`, `deepseek` (default: `MODEL_ID`) |
| `stream` | boolean | no | `true` → SSE stream — real-time if enabled at server startup, otherwise one content chunk + `[DONE]` |
| `temperature` | number | no | Passed through when upstream proxy is used |
| `max_tokens` | number | no | Passed through when upstream proxy is used |

**Message object**

```json
{ "role": "user" | "assistant" | "system", "content": "string or multimodal array" }
```

For multimodal `content` (array), only text parts with `"type": "text"` are extracted for the bridge.

**Response `200` (non-stream)**

```json
{
  "id": "chatcmpl-a1b2c3d4e5f6...",
  "object": "chat.completion",
  "created": 1717939200,
  "model": "gpt",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you today?"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 8,
    "total_tokens": 20
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | `chatcmpl-{uuid}` |
| `object` | string | `"chat.completion"` |
| `created` | integer | Unix timestamp |
| `model` | string | Echo of request model |
| `choices[0].message.content` | string | Assistant reply text |
| `choices[0].finish_reason` | string | `"stop"` |
| `usage` | object | **Estimated** token counts (not from web AI) |

**Response `200` (stream)**

`Content-Type: text/event-stream`

```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1717939200,"model":"gpt","choices":[{"index":0,"delta":{"role":"assistant","content":"Full reply text here"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1717939200,"model":"gpt","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

Behaviour depends on the choice made when starting `server.py` (prompt `y` / `n` / Enter, or `--stream`):

- **Stream on:** partial text is forwarded while the web AI is still typing — many `delta.content` chunks, then the final `finish_reason: "stop"` chunk and `[DONE]`. While waiting, the server sends `: keep-alive` SSE comments every 15s. If the page rewrites text already streamed, the remainder of the final reply is appended after a blank line.
- **Stream off** (or upstream proxy configured): waits for the full web AI response, then emits it in one chunk (shown above).

`GET /health` reports the current mode in `stream_enabled`.

**Errors**

| Status | Body | Cause |
|--------|------|-------|
| `400` | `{"error":"messages is required"}` | Missing or empty `messages` |
| `400` | `{"error":"Invalid JSON: ..."}` | Malformed JSON body |
| `404` | `{"error":"Not found"}` | Unknown path |

**Fallback content** (bridge timeout / no extension):

```text
[Fallback] You asked: {user_message}
```

---

# Port 3000 — Bridge API

Used by the Chrome extension and internally by the Public API (`5000` → `3000`).

## GET `/health`

**Request**

```http
GET /health HTTP/1.1
Host: 127.0.0.1:3000
```

**Response `200`**

```json
{
  "status": "ok",
  "messages_received": 42,
  "timestamp": "2026-06-09T12:00:00.000000"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `"ok"` |
| `messages_received` | integer | Total messages logged since server start |
| `timestamp` | string | ISO 8601 local time |

---

## GET `/api/ai/config`

Returns available AI platforms from `ai_models_config.json`.

**Request**

```http
GET /api/ai/config HTTP/1.1
Host: 127.0.0.1:3000
```

**Response `200`**

```json
{
  "models": {
    "claude": {
      "name": "Claude",
      "model": "claude",
      "url_pattern": "https://claude.ai/*"
    },
    "chatgpt": {
      "name": "ChatGPT",
      "model": "gpt",
      "url_pattern": "https://chatgpt.com/*"
    },
    "gemini": {
      "name": "Gemini",
      "model": "gemini",
      "url_pattern": "https://gemini.google.com/*"
    },
    "deepseek": {
      "name": "DeepSeek",
      "model": "deepseek",
      "url_pattern": "https://chat.deepseek.com/*"
    }
  },
  "status": "ok"
}
```

**Response `500`**

```json
{
  "error": "Failed to load config: ..."
}
```

---

## GET `/api/ai/user-input`

Extension polls this endpoint (~every 3s) for pending chat requests.

**Request**

```http
GET /api/ai/user-input HTTP/1.1
Host: 127.0.0.1:3000
```

**Response `200` — queue empty**

```json
{
  "input": null
}
```

**Response `200` — pending request**

```json
{
  "input": {
    "message": "What is 2+2?",
    "request_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "model": "claude",
    "is_api_request": true
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `input` | object \| null | Dequeued item or `null` |
| `input.message` | string | User prompt to inject |
| `input.request_id` | string | Correlates with `/api/ai/message` response |
| `input.model` | string | Target model / platform |
| `input.is_api_request` | boolean | `true` when originated from Public API |
| `input.stream` | boolean | `true` for real-time stream requests — worker posts partial text to `/api/ai/message-chunk` |

---

## POST `/api/ai/message-chunk`

Worker sends the partial reply for a streaming request (only when `input.stream` is `true`). `content` is the **full text so far**, not a delta — the server computes the delta. The final reply still goes to `/api/ai/message`.

```json
{
  "request_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "content": "The answer"
}
```

**Response `200`:** `{"ok": true}`

---

## POST `/api/ai/message`

Extension sends user/assistant messages back to the bridge. Acknowledged immediately; processing continues asynchronously.

**Request**

```http
POST /api/ai/message HTTP/1.1
Host: 127.0.0.1:3000
Content-Type: application/json
```

**Body**

```json
{
  "role": "assistant",
  "content": "The answer is 4.",
  "timestamp": 1717939200123,
  "request_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `role` | string | no | `"user"`, `"assistant"`, or other (default: `"unknown"`) |
| `content` | string | no | Message text |
| `timestamp` | string \| number | no | Client timestamp (default: server ISO time) |
| `request_id` | string | no | **Required for API flow** — matches pending chat request |

**Response `200`**

```json
{
  "ok": true
}
```

When `role` is `"assistant"` and `request_id` matches a pending request, the bridge unblocks `/api/ai/chat-request`.

---

## POST `/api/ai/chat-request`

Internal blocking endpoint: enqueue prompt for extension and wait for assistant reply.

Called by Public API (`5000`), not intended for direct client use.

**Request**

```http
POST /api/ai/chat-request HTTP/1.1
Host: 127.0.0.1:3000
Content-Type: application/json
```

**Body**

```json
{
  "message": "Explain quicksort briefly",
  "request_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "model": "gpt"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | **yes** | User prompt |
| `request_id` | string | no | Correlation ID (UUID generated by API if omitted) |
| `model` | string | no | Preferred model (default: `"auto"`) |

**Response `200` — success**

```json
{
  "content": "Quicksort is a divide-and-conquer sorting algorithm..."
}
```

**Response `400`**

```json
{
  "error": "message is required"
}
```

**Response `504` — timeout**

Only when `bridge_wait_seconds` > 0 in config.

```json
{
  "error": "Backend timeout - no model response received within 120s"
}
```

Default config: `bridge_wait_seconds: 0` → wait unlimited.

---

# End-to-end flow

```
Client                    Port 5000              Port 3000              Extension           Web AI
  |                          |                      |                      |                  |
  | POST /v1/chat/completions|                      |                      |                  |
  |------------------------->|                      |                      |                  |
  |                          | POST /api/ai/chat-request                 |                  |
  |                          |--------------------->|                      |                  |
  |                          |                      | (queue)              |                  |
  |                          |                      | GET /api/ai/user-input                  |
  |                          |                      |<---------------------|                  |
  |                          |                      |                      | inject + send    |
  |                          |                      |                      |----------------->|
  |                          |                      |                      | read response    |
  |                          |                      | POST /api/ai/message |<-----------------|
  |                          |                      |<---------------------|                  |
  |                          |<---------------------| { content }          |                  |
  |<-------------------------| OpenAI JSON          |                      |                  |
```

---

# Quick examples

**Health**

```bash
curl http://127.0.0.1:5000/health
curl http://127.0.0.1:3000/health
```

**List models**

```bash
curl http://127.0.0.1:5000/v1/models
```

**Chat**

```bash
curl -X POST http://127.0.0.1:5000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude","messages":[{"role":"user","content":"Hi"}]}'
```

**Available platforms (bridge)**

```bash
curl http://127.0.0.1:3000/api/ai/config
```

---

# Error format summary

All JSON errors follow:

```json
{
  "error": "Human-readable message"
}
```

| Status | Typical endpoints |
|--------|-------------------|
| `400` | Invalid JSON, missing required fields |
| `404` | Unknown path |
| `500` | Config load failure (`/api/ai/config`) |
| `504` | Bridge timeout (`/api/ai/chat-request`) |
