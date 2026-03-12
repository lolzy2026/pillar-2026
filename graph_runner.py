"""
app/core/graph_runner.py

GraphRunner — orchestrates LangGraph execution with SSE streaming.

Key responsibilities:
1. Accept bearer_token per request, pass to MCPSessionManager for
   tool fetching and session building.
2. Set contextvars (session_id, elicitation emitter) before graph
   invocation, always reset them in a finally block.
3. Stream SSE events (node lifecycle, tokens, elicitation prompts, completion).
4. Expose resolve_elicitation() for the /assist endpoint to call when
   the user submits an elicitation response.

SSE event contract:
    {"type": "node_start",    "node": "<name>"}
    {"type": "node_complete", "node": "<name>"}
    {"type": "token",         "content": "..."}
    {"type": "elicitation",   "elicitation_id": "...", "mode": "form"|"url",
                               "message": "...", "schema": {...}|null,
                               "url": "..."|null, "tool_name": "..."}
    {"type": "complete",      "answer": "...", "elicitation_count": N}
    {"type": "error",         "message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator

from langchain_core.messages import HumanMessage

from app.core.elicitation_bridge import ElicitationBridge
from app.core.mcp_session_manager import (
    MCPSessionManager,
    current_session_id,
    elicitation_event_emitter,
)
from app.models.state import ElicitationRequest, GraphState

logger = logging.getLogger(__name__)

# Nodes we surface to the UI as lifecycle events
_TRACKED_NODES = {
    "guardrail_node",
    "query_rephrase_node",
    "mcp_node",
    "answer_generation_node",
}


def _sse(data: dict) -> str:
    """Serialize a dict as a Server-Sent Events data line."""
    return f"data: {json.dumps(data)}\n\n"


class GraphRunner:
    """
    One instance lives for the lifetime of the FastAPI app (stored in app.state).
    It is stateless between requests — all per-request state is local to run().
    """

    def __init__(
        self,
        compiled_graph: Any,
        bridge: ElicitationBridge,
        mcp_manager: MCPSessionManager,
    ) -> None:
        self._graph = compiled_graph
        self._bridge = bridge
        self._mcp_manager = mcp_manager

    async def run(
        self,
        session_id: str,
        user_input: str,
        bearer_token: str,
    ) -> AsyncGenerator[str, None]:
        """
        Execute the LangGraph pipeline for a user message.

        Yields SSE-formatted strings. The generator stays alive until the
        graph fully completes — even if an elicitation suspends execution
        mid-way. The elicitation SSE event is emitted from within the
        on_elicitation callback through the event_queue bridge below.

        Args:
            session_id:    Stable identifier for this user's conversation thread.
                           Used as LangGraph checkpoint thread_id.
            user_input:    The user's message text.
            bearer_token:  Raw Bearer token forwarded to MCP server for auth
                           and namespace enforcement.
        """
        # Per-request event queue: on_elicitation pushes events here;
        # this generator drains them and yields SSE strings.
        # Using asyncio.Queue avoids any threading concerns — everything
        # stays on the single event loop.
        event_queue: asyncio.Queue[dict | None] = asyncio.Queue()

        async def emit_elicitation(request: ElicitationRequest) -> None:
            """Callable stored in elicitation_event_emitter contextvar."""
            await event_queue.put({
                "type": "elicitation",
                "elicitation_id": request.elicitation_id,
                "mode": request.mode,
                "message": request.message,
                "schema": request.schema,
                "url": request.url,
                "tool_name": request.tool_name,
                "server_name": request.server_name,
            })

        # ── Set contextvars for this request's async task ──────────────────
        # These tokens allow reset() in the finally block, which is mandatory.
        # Resetting ensures the contextvar values don't persist if asyncio
        # ever reuses a task object (rare, but correct practice).
        sid_token = current_session_id.set(session_id)
        emit_token = elicitation_event_emitter.set(emit_elicitation)

        # ── Fetch tools for this user's namespace ──────────────────────────
        # get_tools() uses the token-keyed cache. On cache hit this is
        # essentially free. On miss it opens a short-lived MCP session.
        try:
            tools = await self._mcp_manager.get_tools(bearer_token)
        except Exception as exc:
            current_session_id.reset(sid_token)
            elicitation_event_emitter.reset(emit_token)
            yield _sse({"type": "error", "message": f"Failed to load tools: {exc}"})
            return

        # ── Build the MCP client for this request ──────────────────────────
        # The client is scoped to this request. It carries the user's token
        # so the MCP server can authorize tool execution and enforce namespace.
        # The on_elicitation callback is wired inside build_client().
        mcp_client = self._mcp_manager.build_client(bearer_token)

        # ── Build initial graph state ──────────────────────────────────────
        initial_state = GraphState(
            session_id=session_id,
            user_input=user_input,
            messages=[HumanMessage(content=user_input)],
        )
        config = {"configurable": {"thread_id": session_id}}

        # ── Launch graph as a background task ──────────────────────────────
        # The graph task and this generator run concurrently on the same
        # event loop. The graph task pushes events to event_queue; this
        # generator drains them and yields SSE strings to the HTTP client.
        graph_task = asyncio.create_task(
            self._run_graph(initial_state, config, mcp_client, tools, event_queue),
            name=f"graph-{session_id}",
        )

        try:
            # Drain event queue until graph task signals completion (None sentinel)
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    if graph_task.done():
                        break
                    continue

                if event is None:   # Sentinel: graph is done
                    break

                yield _sse(event)

            # Retrieve final state and emit completion event
            final_state: GraphState = await graph_task
            yield _sse({
                "type": "complete",
                "answer": final_state.final_answer,
                "node_trace": final_state.node_trace,
                "elicitation_count": len(final_state.elicitation_history),
            })

        except asyncio.CancelledError:
            logger.warning("Graph execution cancelled: session=%s", session_id)
            graph_task.cancel()
            yield _sse({"type": "error", "message": "Request cancelled"})

        except Exception as exc:
            logger.exception("Graph execution error: session=%s", session_id)
            graph_task.cancel()
            yield _sse({"type": "error", "message": str(exc)})

        finally:
            # ALWAYS reset contextvars — this is non-negotiable.
            # Failure to reset would cause stale session_id to linger on
            # the task's context, potentially leaking into future work if
            # the task object is ever reused by the event loop.
            current_session_id.reset(sid_token)
            elicitation_event_emitter.reset(emit_token)

    def resolve_elicitation(
        self,
        session_id: str,
        elicitation_id: str,
        action: str,
        content: dict[str, Any] | None = None,
    ) -> bool:
        """
        Resolve a pending elicitation with the user's response.

        Called by the /assist endpoint when is_elicitation_response=True.
        Unblocks the on_elicitation callback, which returns ElicitResult
        to the MCP server and allows tool execution to continue on the
        original request's SSE stream.

        Returns True if resolved, False if no pending elicitation found.
        """
        return self._bridge.resolve(
            session_id=session_id,
            elicitation_id=elicitation_id,
            action=action,
            content=content,
        )

    # ── Private ───────────────────────────────────────────────────────────────

    async def _run_graph(
        self,
        initial_state: GraphState,
        config: dict,
        mcp_client: Any,
        tools: list,
        event_queue: asyncio.Queue,
    ) -> GraphState:
        """
        Execute the LangGraph inside a background task.

        Pushes node lifecycle and token events to event_queue.
        Sends a None sentinel when complete (success or failure).

        The mcp_client is passed through config so mcp_node can access
        it without it being part of the serialised checkpoint state.
        """
        try:
            # Inject mcp_client and tools via LangGraph config (not state).
            # State is checkpointed to PostgreSQL — we don't want live objects there.
            # Config is in-memory only, per-invocation.
            run_config = {
                **config,
                "configurable": {
                    **config.get("configurable", {}),
                    "mcp_client": mcp_client,
                    "mcp_tools": tools,
                    # Injected so mcp_node can register Futures for
                    # UrlElicitationRequiredError (-32042) handling
                    "elicitation_bridge": self._bridge,
                },
            }

            async for event in self._graph.astream_events(
                initial_state.model_dump(),
                config=run_config,
                version="v2",
            ):
                event_type = event.get("event", "")
                node_name = event.get("name", "")

                if event_type == "on_chain_start" and node_name in _TRACKED_NODES:
                    await event_queue.put({"type": "node_start", "node": node_name})

                elif event_type == "on_chain_end" and node_name in _TRACKED_NODES:
                    await event_queue.put({"type": "node_complete", "node": node_name})

                elif event_type == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        await event_queue.put({"type": "token", "content": chunk.content})

            # Read final state from checkpointer
            state_snapshot = await self._graph.aget_state(config)
            return (
                GraphState(**state_snapshot.values)
                if state_snapshot and state_snapshot.values
                else GraphState()
            )

        except Exception as exc:
            logger.exception("Error inside graph execution: session=%s", id(config))
            await event_queue.put({"type": "error", "message": str(exc)})
            raise

        finally:
            await event_queue.put(None)  # Always signal completion
