"""Full user-path E2E against real containers (Python Playwright).

Flow, every UI step performed like a real person in headless Chromium:
  1. Dismiss telemetry, skip onboarding, open Manage Backends, add a local
     backend pointing at the sandbox-server (type + submit), and select it.
  2. Create a provider connection (openai + custom base_url + runtime key).
  3. Create + activate an LLM profile linked to that connection.
  4. Start a conversation, send one short message, prove the agent talks
     (chat reply token + trivial terminal output).

Secrets: the LLM api key arrives via the E2E_LLM_API_KEY env var, is kept in
memory only, filled into a password field, and never logged. Run via
``python e2e/run_e2e.py`` (builds stack, runs this file, tears down).
"""

from __future__ import annotations

import json
import os
import re
import time

import pytest
from playwright.sync_api import Locator, Page, expect

FRONTEND_URL = os.environ.get('E2E_FRONTEND_URL', 'http://localhost:8080')
BACKEND_URL = os.environ.get('E2E_BACKEND_URL', 'http://localhost:3000')
SESSION_KEY = os.environ.get('E2E_SESSION_KEY', '')
LLM_API_KEY = os.environ.get('E2E_LLM_API_KEY', '')
CONNECTION_NAME = os.environ.get('E2E_CONNECTION_NAME', 'e2e-openai')
PROFILE_NAME = os.environ.get('E2E_PROFILE_NAME', 'e2e-profile')
MODEL_ID = os.environ.get('E2E_MODEL_ID', 'openai/muse-spark-1.3-contributor')
PROVIDER_BASE_URL = os.environ.get(
    'E2E_PROVIDER_BASE_URL', 'https://opencode.ai/zen/go/v1'
)

REPLY_TOKEN = 'PING_OK'
BASH_TOKEN = 'E2E_TRIVIAL_OK'

_started_at = time.monotonic()


def elapsed() -> str:
    return f'{time.monotonic() - _started_at:.1f}s'


def dismiss_telemetry_dialog(page: Page) -> None:
    """Opt out of telemetry if the dialog is showing; no-op otherwise."""
    try:
        confirm = page.get_by_test_id('confirm-telemetry-preferences')
        if not confirm.is_visible(timeout=3_000):
            return
        box = page.get_by_role(
            'checkbox', name=re.compile('anonymous usage data', re.I)
        )
        if box.count():
            try:
                if box.first.is_checked():
                    box.first.uncheck()
            except Exception:
                pass  # confirm below still dismisses the dialog
        confirm.click()
        expect(confirm).to_have_count(0, timeout=30_000)
    except Exception:
        pass  # dialog absent or already gone


def resilient_click(page: Page, locator: Locator) -> None:
    """Click, clearing a lazily-appearing telemetry overlay once on failure."""
    try:
        locator.click(timeout=10_000)
    except Exception:
        dismiss_telemetry_dialog(page)
        locator.click(timeout=15_000)


