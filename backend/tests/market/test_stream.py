"""Tests for the SSE streaming endpoint."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


class TestCreateStreamRouter:
    """Tests for the create_stream_router factory."""

    def test_router_has_prices_endpoint(self):
        """Test that the router exposes the /api/stream/prices GET endpoint."""
        cache = PriceCache()
        router = create_stream_router(cache)
        routes = {route.path for route in router.routes}
        assert "/api/stream/prices" in routes


@pytest.mark.asyncio
class TestGenerateEvents:
    """Unit tests for the _generate_events async generator."""

    def _make_request(self, disconnect_sequence: list[bool]) -> AsyncMock:
        """Create a mock Request with a predefined disconnect sequence."""
        mock_request = AsyncMock()
        mock_request.client = AsyncMock()
        mock_request.client.host = "127.0.0.1"
        mock_request.is_disconnected = AsyncMock(side_effect=disconnect_sequence)
        return mock_request

    async def test_emits_retry_directive_first(self):
        """Test that the first yielded item is the SSE retry directive."""
        cache = PriceCache()
        request = self._make_request([True])

        events = []
        async for event in _generate_events(cache, request, interval=0.0):
            events.append(event)

        assert events[0] == "retry: 1000\n\n"

    async def test_stops_on_immediate_disconnect(self):
        """Test that the generator stops after the retry line if client is already gone."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        request = self._make_request([True])

        events = []
        async for event in _generate_events(cache, request, interval=0.0):
            events.append(event)

        assert events == ["retry: 1000\n\n"]

    async def test_emits_price_data_when_cache_has_prices(self):
        """Test that price data is emitted when the cache has entries."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        request = self._make_request([False, True])

        events = []
        async for event in _generate_events(cache, request, interval=0.0):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 1

        payload = json.loads(data_events[0].removeprefix("data: ").strip())
        assert "AAPL" in payload
        assert "GOOGL" in payload
        assert payload["AAPL"]["price"] == 190.00
        assert payload["GOOGL"]["price"] == 175.00

    async def test_no_data_event_when_cache_empty(self):
        """Test that no data event is emitted when the cache is empty."""
        cache = PriceCache()
        request = self._make_request([False, True])

        events = []
        async for event in _generate_events(cache, request, interval=0.0):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) == 0

    async def test_does_not_repeat_on_unchanged_version(self):
        """Test that data is not re-emitted when the cache version hasn't changed."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        request = self._make_request([False, False, False, True])

        events = []
        async for event in _generate_events(cache, request, interval=0.0):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) == 1

    async def test_emits_again_after_cache_update(self):
        """Test that a new data event is emitted after the cache version increments."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        call_count = 0

        async def is_disconnected() -> bool:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                cache.update("AAPL", 191.00)
            return call_count > 3

        mock_request = AsyncMock()
        mock_request.client = AsyncMock()
        mock_request.client.host = "127.0.0.1"
        mock_request.is_disconnected = is_disconnected

        events = []
        async for event in _generate_events(cache, mock_request, interval=0.0):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) == 2

    async def test_data_event_includes_all_required_fields(self):
        """Test that emitted price data includes all fields from PriceUpdate.to_dict()."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("AAPL", 191.00)  # Creates an "up" direction
        request = self._make_request([False, True])

        events = []
        async for event in _generate_events(cache, request, interval=0.0):
            events.append(event)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) == 1

        payload = json.loads(data_events[0].removeprefix("data: ").strip())
        aapl = payload["AAPL"]
        assert aapl["ticker"] == "AAPL"
        assert aapl["price"] == 191.00
        assert aapl["previous_price"] == 190.00
        assert "timestamp" in aapl
        assert "change" in aapl
        assert "change_percent" in aapl
        assert aapl["direction"] == "up"
