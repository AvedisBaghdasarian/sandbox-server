"""Event reads fall back to the persisted archive when the sandbox 404s."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.datastructures import QueryParams

from openhands.agent_server.models import EventPage
from openhands.app_server.local_protocol.router import (
    conversation_events_fallback,
    conversation_events_search_fallback,
)


def _request(query: str = '', method: str = 'GET') -> MagicMock:
    request = MagicMock()
    request.method = method
    request.url.query = ''
    request.query_params = QueryParams(query)
    request.headers = {}
    request.body = AsyncMock(return_value=b'')
    return request


def _missing_conversation_info_service() -> MagicMock:
    info_service = MagicMock()
    info_service.get_app_conversation_info = AsyncMock(return_value=None)
    return info_service


def _event_service() -> MagicMock:
    service = MagicMock()
    service.search_events = AsyncMock(
        return_value=EventPage(items=[], next_page_id=None)
    )
    service.batch_get_events = AsyncMock(return_value=[])
    return service


class TestArchiveFallback:
    @pytest.mark.asyncio
    async def test_search_falls_back_on_sandbox_404(self):
        """GET search with no conversation row serves the archive page."""
        # Arrange
        cid = uuid4()
        event_service = _event_service()

        # Act
        result = await conversation_events_search_fallback(
            str(cid),
            _request('limit=10&page_id=abc'),
            app_conversation_info_service=_missing_conversation_info_service(),
            sandbox_service=MagicMock(),
            httpx_client=AsyncMock(),
            event_service=event_service,
        )

        # Assert: archive served with the browser's pagination params
        assert isinstance(result, EventPage)
        assert result.items == []
        _, kwargs = event_service.search_events.call_args
        assert kwargs['conversation_id'] == cid
        assert kwargs['limit'] == 10
        assert kwargs['page_id'] == 'abc'

    @pytest.mark.asyncio
    async def test_batch_get_falls_back_on_sandbox_404(self):
        """GET events with ids serves archived events when the sandbox 404s."""
        # Arrange
        cid = uuid4()
        eid1, eid2 = uuid4(), uuid4()
        event_service = _event_service()

        # Act
        result = await conversation_events_fallback(
            str(cid),
            _request(f'id={eid1}&id={eid2}'),
            app_conversation_info_service=_missing_conversation_info_service(),
            sandbox_service=MagicMock(),
            httpx_client=AsyncMock(),
            event_service=event_service,
        )

        # Assert
        assert result == []
        event_service.batch_get_events.assert_called_once_with(cid, [eid1, eid2])

    @pytest.mark.asyncio
    async def test_post_never_falls_back(self):
        """POST (send message) still surfaces the sandbox 404."""
        # Arrange
        event_service = _event_service()

        # Act / Assert
        with pytest.raises(HTTPException) as ei:
            await conversation_events_search_fallback(
                str(uuid4()),
                _request(method='POST'),
                app_conversation_info_service=_missing_conversation_info_service(),
                sandbox_service=MagicMock(),
                httpx_client=AsyncMock(),
                event_service=event_service,
            )
        assert ei.value.status_code == 404
        event_service.search_events.assert_not_called()
