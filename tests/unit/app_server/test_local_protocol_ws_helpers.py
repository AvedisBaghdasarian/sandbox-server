"""Unit tests for WebSocket bridge helpers."""

import pytest

from openhands.app_server.local_protocol.helpers import build_upstream_ws_url


class TestBuildUpstreamWsUrl:
    def test_no_query(self):
        url = build_upstream_ws_url('http://localhost:18000', '/sockets/events/abc', '')
        assert url == 'http://localhost:18000/sockets/events/abc'

    def test_with_query(self):
        url = build_upstream_ws_url(
            'http://localhost:18000', '/sockets/events/abc', 'resend_mode=since&after_timestamp=123'
        )
        assert url == 'http://localhost:18000/sockets/events/abc?resend_mode=since&after_timestamp=123'

    def test_strips_trailing_slash(self):
        url = build_upstream_ws_url('http://localhost:18000/', '/sockets/bash-events', '')
        assert url == 'http://localhost:18000/sockets/bash-events'

    def test_bash_events_with_query(self):
        url = build_upstream_ws_url('http://localhost:18000', '/sockets/bash-events', 'foo=bar')
        assert url == 'http://localhost:18000/sockets/bash-events?foo=bar'


@pytest.mark.skip(reason='requires live sandbox')
def test_ws_bridge_requires_live_sandbox():
    """Placeholder for live sandbox WebSocket bridge test."""
    # This would test the full WebSocket relay with a live sandbox.
    pass
