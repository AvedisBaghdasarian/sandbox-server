"""Boot reconciliation recycles referenced stale sandboxes, deletes orphans."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openhands.app_server.app_lifespan.oss_app_lifespan_service import (
    OssAppLifespanService,
)
from openhands.app_server.sandbox.docker_sandbox_service import (
    ConversationShell,
    DockerSandboxService,
)


def _db_rows(rows):
    """Fake get_db_session CM yielding a session returning the given rows."""
    session = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    session.execute = AsyncMock(return_value=result)

    @asynccontextmanager
    async def _ctx(state):
        yield session

    return _ctx


def _sandbox_service_cm(service):
    @asynccontextmanager
    async def _ctx(state):
        yield service

    return _ctx


def _docker_service():
    service = DockerSandboxService(
        sandbox_spec_service=MagicMock(),
        container_name_prefix='oh-test-',
        host_port=3000,
        container_url_pattern='http://localhost:{port}',
        mounts=[],
        exposed_ports=[],
        health_check_path='/health',
        httpx_client=AsyncMock(),
        max_num_sandboxes=3,
        docker_client=MagicMock(),
    )
    service.cleanup_orphan_sandboxes = AsyncMock(return_value=0)
    service.cleanup_orphan_volumes = AsyncMock(return_value=0)
    service.recycle_sandbox = AsyncMock(return_value=None)
    return service


class TestBootReconcile:
    @pytest.mark.asyncio
    async def test_referenced_recycled_unreferenced_not(self):
        """Only DB-referenced sandboxes go to recycle; orphans go to deletion."""
        # Arrange: one conversation row referencing oh-test-ref
        rows = [
            (
                'conv1',
                'oh-test-ref',
                'model-x',
                {'archiveworkspacepath': '/ws'},
            )
        ]
        service = _docker_service()

        # Act
        with (
            patch(
                'openhands.app_server.services.db_session.get_db_session',
                _db_rows(rows),
            ),
            patch(
                'openhands.app_server.config.get_sandbox_service',
                _sandbox_service_cm(service),
            ),
        ):
            await OssAppLifespanService()._reconcile_orphan_sandboxes()

        # Assert: orphan path got exactly the referenced set; recycle got shells
        service.cleanup_orphan_sandboxes.assert_called_once_with({'oh-test-ref'})
        service.recycle_sandbox.assert_called_once()
        args, _ = service.recycle_sandbox.call_args
        assert args[0] == 'oh-test-ref'
        assert args[1] == [
            ConversationShell(
                conversation_id='conv1',
                llm_model='model-x',
                working_dir='/ws',
            )
        ]

    @pytest.mark.asyncio
    async def test_non_docker_service_skips(self):
        """Process mode has no persistent orphans — nothing runs."""
        # Arrange
        service = MagicMock()  # not a DockerSandboxService

        # Act
        with (
            patch(
                'openhands.app_server.services.db_session.get_db_session',
                _db_rows([]),
            ),
            patch(
                'openhands.app_server.config.get_sandbox_service',
                _sandbox_service_cm(service),
            ),
        ):
            await OssAppLifespanService()._reconcile_orphan_sandboxes()

        # Assert: no crash, nothing else needed (early return before cleanup)

    @pytest.mark.asyncio
    async def test_recycle_failure_does_not_break_boot(self):
        """A failing recycle is contained per-sandbox; boot continues."""
        # Arrange
        rows = [('conv1', 'oh-test-ref', None, None)]
        service = _docker_service()
        service.recycle_sandbox = AsyncMock(side_effect=RuntimeError('boom'))

        # Act (must not raise)
        with (
            patch(
                'openhands.app_server.services.db_session.get_db_session',
                _db_rows(rows),
            ),
            patch(
                'openhands.app_server.config.get_sandbox_service',
                _sandbox_service_cm(service),
            ),
        ):
            await OssAppLifespanService()._reconcile_orphan_sandboxes()

        # Assert
        service.recycle_sandbox.assert_called_once()
