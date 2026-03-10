"""
app/core/graph_runner.py

GraphRunner — orchestrates LangGraph execution with SSE streaming.

Responsibilities:
1. Set contextvars (session_id, event emitter) before graph invocation
2. Execute the compiled graph as an async generator (streaming)
3. Emit SSE events for: tokens, node updates, elicitation requests, completion
4. Handle elicitation response routing (resolve the bridge Future)

SSE Event types emitted:
    {"type": "token", "content": "..."}
    {"type": "node_start", "node": "mcp_node"}
    {"type": "node_complete", "node": "mcp_node"}
    {"type": "elicitation", "elicitation_id": "...", "mode": "form"|"url",
     "message": "...", "schema": {...} | null, "url": "..." | null}
    {"type": "complete", "answer": "..."}
    {"type": "error", "message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator

from langchain_core.messages import HumanMessage

from app.core.elicitation_bridge import ElicitationBridge
from app.core.mcp_session_manager import (
    current_session_id,
    elicitation_event_emitter,
)
from app.models.state import ElicitationRequest, GraphState

logger = logging.getLogger(__name__)


def _sse_event(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


class GraphRunner:
    """
    Manages graph execution and SSE event streaming for a single request.

    One GraphRunner instance is created per /assist request.
    """

    def __init__(
        self,
        compiled_graph: Any,
        bridge: ElicitationBridge,
    ) -> None:
        self._graph = compiled_graph
        self._bridge = bridge

    async def run(
        self,
        session_id: str,
        user_input: str,
    ) -> AsyncGenerator[str, None]:
        """
        Execute the graph for a new user message.

        Yields SSE-formatted strings for the streaming response.
        If an elicitation event is triggered, yields an elicitation SSE event
        and then continues yielding until the graph completes.

        Args:
            session_id: Unique identifier for this user session
            user_input: The user's message

        Yields:
            SSE-formatted event strings
        """
        # Queue for SSE events that can be emitted from within callbacks
        # (e.g., elicitation events fired from inside on_elicitation)
        event_queue: asyncio.Queue[dict | None] = asyncio.Queue()

        async def emit_elicitation_event(request: ElicitationRequest) -> None:
            """Called from MCPSessionManager.on_elicitation to push event to SSE stream."""
            await event_queue.put({
                "type": "elicitation",
                "elicitation_id": request.elicitation_id,
                "mode": request.mode,
                "message": request.message,
                "schema": request.schema,
                "url": request.url,
                "server_name": request.server_name,
                "tool_name": request.tool_name,
            })

        # ── Set contextvars for this invocation ───────────────────────────────
        # These are read by MCPSessionManager._on_elicitation
        session_token = current_session_id.set(session_id)
        emitter_token = elicitation_event_emitter.set(emit_elicitation_event)

        # ── Build initial state ───────────────────────────────────────────────
        initial_state = GraphState(
            session_id=session_id,
            user_input=user_input,
            messages=[HumanMessage(content=user_input)],
        )

        # LangGraph config — thread_id is the session_id for checkpointing
        config = {"configurable": {"thread_id": session_id}}

        # ── Run graph as async task, drain event queue concurrently ──────────
        graph_task = asyncio.create_task(
            self._run_graph(initial_state, config, event_queue),
            name=f"graph-{session_id}",
        )

        try:
            # Drain event queue until graph completes (None sentinel)
            while True:
                try:
                    # Short timeout so we don't block forever if queue is empty
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    # Check if graph is done
                    if graph_task.done():
                        break
                    continue

                if event is None:
                    # Sentinel: graph execution complete
                    break

                yield _sse_event(event)

            # ── Graph completed — get final state ─────────────────────────────
            final_state: GraphState = await graph_task

            yield _sse_event({
                "type": "complete",
                "answer": final_state.final_answer,
                "node_trace": final_state.node_trace,
                "elicitation_count": len(final_state.elicitation_history),
            })

        except asyncio.CancelledError:
            logger.warning("Graph execution cancelled for session %s", session_id)
            graph_task.cancel()
            yield _sse_event({"type": "error", "message": "Request cancelled"})

        except Exception as e:
            logger.exception("Graph execution error for session %s", session_id)
            graph_task.cancel()
            yield _sse_event({"type": "error", "message": str(e)})

        finally:
            # Always reset contextvars
            current_session_id.reset(session_token)
            elicitation_event_emitter.reset(emitter_token)

    async def _run_graph(
        self,
        initial_state: GraphState,
        config: dict,
        event_queue: asyncio.Queue,
    ) -> GraphState:
        """
        Execute the LangGraph and push node events to the queue.
        Sends a None sentinel when complete.
        """
        try:
            final_state = None

            # Use astream_events for rich streaming (node start/complete + tokens)
            async for event in self._graph.astream_events(
                initial_state.model_dump(),
                config=config,
                version="v2",
            ):
                event_type = event.get("event", "")
                node_name = event.get("name", "")

                if event_type == "on_chain_start" and node_name in {
                    "guardrail_node", "query_rephrase_node",
                    "mcp_node", "elicitation_node", "answer_generation_node",
                }:
                    await event_queue.put({
                        "type": "node_start",
                        "node": node_name,
                    })

                elif event_type == "on_chain_end" and node_name in {
                    "guardrail_node", "query_rephrase_node",
                    "mcp_node", "elicitation_node", "answer_generation_node",
                }:
                    await event_queue.put({
                        "type": "node_complete",
                        "node": node_name,
                    })

                elif event_type == "on_chat_model_stream":
                    # Stream LLM tokens to the UI
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        await event_queue.put({
                            "type": "token",
                            "content": chunk.content,
                        })

            # Get final state from checkpointer
            state_snapshot = await self._graph.aget_state(config)
            if state_snapshot and state_snapshot.values:
                final_state = GraphState(**state_snapshot.values)
            else:
                final_state = GraphState()

            return final_state

        except Exception as e:
            logger.exception("Error inside graph execution")
            await event_queue.put({"type": "error", "message": str(e)})
            raise

        finally:
            # Signal the outer coroutine that graph is done
            await event_queue.put(None)

    def resolve_elicitation(
        self,
        session_id: str,
        elicitation_id: str,
        action: str,
        content: dict[str, Any] | None = None,
    ) -> bool:
        """
        Resolve a pending elicitation with the user's response.

        Called from the /assist endpoint when is_elicitation_response=True.
        This unblocks the on_elicitation callback, allowing tool execution
        to continue on the original request's SSE stream.

        Args:
            session_id: The session that has a pending elicitation
            elicitation_id: Which elicitation to resolve
            action: "accept" | "decline" | "cancel"
            content: Form data (for form mode, action=="accept")

        Returns:
            True if resolved successfully, False if not found
        """
        return self._bridge.resolve(
            session_id=session_id,
            elicitation_id=elicitation_id,
            action=action,
            content=content,
        )
