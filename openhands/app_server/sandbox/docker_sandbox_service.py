import asyncio
import logging
import os
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import AsyncGenerator

import base62
import docker
import httpx
from docker.errors import APIError, NotFound
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from openhands.agent_server.utils import utc_now
from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.docker_sandbox_spec_service import get_docker_client
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    VSCODE,
    WORKER_1,
    WORKER_2,
    ExposedUrl,
    SandboxInfo,
    SandboxPage,
    SandboxRecord,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_service import (
    SESSION_API_KEY_VARIABLE,
    WEBHOOK_CALLBACK_VARIABLE,
    SandboxService,
    SandboxServiceInjector,
    get_sandbox_startup_timeout,
)
from openhands.app_server.sandbox.sandbox_spec_service import (
    SandboxSpecService,
    resolve_sandbox_spec,
)
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.utils.docker_utils import (
    replace_localhost_hostname_for_docker,
)

_logger = logging.getLogger(__name__)
STARTUP_GRACE_SECONDS = 15

_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')

WORKSPACE_VOLUME_PREFIX = 'openhands-workspace-'


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from container log output."""
    return _ANSI_ESCAPE_RE.sub('', text)


@dataclass
class ConversationShell:
    """Minimal identity of a conversation to re-create on a recycled sandbox."""

    conversation_id: str
    llm_model: str | None = None
    working_dir: str | None = None


def _get_use_host_network_default() -> bool:
    """Get the default value for use_host_network from environment variables.

    This function is called at runtime (not at class definition time) to ensure
    that environment variable changes are picked up correctly.
    """
    value = os.getenv('AGENT_SERVER_USE_HOST_NETWORK', '')
    return value.lower() in ('true', '1', 'yes')


def _get_kvm_enabled_default() -> bool:
    """Get the default value for kvm_enabled from environment variables."""
    value = os.getenv('SANDBOX_KVM_ENABLED', '')
    return value.lower() in ('true', '1', 'yes')


class VolumeMount(BaseModel):
    """Mounted volume within the container."""

    host_path: str
    container_path: str
    mode: str = 'rw'

    model_config = ConfigDict(frozen=True)


class ExposedPort(BaseModel):
    """Exposed port within container to be matched to a free port on the host."""

    name: str
    description: str
    container_port: int = 8000

    model_config = ConfigDict(frozen=True)


@dataclass
class DockerSandboxService(SandboxService):
    """Sandbox service built on docker.

    The Docker API does not currently support async operations, so some of these operations will block.
    Given that the docker API is intended for local use on a single machine, this is probably acceptable.
    """

    sandbox_spec_service: SandboxSpecService
    container_name_prefix: str
    host_port: int
    container_url_pattern: str
    mounts: list[VolumeMount]
    exposed_ports: list[ExposedPort]
    health_check_path: str | None
    httpx_client: httpx.AsyncClient
    max_num_sandboxes: int
    web_url: str | None = None
    permitted_cors_origins: list[str] = field(default_factory=list)
    extra_hosts: dict[str, str] = field(default_factory=dict)
    docker_client: docker.DockerClient = field(default_factory=get_docker_client)
    startup_grace_seconds: int = STARTUP_GRACE_SECONDS
    use_host_network: bool = False
    kvm_enabled: bool = False
    default_sandbox_spec_id: str | None = None

    async def _docker(self, func, *args, **kwargs):
        """Run a blocking docker-py call in a worker thread.

        The Docker SDK is synchronous; every daemon round-trip goes through
        here so slow ops (image pulls, stops) never freeze the event loop.
        Pass bound methods (e.g. ``self.docker_client.containers.list``) so
        the DI seam for tests is preserved, along with NotFound/APIError
        mapping which behaves identically across the thread boundary.
        """
        return await asyncio.to_thread(func, *args, **kwargs)

    def _expected_webhook_base_url(self) -> str:
        """Webhook callback URL sandboxes must carry (mirrors start_sandbox)."""
        return f'http://host.docker.internal:{self.host_port}/api/v1/webhooks'

    def is_webhook_url_stale(self, env: dict[str, str | None]) -> bool:
        """True when a container's webhook callback URL differs from current config.

        A missing ``OH_WEBHOOKS_0_BASE_URL`` counts as stale. Pure function of
        the container env dict and ``self.host_port`` — unit-testable without
        Docker.
        """
        return env.get(WEBHOOK_CALLBACK_VARIABLE) != self._expected_webhook_base_url()

    def _find_unused_port(self) -> int:
        """Find an unused port on the host machine."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            s.listen(1)
            port = s.getsockname()[1]
        return port

    def _docker_status_to_sandbox_status(self, docker_status: str) -> SandboxStatus:
        """Convert Docker container status to SandboxStatus."""
        status_mapping = {
            'running': SandboxStatus.RUNNING,
            'paused': SandboxStatus.PAUSED,
            # An exited container is dead, not paused — surface it as ERROR
            # with the exit code / log tail in status_detail.
            'exited': SandboxStatus.ERROR,
            'created': SandboxStatus.STARTING,
            'restarting': SandboxStatus.STARTING,
            'removing': SandboxStatus.MISSING,
            'dead': SandboxStatus.ERROR,
        }
        return status_mapping.get(docker_status.lower(), SandboxStatus.ERROR)

    async def _error_status_detail(self, container) -> str | None:
        """Build status_detail for an errored container: exit code + log tail."""
        parts: list[str] = []
        try:
            exit_code = container.attrs.get('State', {}).get('ExitCode')
        except Exception:
            exit_code = None
        if exit_code is not None:
            parts.append(f'exit code {exit_code}')
        try:
            raw_logs = await self._docker(container.logs, tail=20)
        except Exception:
            raw_logs = b''
        if isinstance(raw_logs, bytes):
            log_text = raw_logs.decode('utf-8', errors='replace')
        else:
            log_text = str(raw_logs)
        log_text = _strip_ansi(log_text).strip()
        if len(log_text) > 600:
            log_text = log_text[-600:]
        if log_text:
            parts.append(f'logs: {log_text}')
        return '; '.join(parts) if parts else None

    def _get_container_env_vars(self, container) -> dict[str, str | None]:
        env_vars_list = container.attrs['Config']['Env']
        result = {}
        for env_var in env_vars_list:
            if '=' in env_var:
                key, value = env_var.split('=', 1)
                result[key] = value
            else:
                # Handle cases where an environment variable might not have a value
                result[env_var] = None
        return result

    async def _container_to_sandbox_info(self, container) -> SandboxInfo | None:
        """Convert Docker container to SandboxInfo."""
        # Convert Docker status to runtime status
        status = self._docker_status_to_sandbox_status(container.status)

        # Parse creation time
        created_str = container.attrs.get('Created', '')
        try:
            created_at = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            created_at = utc_now()

        # Get URL and session key for running containers
        exposed_urls = None
        session_api_key = None

        if status == SandboxStatus.RUNNING:
            # Get session API key first
            env = self._get_container_env_vars(container)
            session_api_key = env.get(SESSION_API_KEY_VARIABLE)

            # Get the exposed port mappings
            exposed_urls = []

            # Check if container is using host network mode
            network_mode = container.attrs.get('HostConfig', {}).get('NetworkMode', '')
            is_host_network = network_mode == 'host'

            if is_host_network:
                # Host network mode: container ports are directly accessible on host
                for exposed_port in self.exposed_ports:
                    host_port = exposed_port.container_port
                    url = self.container_url_pattern.format(port=host_port)

                    # VSCode URLs require the api_key and working dir
                    if exposed_port.name == VSCODE:
                        url += f'/?tkn={session_api_key}&folder={container.attrs["Config"]["WorkingDir"]}'

                    exposed_urls.append(
                        ExposedUrl(
                            name=exposed_port.name,
                            url=url,
                            port=exposed_port.container_port,
                        )
                    )
            else:
                # Bridge network mode: use port bindings
                port_bindings = container.attrs.get('NetworkSettings', {}).get(
                    'Ports', {}
                )
                if port_bindings:
                    for container_port, host_bindings in port_bindings.items():
                        if host_bindings:
                            host_port = int(host_bindings[0]['HostPort'])
                            matching_port = next(
                                (
                                    ep
                                    for ep in self.exposed_ports
                                    if container_port == f'{ep.container_port}/tcp'
                                ),
                                None,
                            )
                            if matching_port:
                                url = self.container_url_pattern.format(port=host_port)

                                # VSCode URLs require the api_key and working dir
                                if matching_port.name == VSCODE:
                                    url += f'/?tkn={session_api_key}&folder={container.attrs["Config"]["WorkingDir"]}'

                                exposed_urls.append(
                                    ExposedUrl(
                                        name=matching_port.name,
                                        url=url,
                                        port=matching_port.container_port,
                                    )
                                )

        if not container.image.tags:
            _logger.debug(
                f'Skipping container {container.name!r}: image has no tags (image id: {container.image.id})'
            )
            return None

        status_detail = None
        if status == SandboxStatus.ERROR:
            status_detail = await self._error_status_detail(container)

        return SandboxInfo(
            id=container.name,
            created_by_user_id=None,
            sandbox_spec_id=container.image.tags[0],
            status=status,
            session_api_key=session_api_key,
            exposed_urls=exposed_urls,
            created_at=created_at,
            status_detail=status_detail,
        )

    async def _container_to_checked_sandbox_info(self, container) -> SandboxInfo | None:
        sandbox_info = await self._container_to_sandbox_info(container)
        if (
            sandbox_info
            and self.health_check_path is not None
            and sandbox_info.exposed_urls
        ):
            app_server_url = next(
                exposed_url.url
                for exposed_url in sandbox_info.exposed_urls
                if exposed_url.name == AGENT_SERVER
            )
            try:
                # When running in Docker, replace localhost hostname with host.docker.internal for internal requests
                app_server_url = replace_localhost_hostname_for_docker(app_server_url)

                response = await self.httpx_client.get(
                    f'{app_server_url}{self.health_check_path}'
                )
                response.raise_for_status()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Get the started_at from the docker container info and fallback to sandbox created_at
                try:
                    state = container.attrs['State']
                    started_at = datetime.fromisoformat(state['StartedAt'])
                except Exception:
                    _logger.debug('Error getting container start time')
                    started_at = sandbox_info.created_at

                # If the server has exceeded the startup grace period, it's an error
                if started_at < utc_now() - timedelta(
                    seconds=self.startup_grace_seconds
                ):
                    _logger.info(
                        f'Sandbox server not running: {app_server_url} : {exc}'
                    )
                    sandbox_info.status = SandboxStatus.ERROR
                else:
                    _logger.debug(
                        f'Sandbox server not yet available (still starting): '
                        f'{app_server_url} : {exc}'
                    )
                    sandbox_info.status = SandboxStatus.STARTING
                sandbox_info.exposed_urls = None
                sandbox_info.session_api_key = None
        return sandbox_info

    async def search_sandboxes(
        self,
        page_id: str | None = None,
        limit: int = 100,
    ) -> SandboxPage:
        """Search for sandboxes."""
        try:
            # Get all containers with our prefix
            all_containers = await self._docker(
                self.docker_client.containers.list, all=True
            )
            sandboxes = []

            for container in all_containers:
                if container.name and container.name.startswith(
                    self.container_name_prefix
                ):
                    sandbox_info = await self._container_to_checked_sandbox_info(
                        container
                    )
                    if sandbox_info:
                        sandboxes.append(sandbox_info)

            # Sort by creation time (newest first)
            sandboxes.sort(key=lambda x: x.created_at, reverse=True)

            # Apply pagination
            start_idx = 0
            if page_id:
                try:
                    start_idx = int(page_id)
                except ValueError:
                    start_idx = 0

            end_idx = start_idx + limit
            paginated_containers = sandboxes[start_idx:end_idx]

            # Determine next page ID
            next_page_id = None
            if end_idx < len(sandboxes):
                next_page_id = str(end_idx)

            return SandboxPage(items=paginated_containers, next_page_id=next_page_id)

        except APIError:
            return SandboxPage(items=[], next_page_id=None)

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        """Get a single sandbox info."""
        try:
            if not sandbox_id.startswith(self.container_name_prefix):
                return None
            container = await self._docker(
                self.docker_client.containers.get, sandbox_id
            )
            return await self._container_to_checked_sandbox_info(container)
        except (NotFound, APIError):
            return None

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        """Get a single sandbox by session API key."""
        try:
            # Get all containers with our prefix
            all_containers = await self._docker(
                self.docker_client.containers.list, all=True
            )

            for container in all_containers:
                if container.name and container.name.startswith(
                    self.container_name_prefix
                ):
                    # Check if this container has the matching session API key
                    env_vars = self._get_container_env_vars(container)
                    container_session_key = env_vars.get(SESSION_API_KEY_VARIABLE)

                    if container_session_key == session_api_key:
                        return await self._container_to_checked_sandbox_info(container)

            return None
        except (NotFound, APIError):
            return None

    async def get_sandbox_record_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxRecord | None:
        """Get persisted sandbox identity by session API key."""
        try:
            all_containers = await self._docker(
                self.docker_client.containers.list, all=True
            )
            for container in all_containers:
                if container.name and container.name.startswith(
                    self.container_name_prefix
                ):
                    env_vars = self._get_container_env_vars(container)
                    if env_vars.get(SESSION_API_KEY_VARIABLE) == session_api_key:
                        return SandboxRecord(
                            id=container.name,
                            created_by_user_id=None,
                        )
            return None
        except (NotFound, APIError):
            return None

    async def start_sandbox(
        self,
        sandbox_spec_id: str | None = None,
        sandbox_id: str | None = None,
        exempt_sandbox_ids: set[str] | None = None,
    ) -> SandboxInfo:
        """Start a new sandbox."""
        # Warn about port collision risk when using host network mode with multiple sandboxes
        if self.use_host_network and self.max_num_sandboxes > 1:
            _logger.warning(
                'Host network mode is enabled with max_num_sandboxes > 1. '
                'Multiple sandboxes will attempt to bind to the same ports, '
                'which may cause port collision errors. Consider setting '
                'max_num_sandboxes=1 when using host network mode.'
            )

        # Enforce sandbox limits by cleaning up old sandboxes
        await self.pause_old_sandboxes(self.max_num_sandboxes - 1, exempt_sandbox_ids)

        sandbox_spec = await resolve_sandbox_spec(
            sandbox_spec_id,
            self.default_sandbox_spec_id,
            self.sandbox_spec_service,
            _logger,
        )

        # Generate a sandbox id if none was provided
        if sandbox_id is None:
            sandbox_id = base62.encodebytes(os.urandom(16))

        # Generate container name and session api key
        container_name = f'{self.container_name_prefix}{sandbox_id}'
        session_api_key = base62.encodebytes(os.urandom(32))

        env_vars = self._build_sandbox_env_vars(sandbox_spec, session_api_key)
        port_mappings = self._build_port_mappings(env_vars)

        # Prepare labels
        labels = {
            'sandbox_spec_id': sandbox_spec.id,
        }

        # Prepare volumes
        volumes = {
            mount.host_path: {
                'bind': mount.container_path,
                'mode': mount.mode,
            }
            for mount in self.mounts
        }

        if self.use_host_network:
            _logger.info(f'Starting sandbox {container_name} with host network mode')

        if self.kvm_enabled:
            _logger.info(
                f'Starting sandbox {container_name} with KVM device passthrough'
            )

        container = await self._run_container(
            image=sandbox_spec.id,
            command=sandbox_spec.command,  # Use default command from image
            name=container_name,
            env_vars=env_vars,
            port_mappings=port_mappings,
            volumes=volumes,
            working_dir=sandbox_spec.working_dir,
            labels=labels,
        )

        sandbox_info = await self._container_to_sandbox_info(container)
        assert sandbox_info is not None
        return sandbox_info

    def _build_sandbox_env_vars(self, sandbox_spec, session_api_key: str) -> dict:
        """Build the container env: spec initial_env + session key + webhook + CORS."""
        env_vars = sandbox_spec.initial_env.copy()
        env_vars[SESSION_API_KEY_VARIABLE] = session_api_key
        env_vars[WEBHOOK_CALLBACK_VARIABLE] = self._expected_webhook_base_url()

        # Set CORS origins for remote browser access when web_url is configured.
        # This allows the agent-server container to accept requests from the
        # frontend when running OpenHands on a remote machine.
        # Each origin gets its own indexed env var (OH_ALLOW_CORS_ORIGINS_0, _1, etc.)
        cors_origins: list[str] = []
        if self.web_url:
            cors_origins.append(self.web_url)
        cors_origins.extend(self.permitted_cors_origins)
        # Deduplicate while preserving order
        seen: set[str] = set()
        for origin in cors_origins:
            if origin not in seen:
                seen.add(origin)
                idx = len(seen) - 1
                env_vars[f'OH_ALLOW_CORS_ORIGINS_{idx}'] = origin
        return env_vars

    def _build_port_mappings(self, env_vars: dict) -> dict[int, int] | None:
        """Build host port mappings, recording container ports in env_vars.

        When using host network, container ports are directly accessible on the
        host so no mapping is needed (returns None); otherwise maps each
        exposed container port to a random free host port. Mutates ``env_vars``
        with the ``{port.name}`` entries, exactly as start_sandbox always did.
        """
        if self.use_host_network:
            # Host network mode: container ports are directly accessible
            for exposed_port in self.exposed_ports:
                env_vars[exposed_port.name] = str(exposed_port.container_port)
            return None
        # Bridge network mode: map container ports to random host ports
        port_mappings: dict[int, int] = {}
        for exposed_port in self.exposed_ports:
            host_port = self._find_unused_port()
            port_mappings[exposed_port.container_port] = host_port
            env_vars[exposed_port.name] = str(exposed_port.container_port)
        return port_mappings

    async def _run_container(
        self,
        *,
        image: str,
        command,
        name: str,
        env_vars: dict,
        port_mappings: dict[int, int] | None,
        volumes: dict,
        working_dir: str | None,
        labels: dict,
    ):
        """Create and start a sandbox container (daemon call off the loop)."""
        # Determine network mode
        network_mode = 'host' if self.use_host_network else None

        # Determine devices to pass through (e.g., /dev/kvm for hardware virtualization)
        devices = ['/dev/kvm:/dev/kvm:rwm'] if self.kvm_enabled else None

        try:
            # Create and start the container
            container = await self._docker(
                self.docker_client.containers.run,  # type: ignore[call-overload,misc]
                image=image,
                command=command,
                remove=False,
                name=name,
                environment=env_vars,
                ports=port_mappings,
                volumes=volumes,
                working_dir=working_dir,
                labels=labels,
                detach=True,
                # Use Docker's tini init process to ensure proper signal handling and reaping of
                # zombie child processes.
                init=True,
                # Allow agent-server containers to resolve host.docker.internal
                # and other custom hostnames for LAN deployments
                # Note: extra_hosts is not needed with host network mode
                extra_hosts=self.extra_hosts
                if self.extra_hosts and not self.use_host_network
                else None,
                # Network mode: 'host' for host networking, None for default bridge
                network_mode=network_mode,
                # Device passthrough for KVM hardware virtualization
                devices=devices,
            )
            return container
        except APIError as e:
            raise SandboxError('Failed to start container') from e

    async def resume_sandbox(
        self, sandbox_id: str, exempt_sandbox_ids: set[str] | None = None
    ) -> bool:
        """Resume a paused sandbox."""
        # Enforce sandbox limits by cleaning up old sandboxes
        await self.pause_old_sandboxes(self.max_num_sandboxes - 1, exempt_sandbox_ids)

        try:
            if not sandbox_id.startswith(self.container_name_prefix):
                return False
            container = await self._docker(
                self.docker_client.containers.get, sandbox_id
            )

            if container.status == 'paused':
                await self._docker(container.unpause)
            elif container.status == 'exited':
                await self._docker(container.start)

            return True
        except (NotFound, APIError):
            return False

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        """Pause a running sandbox."""
        try:
            if not sandbox_id.startswith(self.container_name_prefix):
                return False
            container = await self._docker(
                self.docker_client.containers.get, sandbox_id
            )

            if container.status == 'running':
                await self._docker(container.pause)

            return True
        except (NotFound, APIError):
            return False

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        """Delete a sandbox."""
        try:
            if not sandbox_id.startswith(self.container_name_prefix):
                return False
            container = await self._docker(
                self.docker_client.containers.get, sandbox_id
            )

            # Stop the container if it's running
            if container.status in ['running', 'paused']:
                await self._docker(container.stop, timeout=10)

            # Remove the container
            await self._docker(container.remove)

            # Remove associated volume
            try:
                volume_name = f'{WORKSPACE_VOLUME_PREFIX}{sandbox_id}'
                volume = await self._docker(self.docker_client.volumes.get, volume_name)
                await self._docker(volume.remove)
            except (NotFound, APIError) as exc:
                # Volume might not exist or already removed — warn, don't fail
                _logger.warning(
                    f'Failed to remove volume for sandbox {sandbox_id}: {exc}'
                )

            return True
        except (NotFound, APIError):
            return False

    async def cleanup_orphan_sandboxes(self, referenced_sandbox_ids: set[str]) -> int:
        """Delete sandbox containers with no referencing DB row.

        Lists all containers with this service's name prefix and deletes any
        whose id is not in ``referenced_sandbox_ids`` (stop + remove, same
        semantics as ``delete_sandbox``). Failures are tolerated per-container.

        Returns the number of containers deleted.
        """
        deleted = 0
        try:
            all_containers = await self._docker(
                self.docker_client.containers.list, all=True
            )
        except APIError as exc:
            _logger.warning(f'Failed to list containers for orphan cleanup: {exc}')
            return 0
        for container in all_containers:
            name = getattr(container, 'name', None)
            if not name or not name.startswith(self.container_name_prefix):
                continue
            if name in referenced_sandbox_ids:
                continue
            try:
                if await self.delete_sandbox(name):
                    deleted += 1
                    _logger.info(f'Deleted orphan sandbox container {name}')
            except Exception as exc:
                _logger.warning(
                    f'Failed to delete orphan sandbox container {name}: {exc}'
                )
        return deleted

    async def cleanup_orphan_volumes(self) -> int:
        """Delete unreferenced ``openhands-workspace-*`` volumes.

        Skips any volume still attached to a container. Failures are tolerated
        per-volume. Returns the number of volumes deleted.
        """
        deleted = 0
        try:
            volumes = await self._docker(
                self.docker_client.volumes.list,
                filters={'name': WORKSPACE_VOLUME_PREFIX},
            )
        except APIError as exc:
            _logger.warning(f'Failed to list volumes for orphan cleanup: {exc}')
            return 0
        attached: set[str] = set()
        try:
            for container in await self._docker(
                self.docker_client.containers.list, all=True
            ):
                try:
                    mounts = container.attrs.get('Mounts', []) or []
                except Exception:
                    mounts = []
                for mount in mounts:
                    vol_name = mount.get('Name')
                    if vol_name:
                        attached.add(vol_name)
        except APIError as exc:
            _logger.warning(f'Failed to list containers for volume scan: {exc}')
            return 0
        for volume in volumes:
            vol_name = getattr(volume, 'name', None)
            if not vol_name or not vol_name.startswith(WORKSPACE_VOLUME_PREFIX):
                continue
            if vol_name in attached:
                continue
            try:
                await self._docker(volume.remove)
                deleted += 1
                _logger.info(f'Deleted orphan volume {vol_name}')
            except Exception as exc:
                _logger.warning(f'Failed to delete orphan volume {vol_name}: {exc}')
        return deleted

    async def recycle_sandbox(
        self, sandbox_id: str, conversations: list[ConversationShell] | None = None
    ) -> SandboxInfo | None:
        """Recycle a sandbox whose webhook callback URL is stale.

        Re-runs the container with the same name, session key, image, mounts
        (including the ``openhands-workspace-{sandbox_id}`` workspace volume),
        labels and working dir, but with freshly built env from the current
        sandbox spec — so the only differences are the corrected
        ``OH_WEBHOOKS_0_BASE_URL`` (plus current CORS origins / port env).
        The old container is quarantined as ``<name>-stale-<timestamp>`` first
        and removed only after the replacement is healthy; conversation shells
        are re-created idle (no initial message) afterwards.

        Returns the new ``SandboxInfo``, or None when the sandbox does not
        exist, is not in a recyclable state (only RUNNING/PAUSED/STARTING are
        recycled), or already carries the current webhook URL (no-op).

        Raises ``SandboxError`` when the rebuild fails even after a
        best-effort rollback (rename-back + start of the old container).

        What SURVIVES a recycle: sandbox id and name, browser session key,
        DB rows, workspace FILES (same volume reattached), image, mounts,
        labels, working dir, conversation shells (re-created idle; full event
        history served from the app_server archive via the adapter fallback).
        What does NOT survive: running processes, terminal sessions, jupyter
        kernels, the agent's in-memory working state (the live agent restarts
        from scratch on the next user message), and any events not yet POSTed
        to the app_server before the old container stopped.
        """
        if not sandbox_id.startswith(self.container_name_prefix):
            return None
        try:
            container = await self._docker(
                self.docker_client.containers.get, sandbox_id
            )
        except (NotFound, APIError):
            return None

        docker_status = container.status
        sandbox_status = self._docker_status_to_sandbox_status(docker_status)
        if sandbox_status not in (
            SandboxStatus.RUNNING,
            SandboxStatus.PAUSED,
            SandboxStatus.STARTING,
        ):
            _logger.info(
                f'Skipping recycle of sandbox {sandbox_id}: status {docker_status}'
            )
            return None

        env = self._get_container_env_vars(container)
        if not self.is_webhook_url_stale(env):
            # Fresh URL — nothing to do.
            return await self._container_to_checked_sandbox_info(container)

        session_api_key = env.get(SESSION_API_KEY_VARIABLE)
        if not session_api_key:
            _logger.error(
                f'Refusing to recycle sandbox {sandbox_id}: no session key to preserve'
            )
            return None
        old_url = env.get(WEBHOOK_CALLBACK_VARIABLE)

        image_tags = container.image.tags or []
        if not image_tags:
            _logger.error(
                f'Refusing to recycle sandbox {sandbox_id}: image has no tags'
            )
            return None
        image = image_tags[0]
        labels = dict(getattr(container, 'labels', None) or {})
        command = (container.attrs.get('Config') or {}).get('Cmd')
        config_working_dir = (container.attrs.get('Config') or {}).get('WorkingDir')

        # Rebuild the volumes mapping from the live mounts so the workspace
        # volume (and any other mounts) reattach identically.
        rebuilt_volumes: dict = {}
        for mount in container.attrs.get('Mounts', []) or []:
            if mount.get('Type') == 'volume':
                key = mount.get('Name')
            else:
                key = mount.get('Source')
            dest = mount.get('Destination')
            if not key or not dest:
                continue
            rebuilt_volumes[key] = {'bind': dest, 'mode': mount.get('Mode', 'rw')}
        expected_volume = f'{WORKSPACE_VOLUME_PREFIX}{sandbox_id}'
        if expected_volume not in rebuilt_volumes:
            _logger.warning(
                f'Recycling sandbox {sandbox_id} without its workspace volume '
                f'{expected_volume} attached to the old container'
            )

        # Resolve the CURRENT spec (same spec id the sandbox was built from,
        # so custom images are preserved) BEFORE touching the old container.
        try:
            sandbox_spec = await resolve_sandbox_spec(
                labels.get('sandbox_spec_id'),
                self.default_sandbox_spec_id,
                self.sandbox_spec_service,
                _logger,
            )
        except ValueError as exc:
            raise SandboxError(f'Cannot recycle sandbox {sandbox_id}: {exc}') from exc

        fresh_env = self._build_sandbox_env_vars(sandbox_spec, session_api_key)
        port_mappings = self._build_port_mappings(fresh_env)
        new_url = fresh_env.get(WEBHOOK_CALLBACK_VARIABLE)

        # Stop + quarantine the old container (tolerate already-stopped).
        try:
            await self._docker(container.stop, timeout=30)
        except (NotFound, APIError) as exc:
            _logger.warning(f'Failed to stop sandbox {sandbox_id} for recycle: {exc}')
        stale_name = f'{sandbox_id}-stale-{utc_now().strftime("%Y%m%dT%H%M%S")}'
        quarantined = False
        try:
            await self._docker(container.rename, stale_name)
            quarantined = True
        except (NotFound, APIError) as exc:
            # Old container still owns its name — try to leave it usable.
            _logger.error(
                f'Failed to quarantine sandbox {sandbox_id} for recycle: {exc}'
            )
            try:
                await self._docker(container.start)
            except (NotFound, APIError):
                pass
            raise SandboxError(
                f'Failed to recycle sandbox {sandbox_id}: quarantine failed'
            ) from exc

        try:
            await self._run_container(
                image=image,
                command=command,
                name=sandbox_id,
                env_vars=fresh_env,
                port_mappings=port_mappings,
                volumes=rebuilt_volumes,
                working_dir=sandbox_spec.working_dir or config_working_dir,
                labels=labels,
            )
            new_info = await self.wait_for_sandbox_running(
                sandbox_id,
                timeout=get_sandbox_startup_timeout(),
                poll_interval=2,
                httpx_client=self.httpx_client,
            )
            await self._recreate_conversation_shells(
                new_info,
                conversations or [],
                default_working_dir=sandbox_spec.working_dir or config_working_dir,
            )
        except Exception as exc:
            _logger.error(
                f'Recycle of sandbox {sandbox_id} failed; attempting rollback',
                exc_info=True,
            )
            await self._rollback_recycle(
                container, sandbox_id, quarantined, docker_status == 'paused'
            )
            raise SandboxError(f'Failed to recycle sandbox {sandbox_id}') from exc

        # Replacement is healthy — remove the quarantined stale container.
        try:
            await self._docker(container.remove)
        except (NotFound, APIError) as exc:
            _logger.error(
                f'Recycled sandbox {sandbox_id} but failed to remove quarantined '
                f'container {stale_name}: {exc}'
            )
        _logger.warning(
            f'Recycled sandbox {sandbox_id}: webhook {old_url} -> {new_url}. '
            'Workspace files, session key and conversation shells preserved; '
            'running processes, terminal sessions, jupyter kernels and '
            'unflushed events did NOT survive (see recycle_sandbox docstring).'
        )
        return new_info

    async def _rollback_recycle(
        self,
        stale_container,
        sandbox_id: str,
        quarantined: bool,
        was_paused: bool,
    ) -> None:
        """Best-effort rollback: rename the stale container back and start it."""
        try:
            stale_id = stale_container.id
        except Exception:
            stale_id = None
        try:
            if quarantined:
                await self._docker(stale_container.rename, sandbox_id)
            await self._docker(stale_container.start)
            if was_paused:
                try:
                    await self._docker(stale_container.pause)
                except (NotFound, APIError):
                    pass
            _logger.warning(f'Rolled back recycle of sandbox {sandbox_id}')
        except (NotFound, APIError) as exc:
            _logger.error(f'Rollback of sandbox {sandbox_id} recycle failed: {exc}')
        # Remove the broken replacement if one exists under the original name.
        # Identity is by container id: after a successful rename-back, a fresh
        # get() returns the restored stale container itself — never remove it.
        try:
            candidate = await self._docker(
                self.docker_client.containers.get, sandbox_id
            )
            candidate_id = getattr(candidate, 'id', None)
            if stale_id is None or candidate_id != stale_id:
                try:
                    await self._docker(candidate.stop, timeout=10)
                except (NotFound, APIError):
                    pass
                try:
                    await self._docker(candidate.remove)
                except (NotFound, APIError):
                    pass
        except (NotFound, APIError):
            pass

    async def _recreate_conversation_shells(
        self,
        sandbox: SandboxInfo,
        conversations: list[ConversationShell],
        default_working_dir: str | None,
    ) -> None:
        """Re-create idle conversation shells on a recycled sandbox.

        POSTs each conversation with the SAME id, the stored llm_model and NO
        initial_message (the agent must not act spontaneously). Failures are
        logged loudly per conversation but never abort the rest — an
        unrecreated conversation stays reachable via the event archive.
        """
        if not conversations:
            return
        try:
            agent_server_url = self._get_agent_server_url(sandbox)
        except SandboxError as exc:
            _logger.error(
                f'Cannot re-create conversation shells on sandbox {sandbox.id}: {exc}'
            )
            return
        headers = (
            {'X-Session-API-Key': sandbox.session_api_key}
            if sandbox.session_api_key
            else {}
        )
        for shell in conversations:
            body: dict = {
                'conversation_id': shell.conversation_id,
                'workspace': {'working_dir': shell.working_dir or default_working_dir},
            }
            if shell.llm_model:
                body['agent_settings'] = {'llm': {'model': shell.llm_model}}
            try:
                resp = await self.httpx_client.post(
                    f'{agent_server_url.rstrip("/")}/api/conversations',
                    json=body,
                    headers=headers,
                    timeout=60.0,
                )
                resp.raise_for_status()
                _logger.info(
                    f'Re-created conversation shell {shell.conversation_id} '
                    f'on recycled sandbox {sandbox.id}'
                )
            except Exception as exc:
                _logger.error(
                    f'Failed to re-create conversation shell '
                    f'{shell.conversation_id} on recycled sandbox {sandbox.id} '
                    f'(reachable only via the event archive): {exc}'
                )


