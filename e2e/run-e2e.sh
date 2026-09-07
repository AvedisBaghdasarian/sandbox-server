#!/usr/bin/env bash
# Full user-path E2E runner: compose up -> Playwright (headless Chromium) -> down.
#
#   ./e2e/run-e2e.sh [--keep] [--no-build]
#
# Flags:
#   --keep      Leave the stack running after the spec (debugging). Default tears down.
#   --no-build  Skip `docker compose build` (reuse the previous e2e image).
#
# Secrets handling:
#   - The LLM api key is read from the repo-root `.env` (`api_key=` line) into
#     memory only, exported for the spec process, and NEVER printed, logged,
#     or written to any file. Shell tracing stays off for the whole run.
#   - SESSION_API_KEY / OH_SECRET_KEY are throwaway values generated fresh for
#     every run (never reused, never printed).
set -uo pipefail
# NOTE: no `set -x` anywhere in this script on purpose (secret hygiene).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

KEEP=0
NO_BUILD=0
for arg in "$@"; do
  case "${arg}" in
    --keep) KEEP=1 ;;
    --no-build) NO_BUILD=1 ;;
    *) echo "Unknown flag: ${arg} (expected --keep and/or --no-build)" >&2; exit 2 ;;
  esac
done

PROJECT=sandbox-e2e
APP_PORT="${APP_PORT:-3000}"
FRONTEND_PORT="${E2E_FRONTEND_PORT:-8080}"
BACKEND_URL="http://localhost:${APP_PORT}"
FRONTEND_URL="http://localhost:${FRONTEND_PORT}"

# --- Secrets (memory only; never echoed) ------------------------------------
E2E_LLM_API_KEY="$(sed -n 's/^api_key=//p' .env 2>/dev/null | head -n 1 | tr -d '\r\n ')"
if [ -z "${E2E_LLM_API_KEY}" ]; then
  echo "FAIL: repo-root .env is missing the 'api_key' entry; refusing to run." >&2
  exit 1
fi
SESSION_API_KEY="$(openssl rand -hex 32)"
OH_SECRET_KEY="$(openssl rand -hex 32)"
export SESSION_API_KEY OH_SECRET_KEY
export E2E_LLM_API_KEY
export E2E_SESSION_KEY="${SESSION_API_KEY}"
# Bridge the LLM credential into sandboxes (belt-and-braces alongside the
# shim's materialize-on-create in local_protocol/router.py, which resolves a
# linked provider_connection_id into inline key/base_url before forwarding).
# The agent-server's litellm honors OPENAI_API_KEY/OPENAI_API_BASE env, which
# OH_AGENT_SERVER_ENV forwards into every sandbox. Same key value the UI
# stores on the provider connection; memory-only, never printed, never
# written to any file.
export E2E_PROVIDER_BASE_URL="${E2E_PROVIDER_BASE_URL:-https://opencode.ai/zen/go/v1}"
export E2E_FRONTEND_URL="${FRONTEND_URL}"
export E2E_BACKEND_URL="${BACKEND_URL}"
export E2E_CONNECTION_NAME="${E2E_CONNECTION_NAME:-e2e-openai}"
export E2E_PROFILE_NAME="${E2E_PROFILE_NAME:-e2e-profile}"
export E2E_MODEL_ID="${E2E_MODEL_ID:-openai/muse-spark-1.3-contributor}"
export OH_AGENT_SERVER_ENV="{\"OPENAI_API_KEY\":\"${E2E_LLM_API_KEY}\",\"OPENAI_API_BASE\":\"${E2E_PROVIDER_BASE_URL}\",\"OPENAI_BASE_URL\":\"${E2E_PROVIDER_BASE_URL}\"}"

COMPOSE=(docker compose --project-name "${PROJECT}" -f e2e/docker-compose.e2e.yml)

teardown() {
  if [ "${KEEP}" -eq 1 ]; then
    echo "Keeping stack running (--keep). Tear down with:"
    echo "  docker compose --project-name ${PROJECT} -f e2e/docker-compose.e2e.yml down -v"
    return
  fi
  echo "Tearing down test-scoped stack (project ${PROJECT})..."
  # Only this run's project resources (prefixed ${PROJECT}_*) are removed.
  "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
  # Remove sandbox containers spawned by this run only: containers that did
  # not exist before stack-up AND carry the sandbox label. Everything else
  # (including other stacks' sandboxes) is left untouched.
  if [ -n "${PREEXISTING_CONTAINERS:-}" ]; then
    KNOWN=" ${PREEXISTING_CONTAINERS} "
  else
    KNOWN=" "
  fi
  for id in $(docker ps -aq --filter "label=sandbox_spec_id" 2>/dev/null || true); do
    case "${KNOWN}" in
      *" ${id} "*) ;;
      *) docker rm -f "${id}" >/dev/null 2>&1 || true ;;
    esac
  done
}
trap teardown EXIT

# --- Build / pull -------------------------------------------------------------
if [ "${NO_BUILD}" -eq 0 ]; then
  echo "Building sandbox-server image (coolify branch Dockerfile)..."
  "${COMPOSE[@]}" build sandbox-server
