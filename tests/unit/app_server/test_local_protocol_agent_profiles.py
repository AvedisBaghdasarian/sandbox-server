"""Unit tests for the agent-profiles local-protocol shim (no Docker).

Covers the ``/api/agent-profiles`` surface the frontend expects:
- ``GET /api/agent-profiles`` (lazy seed of one default profile)
- ``GET/POST/DELETE /api/agent-profiles/{name}``
- ``POST /api/agent-profiles/{name}/rename``
- ``POST /api/agent-profiles/{id}/activate`` (pointer-only)
- ``POST /api/agent-profiles/{name}/materialize`` (dry-run diagnostics)
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from openhands.app_server.app import app
from openhands.app_server.file_store import get_file_store
from openhands.app_server.integrations.provider import ProviderToken, ProviderType
from openhands.app_server.integrations.service_types import UserGitInfo
from openhands.app_server.secrets.secrets_models import Secrets
from openhands.app_server.secrets.secrets_store import SecretsStore
from openhands.app_server.settings.file_settings_store import FileSettingsStore
from openhands.app_server.settings.settings_models import Settings
from openhands.app_server.settings.settings_router import _user_profile_locks
from openhands.app_server.settings.settings_store import SettingsStore
from openhands.app_server.user_auth.user_auth import UserAuth
from openhands.sdk.llm import LLM
from openhands.sdk.settings import OpenHandsAgentSettings


@pytest.fixture(autouse=True)
def _reset_profile_locks():
    """Locks bind to the event loop that first awaited them; FastAPI TestClient
    spins a fresh loop per test, so any stale Lock carried over from a previous
    test would be attached to a dead loop. Clearing between tests fixes it."""
    _user_profile_locks.clear()
    yield
    _user_profile_locks.clear()


class _MockUserAuth(UserAuth):
    def __init__(self, settings_store: SettingsStore) -> None:
        self._settings = None
        self._settings_store = settings_store

    async def get_user_id(self) -> str | None:
        return 'test-user'

    async def get_user_email(self) -> str | None:
        return 'test-email@example.com'

    async def get_access_token(self) -> SecretStr | None:
        return SecretStr('test-token')

    async def get_provider_tokens(
        self,
    ) -> dict[ProviderType, ProviderToken] | None:
        return None

    async def get_user_settings_store(self) -> SettingsStore | None:
        return self._settings_store

    async def get_secrets_store(self) -> SecretsStore | None:
        return None

    async def get_secrets(self) -> Secrets | None:
        return None

    async def get_mcp_api_key(self) -> str | None:
        return None

    async def get_user_git_info(self) -> UserGitInfo | None:
        return None

    @classmethod
    async def get_instance(cls, request: Request) -> UserAuth:
        raise NotImplementedError  # patched per-test

    @classmethod
    async def get_for_user(cls, user_id: str) -> UserAuth:
        raise NotImplementedError  # patched per-test


@pytest.fixture
def settings_store(tmp_path: Path) -> FileSettingsStore:
    return FileSettingsStore(get_file_store('local', str(tmp_path)))


@pytest.fixture
def test_client(settings_store):
    """TestClient wired to an in-memory settings store the test can seed directly."""
    auth = _MockUserAuth(settings_store)
    with (
        patch.dict(
            os.environ,
            {'SESSION_API_KEY': '', 'ALLOW_SHORT_CONTEXT_WINDOWS': 'true'},
            clear=False,
        ),
        patch('openhands.app_server.utils.dependencies._SESSION_API_KEY', None),
        patch(
            'openhands.app_server.user_auth.user_auth.UserAuth.get_instance',
            return_value=auth,
        ),
        patch(
            'openhands.app_server.settings.file_settings_store.FileSettingsStore.get_instance',
            AsyncMock(return_value=settings_store),
        ),
    ):
        yield TestClient(app)


def _base_settings() -> Settings:
    return Settings(
        agent_settings=OpenHandsAgentSettings(
            llm=LLM(
                model='openai/gpt-4o',
                api_key=SecretStr('sk-current'),
            ),
        ),
    )


async def _seed(store: FileSettingsStore, settings: Settings) -> None:
    await store.store(settings)


def _profile_body(**overrides):
    body = {'agent_kind': 'openhands', 'llm_profile_ref': 'default'}
    body.update(overrides)
    return body


def _save_profile(test_client, name: str, **overrides):
    response = test_client.post(
        f'/api/agent-profiles/{name}', json=_profile_body(**overrides)
    )
    assert response.status_code == 201
    return response.json()


# ── GET /api/agent-profiles (list + lazy seed) ─────────────────────


@pytest.mark.asyncio
async def test_list_seeds_default_profile(test_client, settings_store):
    # Arrange
    await _seed(settings_store, _base_settings())

    # Act
    first = test_client.get('/api/agent-profiles')

    # Assert — one seeded profile with the active pointer set
    assert first.status_code == 200
    body = first.json()
    assert [p['name'] for p in body['profiles']] == ['default']
    assert body['active_agent_profile_id'] is not None

    # Act — a second list is stable (no duplicate seed)
    second = test_client.get('/api/agent-profiles').json()

    # Assert
    assert [p['name'] for p in second['profiles']] == ['default']
    assert second['active_agent_profile_id'] == body['active_agent_profile_id']


# ── POST + GET /api/agent-profiles/{name} ───────────────────────────


@pytest.mark.asyncio
async def test_save_and_get_profile(test_client, settings_store):
    # Arrange
    await _seed(settings_store, _base_settings())

    # Act
    saved = _save_profile(test_client, 'my-agent')
    detail = test_client.get('/api/agent-profiles/my-agent')

    # Assert
    assert saved == {'name': 'my-agent', 'message': "Agent profile 'my-agent' saved"}
    assert detail.status_code == 200
    assert detail.json()['name'] == 'my-agent'
    assert detail.json()['profile']['llm_profile_ref'] == 'default'


@pytest.mark.asyncio
async def test_save_invalid_profile_returns_422(test_client, settings_store):
    # Arrange — OpenHands profiles require llm_profile_ref
    await _seed(settings_store, _base_settings())

    # Act
    response = test_client.post(
        '/api/agent-profiles/bad', json={'agent_kind': 'openhands'}
    )

    # Assert
    assert response.status_code == 422


def test_get_unknown_profile_returns_404(test_client):
    # Arrange — empty store, no seed read (detail never seeds)
    # Act
    response = test_client.get('/api/agent-profiles/missing')

    # Assert
    assert response.status_code == 404


# ── DELETE /api/agent-profiles/{name} ───────────────────────────────


@pytest.mark.asyncio
async def test_delete_profile(test_client, settings_store):
    # Arrange
    await _seed(settings_store, _base_settings())
    _save_profile(test_client, 'temp')

    # Act
    response = test_client.delete('/api/agent-profiles/temp')

    # Assert — deleted detail reads 404 afterwards
    assert response.status_code == 200
    assert test_client.get('/api/agent-profiles/temp').status_code == 404


# ── POST /api/agent-profiles/{name}/rename ──────────────────────────


@pytest.mark.asyncio
async def test_rename_profile(test_client, settings_store):
    # Arrange
    await _seed(settings_store, _base_settings())
    _save_profile(test_client, 'before')

    # Act
    response = test_client.post(
        '/api/agent-profiles/before/rename', json={'new_name': 'after'}
    )

    # Assert
    assert response.status_code == 200
    assert response.json()['name'] == 'after'
    assert test_client.get('/api/agent-profiles/after').status_code == 200
    assert test_client.get('/api/agent-profiles/before').status_code == 404


@pytest.mark.asyncio
async def test_rename_conflict_returns_409(test_client, settings_store):
    # Arrange — target name already taken
    await _seed(settings_store, _base_settings())
    _save_profile(test_client, 'aaa')
    _save_profile(test_client, 'bbb')

    # Act
    response = test_client.post(
        '/api/agent-profiles/aaa/rename', json={'new_name': 'bbb'}
    )

    # Assert
    assert response.status_code == 409


def test_rename_missing_returns_404(test_client):
    # Arrange — no profiles exist
    # Act
    response = test_client.post(
        '/api/agent-profiles/missing/rename', json={'new_name': 'other'}
    )

    # Assert
    assert response.status_code == 404


# ── POST /api/agent-profiles/{id}/activate ──────────────────────────


@pytest.mark.asyncio
async def test_activate_profile_moves_pointer(test_client, settings_store):
    # Arrange — seed plus a second profile
    await _seed(settings_store, _base_settings())
    seeded = test_client.get('/api/agent-profiles').json()
    seeded_id = seeded['active_agent_profile_id']
    _save_profile(test_client, 'second')
    other_id = next(
        p['id']
        for p in test_client.get('/api/agent-profiles').json()['profiles']
        if p['name'] == 'second'
    )
    assert other_id != seeded_id

    # Act
    response = test_client.post(f'/api/agent-profiles/{other_id}/activate')

    # Assert — pointer-only: new id active, agent_settings untouched
    assert response.status_code == 200
    assert response.json()['agent_settings_applied'] is False
    listed = test_client.get('/api/agent-profiles').json()
    assert listed['active_agent_profile_id'] == other_id


def test_activate_unknown_id_returns_404(test_client):
    # Arrange — no profiles exist
    # Act
    response = test_client.post('/api/agent-profiles/does-not-exist/activate')

    # Assert
    assert response.status_code == 404


# ── POST /api/agent-profiles/{name}/materialize ─────────────────────


@pytest.mark.asyncio
async def test_materialize_profile(test_client, settings_store):
    # Arrange — an LLM profile behind the seed's llm_profile_ref
    await _seed(settings_store, _base_settings())
    save = test_client.post(
        '/api/profiles/default',
        json={'llm': {'model': 'openai/gpt-4o', 'api_key': 'sk-x'}},
    )
    assert save.status_code == 200
    test_client.get('/api/agent-profiles')

    # Act
    response = test_client.post('/api/agent-profiles/default/materialize')

    # Assert — dry-run resolves the ref instead of raising on it
    assert response.status_code == 200
    body = response.json()
    assert body['valid'] is True
    assert body['llm_profile_resolved'] is True


def test_materialize_unknown_returns_404(test_client):
    # Arrange — no profiles exist
    # Act
    response = test_client.post('/api/agent-profiles/missing/materialize')

    # Assert
    assert response.status_code == 404
