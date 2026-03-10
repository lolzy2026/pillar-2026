"""
tests/test_elicitation.py

Tests for the elicitation flow — the most critical component.

Coverage:
- ElicitationBridge: register, resolve, TTL, duplicate handling
- on_elicitation callback: correct Future blocking and resolution
- /assist endpoint: normal flow, elicitation response routing, validation
- Session isolation: two concurrent sessions don't interfere
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from mcp.types import ElicitResult

from app.core.elicitation_bridge import (
    ElicitationBridge,
    ElicitationTimeoutError,
)
from app.models.state import ElicitationMode, ElicitationRequest


# ─────────────────────────────────────────────────────────────────────────────
# ElicitationBridge unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestElicitationBridge:

    @pytest.fixture
    async def bridge(self):
        b = ElicitationBridge(ttl_seconds=10)
        await b.start()
        yield b
        await b.stop()

    @pytest.mark.asyncio
    async def test_register_returns_future(self, bridge):
        future = bridge.register("session-1", "elicit-1")
        assert isinstance(future, asyncio.Future)
        assert not future.done()

    @pytest.mark.asyncio
    async def test_resolve_accept_unblocks_future(self, bridge):
        future = bridge.register("session-1", "elicit-1")

        resolved = bridge.resolve(
            session_id="session-1",
            elicitation_id="elicit-1",
            action="accept",
            content={"email": "test@example.com"},
        )

        assert resolved is True
        assert future.done()
        result: ElicitResult = future.result()
        assert result.action == "accept"
        assert result.content == {"email": "test@example.com"}

    @pytest.mark.asyncio
    async def test_resolve_decline(self, bridge):
        future = bridge.register("session-1", "elicit-1")
        bridge.resolve("session-1", "elicit-1", action="decline")

        result = future.result()
        assert result.action == "decline"

    @pytest.mark.asyncio
    async def test_resolve_unknown_session_returns_false(self, bridge):
        resolved = bridge.resolve("nonexistent", "elicit-1", action="accept")
        assert resolved is False

    @pytest.mark.asyncio
    async def test_resolve_twice_returns_false_second_time(self, bridge):
        bridge.register("session-1", "elicit-1")
        first = bridge.resolve("session-1", "elicit-1", action="accept")
        second = bridge.resolve("session-1", "elicit-1", action="accept")

        assert first is True
        assert second is False  # Already resolved

    @pytest.mark.asyncio
    async def test_session_isolation(self, bridge):
        """Two sessions with elicitations don't interfere."""
        future_a = bridge.register("session-a", "elicit-1")
        future_b = bridge.register("session-b", "elicit-1")

        bridge.resolve("session-a", "elicit-1", action="accept", content={"val": "A"})

        # session-b future should still be pending
        assert future_a.done()
        assert not future_b.done()

        bridge.resolve("session-b", "elicit-1", action="accept", content={"val": "B"})
        assert future_b.done()
        assert future_b.result().content == {"val": "B"}

    @pytest.mark.asyncio
    async def test_has_pending(self, bridge):
        assert not bridge.has_pending("session-1")
        bridge.register("session-1", "elicit-1")
        assert bridge.has_pending("session-1")
        bridge.resolve("session-1", "elicit-1", action="cancel")
        assert not bridge.has_pending("session-1")

    @pytest.mark.asyncio
    async def test_pending_count(self, bridge):
        assert bridge.pending_count() == 0
        bridge.register("session-1", "elicit-1")
        bridge.register("session-2", "elicit-1")
        assert bridge.pending_count() == 2
        bridge.resolve("session-1", "elicit-1", action="accept")
        assert bridge.pending_count() == 1

    @pytest.mark.asyncio
    async def test_stop_cancels_all_futures(self, bridge):
        future = bridge.register("session-1", "elicit-1")
        await bridge.stop()

        assert future.done()
        assert future.exception() is not None


# ─────────────────────────────────────────────────────────────────────────────
# Elicitation flow integration test
# ─────────────────────────────────────────────────────────────────────────────