class DockerSandboxServiceInjector(SandboxServiceInjector):
    """Dependency injector for docker sandbox services."""

    container_url_pattern: str = Field(
        default='http://localhost:{port}',
        description=(
            'URL pattern for exposed sandbox ports. Use {port} as placeholder. '
            'For remote access, set to your server IP (e.g., http://192.168.1.100:{port}). '
            'Configure via OH_SANDBOX_CONTAINER_URL_PATTERN environment variable.'
        ),
    )
    host_port: int = Field(
        default=3000,
        description=(
            'The port on which the main OpenHands app server is running. '
            'Used for webhook callbacks from agent-server containers. '
            'If running OpenHands on a non-default port, set this to match. '
            'Configure via OH_SANDBOX_HOST_PORT environment variable.'
        ),
    )
    container_name_prefix: str = 'oh-agent-server-'
    max_num_sandboxes: int = Field(
        default=5,
        description='Maximum number of sandboxes allowed to run simultaneously',
    )
    mounts: list[VolumeMount] = Field(default_factory=list)
    exposed_ports: list[ExposedPort] = Field(
        default_factory=lambda: [
            ExposedPort(
                name=AGENT_SERVER,
                description=(
                    'The port on which the agent server runs within the container'
                ),
                container_port=8000,
            ),
            ExposedPort(
                name=VSCODE,
                description=(
                    'The port on which the VSCode server runs within the container'
                ),
                container_port=8001,
            ),
            ExposedPort(
                name=WORKER_1,
                description=(
                    'The first port on which the agent should start application servers.'
                ),
                container_port=8011,
            ),
            ExposedPort(
                name=WORKER_2,
                description=(
                    'The second port on which the agent should start application servers.'
                ),
                container_port=8012,
            ),
        ]
    )
    health_check_path: str | None = Field(
        default='/health',
        description=(
            'The url path in the sandbox agent server to check to '
            'determine whether the server is running'
        ),
    )
    extra_hosts: dict[str, str] = Field(
        default_factory=lambda: {'host.docker.internal': 'host-gateway'},
        description=(
            'Extra hostname mappings to add to agent-server containers. '
            'This allows containers to resolve hostnames like host.docker.internal '
            'for LAN deployments and MCP connections. '
            'Format: {"hostname": "ip_or_gateway"}'
        ),
    )
    startup_grace_seconds: int = Field(
        default=STARTUP_GRACE_SECONDS,
        description=(
            'Number of seconds were no response from the agent server is acceptable'
            'before it is considered an error'
        ),
    )
    use_host_network: bool = Field(
        default_factory=_get_use_host_network_default,
        description=(
            'Whether to use host networking mode for agent-server containers. '
            'When enabled, containers share the host network namespace, '
            'making all container ports directly accessible on the host. '
            'This is useful for reverse proxy setups where dynamic port mapping '
            'is problematic. Configure via AGENT_SERVER_USE_HOST_NETWORK environment variable.'
        ),
    )
    kvm_enabled: bool = Field(
        default_factory=_get_kvm_enabled_default,
        description=(
            'Whether to pass through /dev/kvm to sandbox containers for hardware '
            'virtualization support. When enabled, sandboxes can run KVM-accelerated '
            'virtual machines instead of using slower emulation. Requires the host '
            'to have KVM available (/dev/kvm must exist and be accessible). '
            'Configure via SANDBOX_KVM_ENABLED environment variable.'
        ),
    )

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxService, None]:
        # Define inline to prevent circular lookup
        from openhands.app_server.config import (
            get_global_config,
            get_httpx_client,
            get_sandbox_spec_service,
        )

        # Get web_url and permitted_cors_origins from global config
        config = get_global_config()
        web_url = config.web_url

        async with (
            get_httpx_client(state) as httpx_client,
            get_sandbox_spec_service(state) as sandbox_spec_service,
        ):
            yield DockerSandboxService(
                sandbox_spec_service=sandbox_spec_service,
                container_name_prefix=self.container_name_prefix,
                host_port=self.host_port,
                container_url_pattern=self.container_url_pattern,
                mounts=self.mounts,
                exposed_ports=self.exposed_ports,
                health_check_path=self.health_check_path,
                httpx_client=httpx_client,
                max_num_sandboxes=self.max_num_sandboxes,
                web_url=web_url,
                permitted_cors_origins=config.permitted_cors_origins,
                extra_hosts=self.extra_hosts,
                startup_grace_seconds=self.startup_grace_seconds,
                use_host_network=self.use_host_network,
                kvm_enabled=self.kvm_enabled,
            )
