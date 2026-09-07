# sandbox-server E2E (real containers + headless Chromium)

Proves the full user path against the real stack: published `agent-canvas`
UI driving a freshly built `sandbox-server`, with one cheap live LLM turn.

## Topology

| Service | Source | Ports |
|---|---|---|
| `sandbox-server` | built from `./containers/app/Dockerfile` (coolify branch) | host `${APP_PORT:-3000}` → container `3000` |
| `frontend` | `ghcr.io/openhands/agent-canvas:latest` (pulled) | host `${E2E_FRONTEND_PORT:-8080}` → container `8000` |

Compose file: `e2e/docker-compose.e2e.yml` (standalone, run from repo root).
Project name `sandbox-e2e` prefixes all containers/volumes, so
`down -v` only removes this run's resources (`sandbox-e2e_*`).

## What the spec does (`e2e/tests/full-user-path.spec.ts`)

1. Opens the frontend (served under `/canvas`), opts out of telemetry,
   skips onboarding, opens Manage Backends, types a local backend
   (`http://localhost:3000` + generated session key), submits, waits for the
   new row to become selectable (reachable/green), and selects it.
2. In LLM settings creates provider connection `e2e-openai`
   (`provider=openai`, `base_url=https://opencode.ai/zen/go/v1`, key from
   repo-root `.env` → `api_key`), asserts it is listed.
3. Creates profile `e2e-profile` with model exactly
   `openai/muse-spark-1.3-contributor` linked via `provider_connection_id`
   (no inline `api_key`), asserts it is listed, then activates it.
4. Starts a conversation from the UI, sends one short message requesting the
   word `PING_OK` plus `echo E2E_TRIVIAL_OK`, asserts both tokens appear
   outside the user's own message (UI-first; events API only corroborates).

Headless Chromium via Playwright, explicit waits (no sleeps), max 1 retry.

## Run

```bash
./e2e/run-e2e.sh [--keep] [--no-build]
```

- Throwaway `SESSION_API_KEY`/`OH_SECRET_KEY` are generated per run.
- The LLM key is read at runtime from repo-root `.env` (`api_key=`); the run
  fails fast if it is missing. It lives in memory only and is never printed,
  logged, or written to any file/report (password fields, no tracing, no
  video/trace artifacts).
- Sandbox warm-up: `run-e2e.sh` creates (then deletes) one messageless
  throwaway conversation before the spec so a sandbox is running. Without it
  the frontend's first create fails: it resolves its relative working dir via
  `GET /api/file/home`, which 404s (`No sandbox available`) on a fresh stack
  — a real product gap (fresh user → first Send dies in an error toast). The
  throwaway sends no message, so this costs zero LLM spend.
- Artifacts (`test-results/`, `playwright-report/`) are gitignored.

## Secrets & git hygiene

- Never commit `.env`, keys, or `e2e/` run artifacts. `.env` is already in
  `.gitignore`; `e2e/test-results`, `e2e/playwright-report`, `e2e/node_modules`
  and logs are ignored too.
- Override names/ports via env: `APP_PORT`, `E2E_FRONTEND_PORT`,
  `E2E_CONNECTION_NAME`, `E2E_PROFILE_NAME`, `E2E_MODEL_ID`,
  `E2E_PROVIDER_BASE_URL`.
