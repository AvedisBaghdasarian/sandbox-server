"""Unit tests for the /api catch-all passthrough."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from openhands.app_server.local_protocol.router import _api_catch_all_forward
from openhands.app_server.sandbox.sandbox_models import AGENT_SERVER, SandboxStatus


def _request(method: str = 'GET', query: str = '') -> MagicMock:
    request = MagicMock()
    request.method = method
    request.url.query = query
    request.headers = {'content-type': 'application/json'} if method == 'POST' else {}
    request.body = AsyncMock(return_value=b'{"x":1}')
    return request


def _running_sandbox() -> MagicMock:
    sandbox = MagicMock()
    sandbox.id = 'sbx-1'
    sandbox.status = SandboxStatus.RUNNING
    sandbox.session_api_key = 'sk-sbx'
    url = MagicMock()
    url.name = AGENT_SERVER
    url.url = 'http://localhost:33999'
    sandbox.exposed_urls = [url]
    return sandbox


def _sandbox_service(sandbox) -> MagicMock:
    service = MagicMock()
    service.search_sandboxes = AsyncMock(
        return_value=MagicMock(items=[sandbox] if sandbox else [])
    )
    service.get_sandbox = AsyncMock(return_value=sandbox)
    return service


def _httpx_response(
    status_code: int = 200,
    headers: dict | None = None,
    content: bytes = b'{}',
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = (
        headers if headers is not None else {'content-type': 'application/json'}
    )
    resp.content = content
    return resp


class TestApiCatchAllForward:
    @pytest.mark.asyncio
    async def test_forwards_to_running_sandbox_with_sandbox_key(self):
        """Unmatched /api/* reaches the sandbox, authenticated with its key."""
        # Arrange
        sandbox = _running_sandbox()
        httpx_client = MagicMock()
        httpx_client.request = AsyncMock(
            return_value=_httpx_response(200, {'content-type': 'application/json'})
        )

        # Act
        response = await _api_catch_all_forward(
            _request(), 'vscode/status', _sandbox_service(sandbox), httpx_client
        )

        # Assert
        assert response.status_code == 200
        call = httpx_client.request.call_args
        assert call.args[0] == 'GET'
        assert call.args[1] == 'http://localhost:33999/api/vscode/status'
        assert call.kwargs['headers'] == {'X-Session-API-Key': 'sk-sbx'}

    @pytest.mark.asyncio
    async def test_forwards_body_and_content_type_for_post(self):
        # Arrange
        sandbox = _running_sandbox()
        httpx_client = MagicMock()
        httpx_client.request = AsyncMock(
            return_value=_httpx_response(200, {'content-type': 'application/json'})
        )

        # Act
        await _api_catch_all_forward(
            _request('POST'),
            'bash/execute_bash_command',
            _sandbox_service(sandbox),
            httpx_client,
        )

        # Assert
        call = httpx_client.request.call_args
        assert call.kwargs['content'] == b'{"x":1}'
        assert call.kwargs['headers']['content-type'] == 'application/json'

    @pytest.mark.asyncio
    async def test_automation_prefix_stays_404(self):
        """The UI counts on automation 404s to treat the backend as absent."""
        with pytest.raises(HTTPException) as exc_info:
            await _api_catch_all_forward(
                _request(),
                'automation/health',
                _sandbox_service(_running_sandbox()),
                MagicMock(),
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_no_sandbox_returns_404(self):
        with pytest.raises(HTTPException) as exc_info:
            await _api_catch_all_forward(
                _request(), 'vscode/status', _sandbox_service(None), MagicMock()
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_stopped_sandbox_not_booted(self):
        """Probes never boot or resume a sandbox; they just 404."""
        sandbox = _running_sandbox()
        sandbox.status = SandboxStatus.PAUSED
        httpx_client = MagicMock()
        httpx_client.request = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await _api_catch_all_forward(
                _request(), 'vscode/status', _sandbox_service(sandbox), httpx_client
            )
        assert exc_info.value.status_code == 404
        httpx_client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_hop_by_hop_and_framing_headers_filtered(self):
        """httpx decodes bodies; content-encoding/length must not pass through."""
        # Arrange
        sandbox = _running_sandbox()
        httpx_client = MagicMock()
        httpx_client.request = AsyncMock(
            return_value=_httpx_response(
                200,
                {
                    'content-type': 'application/json',
                    'content-encoding': 'gzip',
                    'content-length': '999',
                    'transfer-encoding': 'chunked',
                },
            )
        )

        # Act
        response = await _api_catch_all_forward(
            _request(), 'plugins/installed', _sandbox_service(sandbox), httpx_client
        )

        # Assert
        forwarded = {k.lower() for k in response.headers}
        assert not forwarded & {'content-encoding', 'transfer-encoding'}
        # Starlette recomputes content-length from the decoded body — the
        # stale upstream length must not leak through.
        assert response.headers['content-length'] == str(len(b'{}'))

    @pytest.mark.asyncio
    async def test_upstream_connection_error_is_502(self):
        sandbox = _running_sandbox()
        httpx_client = MagicMock()
        httpx_client.request = AsyncMock(side_effect=httpx.ConnectError('refused'))
        with pytest.raises(HTTPException) as exc_info:
            await _api_catch_all_forward(
                _request(), 'vscode/status', _sandbox_service(sandbox), httpx_client
            )
        assert exc_info.value.status_code == 502


class TestCatchAllRouteOrder:
    def test_catch_all_is_the_last_api_route_in_the_app(self):
        """Real agent-server routes must win; only true gaps fall through.

        app.py includes the catch-all router after the v1 (real) routers, so
        e.g. the real /api/profiles on the settings router must appear before
        the catch-all.
        """
        from openhands.app_server.app import app

        api_routes = [
            route
            for route in app.routes
            if getattr(route, 'path', '').startswith('/api')
        ]
        assert api_routes, 'no api routes found in app'
        last = api_routes[-1]
        assert getattr(last, 'path', '') == '/api/{path:path}'
        profiles_idx = next(
            i
            for i, route in enumerate(api_routes)
            if getattr(route, 'path', '').endswith('/profiles')
        )
        catch_all_idx = len(api_routes) - 1
        assert profiles_idx < catch_all_idx
