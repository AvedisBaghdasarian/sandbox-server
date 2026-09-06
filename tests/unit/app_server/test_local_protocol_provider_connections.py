"""Unit tests for the local-protocol parity routes (no Docker).

Covers the agent-server 1.43+ surface the shim exposes at root:
- ``GET/POST /api/llm/provider-connections`` and
  ``PATCH/DELETE /api/llm/provider-connections/{id}``
- ``POST /api/profiles/{name}/validate`` (``{valid, error}`` shape)
- linked-profile saves through ``POST /api/profiles/{name}``
  (``provider_connection_id`` accepted, inline credentials cleared)
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
from openhands.sdk.llm.exceptions import LLMError, LLMRateLimitError
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


def _connection_body(**overrides):
    body = {
        'display_name': 'My OpenAI',
        'provider': 'openai',
        'api_key': 'sk-conn-1',
    }
    body.update(overrides)
    return body


def _create_connection(test_client, **overrides):
    response = test_client.post(
        '/api/llm/provider-connections', json=_connection_body(**overrides)
    )
    assert response.status_code == 201
    return response.json()


# ── POST /api/llm/provider-connections + GET list ────────────────────


@pytest.mark.asyncio
async def test_create_connection_persists_and_lists(test_client, settings_store):
    # Arrange
    await _seed(settings_store, _base_settings())

    # Act
    created = _create_connection(test_client)

    # Assert
    assert created['api_key_set'] is True
    listed = test_client.get('/api/llm/provider-connections').json()
    assert [c['id'] for c in listed] == [created['id']]


def test_create_connection_rejects_missing_api_key(test_client):
    # Arrange — no display_name/provider/api_key triad
    # Act
    response = test_client.post(
        '/api/llm/provider-connections',
        json={'display_name': 'No key', 'provider': 'openai'},
    )

    # Assert
    assert response.status_code == 422


# ── PATCH /api/llm/provider-connections/{id} ─────────────────────────


@pytest.mark.asyncio
async def test_update_connection_renames(test_client, settings_store):
    # Arrange
    await _seed(settings_store, _base_settings())
    created = _create_connection(test_client)

    # Act
    response = test_client.patch(
        f'/api/llm/provider-connections/{created["id"]}',
        json={'display_name': 'Renamed'},
    )

    # Assert
    assert response.status_code == 200
    assert response.json()['display_name'] == 'Renamed'


def test_update_connection_rejects_empty_body(test_client):
    # Arrange — body present but no fields set
    # Act
    response = test_client.patch(
        '/api/llm/provider-connections/does-not-matter', json={}
    )

    # Assert
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_connection_rejects_cleared_api_key(test_client, settings_store):
    # Arrange
    await _seed(settings_store, _base_settings())
    created = _create_connection(test_client)

    # Act
    response = test_client.patch(
        f'/api/llm/provider-connections/{created["id"]}',
        json={'api_key': None},
    )

    # Assert
    assert response.status_code == 422


def test_update_unknown_connection_returns_404(test_client):
    # Arrange — no connections exist
    # Act
    response = test_client.patch(
        '/api/llm/provider-connections/missing-id',
        json={'display_name': 'x'},
    )

    # Assert
    assert response.status_code == 404


# ── DELETE /api/llm/provider-connections/{id} ────────────────────────


@pytest.mark.asyncio
async def test_delete_unreferenced_connection(test_client, settings_store):
    # Arrange
    await _seed(settings_store, _base_settings())
    created = _create_connection(test_client)

    # Act
    response = test_client.delete(
        f'/api/llm/provider-connections/{created["id"]}'
    )

    # Assert
    assert response.status_code == 200
    assert test_client.get('/api/llm/provider-connections').json() == []


@pytest.mark.asyncio
async def test_delete_referenced_connection_is_guarded(test_client, settings_store):
    # Arrange — a profile links to the connection
    await _seed(settings_store, _base_settings())
    created = _create_connection(test_client)
    save = test_client.post(
        '/api/profiles/linked',
        json={
            'llm': {
                'model': 'openai/gpt-4o',
                'provider_connection_id': created['id'],
            }
        },
    )
    assert save.status_code == 200

    # Act
    response = test_client.delete(
        f'/api/llm/provider-connections/{created["id"]}'
    )

    # Assert — guarded while the link exists, deletable once unlinked
    assert response.status_code == 409
    assert 'linked' in response.json()['detail']
    test_client.delete('/api/profiles/linked')
    assert (
        test_client.delete(
            f'/api/llm/provider-connections/{created["id"]}'
        ).status_code
        == 200
    )


# ── Linked profile saves ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_profile_with_null_connection_id(test_client, settings_store):
    # Arrange — explicit null must not trip StrictLLM(extra=forbid)
    await _seed(settings_store, _base_settings())

    # Act
    response = test_client.post(
        '/api/profiles/p',
        json={
            'llm': {'model': 'openai/gpt-4o', 'provider_connection_id': None},
        },
    )

    # Assert
    assert response.status_code == 200
    stored = await settings_store.load()
    assert stored.llm_profiles.get('p').provider_connection_id is None


@pytest.mark.asyncio
async def test_save_linked_profile_clears_inline_credentials(
    test_client, settings_store
):
    # Arrange
    await _seed(settings_store, _base_settings())
    created = _create_connection(test_client)

    # Act — stale inline credentials must not survive alongside the link
    response = test_client.post(
        '/api/profiles/linked',
        json={
            'llm': {
                'model': 'openai/gpt-4o',
                'api_key': 'sk-stale',
                'base_url': 'https://stale.example.com',
                'provider_connection_id': created['id'],
            }
        },
    )

    # Assert
    assert response.status_code == 200
    stored = await settings_store.load()
    saved = stored.llm_profiles.get('linked')
    assert saved.provider_connection_id == created['id']
    assert saved.api_key is None
    assert saved.base_url is None


@pytest.mark.asyncio
async def test_linked_profile_reports_effective_key_and_broken_link(
    test_client, settings_store
):
    # Arrange — one live link and one dangling reference
    await _seed(settings_store, _base_settings())
    created = _create_connection(test_client)
    test_client.post(
        '/api/profiles/linked',
        json={
            'llm': {
                'model': 'openai/gpt-4o',
                'provider_connection_id': created['id'],
            }
        },
    )
    test_client.post(
        '/api/profiles/dangling',
        json={
            'llm': {'model': 'openai/gpt-4o', 'provider_connection_id': 'gone'}
        },
    )

    # Act
    rows = {
        p['name']: p for p in test_client.get('/api/profiles').json()['profiles']
    }

    # Assert
    assert rows['linked']['api_key_set'] is True
    assert rows['linked']['provider_connection_broken'] is False
    assert rows['dangling']['provider_connection_broken'] is True


@pytest.mark.asyncio
async def test_get_linked_profile_reports_connection_key(
    test_client, settings_store
):
    # Arrange
    await _seed(settings_store, _base_settings())
    created = _create_connection(test_client)
    test_client.post(
        '/api/profiles/linked',
        json={
            'llm': {
                'model': 'openai/gpt-4o',
                'provider_connection_id': created['id'],
            }
        },
    )

    # Act
    body = test_client.get('/api/profiles/linked').json()

    # Assert
    assert body['api_key_set'] is True
    assert body['config']['provider_connection_id'] == created['id']
    assert body['config']['api_key'] is None


# ── POST /api/profiles/{name}/validate ──────────────────────────────


@pytest.mark.asyncio
async def test_validate_profile_success(test_client, settings_store):
    # Arrange — stub the network call; the ping must use max_tokens=1
    await _seed(settings_store, _base_settings())
    with (
        patch.object(LLM, 'acompletion', new=AsyncMock()) as mock_complete,
        patch.object(LLM, 'aresponses', new=AsyncMock()),
    ):
        # Act
        response = test_client.post(
            '/api/profiles/p/validate',
            json={'llm': {'model': 'openai/gpt-4o', 'api_key': 'sk-x'}},
        )

    # Assert
    assert response.status_code == 200
    assert response.json() == {'valid': True, 'error': None}
    _, kwargs = mock_complete.call_args
    assert kwargs['max_tokens'] == 1


@pytest.mark.asyncio
async def test_validate_profile_failure_reports_error(
    test_client, settings_store
):
    # Arrange — a blocking provider error (e.g. bad key)
    await _seed(settings_store, _base_settings())
    with (
        patch.object(
            LLM, 'acompletion', new=AsyncMock(side_effect=LLMError('bad key'))
        ),
        patch.object(LLM, 'aresponses', new=AsyncMock()),
    ):
        # Act
        response = test_client.post(
            '/api/profiles/p/validate',
            json={'llm': {'model': 'openai/gpt-4o', 'api_key': 'sk-bad'}},
        )

    # Assert
    assert response.status_code == 200
    body = response.json()
    assert body['valid'] is False
    assert body['error']['type'] == 'LLMError'


@pytest.mark.asyncio
async def test_validate_profile_transient_error_is_non_blocking(
    test_client, settings_store
):
    # Arrange — rate limits must not block the save
    await _seed(settings_store, _base_settings())
    with (
        patch.object(
            LLM,
            'acompletion',
            new=AsyncMock(side_effect=LLMRateLimitError('slow down')),
        ),
        patch.object(LLM, 'aresponses', new=AsyncMock()),
    ):
        # Act
        response = test_client.post(
            '/api/profiles/p/validate',
            json={'llm': {'model': 'openai/gpt-4o', 'api_key': 'sk-x'}},
        )

    # Assert
    assert response.json() == {'valid': True, 'error': None}
