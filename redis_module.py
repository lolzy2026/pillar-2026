from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError


class RedisModule:
    def __init__(
        self,
        redis_url: str,
        reply_stream: str = 'assist:elicitation:replies',
        reply_group: str = 'assist:elicitation:workers',
    ):
        self.redis_url = redis_url
        self.reply_stream = reply_stream
        self.reply_group = reply_group
        self.redis = Redis.from_url(redis_url, decode_responses=True)

    async def connect(self) -> None:
        await self._ensure_group(self.reply_stream, self.reply_group)

    async def close(self) -> None:
        await self.redis.close()

    async def _ensure_group(self, stream: str, group: str) -> None:
        try:
            await self.redis.xgroup_create(stream, group, id='0-0', mkstream=True)
        except ResponseError as exc:
            if 'BUSYGROUP' not in str(exc):
                raise

    async def push_elicitation_reply(self, elicitation_id: str, payload: dict[str, Any]) -> str:
        body = {'elicitation_id': elicitation_id, 'payload': json.dumps(payload)}
        return await self.redis.xadd(self.reply_stream, body)

    async def read_elicitation_replies(
        self,
        consumer_name: str,
        count: int = 10,
        block_ms: int = 1000,
    ) -> list[tuple[str, dict[str, Any]]]:
        entries = await self.redis.xreadgroup(
            self.reply_group,
            consumer_name,
            {self.reply_stream: '>'},
            count=count,
            block=block_ms,
        )
        decoded: list[tuple[str, dict[str, Any]]] = []
        for _stream, messages in entries:
            for message_id, fields in messages:
                payload = json.loads(fields.get('payload', '{}'))
                decoded.append((message_id, {'elicitation_id': fields.get('elicitation_id'), 'payload': payload}))
        return decoded

    async def ack_elicitation_reply(self, message_id: str) -> None:
        await self.redis.xack(self.reply_stream, self.reply_group, message_id)

    async def set_pending_elicitation(self, elicitation_id: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        await self.redis.set(f'assist:pending:{elicitation_id}', json.dumps(payload), ex=ttl_seconds)

    async def clear_pending_elicitation(self, elicitation_id: str) -> None:
        await self.redis.delete(f'assist:pending:{elicitation_id}')

    async def list_pending_elicitations(self) -> list[dict[str, Any]]:
        cursor = 0
        results: list[dict[str, Any]] = []
        while True:
            cursor, keys = await self.redis.scan(cursor=cursor, match='assist:pending:*', count=100)
            for key in keys:
                value = await self.redis.get(key)
                if not value:
                    continue
                payload = json.loads(value)
                payload['elicitation_id'] = key.split('assist:pending:')[-1]
                results.append(payload)
            if cursor == 0:
                break
        return results

    async def cleanup_demo_data(self) -> None:
        await self.redis.delete(self.reply_stream)
        cursor = 0
        while True:
            cursor, keys = await self.redis.scan(cursor=cursor, match='assist:pending:*', count=100)
            if keys:
                await self.redis.delete(*keys)
            if cursor == 0:
                break
