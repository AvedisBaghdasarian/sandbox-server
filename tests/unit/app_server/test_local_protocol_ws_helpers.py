"""Unit tests for WebSocket bridge helpers."""

import json
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketState

import openhands.app_server.local_protocol.router as router_mod
from openhands.app_server.local_protocol.helpers import build_upstream_ws_url
from openhands.app_server.local_protocol.router import (
    _is_ws_auth_frame,
    _ws_relay_browser_to_upstream,
)


class TestBuildUpstreamWsUrl:
    def test_no_query(self):
        url = build_upstream_ws_url('http://localhost:18000', '/sockets/events/abc', '')
        assert url == 'ws://localhost:18000/sockets/events/abc'

    def test_with_query(self):
        url = build_upstream_ws_url(
            'http://localhost:18000',
            '/sockets/events/abc',
            'resend_mode=since&after_timestamp=123',
        )
        assert (
            url
            == 'ws://localhost:18000/sockets/events/abc?resend_mode=since&after_timestamp=123'
        )

    def test_https_maps_to_wss(self):
        url = build_upstream_ws_url('https://example.com', '/sockets/bash-events', '')
        assert url == 'wss://example.com/sockets/bash-events'

    def test_strips_trailing_slash(self):
        url = build_upstream_ws_url(
            'http://localhost:18000/', '/sockets/bash-events', ''
        )
        assert url == 'ws://localhost:18000/sockets/bash-events'

    def test_bash_events_with_query(self):
        url = build_upstream_ws_url(
            'http://localhost:18000', '/sockets/bash-events', 'foo=bar'
        )
        assert url == 'ws://localhost:18000/sockets/bash-events?foo=bar'


class TestIsWsAuthFrame:
    def test_auth_frame_detected(self):
        assert _is_ws_auth_frame(json.dumps({'type': 'auth', 'session_api_key': 'k'}))

    def test_other_frames_pass_through(self):
        assert not _is_ws_auth_frame('hello')
        assert not _is_ws_auth_frame(json.dumps({'type': 'message'}))
        assert not _is_ws_auth_frame('not json at all')


class _FakeUpstream:
    def __init__(self):
        self.sent: list[str] = []
        self.closed = False

    async def send(self, msg):
        self.sent.append(msg)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


class _FakeConnectCM:
    def __init__(self, upstream):
        self.upstream = upstream

    async def __aenter__(self):
        return self.upstream

    async def __aexit__(self, *exc):
        return False


class _FakeBrowserSocket:
    def __init__(self, incoming=None):
        self.url = SimpleNamespace(query='')
        self.app = SimpleNamespace(state=SimpleNamespace())
        self.client_state = WebSocketState.CONNECTING
        self._incoming = list(incoming or [{'type': 'websocket.disconnect'}])
        self.closed_with = None

    async def accept(self):
        pass

    async def close(self, code=1000, reason=''):
        self.closed_with = (code, reason)

    async def receive(self):
        return self._incoming.pop(0)


@pytest.mark.asyncio
async def test_relay_drops_browser_auth_frames():
    """The browser's own first-message auth must never reach upstream."""
    upstream = _FakeUpstream()
    socket = _FakeBrowserSocket(
        incoming=[
            {
                'type': 'websocket.receive',
                'text': json.dumps({'type': 'auth', 'session_api_key': 'browser-key'}),
            },
            {'type': 'websocket.receive', 'text': 'hello'},
            {'type': 'websocket.disconnect'},
        ]
    )
    await _ws_relay_browser_to_upstream(socket, upstream)
    assert upstream.sent == ['hello']


@pytest.mark.asyncio
async def test_bridge_sends_sandbox_auth_frame_first(monkeypatch):
    """The bridge authenticates upstream with the sandbox's own session key."""
    upstream = _FakeUpstream()
    monkeypatch.setattr(
        router_mod.websockets,
        'connect',
        lambda url, open_timeout=None: _FakeConnectCM(upstream),
    )
    monkeypatch.setattr(
        router_mod,
        '_agent_endpoint_for_ws_bridge',
        _fake_endpoint('http://up:8000', 'sk-sandbox'),
    )
    socket = _FakeBrowserSocket()
    await router_mod._handle_ws_bridge(socket, 'sbx', '/sockets/events/cid')
    assert upstream.sent == [
        json.dumps({'type': 'auth', 'session_api_key': 'sk-sandbox'})
    ]


@pytest.mark.asyncio
async def test_bridge_skips_auth_when_sandbox_has_no_key(monkeypatch):
    """Keyless sandboxes (no auth configured) must not receive an auth frame."""
    upstream = _FakeUpstream()
    monkeypatch.setattr(
        router_mod.websockets,
        'connect',
        lambda url, open_timeout=None: _FakeConnectCM(upstream),
    )
    monkeypatch.setattr(
        router_mod,
        '_agent_endpoint_for_ws_bridge',
        _fake_endpoint('http://up:8000', None),
    )
    socket = _FakeBrowserSocket()
    await router_mod._handle_ws_bridge(socket, 'sbx', '/sockets/bash-events')
    assert upstream.sent == []


def _fake_endpoint(url, key):
    async def resolver(websocket, sandbox_id):
        return (url, key)

    return resolver


@pytest.mark.skip(reason='requires live sandbox')
def test_ws_bridge_requires_live_sandbox():
    """Placeholder for live sandbox WebSocket bridge test."""
    # This would test the full WebSocket relay with a live sandbox.
    pass
