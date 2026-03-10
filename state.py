"""
app/models/state.py

LangGraph state schema for the assist graph.
Includes all fields needed for elicitation flow.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class ElicitationMode(str, Enum):
    FORM = "form"
    URL = "url"


class ElicitationStatus(str, Enum):
    NONE = "none"
    PENDING = "pending"       # Waiting for user input
    RESOLVED = "resolved"     # User has responded
    DECLINED = "declined"     # User declined
    CANCELLED = "cancelled"   # User cancelled


class ElicitationRequest(BaseModel):
    """Represents a single elicitation request from the MCP server."""

    elicitation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    mode: ElicitationMode
    message: str                        # Human-readable prompt for the user
    schema: dict[str, Any] | None = None  # JSON schema for form mode
    url: str | None = None              # URL for url mode
    server_name: str = ""               # Which MCP server triggered this
    tool_name: str = ""                 # Which tool triggered this


class ElicitationResponse(BaseModel):
    """User's response to an elicitation request."""

    elicitation_id: str
    action: str                          # "accept" | "decline" | "cancel"
    content: dict[str, Any] | None = None  # Form data if action == "accept"


class GraphState(BaseModel):
    """
    Complete LangGraph state for the assist graph.

    Note on messages: uses LangGraph's add_messages reducer which
    handles message deduplication and append semantics automatically.
    """

    # ── Core conversation fields ──────────────────────────────────────────
    messages: Annotated[list[BaseMessage], add_messages] = Field(default_factory=list)
    session_id: str = ""
    user_input: str = ""
    rephrased_query: str = ""
    final_answer: str = ""

    # ── Guardrail fields ──────────────────────────────────────────────────
    is_safe: bool = True
    guardrail_reason: str = ""

    # ── Elicitation fields ────────────────────────────────────────────────
    # Current pending elicitation (None if no active elicitation)
    current_elicitation: ElicitationRequest | None = None
    elicitation_status: ElicitationStatus = ElicitationStatus.NONE

    # History of all elicitations in this session (for audit/context)
    elicitation_history: list[dict[str, Any]] = Field(default_factory=list)

    # ── MCP execution fields ──────────────────────────────────────────────
    # Tracks which tools have been called (for future parallel execution support)
    tool_calls_pending: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)

    # ── Metadata ──────────────────────────────────────────────────────────
    error: str | None = None
    node_trace: list[str] = Field(default_factory=list)

    class Config:
        arbitrary_types_allowed = True
