"""E2E fixtures: system Chromium launch args + failure screenshots only.

No video/trace/har: runs handle real credentials and those artifacts can
record request bodies.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Page

SYSTEM_CHROMIUM = '/usr/sbin/chromium'
RESULTS_DIR = Path(__file__).resolve().parent / 'test-results'


@pytest.fixture(scope='session')
def browser_type_launch_args() -> dict:
    args: dict = {'args': ['--no-sandbox', '--disable-dev-shm-usage']}
    if os.path.exists(SYSTEM_CHROMIUM):
        args['executable_path'] = SYSTEM_CHROMIUM
    return args


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f'rep_{rep.when}', rep)


@pytest.fixture(autouse=True)
def _screenshot_on_failure(page: Page, request):
    yield
    rep_call = getattr(request.node, 'rep_call', None)
    if rep_call is not None and rep_call.failed:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(RESULTS_DIR / f'{request.node.name}.png'))
        except Exception:
            pass
