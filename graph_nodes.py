"""
app/nodes/graph_nodes.py

LangGraph node implementations for the assist graph.

Node execution order:
    guardrail_node → query_rephrase_node → mcp_node → answer_generation_node

Two elicitation patterns handled in mcp_node:

    Pattern 1 — ctx.elicit_url() / ctx.elicit() [Suspend/Resume]:
        Tool suspends mid-execution and calls elicitation/create on the client.
        The on_elicitation callback in MCPSessionManager fires automatically.
        The Future bridge handles it transparently inside tool.ainvoke().
        mcp_node sees nothing unusual — ainvoke() just takes longer to return.

    Pattern 2 — raise UrlElicitationRequiredError [Terminate/Retry]:
        Tool raises UrlElicitationRequiredError before it can proceed.
        tool.ainvoke() raises McpError(-32042) immediately.
        mcp_node catches it, calls handle_url_elicitation_error() which:
            - extracts URL(s) from the error payload
            - registers Future(s) in ElicitationBridge
            - emits SSE elicitation event(s) to the UI
            - blocks until user confirms completion via /assist
        mcp_node then RE-INVOKES the tool (server has auth token by now).

Accessing per-request dependencies (mcp_client, mcp_tools, bridge) in nodes:
    LangGraph passes a RunnableConfig to every node. Per-request objects
    that must NOT be checkpointed are injected via config["configurable"]
    by GraphRunner, not via state.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.core.mcp_session_manager import current_session_id
from app.core.url_elicitation_handler import (
    UrlElicitationCancelledError,
    UrlElicitationDeclinedError,
    UrlElicitationTimeoutError,
    handle_url_elicitation_error,
    is_url_elicitation_error,
)
from app.models.state import GraphState

logger = logging.getLogger(__name__)


# ── Guardrail Node ────────────────────────────────────────────────────────────

async def guardrail_node(state: GraphState, config: RunnableConfig) -> dict:
    """
    Safety guardrail node.

    Validates the user's input against safety and policy rules.
    Sets is_safe=False with a reason if the input should be blocked —
    the conditional edge will then route directly to answer_generation_node
    which produces a safe rejection response.

    TODO (teammates): Implement content moderation, PII detection, etc.
    """
    state.node_trace.append("guardrail_node")
    logger.debug("guardrail_node: session=%s", state.session_id)

    # STUB: always safe
    return {
        "is_safe": True,
        "guardrail_reason": "",
        "node_trace": state.node_trace,
    }


def should_continue_after_guardrail(state: GraphState) -> str:
    """Conditional edge: safe input continues, unsafe skips to answer."""
    return "query_rephrase_node" if state.is_safe else "answer_generation_node"


# ── Query Rephrase Node ───────────────────────────────────────────────────────

async def query_rephrase_node(state: GraphState, config: RunnableConfig) -> dict:
    """
    Query rephrasing node.

    Reformulates the user's raw input into a form better suited for
    tool selection and execution (e.g. resolving pronouns, adding context
    from conversation history, standardising terminology).

    TODO (teammates): Implement LLM-based rephrasing.
    """
    state.node_trace.append("query_rephrase_node")
    logger.debug("query_rephrase_node: session=%s", state.session_id)

    # STUB: pass through unchanged
    return {
        "rephrased_query": state.user_input,
        "node_trace": state.node_trace,
    }


# ── MCP Node ──────────────────────────────────────────────────────────────────

async def mcp_node(state: GraphState, config: RunnableConfig) -> dict:
    """
    MCP tool execution node.

    Handles BOTH elicitation patterns transparently:

    Pattern 1 — ctx.elicit_url() / ctx.elicit() (Suspend/Resume):
        on_elicitation callback fires inside tool.ainvoke().
        This node sees nothing unusual — ainvoke() just takes longer.
        The Future bridge in MCPSessionManager handles it completely.

    Pattern 2 — raise UrlElicitationRequiredError (Terminate/Retry):
        tool.ainvoke() raises McpError(-32042).
        This node catches it, calls handle_url_elicitation_error().
        That blocks on a Future until the user confirms URL completion.
        This node then RE-INVOKES the tool (max MAX_RETRIES times).
        The server has processed the OAuth callback by then → success.

    Per-request dependencies are injected via config["configurable"]
    by GraphRunner (not stored in state, so never serialised to PostgreSQL).

    TODO (teammates): Replace the STUB section with real LLM-driven tool
    selection. The elicitation retry logic below is production-ready and
    should be kept as-is.
    """
    state.node_trace.append("mcp_node")

    # ── Retrieve per-request dependencies from config ─────────────────────────
    configurable = config.get("configurable", {})
    mcp_client = configurable.get("mcp_client")
    tools: list = configurable.get("mcp_tools", [])
    bridge = configurable.get("elicitation_bridge")

    if not mcp_client:
        logger.error("mcp_node: mcp_client not found in config.")
        return {
            "tool_results": [],
            "error": "MCP client not available",
            "node_trace": state.node_trace,
        }

    session_id = current_session_id.get() or state.session_id

    logger.debug(
        "mcp_node: session=%s query='%s' tools=%s",
        session_id, state.rephrased_query, [t.name for t in tools],
    )

    # ── Tool execution with URL elicitation retry loop ────────────────────────
    # MAX_URL_ELICITATION_RETRIES: how many times we re-invoke after a -32042.
    # In practice this is almost always 1 — the tool fails once (auth needed),
    # user logs in, tool succeeds on the second attempt.
    MAX_URL_ELICITATION_RETRIES = 3
    tool_results = []

    # ──────────────────────────────────────────────────────────────────────────
    # STUB SECTION — teammates replace this block with real LLM tool selection.
    #
    # The pattern to follow for EACH tool call:
    #
    #   for attempt in range(MAX_URL_ELICITATION_RETRIES + 1):
    #       try:
    #           result = await some_tool.ainvoke(args)
    #           # Pattern 1 elicitation (ctx.elicit/ctx.elicit_url) fires
    #           # transparently inside ainvoke(). No special handling needed.
    #           tool_results.append({"tool": some_tool.name, "result": result})
    #           break  # success
    #
    #       except Exception as exc:
    #           if is_url_elicitation_error(exc):
    #               if attempt >= MAX_URL_ELICITATION_RETRIES:
    #                   tool_results.append({
    #                       "tool": some_tool.name,
    #                       "result": "Tool failed: URL elicitation retry limit reached",
    #                       "error": True,
    #                   })
    #                   break
    #               # Block until user completes the URL flow, then retry
    #               await handle_url_elicitation_error(exc, session_id, bridge)
    #               continue  # retry the tool call
    #           else:
    #               raise  # non-elicitation error — propagate normally
    #
    # The stub below demonstrates the retry loop with a fake tool:
    # ──────────────────────────────────────────────────────────────────────────

    stub_tool_name = "login_v2"  # Replace with LLM-selected tool name

    for attempt in range(MAX_URL_ELICITATION_RETRIES + 1):
        try:
            # TODO: replace with real tool selection and invocation
            # result = await selected_tool.ainvoke(tool_args)
            stub_result = f"Stub result for: {state.rephrased_query} (attempt {attempt + 1})"
            tool_results.append({"tool": stub_tool_name, "result": stub_result})
            break

        except Exception as exc:
            if is_url_elicitation_error(exc):
                if attempt >= MAX_URL_ELICITATION_RETRIES:
                    logger.warning(
                        "URL elicitation retry limit reached: session=%s tool=%s",
                        session_id, stub_tool_name,
                    )
                    tool_results.append({
                        "tool": stub_tool_name,
                        "result": "Tool could not complete: URL elicitation retry limit reached.",
                        "error": True,
                    })
                    break

                logger.info(
                    "URL elicitation required: session=%s tool=%s attempt=%d — "
                    "waiting for user to complete URL flow",
                    session_id, stub_tool_name, attempt + 1,
                )
                try:
                    await handle_url_elicitation_error(exc, session_id, bridge)
                    # User completed the URL flow — retry the tool
                    logger.info(
                        "URL elicitation complete — retrying tool: session=%s tool=%s",
                        session_id, stub_tool_name,
                    )
                    continue

                except (UrlElicitationDeclinedError, UrlElicitationCancelledError) as user_action:
                    logger.info(
                        "URL elicitation not accepted: session=%s reason=%s",
                        session_id, user_action,
                    )
                    tool_results.append({
                        "tool": stub_tool_name,
                        "result": f"Tool not executed: {user_action}",
                        "error": False,  # User choice, not a system error
                    })
                    break

                except UrlElicitationTimeoutError as timeout_exc:
                    logger.warning(
                        "URL elicitation timed out: session=%s", session_id
                    )
                    tool_results.append({
                        "tool": stub_tool_name,
                        "result": "Tool not executed: URL elicitation timed out.",
                        "error": True,
                    })
                    break
            else:
                # Non-elicitation error — let it propagate to the graph error handler
                raise

    return {
        "tool_results": tool_results,
        "messages": [AIMessage(content=f"Tool execution complete for: {state.rephrased_query}")],
        "node_trace": state.node_trace,
    }


# ── Answer Generation Node ────────────────────────────────────────────────────

async def answer_generation_node(state: GraphState, config: RunnableConfig) -> dict:
    """
    Final answer synthesis node.

    Combines tool results (and optionally conversation history) into a
    coherent natural-language response for the user.

    Also handles the guardrail rejection path: if is_safe=False, generates
    a safe refusal message instead of running tool-based answer generation.

    TODO (teammates): Implement LLM-based synthesis from tool_results.
    """
    state.node_trace.append("answer_generation_node")
    logger.debug("answer_generation_node: session=%s", state.session_id)

    if not state.is_safe:
        answer = (
            f"I'm not able to help with that request. {state.guardrail_reason}".strip()
        )
    else:
        results_text = "\n".join(
            f"- {r.get('tool', 'tool')}: {r.get('result', '')}"
            for r in state.tool_results
        )
        answer = f"Based on the available information:\n{results_text}"

    return {
        "final_answer": answer,
        "messages": [AIMessage(content=answer)],
        "node_trace": state.node_trace,
    }