fi
echo "Pulling frontend image (ghcr.io/openhands/agent-canvas:latest)..."
docker pull ghcr.io/openhands/agent-canvas:latest

echo "Starting stack (project ${PROJECT})..."
SESSION_API_KEY="${SESSION_API_KEY}" OH_SECRET_KEY="${OH_SECRET_KEY}" \
  APP_PORT="${APP_PORT}" E2E_FRONTEND_PORT="${FRONTEND_PORT}" \
  "${COMPOSE[@]}" up -d

# --- Health waits (poll, no sleeps longer than the interval) ------------------
echo "Waiting for sandbox-server ${BACKEND_URL}/health ..."
deadline=$((SECONDS + 300))
until curl -fsS "${BACKEND_URL}/health" >/dev/null 2>&1; do
  if [ "${SECONDS}" -ge "${deadline}" ]; then
    echo "FAIL: sandbox-server did not become healthy in 300s." >&2
    "${COMPOSE[@]}" logs --tail=50 sandbox-server 2>/dev/null || true
    exit 1
  fi
  sleep 5
done
echo "sandbox-server healthy."

echo "Waiting for frontend ${FRONTEND_URL} ..."
deadline=$((SECONDS + 240))
until curl -fsS "${FRONTEND_URL}" >/dev/null 2>&1; do
  if [ "${SECONDS}" -ge "${deadline}" ]; then
    echo "FAIL: frontend did not become reachable in 240s." >&2
    "${COMPOSE[@]}" logs --tail=50 frontend 2>/dev/null || true
    exit 1
  fi
  sleep 5
done
echo "frontend reachable."

# --- Sandbox warm-up (environment prep, not a UI step) -------------------------
# Product gap (reported): on a fresh stack the frontend's conversation create
# resolves its relative working dir via GET /api/file/home, which 404s with
# "No sandbox available" until any sandbox exists — so the very first UI send
# can never succeed. Booting one sandbox up front unblocks the real-person
# flow below. Zero LLM spend: the throwaway carries no message and is deleted.
echo "Warming one sandbox (throwaway messageless conversation)..."
WARM_ID="$(cat /proc/sys/kernel/random/uuid)"
WARM_BODY="$(mktemp /tmp/e2e-warm-body.XXXXXX)"
WARM_CODE="000"
# Sandbox boot can flake once (transient 5xx); retry once. No LLM spend is
# involved at any point (messageless create).
for attempt in 1 2 3; do
  WARM_CODE="$(curl -s -o "${WARM_BODY}" -w '%{http_code}' --max-time 330 \
    -H "X-Session-API-Key: ${SESSION_API_KEY}" -H 'Content-Type: application/json' \
    -d "{\"conversation_id\":\"${WARM_ID}\",\"agent_settings\":{\"llm\":{\"model\":\"${E2E_MODEL_ID}\"}},\"workspace\":{\"kind\":\"LocalWorkspace\",\"working_dir\":\"workspace/project/e2e-warmup\"},\"worktree\":true,\"confirmation_policy\":{\"kind\":\"NeverConfirm\"}}" \
    "${E2E_BACKEND_URL}/api/conversations")"
  if [ "${WARM_CODE}" = "201" ] || [ "${WARM_CODE}" = "200" ]; then
    break
  fi
  echo "warm-up attempt ${attempt} returned HTTP ${WARM_CODE}; retrying..." >&2
  sleep 10
done
rm -f "${WARM_BODY}"
if [ "${WARM_CODE}" != "201" ] && [ "${WARM_CODE}" != "200" ]; then
  echo "FAIL: warm-up conversation create returned HTTP ${WARM_CODE} after 3 attempts." >&2
  exit 1
fi
if ! curl -fsS --max-time 30 -H "X-Session-API-Key: ${SESSION_API_KEY}" \
  "${E2E_BACKEND_URL}/api/file/home" >/dev/null 2>&1; then
  echo "FAIL: /api/file/home still unavailable after warm-up." >&2
  exit 1
fi
curl -s -o /dev/null --max-time 30 -X DELETE \
  -H "X-Session-API-Key: ${SESSION_API_KEY}" \
  "${E2E_BACKEND_URL}/api/conversations/${WARM_ID}" || true
echo "warm-up done (sandbox running, throwaway deleted)."

# Record pre-existing containers so teardown removes only sandboxes this run
# spawned (matched by the sandbox label below).
PREEXISTING_CONTAINERS="$(docker ps -aq 2>/dev/null || true)"

# --- Spec deps (system Chromium; no browser download) --------------------------
if [ ! -d e2e/node_modules ]; then
  echo "Installing e2e npm dependencies..."
  (cd e2e && npm install --no-audit --no-fund)
fi

# --- Run -----------------------------------------------------------------------
echo "Running Playwright spec (headless Chromium, max 1 retry)..."
(cd e2e && npx playwright test --config=playwright.config.ts)
status=$?
if [ "${status}" -eq 0 ]; then
  echo "E2E PASS: backend added, connection listed, profile listed/active, chat reply + trivial command output observed."
else
  echo "E2E FAIL: see e2e/test-results and e2e/playwright-report (gitignored)." >&2
fi
exit "${status}"
