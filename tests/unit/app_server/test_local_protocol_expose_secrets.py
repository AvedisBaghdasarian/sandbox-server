"""Unit tests for X-Expose-Secrets handling in local_protocol."""

from pydantic import SecretStr

from openhands.agent_server._secrets_exposure import build_expose_context
from openhands.sdk.llm import LLM
from openhands.sdk.utils.cipher import Cipher, FERNET_TOKEN_PREFIX
from openhands.sdk.utils.pydantic_secrets import REDACTED_SECRET_VALUE


class TestExposeSecretsSerialization:
    def test_encrypted_serialization_produces_fernet_prefix(self):
        # Arrange
        cipher = Cipher('test-secret')
        llm = LLM(model='openai/gpt-4o', api_key=SecretStr('sk-test-secret'))
        ctx = build_expose_context('encrypted', cipher)

        # Act
        dumped = llm.model_dump(mode='json', context=ctx)
        enc = dumped['api_key']

        # Assert
        assert isinstance(enc, str)
        assert enc.startswith(FERNET_TOKEN_PREFIX)
        assert enc.startswith('gAAAAA')
        assert enc != 'sk-test-secret'
        assert enc != REDACTED_SECRET_VALUE

    def test_plaintext_serialization_returns_raw_value(self):
        # Arrange
        cipher = Cipher('test-secret')
        llm = LLM(model='openai/gpt-4o', api_key=SecretStr('sk-test-secret'))
        ctx = build_expose_context('plaintext', cipher)

        # Act
        dumped = llm.model_dump(mode='json', context=ctx)

        # Assert
        assert dumped['api_key'] == 'sk-test-secret'

    def test_redacted_without_context(self):
        # Arrange
        llm = LLM(model='openai/gpt-4o', api_key=SecretStr('sk-test-secret'))

        # Act
        dumped_plain = llm.model_dump(mode='json')
        dumped_empty = llm.model_dump(mode='json', context={})

        # Assert
        assert dumped_plain['api_key'] == REDACTED_SECRET_VALUE
        assert dumped_empty['api_key'] == REDACTED_SECRET_VALUE

    def test_encrypted_differs_from_plaintext(self):
        # Arrange
        cipher = Cipher('test-secret')
        llm = LLM(model='openai/gpt-4o', api_key=SecretStr('sk-unique'))
        ctx_enc = build_expose_context('encrypted', cipher)
        ctx_plain = build_expose_context('plaintext', cipher)

        # Act
        enc = llm.model_dump(mode='json', context=ctx_enc)['api_key']
        plain = llm.model_dump(mode='json', context=ctx_plain)['api_key']

        # Assert
        assert enc != plain
        assert plain == 'sk-unique'
        assert enc.startswith(FERNET_TOKEN_PREFIX)

    def test_encrypted_can_be_decrypted(self):
        # Arrange
        cipher = Cipher('test-secret')
        secret = 'sk-decrypt-me'
        llm = LLM(model='openai/gpt-4o', api_key=SecretStr(secret))
        ctx_enc = build_expose_context('encrypted', cipher)

        # Act
        enc = llm.model_dump(mode='json', context=ctx_enc)['api_key']
        decrypted = cipher.decrypt(enc)

        # Assert
        assert decrypted is not None
        assert decrypted.get_secret_value() == secret


