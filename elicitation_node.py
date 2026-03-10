"""
app/nodes/elicitation_node.py

Elicitation node for the LangGraph assist graph.

IMPORTANT — Read before editing:

The elicitation node is NOT where elicitation is handled. The actual
elicitation blocking happens inside MCPSessionManager._on_elicitation,
which runs within the MCP tool execution context.

This node's role is:
1. Detect if an elicitation was triggered (by checking graph state)
2. Update the graph state to reflect elicitation status
3. Act as a routing checkpoint for the conditional edge

The flow is:
    mcp_node executes tools
        └─ tool calls ctx.elicit()
            └─ on_elicitation() BLOCKS on Future (MCP session stays alive)
                └─ elicitation SSE event emitted to UI (from within on_elicitation)
                └─ user responds via /assist
                └─ Future resolved
                └─ on_elicitation returns → tool continues
        └─ mcp_node finishes
    elicitation_node runs (post-hoc — records elicitation history)
    answer_generation_node runs

The elicitation node also handles the case where the MCP tool returned
an error due to declined/cancelled elicitation, updating state accordingly.
"""

from __future__ import annotations

import logging

from app.models.state import ElicitationStatus, GraphState

logger = logging.getLogger(__name__)


async def elicitation_node(state: GraphState) -> dict:
    """
    Post-MCP elicitation bookkeeping node.

    Updates graph state after tool execution completes (whether elicitation
    happened or not). This node always runs after mcp_node.
    """
    state.node_trace.append("elicitation_node")

    # If no elicitation was involved, pass through cleanly
    if state.current_elicitation is None:
        return {
            "elicitation_status": ElicitationStatus.NONE,
            "node_trace": state.node_trace,
        }

    # Record the elicitation in history for context/audit
    elicitation_record = {
        "elicitation_id": state.current_elicitation.elicitation_id,
        "mode": state.current_elicitation.mode,
        "message": state.current_elicitation.message,
        "server_name": state.current_elicitation.server_name,
        "tool_name": state.current_elicitation.tool_name,
        "status": state.elicitation_status,
    }

    history = list(state.elicitation_history)
    history.append(elicitation_record)

    logger.info(
        "Elicitation node processed: session=%s elicitation_id=%s status=%s",
        state.session_id,
        state.current_elicitation.elicitation_id,
        state.elicitation_status,
    )

    return {
        "elicitation_history": history,
        "current_elicitation": None,  # Clear after processing
        "elicitation_status": ElicitationStatus.NONE,
        "node_trace": state.node_trace,
    }


def should_run_elicitation(state: GraphState) -> str:
    """
    Conditional edge function: decides whether to route through
    the elicitation node after the MCP node.

    Currently always routes to elicitation_node (which is a passthrough
    if no elicitation occurred). This design allows future logic to
    short-circuit or branch differently based on elicitation outcomes.

    Returns:
        "elicitation_node" — always, for now
        Future: could return "answer_generation_node" to skip elicitation bookkeeping
    """
    return "elicitation_node"
