# Local-Protocol Adapter

Exposes the agent-canvas "local protocol" (the modern `openhands-agent-server`
HTTP/WebSocket API surface) on top of this app_server's existing `/api/v1`
surface and its per-session sandboxes.

## Why this exists

The app_server is a control plane. Each conversation's actual agent runs in a
per-session sandbox container that runs the modern `openhands-agent-server`
(which already speaks the local protocol: `/server_info`, `/api/conversations`,
`/api/conversations/{id}/events`, `/sockets/events/{id}`, `/api/git/*`,
`/api/file/*`, `/api/settings`, ...). The browser (agent-canvas) should see ONE
origin (this server) speaking the local protocol, while this server routes each
conversation's traffic to the right sandbox internally. This preserves
"browser only talks to the proxy" while keeping real per-session isolation.

## Architecture

```
browser (agent-canvas)
   │  local protocol over one origin
   ▼
local_protocol router (mounted at "/")
   │
   ├── global endpoints: serve from app_server services directly
   │     /server_info, /api/settings(+schemas+secrets), /api/llm/*, /api/profiles/*
   │
   ├── conversation lifecycle: via app_server conversation/sandbox services
   │     POST /api/conversations  (start -> poll until READY -> return handle)
   │     GET /api/conversations, /api/conversations/search, /api/conversations/{id}
   │     DELETE /api/conversations/{id}
   │
   └── conversation-scoped runtime: PROXY to the sandbox's agent-server
         /api/conversations/{id}/events (GET/search/count, POST = send message)
         /api/conversations/{id}/git/changes, /git/diff
         /api/conversations/{id}/file...
         /sockets/events/{conversation_id}      (WebSocket bridge)
         /sockets/bash-events                   (WebSocket bridge)
```

## Core rule: conversation_url rewriting

The app_server returns `conversation_url = <sandbox agent-server url>/api/conversations/{id.hex}`
and a per-sandbox `session_api_key`. The adapter MUST rewrite every
`conversation_url` it returns to the browser to point at THIS origin:

```
<external base>/api/conversations/{id.hex}
```

where `<external base>` is the adapter's externally-reachable base URL
(derived from the request host / config, e.g. `http://<host>:3000`). The
`session_api_key` is passed through unchanged — the adapter uses it as
`X-Session-API-Key` when proxying to the sandbox. agent-canvas derives its REST
base and its WebSocket URL (`/sockets/events/{id}`) from `conversation_url`, so
rewriting it to this origin is what keeps the browser on the single origin.

## Conversation -> sandbox resolution

Reuse the existing services:
- `AppConversationInfoService` (DB row `sandbox_id`) to map
  `conversation_id -> sandbox_id`.
- `SandboxService.get_sandbox(sandbox_id)` -> `SandboxInfo` which carries
  `session_api_key` and `exposed_urls` (the `AGENT_SERVER` exposed URL = the
  sandbox's agent-server base URL).
- If the sandbox is PAUSED, `resume_sandbox` first (mirroring
  `live_status_app_conversation_service`).

## Auth

Require `X-Session-API-Key` on the adapter routes, validated against the
configured global session key (the same `SESSION_API_KEY` env that the app
server's `get_dependencies()` uses). agent-canvas sends this header on every
call. `/server_info` should stay unauthenticated (the frontend probes it
before auth is known, mirroring the modern agent-server which leaves
`/server_info` unprotected).

## Endpoint mapping table

| Local protocol | Source |
|---|---|
| `GET /server_info` | Synthesize: `{version: <agent-server version>, sdk_version: ..., usable_tools: [...], ...}`. Must report version >= 1.28.0 (agent-canvas floor). |
| `GET/PATCH /api/settings` | Map to `GET /api/v1/settings` (response) and `POST /api/v1/settings` with `*_diff` payload (write). |
| `GET /api/settings/agent-schema`, `conversation-schema` | Proxy to `/api/v1/settings/agent-schema` / `conversation-schema`. |
| `GET /api/settings/secrets`, `GET /api/settings/secrets/{name}`, `PUT /api/settings/secrets`, `DELETE /api/settings/secrets/{name}` | Map to `/api/v1/secrets` (search/create), `PUT /api/v1/secrets/{id}`, `DELETE /api/v1/secrets/{id}`. |
| `GET /api/llm/providers`, `GET /api/llm/models`, `GET /api/llm/models/verified` | Map to `/api/v1/config/providers/search` and `/api/v1/config/models/search`. |
| `GET/POST/DELETE /api/profiles/{name}`, `/api/profiles` | Map to `/api/v1/settings/profiles*`. |
| `POST /api/conversations` | Start via app_server conversation service; poll until READY; return local `ConversationInfo` with rewritten `conversation_url`. |
| `GET /api/conversations`, `GET /api/conversations/search`, `GET /api/conversations/{id}`, `DELETE /api/conversations/{id}` | Map to `/api/v1/app-conversations*`, rewriting `conversation_url`. |
| `GET /api/conversations/{id}/events/search`, `/events/count`, `GET /api/conversations/{id}/events` | Proxy to the sandbox agent-server. |
| `POST /api/conversations/{id}/events` (send message) | Proxy to sandbox agent-server `POST /api/conversations/{id}/events`. |
| `GET /api/conversations/{id}/git/changes`, `/git/diff` | Proxy to sandbox agent-server. |
| `GET /api/file/home`, file endpoints | Proxy to sandbox agent-server. |
| `GET /api/skills/search`, MCP endpoints | Map from `/api/v1/skills*` / MCP router. |
| WS `/sockets/events/{conversation_id}` | Bridge to sandbox agent-server WS at `{sandbox-base}/sockets/events/{conversation_id}` with the sandbox session key. |
| WS `/sockets/bash-events` | Bridge to sandbox agent-server WS at `{sandbox-base}/sockets/bash-events`. |

## Testing

- Unit tests for pure logic (URL rewriting, conversation->sandbox resolution
  mapping, settings/secrets/config shape mapping) run without Docker.
- End-to-end: run this app (uvicorn), start a conversation, and verify the
  browser (agent-canvas) can boot (settings + server_info), create a
  conversation, and stream events over the bridged WebSocket.
