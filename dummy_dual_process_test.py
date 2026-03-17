from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import time

from assist_service import ElicitationHandler, RedisModule, SessionManager

REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
MCP_SERVER_URL = os.getenv('MCP_SERVER_URL', 'http://localhost:8001/mcp')


def _worker_execute_tool() -> None:
    async def run() -> None:
        redis_module = RedisModule(REDIS_URL)
        await redis_module.connect()
        elicitation_handler = ElicitationHandler(redis_module, consumer_name='worker-exec')
        await elicitation_handler.start()

        manager = SessionManager(
            redis_module=redis_module,
            elicitation_handler=elicitation_handler,
            mcp_server_url=MCP_SERVER_URL,
            form_timeout_seconds=3,
        )

        try:
            print('[proc-1] Executing tool request that triggers form elicitation...')
            result = await manager.execute_tool('needs-form: demo input required')
            print(f'[proc-1] Tool execution result: {result}')
        except asyncio.TimeoutError:
            print('[proc-1] Timed out waiting for elicitation reply.')
        finally:
            await elicitation_handler.stop()
            await redis_module.close()

    asyncio.run(run())


def _worker_send_elicitation_reply() -> None:
    async def run() -> None:
        redis_module = RedisModule(REDIS_URL)
        await redis_module.connect()

        print('[proc-2] Waiting to discover pending elicitation...')
        start = time.time()
        discovered = None
        while time.time() - start < 20:
            pending = await redis_module.list_pending_elicitations()
            if pending:
                discovered = pending[0]
                break
            await asyncio.sleep(0.5)

        if not discovered:
            print('[proc-2] No pending elicitation found.')
            await redis_module.close()
            return

        elicitation_id = discovered['elicitation_id']
        print(f'[proc-2] Found pending elicitation: {elicitation_id}. Sending response...')
        await redis_module.push_elicitation_reply(elicitation_id, {'value': 'approved-from-proc-2'})
        await redis_module.close()

    asyncio.run(run())


def main() -> None:
    async def setup() -> None:
        redis_module = RedisModule(REDIS_URL)
        await redis_module.connect()
        await redis_module.cleanup_demo_data()
        await redis_module.close()

    asyncio.run(setup())

    p1 = mp.Process(target=_worker_execute_tool)
    p2 = mp.Process(target=_worker_send_elicitation_reply)

    p1.start()
    time.sleep(15)
    p2.start()

    p1.join()
    p2.join()

    print('[main] Dual-process dummy test complete.')


if __name__ == '__main__':
    main()
