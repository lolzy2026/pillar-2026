from __future__ import annotations

import uuid
from typing import Any

from assist_service.elicitation_handler import ElicitationHandler
from assist_service.redis_module import RedisModule

try:
    from langchain_mcp_adapters.client import Callbacks, MultiServerMCPClient
except Exception as exc:  # pragma: no cover
    raise RuntimeError('langchain-mcp-adapters is required for SessionManager') from exc


class SessionManager:
    """Minimal MCP session manager.

    Responsibilities:
    - Build MCP client session via langchain-mcp-adapters.
    - Register on_elicitation callback.
    - Manage form-mode pause/resume through ElicitationHandler.
    """

    def __init__(
        self,
        redis_module: RedisModule,
        elicitation_handler: ElicitationHandler,
        mcp_server_url: str,
        mcp_transport: str = 'streamable_http',
        form_timeout_seconds: int = 60,
    ):
        self.redis_module = redis_module
        self.elicitation_handler = elicitation_handler
        self.form_timeout_seconds = form_timeout_seconds

        callbacks = Callbacks(on_elicitation=self._on_elicitation)
        self.client = MultiServerMCPClient(
            {
                'primary': {
                    'url': mcp_server_url,
                    'transport': mcp_transport,
                }
            },
            callbacks=callbacks,
        )

    async def execute_tool(self, message: str) -> dict[str, Any]:
        # Dummy execution path for integration testing.
        # Replace this with real tool selection + tool call using self.client.
        if 'needs-form' in message.lower():
            elicitation_id = str(uuid.uuid4())
            response = await self._on_elicitation(
                {
                    'mode': 'form',
                    'elicitation_id': elicitation_id,
                    'prompt': 'Provide value for simulated tool input',
                    'form': {'fields': [{'name': 'value', 'type': 'string', 'required': True}]},
                }
            )
            return {'status': 'completed', 'kind': 'form', 'elicitation_response': response}

        return {'status': 'completed', 'kind': 'direct', 'output': f'processed: {message}'}

    async def _on_elicitation(self, elicitation_request: Any) -> dict[str, Any]:
        payload = self._normalize_request(elicitation_request)
        mode = payload.get('mode', 'form')
        elicitation_id = payload.get('elicitation_id') or str(uuid.uuid4())
        payload['elicitation_id'] = elicitation_id

        if mode != 'form':
            return {'status': 'unsupported_mode', 'mode': mode, 'elicitation_id': elicitation_id}

        return await self.elicitation_handler.wait_for_form_response(
            elicitation_id=elicitation_id,
            prompt_payload=payload,
            timeout_seconds=self.form_timeout_seconds,
        )

    @staticmethod
    def _normalize_request(elicitation_request: Any) -> dict[str, Any]:
        if isinstance(elicitation_request, dict):
            return dict(elicitation_request)

        payload: dict[str, Any] = {}
        for attr in ('mode', 'elicitation_id', 'elicitationId', 'prompt', 'form', 'url'):
            value = getattr(elicitation_request, attr, None)
            if value is not None:
                key = 'elicitation_id' if attr in ('elicitation_id', 'elicitationId') else attr
                payload[key] = value
        return payload
