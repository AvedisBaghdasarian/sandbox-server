"""create_conversation maps a still-starting sandbox to retryable 503."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from openhands.app_server.errors import SandboxError
from openhands.app_server.local_protocol.router import create_conversation
from openhands.app_server.sandbox.sandbox_models import SandboxInfo, SandboxStatus


def _sandbox(status: SandboxStatus) -> SandboxInfo:
    return SandboxInfo(
        id='sb1',
        created_by_user_id=None,
        sandbox_spec_id='spec',
        status=status,
        session_api_key=None,
        created_at=datetime.now(timezone.utc),
    )


def _request() -> MagicMock:
    request = MagicMock()
    request.base_url = 'http://localhost:3000/'
    return request


class TestCreateConversationTimeout:
    @pytest.mark.asyncio
    async def test_still_starting_maps_to_503(self):
        """wait_for timeout while STARTING → 503 retryable."""
        # Arrange
        sandbox_service = MagicMock()
        sandbox_service.start_sandbox = AsyncMock(
            return_value=_sandbox(SandboxStatus.STARTING)
        )
        sandbox_service.wait_for_sandbox_running = AsyncMock(
            side_effect=SandboxError('timed out')
        )
        sandbox_service.get_sandbox = AsyncMock(
            return_value=_sandbox(SandboxStatus.STARTING)
        )

        # Act
        with pytest.raises(HTTPException) as ei:
            await create_conversation(
                _request(),
                {},
                sandbox_service=sandbox_service,
                sandbox_spec_service=MagicMock(),
                app_conversation_info_service=MagicMock(),
                httpx_client=AsyncMock(spec=httpx.AsyncClient),
            )

        # Assert
        assert ei.value.status_code == 503
        assert 'still starting' in ei.value.detail

    @pytest.mark.asyncio
    async def test_error_state_maps_to_502(self):
        """wait_for failure with ERROR sandbox → 502."""
        # Arrange
        sandbox_service = MagicMock()
        sandbox_service.start_sandbox = AsyncMock(
            return_value=_sandbox(SandboxStatus.ERROR)
        )
        sandbox_service.wait_for_sandbox_running = AsyncMock(
            side_effect=SandboxError('crashed')
        )
        sandbox_service.get_sandbox = AsyncMock(
            return_value=_sandbox(SandboxStatus.ERROR)
        )

        # Act
        with pytest.raises(HTTPException) as ei:
            await create_conversation(
                _request(),
                {},
                sandbox_service=sandbox_service,
                sandbox_spec_service=MagicMock(),
                app_conversation_info_service=MagicMock(),
                httpx_client=AsyncMock(spec=httpx.AsyncClient),
            )

        # Assert
        assert ei.value.status_code == 502
        assert 'crashed' in ei.value.detail

    @pytest.mark.asyncio
    async def test_uses_configured_startup_timeout(self, monkeypatch):
        """wait_for uses OH_SANDBOX_STARTUP_TIMEOUT (default 300)."""
        # Arrange
        from openhands.app_server.sandbox.sandbox_service import (
            get_sandbox_startup_timeout,
        )

        monkeypatch.delenv('OH_SANDBOX_STARTUP_TIMEOUT', raising=False)
        assert get_sandbox_startup_timeout() == 300
        monkeypatch.setenv('OH_SANDBOX_STARTUP_TIMEOUT', '45')
        assert get_sandbox_startup_timeout() == 45
        monkeypatch.setenv('OH_SANDBOX_STARTUP_TIMEOUT', 'bogus')
        assert get_sandbox_startup_timeout() == 300