def poll_state(page: Page, timeout_ms: int = 180_000) -> str:
    """Drive to the app shell: clear telemetry, skip onboarding, wait for shell."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if time.monotonic() > deadline:
            raise AssertionError('timed out waiting for app shell')
        tel = page.get_by_test_id('confirm-telemetry-preferences')
        if tel.count():
            box = page.get_by_role(
                'checkbox', name=re.compile('anonymous usage data', re.I)
            )
            if box.count():
                try:
                    if box.first.is_checked():
                        box.first.uncheck()
                except Exception:
                    pass
            try:
                tel.click(timeout=5_000)
            except Exception:
                pass  # overlay race; next iteration retries
            time.sleep(0.5)
            continue
        if page.get_by_test_id('onboarding-modal').count():
            skip = page.get_by_test_id('onboarding-skip')
            if skip.count():
                try:
                    skip.first.click(timeout=5_000)
                except Exception:
                    pass
            else:
                nxt = page.get_by_test_id('onboarding-agent-next')
                if nxt.count():
                    try:
                        nxt.click(timeout=5_000)
                    except Exception:
                        pass
            time.sleep(0.5)
            continue
        if page.get_by_test_id('backend-selector').count():
            return 'shell'
        time.sleep(0.5)


def body_has_token_outside_user_messages(page: Page, token: str) -> bool:
    """True when token appears in the page outside the user's own bubbles."""
    try:
        return bool(
            page.evaluate(
                """(expected) => {
                    const body = document.body.cloneNode(true);
                    if (!(body instanceof HTMLElement)) return false;
                    body.querySelectorAll('[data-testid="user-message"]')
                        .forEach((node) => node.remove());
                    return body.textContent?.includes(expected) ?? false;
                }""",
                token,
            )
        )
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _telemetry_opt_out(page: Page) -> None:
    # Telemetry opt-out only. Deliberately NOT setting `openhands-onboarded`
    # so the real first-run backend form flow is exercised.
    page.add_init_script(
        """() => {
            window.localStorage.setItem("analytics-consent", "false");
            window.localStorage.setItem("openhands-telemetry-consent", "denied");
            window.localStorage.setItem("openhands-telemetry-first-use", "true");
        }"""
    )


