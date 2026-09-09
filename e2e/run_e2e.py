#!/usr/bin/env python3
"""E2E runner: compose up -> pytest (headless Chromium) -> down.

Usage:
    python e2e/run_e2e.py [--keep] [--no-build]

Secrets: the LLM key is read from the repo-root ``.env`` (``api_key=``) into
memory only and passed to the spec process via environment. It is never
printed, logged, or written to any file. SESSION_API_KEY / OH_SECRET_KEY are
throwaway values generated fresh per run.
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT = 'sandbox-e2e'
COMPOSE_FILE = REPO_ROOT / 'e2e' / 'docker-compose.e2e.yml'

APP_PORT = os.environ.get('APP_PORT', '3000')
FRONTEND_PORT = os.environ.get('E2E_FRONTEND_PORT', '8080')
EDGE_PORT = os.environ.get('E2E_EDGE_PORT', '8099')
# Public backend address, mount prefix included — the exact production shape.
# The browser and all test API calls use this; :3000 stays published for
# health checks and sandbox webhook callbacks only.
PUBLIC_BASE = f'http://localhost:{EDGE_PORT}/sandbox-server'
BACKEND_URL = PUBLIC_BASE
FRONTEND_URL = f'http://localhost:{FRONTEND_PORT}'


def read_llm_key() -> str:
    for line in (REPO_ROOT / '.env').read_text().splitlines():
        if line.startswith('api_key='):
            return line.removeprefix('api_key=').strip().strip('"\'')
    sys.exit("FAIL: repo-root .env is missing the 'api_key' entry; refusing to run.")


def compose(
    *args: str, env: dict[str, str], check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            'docker',
            'compose',
            '--project-name',
            PROJECT,
            '-f',
            str(COMPOSE_FILE),
            *args,
        ],
        cwd=REPO_ROOT,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def wait_healthy(url: str, name: str, budget_s: int) -> None:
    print(f'Waiting for {name} {url} ...', flush=True)
    deadline = time.monotonic() + budget_s
    # Accept any sub-400 status: the frontend answers `/` with a 308 to
    # `/canvas/`, which is healthy (curl -fsS behaves the same way).
    with httpx.Client(timeout=5, follow_redirects=True) as client:
        while True:
            try:
                if client.get(url).is_success:
                    print(f'{name} reachable.', flush=True)
                    return
            except httpx.RequestError:
                pass
            if time.monotonic() > deadline:
                logs = compose(
                    'logs',
                    '--tail=50',
                    'edge' if name == 'edge' else name,
                    env=os.environ.copy(),
                    check=False,
                )
                print(logs.stdout[-4000:], flush=True)
                sys.exit(f'FAIL: {name} did not become reachable in {budget_s}s.')
            time.sleep(5)


def warm_up_sandbox(backend_url: str, session_key: str, model: str) -> None:
    """Boot one sandbox via a messageless throwaway conversation (zero LLM spend).

    Fresh stacks 404 ``GET /api/file/home`` until any sandbox exists, which
    would fail the first UI send; the throwaway is deleted afterwards.
    """
    print('Warming one sandbox (throwaway messageless conversation)...', flush=True)
    warm_id = str(uuid.uuid4())
    body = {
        'conversation_id': warm_id,
        'agent_settings': {'llm': {'model': model}},
        'workspace': {
            'kind': 'LocalWorkspace',
            'working_dir': 'workspace/project/e2e-warmup',
        },
        'worktree': True,
        'confirmation_policy': {'kind': 'NeverConfirm'},
    }
    headers = {'X-Session-API-Key': session_key}
    code = '000'
    with httpx.Client(timeout=330) as client:
        for _ in range(3):
            try:
                code = str(
                    client.post(
                        f'{backend_url}/api/conversations', json=body, headers=headers
                    ).status_code
                )
            except httpx.RequestError:
                code = '000'
            if code in ('200', '201'):
                break
            print(f'warm-up attempt returned HTTP {code}; retrying...', flush=True)
            time.sleep(10)
    if code not in ('200', '201'):
        sys.exit(
            f'FAIL: warm-up conversation create returned HTTP {code} after 3 attempts.'
        )
    with httpx.Client(timeout=30) as client:
        try:
            ok = client.get(f'{backend_url}/api/file/home', headers=headers).is_success
        except httpx.RequestError:
            ok = False
    if not ok:
        sys.exit('FAIL: /api/file/home still unavailable after warm-up.')
    with httpx.Client(timeout=30) as client:
        try:
            client.delete(f'{backend_url}/api/conversations/{warm_id}', headers=headers)
        except httpx.RequestError:
            pass
    print('warm-up done (sandbox running, throwaway deleted).', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description='Run the docker + Playwright E2E.')
    parser.add_argument(
        '--keep', action='store_true', help='leave the stack running after the spec'
    )
    parser.add_argument(
        '--no-build',
        action='store_true',
        help='skip image build (reuse previous e2e image)',
    )
    args = parser.parse_args()

    llm_key = read_llm_key()
    session_key = secrets.token_hex(32)
    secret_key = secrets.token_hex(32)
    provider_base = os.environ.get(
        'E2E_PROVIDER_BASE_URL', 'https://opencode.ai/zen/go/v1'
    )

    env = {
        **os.environ,
        'SESSION_API_KEY': session_key,
        'OH_SECRET_KEY': secret_key,
        'E2E_SESSION_KEY': session_key,
        'E2E_LLM_API_KEY': llm_key,
        'E2E_FRONTEND_URL': FRONTEND_URL,
        'E2E_BACKEND_URL': BACKEND_URL,
        'E2E_CONNECTION_NAME': os.environ.get('E2E_CONNECTION_NAME', 'e2e-openai'),
        'E2E_PROFILE_NAME': os.environ.get('E2E_PROFILE_NAME', 'e2e-profile'),
        'E2E_MODEL_ID': os.environ.get(
            'E2E_MODEL_ID', 'openai/muse-spark-1.3-contributor'
        ),
        'E2E_PROVIDER_BASE_URL': provider_base,
        # Public base the edge serves (mount prefix included); the shim
        # builds browser-facing conversation_url from it.
        'E2E_PUBLIC_BASE_URL': PUBLIC_BASE,
        'E2E_EDGE_PORT': EDGE_PORT,
        # Belt-and-braces with the shim's materialize-on-create: forward the
        # credential into every sandbox via litellm env.
        'OH_AGENT_SERVER_ENV': (
            '{"OPENAI_API_KEY":"'
            + llm_key
            + '","OPENAI_API_BASE":"'
            + provider_base
            + '","OPENAI_BASE_URL":"'
            + provider_base
            + '"}'
        ),
        'APP_PORT': APP_PORT,
        'E2E_FRONTEND_PORT': FRONTEND_PORT,
        'PLAYWRIGHT_CHROMIUM_EXECUTABLE': '/usr/sbin/chromium',
    }

    preexisting = subprocess.run(
        ['docker', 'ps', '-aq'], capture_output=True, text=True
    ).stdout.split()

    def teardown() -> None:
        if args.keep:
            print('Keeping stack running (--keep). Tear down with:')
            print(
                f'  docker compose --project-name {PROJECT} -f e2e/docker-compose.e2e.yml down -v'
            )
            return
        print('Tearing down test-scoped stack...', flush=True)
        compose('down', '-v', env=env, check=False)
        known = set(preexisting)
        running = subprocess.run(
            ['docker', 'ps', '-aq', '--filter', 'label=sandbox_spec_id'],
            capture_output=True,
            text=True,
        ).stdout.split()
        for cid in running:
            if cid not in known:
                subprocess.run(['docker', 'rm', '-f', cid], capture_output=True)

    try:
        if not args.no_build:
            print('Building sandbox-server image (layer-cached)...', flush=True)
            compose('build', 'sandbox-server', env=env)
        print('Pulling frontend image...', flush=True)
        subprocess.run(
            ['docker', 'pull', 'ghcr.io/openhands/agent-canvas:latest'], check=True
        )
        print('Starting stack...', flush=True)
        compose('up', '-d', env=env)
        wait_healthy(f'http://localhost:{APP_PORT}/health', 'sandbox-server', 300)
        wait_healthy(f'{BACKEND_URL}/health', 'edge', 120)
        wait_healthy(FRONTEND_URL, 'frontend', 240)
        warm_up_sandbox(BACKEND_URL, session_key, env['E2E_MODEL_ID'])
        print('Running Playwright spec (headless Chromium)...', flush=True)
        result = subprocess.run(
            [sys.executable, '-m', 'pytest', 'e2e/test_full_user_path.py', '-v'],
            cwd=REPO_ROOT,
            env={**env, 'PLAYWRIGHT_BROWSERS_PATH': '/root/.cache/ms-playwright'},
        )
        if result.returncode != 0:
            # Persist server logs for diagnosis before teardown. Redact both
            # keys: sandbox agent logs can echo request material.
            dump = compose('logs', '--tail=300', env=env, check=False).stdout
            for secret in (llm_key, session_key):
                if secret:
                    dump = dump.replace(secret, '<redacted>')
            results = REPO_ROOT / 'e2e' / 'test-results'
            results.mkdir(parents=True, exist_ok=True)
            (results / 'compose-logs.txt').write_text(dump[-200_000:])
            print('compose logs saved to e2e/test-results/compose-logs.txt', flush=True)
        print(
            'E2E PASS: backend, connection, profile, chat reply + trivial output observed.'
            if result.returncode == 0
            else 'E2E FAIL: see e2e/test-results (gitignored).',
            flush=True,
        )
        return result.returncode
    finally:
        teardown()


if __name__ == '__main__':
    raise SystemExit(main())
