from __future__ import annotations

import asyncio
from typing import Any

from assist_service.redis_module import RedisModule


class ElicitationHandler:
    def __init__(self, redis_module: RedisModule, consumer_name: str = 'elicitation-consumer'):
        self.redis_module = redis_module
        self.consumer_name = consumer_name
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._guard = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._reader_task = asyncio.create_task(self._read_reply_stream(), name='elicitation-reply-reader')

    async def stop(self) -> None:
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)

    async def wait_for_form_response(
        self,
        elicitation_id: str,
        prompt_payload: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        print(f'elicitation_id: {elicitation_id}')
        print(f'prompt_payload: {prompt_payload}')
        print(f'timeout_seconds: {timeout_seconds}')
        await self.redis_module.set_pending_elicitation(elicitation_id, prompt_payload, ttl_seconds=timeout_seconds)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        async with self._guard:
            self._pending[elicitation_id] = future

        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        finally:
            async with self._guard:
                self._pending.pop(elicitation_id, None)
            await self.redis_module.clear_pending_elicitation(elicitation_id)

    async def _read_reply_stream(self) -> None:
        while self._running:
            try:
                entries = await self.redis_module.read_elicitation_replies(self.consumer_name)
                for message_id, entry in entries:
                    elicitation_id = entry.get('elicitation_id')
                    payload = entry.get('payload') or {}
                    if elicitation_id:
                        await self._resolve_future(elicitation_id, payload)
                    await self.redis_module.ack_elicitation_reply(message_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.25)

    async def _resolve_future(self, elicitation_id: str, payload: dict[str, Any]) -> None:
        async with self._guard:
            future = self._pending.get(elicitation_id)
            if not future or future.done():
                return
            future.set_result(payload)
