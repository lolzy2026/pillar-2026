"""
app/core/mcp_session_manager.py

MCPSessionManager — manages the MultiServerMCPClient at the application level.

Key responsibilities:
1. Holds a single MultiServerMCPClient configured with our elicitation callback
2. The on_elicitation callback bridges to ElicitationBridge using the session_id
   that is injected via contextvars (not passed explicitly, since the MCP SDK
   callback signature is fixed)
3. Provides access to loaded tools for the graph's MCP node
4. Handles graceful startup and shutdown

Why contextvars for session_id injection?
The on_elicitation callback signature is fixed by the MCP SDK:
    async def on_elicitation(mcp_context, params, callback_context) -> ElicitResult

We can't add a session_id parameter. But we need to know WHICH user's session
triggered this elicitation to resolve the correct Future. The solution:
- Set a contextvar `current_session_id` before each graph invocation
- The callback reads it from context — this is safe because each async task
  has its own copy of contextvars
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.callbacks import CallbackContext, Callbacks
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp.shared.context import RequestContext
from mcp.types import ElicitRequestParams, ElicitResult

from app.core.elicitation_bridge import (
    ElicitationBridge,
    ElicitationCancelledError,
    ElicitationTimeoutError,
)
from app.models.state import ElicitationMode, ElicitationRequest

logger = logging.getLogger(__name__)

# ContextVar to carry session_id into the elicitation callback.
# Set before graph invocation, read inside on_elicitation.
current_session_id: ContextVar[str] = ContextVar("current_session_id", default="")

# ContextVar to carry a callback for pushing SSE events from the elicitation callback.
# Set before graph invocation, carries an async callable.
elicitation_event_emitter: ContextVar[Any] = ContextVar(
    "elicitation_event_emitter", default=None
)


class MCPSessionManager:
    """
    Application-level manager for the MCP client.

    Initialized once at FastAPI startup, torn down at shutdown.
    Provides tools to the LangGraph MCP node and wires the elicitation callback.
    """

    def __init__(
        self,
        server_url: str,
        bridge: ElicitationBridge,
        server_name: str = "default",
        transport: str = "streamable_http",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._server_url = server_url
        self._server_name = server_name
        self._transport = transport
        self._extra_headers = extra_headers or {}
        self._bridge = bridge
        self._client: MultiServerMCPClient | None = None
        self._tools: list[BaseTool] = []
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """
        Initialize the MCP client and load tools.
        Called from FastAPI lifespan on startup.
        """
        async with self._lock:
            logger.info(
                "Initializing MCP client: server=%s url=%s transport=%s",
                self._server_name,
                self._server_url,
                self._transport,
            )

            callbacks = Callbacks(on_elicitation=self._on_elicitation)

            self._client = MultiServerMCPClient(
                {
                    self._server_name: {
                        "url": self._server_url,
                        "transport": self._transport,
                        **({"headers": self._extra_headers} if self._extra_headers else {}),
                    }
                },
                callbacks=callbacks,
            )

            self._tools = await self._client.get_tools()
            logger.info(
                "MCP client ready — %d tools loaded: %s",
                len(self._tools),
                [t.name for t in self._tools],
            )

    async def stop(self) -> None:
        """Teardown the MCP client. Called from FastAPI lifespan on shutdown."""
        if self._client:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                logger.exception("Error during MCP client shutdown")
            finally:
                self._client = None
                self._tools = []
        logger.info("MCPSessionManager stopped")

    @property
    def tools(self) -> list[BaseTool]:
        """Return the loaded LangChain tools. Used by the MCP graph node."""
        if not self._tools:
            raise RuntimeError(
                "MCPSessionManager not started — call start() in app lifespan"
            )
        return self._tools

    async def reload_tools(self) -> list[BaseTool]:
        """
        Reload tools from the MCP server.
        Useful if the MCP server's tool list changes at runtime.
        """
        async with self._lock:
            if self._client:
                self._tools = await self._client.get_tools()
                logger.info("MCP tools reloaded: %d tools", len(self._tools))
        return self._tools

    async def _on_elicitation(
        self,
        mcp_context: RequestContext,
        params: ElicitRequestParams,
        context: CallbackContext,
    ) -> ElicitResult:
        """
        MCP elicitation callback — called by the MCP SDK when a tool calls ctx.elicit().

        This method BLOCKS (awaits) until the user provides input via /assist.
        The MCP SSE session stays alive during this entire wait period.

        The session_id is obtained from the `current_session_id` contextvar,
        which is set by GraphRunner before invoking the graph.
        """
        session_id = current_session_id.get()
        if not session_id:
            logger.error(
                "on_elicitation fired but no session_id in context — "
                "was current_session_id set before graph invocation?"
            )
            # Decline gracefully rather than crashing
            return ElicitResult(action="decline", content={})

        # ── Determine elicitation mode ────────────────────────────────────────
        # URL mode: params contains a url field (non-standard extension)
        # Form mode: params contains a requestedSchema
        # Default to form mode per MCP spec
        mode = ElicitationMode.FORM
        url: str | None = None

        # Some MCP servers extend ElicitRequestParams with a url field
        # Check both the standard schema field and any url extension
        raw_params = params.model_dump() if hasattr(params, "model_dump") else {}
        if raw_params.get("url"):
            mode = ElicitationMode.URL
            url = raw_params["url"]

        schema: dict | None = None
        if mode == ElicitationMode.FORM and params.requestedSchema:
            schema = (
                params.requestedSchema.model_dump()
                if hasattr(params.requestedSchema, "model_dump")
                else params.requestedSchema
            )

        # ── Generate a unique elicitation ID ─────────────────────────────────
        import uuid
        elicitation_id = str(uuid.uuid4())

        logger.info(
            "Elicitation requested: session=%s elicitation_id=%s mode=%s "
            "server=%s tool=%s message='%s'",
            session_id,
            elicitation_id,
            mode,
            context.server_name,
            context.tool_name,
            params.message,
        )

        # ── Build the request object for the LangGraph state ─────────────────
        elicitation_request = ElicitationRequest(
            elicitation_id=elicitation_id,
            mode=mode,
            message=params.message,
            schema=schema,
            url=url,
            server_name=context.server_name or self._server_name,
            tool_name=context.tool_name or "",
        )

        # ── Register a Future in the bridge ──────────────────────────────────
        future = self._bridge.register(
            session_id=session_id,
            elicitation_id=elicitation_id,
            server_name=elicitation_request.server_name,
            tool_name=elicitation_request.tool_name,
        )

        # ── Emit SSE event to the UI via the event emitter contextvar ─────────
        # The emitter is an async callable set by GraphRunner before graph invocation.
        # It pushes an SSE event to the currently connected streaming response.
        emitter = elicitation_event_emitter.get()
        if emitter:
            try:
                await emitter(elicitation_request)
            except Exception:
                logger.exception(
                    "Failed to emit elicitation SSE event for session %s", session_id
                )
        else:
            logger.warning(
                "No elicitation_event_emitter set for session %s — "
                "UI will not receive the elicitation event",
                session_id,
            )

        # ── BLOCK here, keeping the MCP session alive ─────────────────────────
        # This await keeps the coroutine (and thus the MCP SSE connection) open
        # until the user submits their response via /assist.
        try:
            result: ElicitResult = await asyncio.wait_for(
                future,
                timeout=600,  # 10-minute hard timeout as safety net
            )
            logger.info(
                "Elicitation resolved: session=%s elicitation_id=%s action=%s",
                session_id,
                elicitation_id,
                result.action,
            )
            return result

        except asyncio.TimeoutError:
            logger.warning(
                "Elicitation timed out waiting for user: session=%s elicitation_id=%s",
                session_id,
                elicitation_id,
            )
            return ElicitResult(action="cancel", content={})

        except (ElicitationTimeoutError, ElicitationCancelledError) as e:
            logger.warning("Elicitation cancelled/timed out: %s", e)
            return ElicitResult(action="cancel", content={})

        except Exception:
            logger.exception(
                "Unexpected error awaiting elicitation Future: session=%s", session_id
            )
            return ElicitResult(action="cancel", content={})