class TestElicitationFlow:
    """
    Tests the full elicitation flow:
    on_elicitation callback blocks → user responds → callback unblocks
    """

    @pytest.mark.asyncio
    async def test_callback_blocks_until_resolved(self):
        """
        Simulate what happens when on_elicitation fires:
        1. Bridge registers a Future
        2. Callback awaits the Future (blocks)
        3. User resolves it via bridge.resolve()
        4. Callback unblocks and returns ElicitResult
        """
        bridge = ElicitationBridge()
        await bridge.start()

        elicitation_id = str(uuid.uuid4())
        session_id = "test-session"
        resolution_result = None

        async def simulated_callback():
            """Simulates MCPSessionManager._on_elicitation"""
            nonlocal resolution_result
            future = bridge.register(session_id, elicitation_id)
            # This is the blocking await
            result = await asyncio.wait_for(future, timeout=5.0)
            resolution_result = result
            return result

        async def simulated_user_response():
            """Simulates user submitting form data via /assist"""
            await asyncio.sleep(0.05)  # Small delay to ensure callback is waiting
            bridge.resolve(
                session_id=session_id,
                elicitation_id=elicitation_id,
                action="accept",
                content={"name": "Alice", "age": 30},
            )

        # Run both concurrently
        await asyncio.gather(
            simulated_callback(),
            simulated_user_response(),
        )

        assert resolution_result is not None
        assert resolution_result.action == "accept"
        assert resolution_result.content == {"name": "Alice", "age": 30}

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_multiple_sequential_elicitations(self):
        """
        Test that sequential elicitations (multiple ctx.elicit() calls from one tool)
        work correctly.
        """
        bridge = ElicitationBridge()
        await bridge.start()

        session_id = "test-session"
        results = []

        async def simulated_tool_with_two_elicitations():
            """A tool that calls ctx.elicit() twice sequentially."""
            for i in range(2):
                elicitation_id = f"elicit-{i}"
                future = bridge.register(session_id, elicitation_id)
                result = await asyncio.wait_for(future, timeout=5.0)
                results.append(result)

        async def simulated_user_responses():
            await asyncio.sleep(0.05)
            bridge.resolve(session_id, "elicit-0", action="accept", content={"step": 0})
            await asyncio.sleep(0.05)
            bridge.resolve(session_id, "elicit-1", action="accept", content={"step": 1})

        await asyncio.gather(
            simulated_tool_with_two_elicitations(),
            simulated_user_responses(),
        )

        assert len(results) == 2
        assert results[0].content == {"step": 0}
        assert results[1].content == {"step": 1}

        await bridge.stop()


# ─────────────────────────────────────────────────────────────────────────────
# API endpoint tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAssistEndpoint:

    @pytest.fixture
    def mock_app(self):
        """Create a test FastAPI app with mocked dependencies."""
        from fastapi import FastAPI
        from app.api.assist import router

        app = FastAPI()
        app.include_router(router)

        bridge = ElicitationBridge()
        mock_runner = MagicMock()

        app.state.elicitation_bridge = bridge
        app.state.graph_runner = mock_runner

        return app, bridge, mock_runner

    def test_elicitation_response_missing_id_returns_400(self, mock_app):
        app, bridge, _ = mock_app
        client = TestClient(app)

        response = client.post("/assist", json={
            "session_id": "session-1",
            "message": "",
            "is_elicitation_response": True,
            # Missing elicitation_id
        })
        assert response.status_code == 400

    def test_elicitation_response_not_found_returns_404(self, mock_app):
        app, bridge, _ = mock_app
        client = TestClient(app)

        response = client.post("/assist", json={
            "session_id": "session-1",
            "message": "",
            "is_elicitation_response": True,
            "elicitation_id": "nonexistent-id",
            "elicitation_action": "accept",
        })
        assert response.status_code == 404

    def test_elicitation_response_resolves_bridge(self, mock_app):
        app, bridge, _ = mock_app

        # Pre-register a pending elicitation
        future = bridge.register("session-1", "elicit-abc")

        client = TestClient(app)
        response = client.post("/assist", json={
            "session_id": "session-1",
            "message": "",
            "is_elicitation_response": True,
            "elicitation_id": "elicit-abc",
            "elicitation_action": "accept",
            "elicitation_content": {"confirmed": True},
        })

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert future.done()
        assert future.result().action == "accept"

    def test_elicitation_action_validation(self, mock_app):
        """Only accept/decline/cancel are valid actions."""
        app, _, _ = mock_app
        client = TestClient(app)

        response = client.post("/assist", json={
            "session_id": "session-1",
            "message": "",
            "is_elicitation_response": True,
            "elicitation_id": "elicit-abc",
            "elicitation_action": "INVALID_ACTION",  # Should fail validation
        })
        assert response.status_code == 422
