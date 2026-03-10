"""
app/graph/builder.py

Builds and compiles the LangGraph assist graph.

The graph is compiled ONCE at application startup (in FastAPI lifespan)
and reused across all requests. Thread safety is guaranteed by LangGraph's
checkpointer — each invocation uses its own thread_id (session_id).

Graph topology:
    START
      │
      ▼
    guardrail_node
      │
      ├─(unsafe)──────────────────────────┐
      │                                   │
      ▼                                   │
    query_rephrase_node                   │
      │                                   │
      ▼                                   │
    mcp_node ◄── tool calls happen here   │
      │          elicitation blocks here  │
      │                                   │
      ▼                                   │
    elicitation_node (bookkeeping)        │
      │                                   │
      ▼                                   ▼
    answer_generation_node ◄──────────────┘
      │
      ▼
     END
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

from app.models.state import GraphState
from app.nodes.elicitation_node import elicitation_node, should_run_elicitation
from app.nodes.graph_nodes import (
    answer_generation_node,
    guardrail_node,
    mcp_node,
    query_rephrase_node,
    should_continue_after_guardrail,
)

logger = logging.getLogger(__name__)


def build_graph(
    checkpointer: AsyncPostgresSaver,
    mcp_tools: list[BaseTool],
) -> Any:
    """
    Build and compile the LangGraph assist graph.

    Args:
        checkpointer: PostgreSQL-backed async checkpointer for state persistence
        mcp_tools: LangChain tools loaded from the MCP server

    Returns:
        Compiled LangGraph (CompiledStateGraph)
    """

    # ── Bind mcp_tools into mcp_node via partial ──────────────────────────────
    # LangGraph nodes must have signature (state) -> dict.
    # We use functools.partial to inject mcp_tools without a custom node class.
    bound_mcp_node = partial(mcp_node, mcp_tools=mcp_tools)
    bound_mcp_node.__name__ = "mcp_node"  # For LangGraph tracing

    # ── Build the graph ───────────────────────────────────────────────────────
    builder = StateGraph(GraphState)

    # Register nodes
    builder.add_node("guardrail_node", guardrail_node)
    builder.add_node("query_rephrase_node", query_rephrase_node)
    builder.add_node("mcp_node", bound_mcp_node)
    builder.add_node("elicitation_node", elicitation_node)
    builder.add_node("answer_generation_node", answer_generation_node)

    # ── Edges ─────────────────────────────────────────────────────────────────

    # Entry
    builder.add_edge(START, "guardrail_node")

    # After guardrail: safe → rephrase, unsafe → answer (rejection)
    builder.add_conditional_edges(
        "guardrail_node",
        should_continue_after_guardrail,
        {
            "query_rephrase_node": "query_rephrase_node",
            "answer_generation_node": "answer_generation_node",
        },
    )

    # Rephrase → MCP
    builder.add_edge("query_rephrase_node", "mcp_node")

    # MCP → Elicitation (always — elicitation_node is a passthrough if no elicitation)
    builder.add_conditional_edges(
        "mcp_node",
        should_run_elicitation,
        {
            "elicitation_node": "elicitation_node",
        },
    )

    # Elicitation → Answer Generation
    builder.add_edge("elicitation_node", "answer_generation_node")

    # Answer Generation → END
    builder.add_edge("answer_generation_node", END)

    # ── Compile ───────────────────────────────────────────────────────────────
    compiled = builder.compile(checkpointer=checkpointer)

    logger.info("LangGraph assist graph compiled successfully")
    return compiled
