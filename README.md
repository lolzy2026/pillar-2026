# MCP Elicitation Service

## Architecture Overview

### The Core Problem & Solution

When an MCP tool calls `ctx.elicit()`, the MCP SDK fires our `on_elicitation` callback **synchronously within the live MCP session context**. This callback must **block and await** until the user provides input — which means:

- The MCP SSE connection to the server stays **alive and open**
- The tool's execution is **suspended in-place** on the MCP server side
- We have an `async` callback function that is literally **waiting** for a `Future` to be resolved

This is our bridge. We don't need Redis, we don't need to "park" connections. We use an `asyncio.Future` as the bridge:

```
MCP Server ──ctx.elicit()──► on_elicitation() callback AWAITS Future
                                         │
                             MCP SSE connection stays ALIVE
                                         │
FastAPI ──SSE stream──► UI receives elicitation event (form schema / URL)
                                         │
User fills form ──► POST /assist {is_elicitation_response: true, session_id: X}
                                         │
ElicitationBridge.resolve(session_id, data) ──► Future.set_result(ElicitResult)
                                         │
on_elicitation unblocks ──► returns ElicitResult to MCP server
                                         │
Tool continues executing normally ──► LangGraph resumes ──► answer generation
```

### Flow Diagrams

#### Normal Flow
```
POST /assist
    └── GraphRunner.run(session_id, user_message)
            ├── guardrail_node
            ├── query_rephrase_node
            ├── mcp_node  ←── tool calls, no elicitation
            └── answer_generation_node
                    └── SSE stream response to UI
```

#### Elicitation Flow
```
POST /assist (initial request)
    └── GraphRunner.run(session_id, user_message)
            ├── guardrail_node
            ├── query_rephrase_node
            └── mcp_node
                    └── tool calls ctx.elicit()
                            └── on_elicitation() BLOCKS on Future
                                    └── SSE event: {type: "elicitation", schema: {...}}
                                            └── UI renders form

POST /assist (elicitation response, same session_id)
    └── GraphRunner.resolve_elicitation(session_id, user_data)
            └── ElicitationBridge.resolve(session_id, ElicitResult)
                    └── Future.set_result() ──► on_elicitation unblocks
                            └── tool continues
                                    └── mcp_node completes
                                            └── answer_generation_node
                                                    └── SSE stream response to UI
```

### Key Components

| Component | File | Responsibility |
|---|---|---|
| `ElicitationBridge` | `app/core/elicitation_bridge.py` | Manages `asyncio.Future` per session, thread-safe, TTL cleanup |
| `MCPSessionManager` | `app/core/mcp_session_manager.py` | App-level `MultiServerMCPClient` with elicitation callback wired |
| `GraphRunner` | `app/core/graph_runner.py` | Executes LangGraph, streams SSE events, handles elicitation branching |
| `elicitation_node` | `app/nodes/elicitation_node.py` | LangGraph node — detects elicitation state, emits SSE event |
| `AssistRouter` | `app/api/assist.py` | Single `/assist` endpoint, routes normal vs elicitation responses |
| `AppState` | `app/models/state.py` | LangGraph state schema including elicitation fields |

### Design Decisions

1. **No LangGraph `interrupt()` for elicitation** — We intentionally do NOT use LangGraph's `interrupt()` primitive here. Why? Because interrupting the graph would close the MCP session context. Instead, we block *inside* the `on_elicitation` callback using an `asyncio.Future`, keeping the MCP session alive. The graph never pauses — it just waits inside the MCP node while the callback is blocking.

2. **LangGraph `interrupt()` is still available** for other human-in-the-loop patterns where MCP is not involved.

3. **`ElicitationBridge` is in-process** — For 50 concurrent users, in-process asyncio Futures are sufficient and far faster than Redis pub/sub. If you scale to multiple FastAPI workers (e.g., gunicorn multiprocess), you would need to add a Redis-backed bridge — the interface is designed for this swap.

4. **Sequential elicitation** — Multiple `ctx.elicit()` calls from a single tool are naturally sequential because each call blocks on its own Future before the next is fired.

5. **Future parallel tools** — The bridge is keyed by `(session_id, elicitation_id)` where `elicitation_id` is a UUID per elicitation event, making it safe for future parallel tool execution where multiple tools could elicit simultaneously.
