"""
app/models/api.py

Pydantic models for the /assist API request and response.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class AssistRequest(BaseModel):
    """
    Request body for POST /assist.

    Handles both:
    1. Normal user messages: is_elicitation_response=False (default)
    2. Elicitation responses: is_elicitation_response=True

    The session_id ties together the entire conversation thread and
    is used by LangGraph as the checkpoint thread_id.
    """

    session_id: str = Field(
        ...,
        description="Unique session identifier. Must be stable across the conversation.",
        min_length=1,
        max_length=128,
    )
    message: str = Field(
        ...,
        description="User's message or elicitation response content.",
        min_length=0,
        max_length=32_000,
    )

    # ── Elicitation response fields ───────────────────────────────────────────
    is_elicitation_response: bool = Field(
        default=False,
        description=(
            "Set to True when this request is a response to an elicitation event. "
            "When True, elicitation_id and elicitation_action must be provided."
        ),
    )
    elicitation_id: str | None = Field(
        default=None,
        description="ID of the elicitation being responded to. Required if is_elicitation_response=True.",
    )
    elicitation_action: str = Field(
        default="accept",
        description="User's action: 'accept' | 'decline' | 'cancel'.",
        pattern="^(accept|decline|cancel)$",
    )
    elicitation_content: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Form data for 'accept' action (form mode). "
            "Should match the schema provided in the elicitation event."
        ),
    )

    @field_validator("elicitation_id")
    @classmethod
    def validate_elicitation_id(cls, v: str | None, info) -> str | None:
        # If is_elicitation_response is True, elicitation_id must be provided
        # Note: Cross-field validation is done in the endpoint handler
        return v

    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "summary": "Normal message",
                    "value": {
                        "session_id": "user-abc-123",
                        "message": "What's my account balance?",
                    },
                },
                {
                    "summary": "Elicitation form response",
                    "value": {
                        "session_id": "user-abc-123",
                        "message": "",
                        "is_elicitation_response": True,
                        "elicitation_id": "550e8400-e29b-41d4-a716-446655440000",
                        "elicitation_action": "accept",
                        "elicitation_content": {
                            "email": "user@example.com",
                            "age": 30,
                        },
                    },
                },
                {
                    "summary": "Elicitation URL mode — user confirmed",
                    "value": {
                        "session_id": "user-abc-123",
                        "message": "",
                        "is_elicitation_response": True,
                        "elicitation_id": "550e8400-e29b-41d4-a716-446655440000",
                        "elicitation_action": "accept",
                        "elicitation_content": {"confirmed": True},
                    },
                },
                {
                    "summary": "Elicitation declined",
                    "value": {
                        "session_id": "user-abc-123",
                        "message": "",
                        "is_elicitation_response": True,
                        "elicitation_id": "550e8400-e29b-41d4-a716-446655440000",
                        "elicitation_action": "decline",
                    },
                },
            ]
        }


class AssistResponse(BaseModel):
    """
    Non-streaming response model (used as documentation reference).
    The actual /assist endpoint returns a StreamingResponse.
    """

    session_id: str
    answer: str
    elicitation_count: int = 0
