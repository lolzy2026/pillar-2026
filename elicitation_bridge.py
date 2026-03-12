"""
app/core/elicitation_bridge.py

ElicitationBridge — the heart of elicitation handling.

Each pending elicitation is represented by an asyncio.Future keyed by
(session_id, elicitation_id). The on_elicitation MCP callback awaits
this Future, keeping the MCP SSE session alive. When the user submits
their response via /assist, we resolve the Future with an ElicitResult,
which unblocks the callback and allows tool execution to continue.

Design notes:
- In-process asyncio.Future is sufficient for 50 concurrent users on
  a single worker. For multi-worker deployments, replace the internal
  store with a Redis pub/sub backend while keeping this interface intact.
- All operations are coroutine-safe. asyncio.Future is not thread-safe,
  so all mutations go through the event loop.
- Futures have a configurable TTL to prevent resource leaks from
  abandoned sessions (e.g., user closes browser mid-elicitation).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.types import ElicitResult

logger = logging.getLogger(__name__)


@dataclass
class PendingElicitation:
    """Tracks a single pending elicitation with its Future and metadata."""

    future: asyncio.Future[ElicitResult]
    session_id: str
    elicitation_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    server_name: str = ""
    tool_name: str = ""


class ElicitationBridge:
    """
    Manages asyncio.Future instances that bridge MCP elicitation callbacks
    to HTTP responses from the user.

    Lifecycle of an elicitation:
    1. MCP node's on_elicitation callback calls `register()` → gets a Future
    2. on_elicitation awaits the Future (MCP session stays alive)
    3. User submits response via /assist → `resolve()` is called
    4. Future resolves → on_elicitation returns ElicitResult → MCP continues
    """

    def __init__(self, ttl_seconds: int = 600) -> None:
        """
        Args:
            ttl_seconds: How long to wait before auto-cancelling an
                         abandoned elicitation. Default 10 minutes.
        """
        self._ttl = timedelta(seconds=ttl_seconds)
        # Key: (session_id, elicitation_id) → PendingElicitation
        self._pending: dict[tuple[str, str], PendingElicitation] = {}
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background TTL cleanup task. Call from app lifespan."""
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="elicitation-bridge-cleanup"
        )
        logger.info("ElicitationBridge started with TTL=%s", self._ttl)

    async def stop(self) -> None:
        """Stop the cleanup task and cancel all pending futures. Call from app lifespan."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Cancel all pending futures gracefully
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(
                    ElicitationCancelledError(
                        f"Server shutting down — elicitation {pending.elicitation_id} cancelled"
                    )
                )
        self._pending.clear()
        logger.info("ElicitationBridge stopped, all pending futures cancelled")

    def register(
        self,
        session_id: str,
        elicitation_id: str,
        server_name: str = "",
        tool_name: str = "",
    ) -> asyncio.Future[ElicitResult]:
        """
        Register a new pending elicitation and return a Future.

        The MCP on_elicitation callback will `await` this Future.
        It will block until `resolve()` or `decline()` is called.

        Args:
            session_id: The user's session identifier
            elicitation_id: Unique ID for this specific elicitation request
            server_name: MCP server that triggered this (for logging)
            tool_name: Tool that triggered this (for logging)

        Returns:
            asyncio.Future[ElicitResult] — awaitable by on_elicitation
        """
        key = (session_id, elicitation_id)

        if key in self._pending:
            logger.warning(
                "Elicitation %s already registered for session %s — overwriting",
                elicitation_id,
                session_id,
            )
            # Cancel the old one to avoid leaks
            old = self._pending[key]
            if not old.future.done():
                old.future.cancel()

        loop = asyncio.get_event_loop()
        future: asyncio.Future[ElicitResult] = loop.create_future()

        self._pending[key] = PendingElicitation(
            future=future,
            session_id=session_id,
            elicitation_id=elicitation_id,
            server_name=server_name,
            tool_name=tool_name,
        )

        logger.debug(
            "Registered elicitation: session=%s elicitation_id=%s tool=%s",
            session_id,
            elicitation_id,
            tool_name,
        )
        return future

    def resolve(
        self,
        session_id: str,
        elicitation_id: str,
        action: str,
        content: dict[str, Any] | None = None,
    ) -> bool:
        """
        Resolve a pending elicitation with the user's response.

        This unblocks the on_elicitation callback in the MCP session,
        allowing tool execution to continue.

        Args:
            session_id: The user's session identifier
            elicitation_id: Which elicitation to resolve
            action: One of "accept", "decline", "cancel"
            content: Form data if action == "accept"

        Returns:
            True if successfully resolved, False if not found/already resolved
        """
        key = (session_id, elicitation_id)
        pending = self._pending.get(key)

        if not pending:
            logger.warning(
                "Attempted to resolve unknown elicitation: session=%s elicitation_id=%s",
                session_id,
                elicitation_id,
            )
            return False

        if pending.future.done():
            logger.warning(
                "Elicitation already resolved: session=%s elicitation_id=%s",
                session_id,
                elicitation_id,
            )
            self._pending.pop(key, None)
            return False

        result = ElicitResult(action=action, content=content or {})
        pending.future.set_result(result)
        self._pending.pop(key, None)

        logger.info(
            "Resolved elicitation: session=%s elicitation_id=%s action=%s",
            session_id,
            elicitation_id,
            action,
        )
        return True

    def has_pending(self, session_id: str) -> bool:
        """Check if a session has any pending elicitation."""
        return any(k[0] == session_id for k in self._pending)

    def get_pending_elicitation_id(self, session_id: str) -> str | None:
        """Get the elicitation_id of the current pending elicitation for a session."""
        for (sid, eid) in self._pending:
            if sid == session_id:
                return eid
        return None

    def pending_count(self) -> int:
        """Return total number of pending elicitations (for monitoring)."""
        return len(self._pending)

    async def _cleanup_loop(self) -> None:
        """Background task that periodically cancels timed-out elicitations."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in elicitation cleanup loop")

    async def _cleanup_expired(self) -> None:
        """Cancel all elicitations that have exceeded their TTL."""
        now = datetime.now(timezone.utc)
        expired_keys = [
            key
            for key, pending in self._pending.items()
            if (now - pending.created_at) > self._ttl
        ]

        for key in expired_keys:
            pending = self._pending.pop(key, None)
            if pending and not pending.future.done():
                pending.future.set_exception(
                    ElicitationTimeoutError(
                        f"Elicitation timed out after {self._ttl.seconds}s "
                        f"for session {pending.session_id}"
                    )
                )
                logger.warning(
                    "Timed out elicitation: session=%s elicitation_id=%s",
                    pending.session_id,
                    pending.elicitation_id,
                )

        if expired_keys:
            logger.info("Cleaned up %d expired elicitations", len(expired_keys))


class ElicitationTimeoutError(Exception):
    """Raised when a user doesn't respond to an elicitation within TTL."""


class ElicitationCancelledError(Exception):
    """Raised when an elicitation is cancelled (e.g., server shutdown)."""
