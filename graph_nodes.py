"""
app/nodes/graph_nodes.py

Boilerplate stub implementations for all non-elicitation graph nodes.
Each node follows the correct LangGraph signature and state contract.
Your teammates will fill in the real logic.

Node responsibilities (summary):
- guardrail_node:       Safety check on user input
- query_rephrase_node:  Rephrase/clarify the user's query
- mcp_node:             Execute MCP tools (this is where elicitation originates)
- answer_generation_node: Generate final answer from tool results
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage

from app.models.state import GraphState

logger = logging.getLogger(__name__)


# ── Guardrail Node ────────────────────────────────────────────────────────────

async def guardrail_node(state: GraphState) -> dict:
    """
    Safety guardrail node.

    Checks the user's input for safety/policy violations.
    Sets is_safe=False and guardrail_reason if the input should be blocked.

    TODO: Implement actual guardrail logic (content moderation, etc.)
    """
    state.node_trace.append("guardrail_node")
    logger.debug("guardrail_node: session=%s", state.session_id)

    # STUB: always passes
    return {
        "is_safe": True,
        "guardrail_reason": "",
        "node_trace": state.node_trace,
    }


def should_continue_after_guardrail(state: GraphState) -> str:
    """
    Conditional edge after guardrail.
    Routes to query_rephrase if safe, else to a rejection response.
    """
    if not state.is_safe:
        return "answer_generation_node"  # Will generate a safe rejection response
    return "query_rephrase_node"


# ── Query Rephrase Node ───────────────────────────────────────────────────────

async def query_rephrase_node(state: GraphState) -> dict:
    """
    Query rephrasing/clarification node.

    Reformulates the user's query for better tool execution.

    TODO: Implement LLM-based query rephrasing.
    """
    state.node_trace.append("query_rephrase_node")
    logger.debug("query_rephrase_node: session=%s", state.session_id)

    # STUB: pass through unchanged
    return {
        "rephrased_query": state.user_input,
        "node_trace": state.node_trace,
    }


# ── MCP Node ──────────────────────────────────────────────────────────────────

async def mcp_node(state: GraphState, mcp_tools: list) -> dict:
    """
    MCP tool execution node.

    Executes one or more MCP tools based on the rephrased query.
    If a tool calls ctx.elicit(), the MCPSessionManager.on_elicitation
    callback fires automatically — this node doesn't need any special
    elicitation handling code. The blocking happens transparently inside
    the tool call.

    When on_elicitation blocks:
    - This node's execution is suspended (inside the tool call await)
    - The MCP SSE connection stays open
    - The elicitation event is emitted to the UI via the contextvar emitter
    - When the user responds, the Future resolves and the tool call continues

    After the tool call returns (with elicitation handled), this node
    updates the graph state with tool results.

    TODO: Implement actual LLM-driven tool selection and execution.
          Consider: tool selection via LLM, result parsing, error handling.
    """
    state.node_trace.append("mcp_node")
    logger.debug("mcp_node: session=%s query='%s'", state.session_id, state.rephrased_query)

    # STUB: return dummy tool results
    # In real implementation:
    # 1. Use LLM to decide which tools to call
    # 2. await tool.ainvoke(args) — this transparently handles elicitation
    # 3. Collect results into state.tool_results

    tool_results = [
        {
            "tool": "stub_tool",
            "result": f"Stub result for query: {state.rephrased_query}",
        }
    ]

    return {
        "tool_results": tool_results,
        "messages": [AIMessage(content=f"Tool executed for: {state.rephrased_query}")],
        "node_trace": state.node_trace,
    }


# ── Answer Generation Node ────────────────────────────────────────────────────

async def answer_generation_node(state: GraphState) -> dict:
    """
    Final answer generation node.

    Synthesizes tool results into a coherent answer for the user.

    TODO: Implement LLM-based answer generation from tool results.
    """
    state.node_trace.append("answer_generation_node")
    logger.debug("answer_generation_node: session=%s", state.session_id)

    # Handle blocked input case
    if not state.is_safe:
        answer = f"I'm unable to help with that request. {state.guardrail_reason}"
    else:
        # STUB: combine tool results into answer
        results_text = "\n".join(
            f"- {r.get('tool', 'tool')}: {r.get('result', '')}"
            for r in state.tool_results
        )
        answer = f"Based on the tool results:\n{results_text}"

    return {
        "final_answer": answer,
        "messages": [AIMessage(content=answer)],
        "node_trace": state.node_trace,
    }