def test_backend_connection_profile_and_talking_conversation(page: Page) -> None:
    assert SESSION_KEY, 'E2E_SESSION_KEY must be set by run_e2e.py'
    assert LLM_API_KEY, 'E2E_LLM_API_KEY must be set by run_e2e.py'

    headers = {'X-Session-API-Key': SESSION_KEY}
    api = page.request
    # Best-effort cleanup so reruns start fresh (server state only; every UI
    # step below still runs in the browser).
    try:
        api.delete(f'{BACKEND_URL}/api/profiles/{PROFILE_NAME}', headers=headers)
    except Exception:
        pass  # missing profile; the UI flow surfaces real errors
    try:
        listed = api.get(f'{BACKEND_URL}/api/llm/provider-connections', headers=headers)
        if listed.ok:
            for item in listed.json():
                if item.get('display_name') == CONNECTION_NAME:
                    api.delete(
                        f'{BACKEND_URL}/api/llm/provider-connections/{item["id"]}',
                        headers=headers,
                    )
    except Exception:
        pass  # best effort only

    # 1. Add a local backend through the real backend form. This image serves
    # the SPA under /canvas and its onboarding has no backend phase (a seeded
    # "Local" backend already exists), so: telemetry -> skip onboarding ->
    # Manage Backends modal -> Add Backend -> select the new backend.
    page.goto(FRONTEND_URL, wait_until='domcontentloaded')
    app_base = page.evaluate('() => `${window.location.origin}/canvas`')

    dismiss_telemetry_dialog(page)
    assert poll_state(page) == 'shell'

    expect(page.get_by_test_id('backend-selector')).to_be_visible(timeout=60_000)
    resilient_click(page, page.get_by_test_id('backend-selector'))
    resilient_click(page, page.get_by_test_id('manage-backends-menu-item'))
    expect(page.get_by_test_id('manage-backends-modal')).to_be_visible()

    resilient_click(page, page.get_by_test_id('manage-backends-add'))
    expect(page.get_by_test_id('add-backend-chooser')).to_be_visible(timeout=30_000)
    resilient_click(page, page.get_by_test_id('add-backend-option-agent-server'))
    expect(page.get_by_test_id('add-backend-agent-server-panel')).to_be_visible()

    location_toggle = page.get_by_test_id('add-backend-location')
    try:
        if location_toggle.is_visible():
            local_option = location_toggle.get_by_role(
                'button', name=re.compile('local', re.I)
            )
            if local_option.count():
                resilient_click(page, local_option.first)
    except Exception:
        pass

    page.get_by_test_id('add-backend-name').fill('e2e-local')
    page.get_by_test_id('add-backend-host').fill(BACKEND_URL)
    page.get_by_test_id('add-backend-api-key').fill(SESSION_KEY)  # masked, never logged
    resilient_click(page, page.get_by_test_id('add-backend-submit'))

    # Reachable/green: the add form closes and the new row is listed.
    expect(page.get_by_test_id('add-backend-modal')).to_have_count(0, timeout=60_000)
    try:
        manage_visible = page.get_by_test_id('manage-backends-modal').is_visible(
            timeout=5_000
        )
    except Exception:
        manage_visible = False
    if not manage_visible:
        resilient_click(page, page.get_by_test_id('backend-selector'))
        resilient_click(page, page.get_by_test_id('manage-backends-menu-item'))
        expect(page.get_by_test_id('manage-backends-modal')).to_be_visible(
            timeout=30_000
        )
    backend_row = page.get_by_test_id('manage-backends-row-e2e-local')
    expect(backend_row).to_be_visible(timeout=60_000)
    print(f'[{elapsed()}] backend added and listed', flush=True)

    row_button = backend_row.get_by_role('button').first
    expect(row_button).to_be_enabled(timeout=120_000)
    resilient_click(page, row_button)
    expect(page.get_by_test_id('manage-backends-modal')).to_have_count(
        0, timeout=30_000
    )
    print(f'[{elapsed()}] backend selected and active', flush=True)

    server_info = api.get(f'{BACKEND_URL}/server_info', headers=headers)
    assert server_info.ok

    # 2. Provider connection: provider=openai + custom base_url + key.
    page.goto(f'{app_base}/settings/llm', wait_until='domcontentloaded')
    dismiss_telemetry_dialog(page)
    expect(page.get_by_test_id('add-provider-connection')).to_be_visible(timeout=60_000)
    resilient_click(page, page.get_by_test_id('add-provider-connection'))
    expect(page.get_by_test_id('provider-connection-modal')).to_be_visible()

    page.get_by_test_id('provider-connection-name-input').fill(CONNECTION_NAME)

    provider_combo = page.get_by_test_id('provider-connection-provider-input')
    resilient_click(page, provider_combo)
    provider_combo.fill('openai')
    exact = page.locator(
        '[data-testid^="provider-item-"]', has_text=re.compile(r'^openai$', re.I)
    )
    if exact.count():
        resilient_click(page, exact.first)
    else:
        resilient_click(
            page, page.get_by_role('option', name=re.compile('openai', re.I)).first
        )

    page.get_by_test_id('provider-connection-api-key-input').fill(LLM_API_KEY)
    page.get_by_test_id('provider-connection-base-url-input').fill(PROVIDER_BASE_URL)
    dismiss_telemetry_dialog(page)
    resilient_click(page, page.get_by_test_id('provider-connection-submit'))

    expect(
        page.get_by_test_id('provider-connection-row').filter(has_text=CONNECTION_NAME)
    ).to_be_visible(timeout=60_000)
    print(f'[{elapsed()}] provider connection listed', flush=True)

    # 3. Profile linked to the connection (no inline api_key), then active.
    dismiss_telemetry_dialog(page)
    resilient_click(page, page.get_by_test_id('add-llm-profile'))
    expect(page.get_by_test_id('profile-name-input')).to_be_visible()
    page.get_by_test_id('profile-name-input').fill(PROFILE_NAME)

    all_toggle = page.get_by_test_id('sdk-section-all-toggle')
    try:
        if all_toggle.is_visible():
            resilient_click(page, all_toggle)
    except Exception:
        pass
    expect(page.get_by_test_id('llm-custom-model-input')).to_be_visible(timeout=30_000)
    page.get_by_test_id('llm-custom-model-input').fill(MODEL_ID)

    connection_combo = page.get_by_test_id('llm-provider-connection-input')
    expect(connection_combo).to_be_visible(timeout=30_000)

    def select_connection() -> None:
        resilient_click(page, connection_combo)
        connection_combo.fill(CONNECTION_NAME)
        option = page.get_by_role('option', name=CONNECTION_NAME)
        expect(option.first).to_be_visible(timeout=15_000)
        resilient_click(page, option.first)

    select_connection()
    # A committed link hides the inline credential inputs; retry once on miss.
    if page.get_by_test_id('llm-api-key-input').count():
        resilient_click(page, connection_combo)
        connection_combo.fill('')
        select_connection()
    expect(page.get_by_test_id('llm-api-key-input')).to_have_count(0, timeout=15_000)

    dismiss_telemetry_dialog(page)
    resilient_click(page, page.get_by_test_id('save-profile-btn'))
    expect(
        page.get_by_test_id('profile-row').filter(has_text=PROFILE_NAME)
    ).to_be_visible(timeout=60_000)
    print(f'[{elapsed()}] profile listed', flush=True)

    profile_row = page.get_by_test_id('profile-row').filter(has_text=PROFILE_NAME)
    expect(profile_row.get_by_test_id('profile-broken-connection-badge')).to_have_count(
        0, timeout=15_000
    )

    resilient_click(page, profile_row.get_by_test_id('profile-menu-trigger'))
    expect(page.get_by_test_id('profile-actions-menu')).to_be_visible()
    set_active = page.get_by_test_id('profile-set-active')
    try:
        if set_active.is_enabled():
            dismiss_telemetry_dialog(page)
            resilient_click(page, set_active)
    except Exception:
        pass  # sole profile may already be active with the item disabled
    expect(profile_row.get_by_test_id('profile-active-badge')).to_be_visible(
        timeout=60_000
    )
    print(f'[{elapsed()}] profile active', flush=True)

    # Pre-flight gate (cheap, ~1 token): prove the linked model actually
    # answers before spending a full conversation cycle on it. Fails fast
    # with the provider error instead of a multi-minute UI timeout.
    connection_id = None
    try:
        listed = api.get(
            f'{BACKEND_URL}/api/llm/provider-connections',
            headers=headers,
            timeout=30_000,
        )
        if listed.ok:
            for item in listed.json():
                if item.get('display_name') == CONNECTION_NAME:
                    connection_id = item.get('id')
    except Exception:
        pass
    assert connection_id, 'provider connection id readable via API'
    verdict = api.post(
        f'{BACKEND_URL}/api/profiles/{PROFILE_NAME}/validate',
        headers=headers,
        data={'llm': {'model': MODEL_ID, 'provider_connection_id': connection_id}},
        timeout=90_000,
    )
    assert verdict.ok, 'validate endpoint reachable'
    verdict_body = verdict.json()
    assert verdict_body.get('valid') is True, (
        f'model cannot complete (fail fast): {verdict_body.get("error")}'
    )
    print(f'[{elapsed()}] model pre-flight valid', flush=True)

    # 4. Start a conversation from the home launcher and prove the agent
    # talks. One short turn, minimal spend: single terminal call + short reply.
    # Single line on purpose: Enter submits, so multiline would partial-send.
    page.goto(f'{app_base}/conversations', wait_until='domcontentloaded')
    dismiss_telemetry_dialog(page)
    expect(page.get_by_test_id('chat-input')).to_be_visible(timeout=60_000)
    expect(page.get_by_role('button', name=PROFILE_NAME).first).to_be_visible(
        timeout=30_000
    )

    user_message = (
        f'Use the terminal tool exactly once to run this command: echo {BASH_TOKEN} '
        f'and then reply with exactly this token and finish: {REPLY_TOKEN}. Nothing else.'
    )
    chat_input = page.get_by_test_id('chat-input')
    deadline = time.monotonic() + 90
    while True:
        current = page.evaluate(
            "() => document.querySelector('[data-testid=\"chat-input\"]')?.textContent ?? ''"
        )
        if BASH_TOKEN in current:
            break
        if time.monotonic() > deadline:
            raise AssertionError('composer text did not stick')
        resilient_click(page, chat_input)
        page.keyboard.press('ControlOrMeta+a')
        chat_input.press_sequentially(user_message, delay=10)

    deadline = time.monotonic() + 120
    while True:
        if page.get_by_test_id('home-llm-not-configured-banner').count():
            raise AssertionError('home stuck LLM-blocked (banner shown)')
        try:
            if page.get_by_test_id('submit-button').is_enabled():
                break
        except Exception:
            pass
        if time.monotonic() > deadline:
            raise AssertionError('composer never became ready')
        time.sleep(0.5)
    resilient_click(page, page.get_by_test_id('submit-button'))
    sent_at = time.monotonic()
    print(f'[{elapsed()}] message sent', flush=True)

    # Sending creates the conversation and navigates; creation boots a fresh
    # sandbox server-side, so allow a bounded budget. On timeout, surface
    # the visible error toast instead of a bare timeout.
    try:
        expect(page).to_have_url(re.compile(r'/conversations/.+'), timeout=180_000)
    except AssertionError:
        toast = page.evaluate(
            '() => Array.from(document.querySelectorAll(\'[role="alert"],[data-testid="toast"]\'))'
            '.map((n) => n.textContent).join(" | ").slice(0, 500)'
        )
        raise AssertionError(f'never navigated to conversation (toast: {toast!r})')
    expect(page.get_by_test_id('chat-input')).to_be_visible(timeout=60_000)
    match = re.search(r'/conversations/([^/?#]+)', page.url)
    assert match, 'conversation id readable from URL'
    conversation_id = match.group(1)

    # Liveness gate: the agent must produce ANY event beyond the user message
    # (action, thought, or error) within a tight budget. A dead model shows
    # exactly one event forever — fail there instead of burning minutes on
    # token polls. Error events fail immediately with the provider message.
    deadline = time.monotonic() + 150
    while True:
        try:
            probe = api.get(
                f'{BACKEND_URL}/api/conversations/{conversation_id}/events/search',
                headers=headers,
                params={'limit': '20', 'sort_order': 'TIMESTAMP_DESC'},
                timeout=15_000,
            )
            if probe.ok:
                items = probe.json().get('items') or []
                blob = json.dumps(items)
                if re.search(r'LLM\w*Error|InternalServerError', blob):
                    raise AssertionError(f'agent errored fast: {blob[:500]!r}')
                if len(items) >= 2:
                    break
        except AssertionError:
            raise
        except Exception:
            pass
        if time.monotonic() > deadline:
            raise AssertionError('agent produced zero events in 150s (dead turn)')
        time.sleep(3)
    print(f'[{elapsed()}] agent live', flush=True)

    deadline = time.monotonic() + 3 * 60
    while not body_has_token_outside_user_messages(page, BASH_TOKEN):
        if time.monotonic() > deadline:
            raise AssertionError('trivial command output never appeared')
        time.sleep(1)
    print(
        f'[{elapsed()}] trivial output observed (+{time.monotonic() - sent_at:.1f}s turn)',
        flush=True,
    )

    deadline = time.monotonic() + 90
    while not body_has_token_outside_user_messages(page, REPLY_TOKEN):
        if time.monotonic() > deadline:
            raise AssertionError('chat reply never appeared')
        time.sleep(1)
    print(f'[{elapsed()}] chat reply observed', flush=True)

    # Corroborate via the events API (UI assertions above are the real proof).
    deadline = time.monotonic() + 30
    while True:
        try:
            response = api.get(
                f'{BACKEND_URL}/api/conversations/{conversation_id}/events/search',
                headers=headers,
                params={'limit': '100', 'sort_order': 'TIMESTAMP_DESC'},
            )
            if response.ok and BASH_TOKEN in response.text():
                break
        except Exception:
            pass
        if time.monotonic() > deadline:
            raise AssertionError('events API never showed command output')
        time.sleep(1)
    print(f'[{elapsed()}] events API corroborates output', flush=True)

    # Best-effort cleanup (volumes are wiped on teardown regardless).
    try:
        api.delete(
            f'{BACKEND_URL}/api/conversations/{conversation_id}', headers=headers
        )
        print(f'[{elapsed()}] conversation cleaned up', flush=True)
    except Exception:
        pass
