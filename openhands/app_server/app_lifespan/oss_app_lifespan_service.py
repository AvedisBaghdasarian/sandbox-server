from __future__ import annotations

import logging
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from openhands.app_server.app_lifespan.app_lifespan_service import AppLifespanService

_logger = logging.getLogger(__name__)


class OssAppLifespanService(AppLifespanService):
    run_alembic_on_startup: bool = True

    async def __aenter__(self):
        if self.run_alembic_on_startup:
            self.run_alembic()
        await self._reconcile_orphan_sandboxes()
        return self

    async def _reconcile_orphan_sandboxes(self) -> None:
        """Delete sandbox containers/volumes with no referencing DB row.

        Best-effort: reconciliation must never block or break app boot.
        Skipped for non-Docker sandbox services (process mode has no
        persistent orphans). Reads sandbox ids straight from the
        ``conversation_metadata`` table so no user/request context is needed.
        """
        try:
            from sqlalchemy import select

            from openhands.app_server.app_conversation.sql_app_conversation_info_service import (
                StoredConversationMetadata,
            )
            from openhands.app_server.config import get_sandbox_service
            from openhands.app_server.sandbox.docker_sandbox_service import (
                ConversationShell,
                DockerSandboxService,
            )
            from openhands.app_server.services.db_session import get_db_session
            from openhands.app_server.services.injector import InjectorState

            state = InjectorState()
            referenced: set[str] = set()
            shells_by_sandbox: dict[str, list] = {}
            async with get_db_session(state) as db_session:
                rows = await db_session.execute(
                    select(
                        StoredConversationMetadata.conversation_id,
                        StoredConversationMetadata.sandbox_id,
                        StoredConversationMetadata.llm_model,
                        StoredConversationMetadata.tags,
                    )
                )
                for conversation_id, sandbox_id, llm_model, tags in rows.all():
                    if not sandbox_id:
                        continue
                    referenced.add(sandbox_id)
                    working_dir = None
                    if isinstance(tags, dict):
                        working_dir = tags.get('archiveworkspacepath')
                    shells_by_sandbox.setdefault(sandbox_id, []).append(
                        ConversationShell(
                            conversation_id=str(conversation_id),
                            llm_model=llm_model,
                            working_dir=working_dir,
                        )
                    )
            async with get_sandbox_service(state) as sandbox_service:
                if not isinstance(sandbox_service, DockerSandboxService):
                    return
                removed = await sandbox_service.cleanup_orphan_sandboxes(referenced)
                volumes = await sandbox_service.cleanup_orphan_volumes()
                if removed or volumes:
                    _logger.info(
                        'Orphan reconciliation removed '
                        f'{removed} sandbox(es) and {volumes} volume(s)'
                    )
                # Recycle REFERENCED sandboxes whose webhook callback URL went
                # stale (e.g. host_port changed across restarts); recycle_sandbox
                # no-ops on fresh ones and logs loudly on real recycles.
                # Per-sandbox try/except: one bad recycle must never break boot.
                for sandbox_id in sorted(referenced):
                    try:
                        await sandbox_service.recycle_sandbox(
                            sandbox_id,
                            shells_by_sandbox.get(sandbox_id, []),
                        )
                    except Exception:
                        _logger.exception(
                            f'Failed to recycle stale sandbox {sandbox_id}; '
                            'continuing boot',
                            stack_info=True,
                        )
        except Exception:
            _logger.exception(
                'Sandbox orphan reconciliation failed; continuing boot',
                stack_info=True,
            )

    async def __aexit__(self, exc_type, exc_value, traceback):
        pass

    def run_alembic(self):
        # Run alembic upgrade head to ensure database is up to date
        alembic_dir = Path(__file__).parent / 'alembic'
        alembic_ini = alembic_dir / 'alembic.ini'

        # Create alembic config with absolute paths
        alembic_cfg = Config(str(alembic_ini))
        alembic_cfg.set_main_option('script_location', str(alembic_dir))

        # Change to alembic directory for the command execution
        original_cwd = os.getcwd()
        try:
            os.chdir(str(alembic_dir.parent))
            command.upgrade(alembic_cfg, 'head')
        finally:
            os.chdir(original_cwd)
