"""
app/api/assist.py

The /assist endpoint — the single API consumed by the chat UI.

Handles two cases:
1. Normal message → runs the full LangGraph pipeline, streams SSE events
2. Elicitation response → resolves the pending Future, returns 200 immediately
   (the original SSE stream on the previous request continues automatically)

Streaming protocol:
    Content-Type: text/event-stream
    Each event: "data: <json>\n\n"

Event types:
    {"type": "node_start", "node": "..."}
    {"type": "node_complete", "node": "..."}
    {"type": "token", "content": "..."}
    {"type": "elicitation", "elicitation_id": "...", "mode": "form"|"url",
     "message": "...", "schema": {...}|null, "url": "..."|null}
    {"type": "complete", "answer": "...", "node_trace": [...], "elicitation_count": N}
    {"type": "error", "message": "..."}
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.elicitation_bridge import ElicitationBridge
from app.core.graph_runner import GraphRunner
from app.models.api import AssistRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def get_bridge(request: Request) -> ElicitationBridge:
    return request.app.state.elicitation_bridge


def get_graph_runner(request: Request) -> GraphRunner:
    return request.app.state.graph_runner


@router.post(
    "/assist",
    summary="Chat assist endpoint",
    description=(
        "Single endpoint for all chat interactions. "
        "Returns a streaming SSE response for normal messages. "
        "Returns 200 JSON for elicitation responses (which unblock the "
        "original SSE stream on the MCP server side)."
    ),
    responses={
        200: {"description": "Success — streaming SSE or elicitation ACK"},
        400: {"description": "Bad request (e.g., missing elicitation_id)"},
        404: {"description": "No pending elicitation found for session"},
        422: {"description": "Validation error"},
        500: {"description": "Internal server error"},
    },
)
async def assist(
    body: AssistRequest,
    bridge: ElicitationBridge = Depends(get_bridge),
    runner: GraphRunner = Depends(get_graph_runner),
) -> StreamingResponse | JSONResponse:
    """
    POST /assist

    Route A — Elicitation response (is_elicitation_response=True):
        Resolves the pending Future in ElicitationBridge.
        The MCP on_elicitation callback unblocks immediately.
        The original SSE stream (from the initial request) continues
        streaming tool execution and the final answer.
        Returns a lightweight JSON ACK to the client.

    Route B — Normal message:
        Runs the LangGraph pipeline.
        Returns a StreamingResponse with SSE events.
        If a tool triggers elicitation mid-execution, an "elicitation" SSE
        event is emitted on THIS stream, and the MCP session stays alive.
        The client must then POST a new /assist with is_elicitation_response=True.
    """

    session_id = body.session_id

    # ── Route A: Elicitation response ─────────────────────────────────────────
    if body.is_elicitation_response:
        return await _handle_elicitation_response(body, bridge)

    # ── Route B: Normal message ────────────────────────────────────────────────
    return await _handle_normal_message(body, runner)


async def _handle_elicitation_response(
    body: AssistRequest,
    bridge: ElicitationBridge,
) -> JSONResponse:
    """Handle an elicitation response from the user."""

    # Validate required fields
    if not body.elicitation_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "elicitation_id is required when is_elicitation_response=True. "
                "Use the elicitation_id from the elicitation SSE event."
            ),
        )

    logger.info(
        "Elicitation response received: session=%s elicitation_id=%s action=%s",
        body.session_id,
        body.elicitation_id,
        body.elicitation_action,
    )

    resolved = bridge.resolve(
        session_id=body.session_id,
        elicitation_id=body.elicitation_id,
        action=body.elicitation_action,
        content=body.elicitation_content,
    )

    if not resolved:
        logger.warning(
            "No pending elicitation found: session=%s elicitation_id=%s",
            body.session_id,
            body.elicitation_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No pending elicitation found for session '{body.session_id}' "
                f"with elicitation_id '{body.elicitation_id}'. "
                "It may have already been resolved or timed out."
            ),
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "ok",
            "message": "Elicitation response received. Tool execution continuing.",
            "session_id": body.session_id,
            "elicitation_id": body.elicitation_id,
            "action": body.elicitation_action,
        },
    )


async def _handle_normal_message(
    body: AssistRequest,
    runner: GraphRunner,
) -> StreamingResponse:
    """Handle a normal chat message with a streaming SSE response."""

    logger.info(
        "Assist request: session=%s message_length=%d",
        body.session_id,
        len(body.message),
    )

    return StreamingResponse(
        runner.run(
            session_id=body.session_id,
            user_input=body.message,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
            "Connection": "keep-alive",
        },
    )
