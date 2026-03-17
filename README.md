# Minimal MCP Session Manager

This project is intentionally reduced to **3 modules**:

1. `session_manager.py`
- Owns MCP tool lifecycle.
- Builds `MultiServerMCPClient(..., callbacks=Callbacks(on_elicitation=...))`.
- Delegates form-mode waits to `ElicitationHandler`.

2. `elicitation_handler.py`
- Creates/stores `asyncio.Future` per `elicitation_id`.
- Waits with `asyncio.wait_for(...)` timeout.
- Continuously consumes Redis Stream replies and resolves matching futures.

3. `redis_module.py`
- All Redis operations (stream read/write, ack, pending elicitation keys).

## Package structure

- `src/assist_service/session_manager.py`
- `src/assist_service/elicitation_handler.py`
- `src/assist_service/redis_module.py`

## Install

```bash
pip install -e .
```

## Environment

- `REDIS_URL` default: `redis://localhost:6379/0`
- `MCP_SERVER_URL` default in dummy test: `http://localhost:8001/mcp`

## Dummy dual-process test

This test runs:
- **Process 1**: sends tool execution request (triggers form elicitation and waits).
- **Process 2**: discovers pending elicitation and sends response on Redis Stream.

Run:

```bash
python scripts/dummy_dual_process_test.py
```

Expected behavior:
- Process 1 blocks waiting on `Future`.
- Process 2 pushes response to Redis stream.
- Elicitation handler resolves Future by `elicitation_id`.
- Process 1 continues and prints completed result.
