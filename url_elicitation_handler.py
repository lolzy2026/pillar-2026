"""
app/core/url_elicitation_handler.py

Handles the UrlElicitationRequiredError (-32042) pattern.

Background — Two URL elicitation patterns in MCP:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Pattern 1 — ctx.elicit_url() [Suspend/Resume]:
    Tool calls ctx.elicit_url(url=...) mid-execution.
    Server suspends the tool, sends elicitation/create to client.
    on_elicitation callback fires → Future bridge handles it.
    Tool resumes with the user's response inline.
    ✓ Already handled by MCPSessionManager._on_elicitation.

Pattern 2 — raise UrlElicitationRequiredError [Terminate/Retry]:
    Tool raises UrlElicitationRequiredError before it can do any work.
    Server terminates the tool call immediately.
    Client receives McpError with code -32042.
    on_elicitation is NEVER called — there is no suspended tool.
    Client must: show URLs → wait for user → RE-INVOKE the tool.
    The server has handled the OAuth callback out-of-band by then,
    so the second tool.ainvoke() call succeeds.
    ✓ Handled HERE.

Why the Future bridge still works here:
    Even though there is no open MCP session to keep alive,
    the asyncio.Future is still the right primitive. The mcp_node
    coroutine suspends on it while the user completes the OAuth flow.
    When the user POSTs /assist with is_elicitation_response=True,
    bridge.resolve() unblocks mcp_node which then retries the tool.
    Zero difference from the bridge's perspective.

Error payload structure (-32042):
    error.data = list of ElicitRequestURLParams, each containing:
        - mode:          "url"
        - message:       Human-readable prompt
        - url:           The URL to open
        - elicitationId: Unique ID for tracking this specific URL
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp import McpError

from app.core.elicitation_bridge import ElicitationBridge
from app.core.mcp_session_manager import elicitation_event_emitter
from app.models.state import ElicitationMode, ElicitationRequest

logger = logging.getLogger(__name__)

# MCP JSON-RPC error code for URL elicitation required
URL_ELICITATION_REQUIRED_CODE = -32042


def is_url_elicitation_error(exc: Exception) -> bool:
    """
    Return True if this exception is a UrlElicitationRequiredError (-32042).

    Checks both the MCP SDK exception type and the raw JSON-RPC error code
    to be robust against SDK version differences.
    """
    # Try to import the specific exception type (SDK >= 1.9.0)
    try:
        from mcp.shared.exceptions import UrlElicitationRequiredError
        if isinstance(exc, UrlElicitationRequiredError):
            return True
    except ImportError:
        pass

    # Fallback: check McpError code directly
    if isinstance(exc, McpError):
        code = getattr(exc, "error", None)
        if code is None:
            return False
        # McpError.error is an ErrorData object with a .code field
        error_code = getattr(code, "code", None)
        return error_code == URL_ELICITATION_REQUIRED_CODE

    return False


def extract_url_elicitations(exc: Exception) -> list[ElicitationRequest]:
    """
    Extract the list of ElicitationRequests from a UrlElicitationRequiredError.

    The error carries a list of ElicitRequestURLParams in its data field.
    Each entry has: mode, message, url, elicitationId.

    Returns an empty list if the payload cannot be parsed (fail-safe).
    """
    import uuid

    requests: list[ElicitationRequest] = []

    # Try to get the elicitations list from the error
    # The SDK stores them differently across versions — handle both
    raw_elicitations: list[Any] = []

    # SDK raises UrlElicitationRequiredError(elicitations: list[ElicitRequestURLParams])
    # The list is stored as .args[0] or as .elicitations attribute
    try:
        from mcp.shared.exceptions import UrlElicitationRequiredError
        if isinstance(exc, UrlElicitationRequiredError):
            raw_elicitations = getattr(exc, "elicitations", None) or (
                exc.args[0] if exc.args and isinstance(exc.args[0], list) else []
            )
    except ImportError:
        pass

    # Fallback: McpError stores payload in .error.data
    if not raw_elicitations and isinstance(exc, McpError):
        error_data = getattr(getattr(exc, "error", None), "data", None)
        if isinstance(error_data, list):
            raw_elicitations = error_data

    for item in raw_elicitations:
        try:
            # item is an ElicitRequestURLParams object or a dict
            if hasattr(item, "url"):
                url = item.url
                message = getattr(item, "message", "Authorization required")
                elicitation_id = (
                    getattr(item, "elicitationId", None)
                    or getattr(item, "elicitation_id", None)
                    or str(uuid.uuid4())
                )
            elif isinstance(item, dict):
                url = item.get("url", "")
                message = item.get("message", "Authorization required")
                elicitation_id = item.get("elicitationId") or item.get("elicitation_id") or str(uuid.uuid4())
            else:
                logger.warning("Unexpected elicitation item type: %s", type(item))
                continue

            requests.append(ElicitationRequest(
                elicitation_id=str(elicitation_id),
                mode=ElicitationMode.URL,
                message=message,
                url=url,
                schema=None,
            ))
        except Exception:
            logger.exception("Failed to parse URL elicitation item: %s", item)

    if not requests:
        logger.error(
            "UrlElicitationRequiredError raised but no URL elicitations could be extracted. "
            "Error: %s  Args: %s", exc, exc.args
        )

    return requests


async def handle_url_elicitation_error(
    exc: Exception,
    session_id: str,
    bridge: ElicitationBridge,
) -> None:
    """
    Handle a UrlElicitationRequiredError by:
    1. Extracting all URL elicitation requests from the error payload.
    2. For each URL:
         a. Register a Future in ElicitationBridge.
         b. Emit an SSE "elicitation" event to the UI.
         c. Await the Future (block until user responds via /assist).
    3. Return when ALL URLs have been resolved by the user.

    After this coroutine returns, the caller (mcp_node) should
    RE-INVOKE the tool — the server will have processed the OAuth
    callback by then and the tool call will succeed.

    Args:
        exc:        The caught UrlElicitationRequiredError / McpError(-32042)
        session_id: Current session identifier (read from contextvar by caller)
        bridge:     The ElicitationBridge to register Futures against
    """
    elicitation_requests = extract_url_elicitations(exc)

    if not elicitation_requests:
        raise ValueError(
            "UrlElicitationRequiredError contained no parseable URL elicitations. "
            "Cannot proceed with elicitation flow."
        ) from exc

    logger.info(
        "URL elicitation required: session=%s count=%d urls=%s",
        session_id,
        len(elicitation_requests),
        [r.url for r in elicitation_requests],
    )

    # Retrieve the SSE emitter from contextvar (set by GraphRunner)
    emitter = elicitation_event_emitter.get()

    # Process URL elicitations sequentially.
    # Per spec: multiple URLs in one error are processed one at a time.
    # (Future parallel support: could use asyncio.gather here instead)
    for request in elicitation_requests:
        logger.info(
            "Processing URL elicitation: session=%s id=%s url=%s",
            session_id, request.elicitation_id, request.url,
        )

        # Register Future — same bridge, same mechanism as form elicitation
        future = bridge.register(
            session_id=session_id,
            elicitation_id=request.elicitation_id,
        )

        # Emit SSE event so the UI can display the URL to the user
        if emitter:
            try:
                await emitter(request)
            except Exception:
                logger.exception(
                    "Failed to emit URL elicitation SSE event: session=%s id=%s",
                    session_id, request.elicitation_id,
                )
        else:
            logger.warning(
                "No SSE emitter set for session=%s — "
                "UI will not receive URL elicitation event.", session_id
            )

        # Block until user confirms they completed the URL flow.
        # No MCP session is open — this is pure asyncio Future wait.
        # The event loop serves all other requests normally during this wait.
        try:
            result = await asyncio.wait_for(future, timeout=600)
            logger.info(
                "URL elicitation resolved: session=%s id=%s action=%s",
                session_id, request.elicitation_id, result.action,
            )

            if result.action == "decline":
                raise UrlElicitationDeclinedError(
                    f"User declined URL elicitation for: {request.url}"
                )
            if result.action == "cancel":
                raise UrlElicitationCancelledError(
                    f"URL elicitation cancelled for: {request.url}"
                )
            # action == "accept" → continue to next URL or return to caller

        except asyncio.TimeoutError:
            logger.warning(
                "URL elicitation timed out: session=%s id=%s", session_id, request.elicitation_id
            )
            raise UrlElicitationTimeoutError(
                f"Timed out waiting for user to complete URL: {request.url}"
            )

    logger.info(
        "All URL elicitations resolved for session=%s — tool will be re-invoked",
        session_id,
    )


class UrlElicitationDeclinedError(Exception):
    """User declined a URL elicitation. Tool should not be re-invoked."""


class UrlElicitationCancelledError(Exception):
    """User cancelled a URL elicitation. Tool should not be re-invoked."""


class UrlElicitationTimeoutError(Exception):
    """URL elicitation timed out. Tool should not be re-invoked."""