class TestLocalProtocolSettingsEndpoint:
    """Integration tests via TestClient with mocked UserAuth."""

    def test_get_settings_encrypted_returns_fernet(self, monkeypatch):
        # Arrange
        from unittest.mock import AsyncMock, MagicMock

        from fastapi.testclient import TestClient

        from openhands.app_server.app import app
        from openhands.app_server.settings.settings_models import Settings
        from openhands.sdk.settings import OpenHandsAgentSettings

        monkeypatch.setenv('OH_SECRET_KEY', 'test-secret-key-12345')
        monkeypatch.delenv('SESSION_API_KEY', raising=False)

        settings = Settings(
            agent_settings=OpenHandsAgentSettings(
                llm=LLM(model='openai/gpt-4o', api_key=SecretStr('sk-test-secret'))
            ),
            search_api_key=SecretStr('search-secret'),
        )

        class MockAuth:
            async def get_user_settings(self):
                return settings

            async def get_user_settings_store(self):
                m = MagicMock()
                m.get_org_marketplaces = AsyncMock(return_value=[])
                return m

            async def get_secrets_store(self):
                return None

            async def get_user_id(self):
                return 'test-user'

            async def get_provider_tokens(self):
                return None

        mock_auth = MockAuth()
        with monkeypatch.context() as m:
            # Patch both import paths used inside _get_settings_inner
            m.setattr(
                'openhands.app_server.user_auth.user_auth.get_user_auth',
                AsyncMock(return_value=mock_auth),
            )
            m.setattr(
                'openhands.app_server.user_auth.get_user_auth',
                AsyncMock(return_value=mock_auth),
            )
            client = TestClient(app)

            # Act: without header -> redacted
            resp_plain = client.get('/api/settings')
            # Act: with encrypted -> Fernet
            resp_enc = client.get(
                '/api/settings', headers={'X-Expose-Secrets': 'encrypted'}
            )
            resp_pt = client.get(
                '/api/settings', headers={'X-Expose-Secrets': 'plaintext'}
            )

        # Assert: without header api_key null
        assert resp_plain.status_code == 200
        assert resp_plain.json()['agent_settings']['llm']['api_key'] is None

        # Assert: encrypted starts with gAAAAA and decrypts
        assert resp_enc.status_code == 200
        enc = resp_enc.json()['agent_settings']['llm']['api_key']
        assert isinstance(enc, str) and enc.startswith('gAAAAA')
        cipher = Cipher('test-secret-key-12345')
        assert cipher.decrypt(enc).get_secret_value() == 'sk-test-secret'
        assert enc != 'sk-test-secret'

        # Assert: plaintext returns raw
        assert resp_pt.status_code == 200
        assert resp_pt.json()['agent_settings']['llm']['api_key'] == 'sk-test-secret'

    def test_get_profile_encrypted_returns_fernet(self, monkeypatch):
        # Arrange
        from unittest.mock import AsyncMock, MagicMock

        from fastapi.testclient import TestClient

        from openhands.app_server.app import app
        from openhands.app_server.settings.settings_models import Settings
        from openhands.sdk.settings import OpenHandsAgentSettings

        monkeypatch.setenv('OH_SECRET_KEY', 'test-secret-key-12345')
        monkeypatch.delenv('SESSION_API_KEY', raising=False)

        settings = Settings(
            agent_settings=OpenHandsAgentSettings(
                llm=LLM(model='openai/gpt-4o', api_key=SecretStr('sk-current'))
            ),
        )
        settings.llm_profiles.save(
            'my-profile', LLM(model='openai/gpt-4o', api_key=SecretStr('sk-profile-secret'))
        )

        class MockAuth:
            async def get_user_settings(self):
                return settings

            async def get_user_settings_store(self):
                m = MagicMock()
                m.get_org_marketplaces = AsyncMock(return_value=[])
                return m

            async def get_secrets_store(self):
                return None

            async def get_user_id(self):
                return 'test-user'

            async def get_provider_tokens(self):
                return None

        mock_auth = MockAuth()
        with monkeypatch.context() as m:
            m.setattr(
                'openhands.app_server.user_auth.user_auth.get_user_auth',
                AsyncMock(return_value=mock_auth),
            )
            m.setattr(
                'openhands.app_server.user_auth.get_user_auth',
                AsyncMock(return_value=mock_auth),
            )
            client = TestClient(app)
            resp_none = client.get('/api/profiles/my-profile')
            resp_enc = client.get(
                '/api/profiles/my-profile', headers={'X-Expose-Secrets': 'encrypted'}
            )
            resp_pt = client.get(
                '/api/profiles/my-profile', headers={'X-Expose-Secrets': 'plaintext'}
            )

        # Assert
        assert resp_none.status_code == 200
        assert resp_none.json()['config']['api_key'] is None
        assert resp_enc.status_code == 200
        assert resp_enc.json()['config']['api_key'].startswith('gAAAAA')
        cipher = Cipher('test-secret-key-12345')
        assert cipher.decrypt(resp_enc.json()['config']['api_key']).get_secret_value() == 'sk-profile-secret'
        assert resp_pt.json()['config']['api_key'] == 'sk-profile-secret'

    def test_get_settings_encrypted_without_key_returns_503(self, monkeypatch):
        # Arrange
        from unittest.mock import AsyncMock, MagicMock

        from fastapi.testclient import TestClient

        from openhands.app_server.app import app
        from openhands.app_server.settings.settings_models import Settings
        from openhands.sdk.settings import OpenHandsAgentSettings

        monkeypatch.delenv('OH_SECRET_KEY', raising=False)
        monkeypatch.delenv('SESSION_API_KEY', raising=False)

        settings = Settings(
            agent_settings=OpenHandsAgentSettings(
                llm=LLM(model='openai/gpt-4o', api_key=SecretStr('sk-test-secret'))
            ),
        )

        class MockAuth:
            async def get_user_settings(self):
                return settings

            async def get_user_settings_store(self):
                m = MagicMock()
                m.get_org_marketplaces = AsyncMock(return_value=[])
                return m

            async def get_secrets_store(self):
                return None

            async def get_user_id(self):
                return 'test-user'

            async def get_provider_tokens(self):
                return None

        mock_auth = MockAuth()
        with monkeypatch.context() as m:
            m.setattr(
                'openhands.app_server.user_auth.user_auth.get_user_auth',
                AsyncMock(return_value=mock_auth),
            )
            m.setattr(
                'openhands.app_server.user_auth.get_user_auth',
                AsyncMock(return_value=mock_auth),
            )
            client = TestClient(app)
            resp = client.get('/api/settings', headers={'X-Expose-Secrets': 'encrypted'})

        # Assert
        assert resp.status_code == 503

    def test_get_settings_returns_defaults_when_missing(self, monkeypatch):
        # Arrange: no settings stored yet → should return 200 with defaults
        from unittest.mock import AsyncMock, MagicMock

        from fastapi.testclient import TestClient

        from openhands.app_server.app import app

        monkeypatch.setenv('OH_SECRET_KEY', 'test-secret-key-12345')
        monkeypatch.delenv('SESSION_API_KEY', raising=False)

        class MockAuth:
            async def get_user_settings(self):
                return None

            async def get_user_settings_store(self):
                m = MagicMock()
                m.get_org_marketplaces = AsyncMock(return_value=[])
                return m

            async def get_secrets_store(self):
                return None

            async def get_user_id(self):
                return 'test-user'

            async def get_provider_tokens(self):
                return None

        mock_auth = MockAuth()
        with monkeypatch.context() as m:
            m.setattr(
                'openhands.app_server.user_auth.user_auth.get_user_auth',
                AsyncMock(return_value=mock_auth),
            )
            m.setattr(
                'openhands.app_server.user_auth.get_user_auth',
                AsyncMock(return_value=mock_auth),
            )
            client = TestClient(app)
            resp_plain = client.get('/api/settings')
            resp_enc = client.get(
                '/api/settings', headers={'X-Expose-Secrets': 'encrypted'}
            )

        # Assert: both return 200 JSON without "error" key, with defaults
        assert resp_plain.status_code == 200
        body = resp_plain.json()
        assert 'error' not in body
        assert body['llm_api_key_set'] is False
        assert body['agent_settings']['llm']['model'] is not None

        assert resp_enc.status_code == 200
        body_enc = resp_enc.json()
        assert 'error' not in body_enc
        assert body_enc['llm_api_key_set'] is False
        # No secrets → encrypted field stays None (no gAAAAA needed)
        assert body_enc['agent_settings']['llm']['api_key'] is None
