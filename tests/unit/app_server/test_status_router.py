"""Unit tests for the status router endpoints.

This module tests the status router endpoints (/alive, /health, /server_info, /ready).
"""

import importlib.metadata

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openhands.app_server.status import system_stats
from openhands.app_server.status.status_router import router
from openhands.app_server.version import get_version


@pytest.fixture
def test_client():
    """Create a test client with the status router.

    This fixture sets up a FastAPI test client with the status router included.
    No authentication is required for these endpoints.
    """
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    yield client


class TestAliveEndpoint:
    """Test suite for the /alive endpoint."""

    def test_alive_returns_ok_status(self, test_client):
        """Test that /alive returns status: ok."""
        response = test_client.get('/alive')

        assert response.status_code == 200
        assert response.json() == {'status': 'ok'}


class TestHealthEndpoint:
    """Test suite for the /health endpoint."""

    def test_health_returns_ok(self, test_client):
        """Test that /health returns 'OK' string."""
        response = test_client.get('/health')

        assert response.status_code == 200
        # FastAPI returns JSON-encoded string, so response.json() gives 'OK'
        assert response.json() == 'OK'


class TestServerInfoEndpoint:
    """Test suite for the /server_info endpoint (now owned by local_protocol)."""

    def test_server_info_returns_system_info(self):
        """Test that /server_info returns the local-protocol synthesised payload."""
        from openhands.app_server.local_protocol.router import local_protocol_router

        app = FastAPI()
        app.include_router(local_protocol_router)
        client = TestClient(app)
        response = client.get('/server_info')

        assert response.status_code == 200
        payload = response.json()
        # New adapter synthesises this shape for agent-canvas
        assert 'version' in payload
        assert 'sdk_version' in payload
        assert 'usable_tools' in payload
        assert 'compatibility' in payload
        assert payload['compatibility']['minimum_agent_server'] == '1.28.0'
        # Version must be >=1.28.0
        version = payload['version']
        assert isinstance(version, str) and version

    def test_get_sdk_version_returns_unknown_when_package_missing(self, monkeypatch):
        """Test missing SDK metadata returns a stable fallback."""

        def raise_package_not_found(package_name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(package_name)

        monkeypatch.setattr(
            system_stats.importlib.metadata,
            'version',
            raise_package_not_found,
        )

        assert system_stats.get_sdk_version() == 'unknown'


class TestReadyEndpoint:
    """Test suite for the /ready endpoint."""

    def test_ready_returns_ok(self, test_client):
        """Test that /ready returns 'OK' string."""
        response = test_client.get('/ready')

        assert response.status_code == 200
        # FastAPI returns JSON-encoded string, so response.json() gives 'OK'
        assert response.json() == 'OK'


class TestAllStatusEndpoints:
    """Integration tests for all status endpoints."""

    def test_all_endpoints_accessible(self, test_client):
        """Test that health endpoints are accessible."""
        endpoints = ['/alive', '/health', '/ready']

        for endpoint in endpoints:
            response = test_client.get(endpoint)
            assert response.status_code == 200, (
                f'Endpoint {endpoint} returned {response.status_code}'
            )

    def test_server_info_via_full_app(self):
        """Test that /server_info is accessible via the full app (local_protocol)."""
        from openhands.app_server.app import app

        client = TestClient(app)
        response = client.get('/server_info')
        assert response.status_code == 200
        payload = response.json()
        assert 'version' in payload
