# MCP Tool Execution with Elicitation - Complete Implementation

## Architecture Overview

This implementation provides a complete FastAPI service for MCP (Model Context Protocol) tool execution with interactive elicitation support.

**Key Features:**
- ✅ Single `/assist` endpoint for both messages and elicitation responses
- ✅ Redis Pub/Sub for efficient callback coordination (no in-memory state)
- ✅ LangGraph with PostgreSQL checkpointer for state persistence
- ✅ Two-node pattern: `mcp_tool_node` and `elicitation_node`
- ✅ Graceful handling of process restarts (accepts failed in-flight requests)
- ✅ Support for 50+ concurrent users

## Components

### 1. **ElicitationSessionManager**
- Manages elicitation lifecycle using Redis Pub/Sub
- No in-memory futures - all state in Redis
- Handles timeouts (3 minutes default)
- Publishes to Redis channels to wake waiting callbacks

### 2. **MCP Tool Node**
- Executes tools via LangChain MCP Adapter
- Catches `ElicitationRequiredException` 
- Routes to elicitation node when needed

### 3. **Elicitation Node**
- Interrupts graph execution
- Waits for user input via `/assist` endpoint
- Clears elicitation state on resume

### 4. **LangGraph Workflow**
```
Router → MCP Tool Node → [Conditional]
                            ↓
                    Elicitation? → Elicitation Node → LLM Node → END
                            ↓
                    No? → LLM Node → END
```

## Installation

### Prerequisites
- Python 3.11+
- Redis 7+
- PostgreSQL 14+

### Setup

1. **Clone and install dependencies:**
```bash
pip install -r requirements.txt
```

2. **Configure environment:**
```bash
cp .env.example .env
# Edit .env with your configuration
```

3. **Initialize PostgreSQL:**
```sql
CREATE DATABASE mcp_assistant;
```

The LangGraph checkpointer will auto-create tables on first run.

4. **Start Redis:**
```bash
redis-server
```

## Running the Service

### Development
```bash
python complete_mcp_implementation.py
```

### Production
```bash
uvicorn complete_mcp_implementation:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1  # IMPORTANT: Must be 1
```

**Note:** Workers must be set to 1 because:
- LangGraph state is process-specific
- Redis Pub/Sub subscriptions don't work across processes
- For horizontal scaling, use multiple containers with load balancing

## API Usage

### 1. Send a Message (New Conversation)

```bash
curl -X POST http://localhost:8000/assist \
  -H "Content-Type: application/json" \
  -d '{
    "request_type": "message",
    "message": "Get sales data for January 2024",
    "thread_id": "conv-123",
    "user_id": "user-456",
    "organization_id": "org-789",
    "auth_token": "Bearer your-token"
  }'
```

**Response (if elicitation needed):**
```json
{
  "response": "I need additional information to proceed.",
  "pending_elicitation": {
    "elicitation_id": "elic-abc-123",
    "schema": {
      "type": "object",
      "properties": {
        "date_range": {"type": "string"},
        "region": {"type": "string"}
      }
    },
    "message": "Please specify the date range and region"
  },
  "status": "awaiting_elicitation"
}
```

### 2. Submit Elicitation Response

```bash
curl -X POST http://localhost:8000/assist \
  -H "Content-Type: application/json" \
  -d '{
    "request_type": "elicitation_response",
    "thread_id": "conv-123",
    "user_id": "user-456",
    "organization_id": "org-789",
    "auth_token": "Bearer your-token",
    "elicitation_id": "elic-abc-123",
    "elicitation_data": {
      "date_range": "2024-01-01 to 2024-01-31",
      "region": "North America"
    }
  }'
```

**Response (after completion):**
```json
{
  "response": "Tool executed successfully. Here are the results.",
  "pending_elicitation": null,
  "status": "completed"
}
```

### 3. Check Session Status (Optional)

```bash
curl http://localhost:8000/session/conv-123/status
```

**Response:**
```json
{
  "thread_id": "conv-123",
  "pending_elicitations": [
    {
      "elicitation_id": "elic-abc-123",
      "schema": {...},
      "message": "Please specify...",
      "created_at": "2024-03-09T10:30:00Z"
    }
  ],
  "has_pending": true
}
```

## Flow Diagrams

### Normal Execution (No Elicitation)
```
1. POST /assist {request_type: "message"}
2. Graph: Router → MCP Tool Node
3. MCP Tool Node: tool.ainvoke() → Success
4. Graph: MCP Tool Node → LLM Node → END
5. Response: {status: "completed", response: "..."}
```

### With Elicitation
```
1. POST /assist {request_type: "message"}
2. Graph: Router → MCP Tool Node
3. tool.ainvoke() → MCP Gateway → elicitation_required
4. Adapter: on_elicitation() callback triggered
5. Callback: Creates elicitation in Redis
6. Callback: Raises ElicitationRequiredException
7. MCP Tool Node: Catches exception, updates state
8. Graph: Routes to Elicitation Node
9. Elicitation Node: Calls interrupt()
10. Graph: Pauses, returns to /assist
11. Response: {status: "awaiting_elicitation", pending_elicitation: {...}}
12. UI displays form to user
13. User fills form
14. POST /assist {request_type: "elicitation_response", ...}
15. Session Manager: submit_elicitation_response()
16. Session Manager: Publishes to Redis channel
17. Callback: wait_for_user_response() receives via Pub/Sub
18. Callback: Returns ElicitResult to adapter
19. Adapter: Forwards to MCP Gateway
20. MCP Gateway: Continues execution, returns result
21. tool.ainvoke() completes
22. Graph: Resumes from checkpoint
23. Graph: Elicitation Node → LLM Node → END
24. Response: {status: "completed", response: "..."}
```

## Redis Data Structure

```
# Elicitation metadata (for UI)
elicitation:{elicitation_id} = {
    "thread_id": str,
    "callback_id": str,
    "status": "pending|completed|timeout",
    "schema": json,
    "message": str,
    "response": json,
    "created_at": timestamp
}
TTL: 300 seconds

# Callback state (for Pub/Sub coordination)
callback:{callback_id} = {
    "elicitation_id": str,
    "thread_id": str,
    "status": "waiting|resolved|timeout",
    "result": json,
    "created_at": timestamp
}
TTL: 300 seconds

# Thread tracking
thread:{thread_id}:callbacks = Set[callback_id]
TTL: 300 seconds

# Pub/Sub channel (ephemeral)
callback:{callback_id}:response
```

## PostgreSQL Tables

LangGraph checkpointer creates these automatically:
- `checkpoints` - Graph state snapshots
- `checkpoint_blobs` - Large state data
- `checkpoint_writes` - Incremental updates

## Error Handling

### Timeout (User doesn't respond within 3 minutes)
```python
# Callback raises TimeoutError
# Tool execution fails
# User sees: "Tool execution timed out"
```

### Process Restart During Elicitation
```python
# Redis state persists
# Pub/Sub subscription is lost
# Callback future is lost
# User submission publishes to channel (no listeners)
# Execution fails
# User sees error and can retry
```

### MCP Gateway Unavailable
```python
# tool.ainvoke() raises connection error
# MCP Tool Node catches it
# User sees: "Failed to connect to MCP Gateway"
```

## Configuration

### Timeouts
```python
ELICITATION_TIMEOUT = 180  # 3 minutes to respond
ELICITATION_TTL = 300      # 5 minutes total (with buffer)
```

### Redis Connection
```python
redis_client = await redis.from_url(
    REDIS_URL,
    encoding="utf-8",
    decode_responses=False
)
```

### PostgreSQL Checkpointer
```python
checkpointer = PostgresSaver(
    connection_string=DATABASE_URL
)
```

## Monitoring

### Health Check
```bash
curl http://localhost:8000/health
```

### Logs
The implementation includes extensive logging:
- `[SessionManager]` - Elicitation lifecycle
- `[MCP Callback]` - Adapter callback events
- `[MCP Tool Node]` - Tool execution
- `[Elicitation Node]` - Graph interrupts
- `[/assist]` - API requests

### Metrics to Track
- Elicitation creation rate
- User response time (created_at → completed_at)
- Timeout rate
- Tool execution duration
- Graph execution duration

## Deployment

### Docker
```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY complete_mcp_implementation.py .
COPY .env .

CMD ["uvicorn", "complete_mcp_implementation:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

### Kubernetes
- Deploy as single replica (workers=1)
- For horizontal scaling, run multiple pods
- Use Redis Cluster for shared state
- Use connection pooling for PostgreSQL

## Testing

### Test Elicitation Flow
```python
import httpx
import asyncio

async def test_elicitation():
    async with httpx.AsyncClient() as client:
        # 1. Send message
        resp1 = await client.post("http://localhost:8000/assist", json={
            "request_type": "message",
            "message": "Get sales data",
            "thread_id": "test-123",
            "user_id": "test-user",
            "organization_id": "test-org",
            "auth_token": "test-token"
        })
        
        data1 = resp1.json()
        assert data1["status"] == "awaiting_elicitation"
        
        elicitation_id = data1["pending_elicitation"]["elicitation_id"]
        
        # 2. Submit response
        resp2 = await client.post("http://localhost:8000/assist", json={
            "request_type": "elicitation_response",
            "thread_id": "test-123",
            "user_id": "test-user",
            "organization_id": "test-org",
            "auth_token": "test-token",
            "elicitation_id": elicitation_id,
            "elicitation_data": {"field": "value"}
        })
        
        data2 = resp2.json()
        assert data2["status"] == "completed"

asyncio.run(test_elicitation())
```

## Troubleshooting

### "Callback not found"
- Redis TTL expired (user took >5 minutes)
- Redis was flushed
- Check Redis connection

### "Graph doesn't resume after elicitation"
- Check PostgreSQL checkpointer connection
- Verify thread_id matches
- Check LangGraph checkpoint tables

### "Pub/Sub message not received"
- Check Redis connection
- Verify channel name matches
- Check for network issues

### "Multiple workers causing issues"
- Must use workers=1
- Each worker has separate Redis Pub/Sub subscription
- State is not shared across workers

## Performance

### Expected Metrics (50 concurrent users)
- Redis: <10ms for pub/sub
- PostgreSQL checkpoint save: <50ms
- Tool execution: 2-10 seconds (depends on MCP Gateway)
- Elicitation waiting: 30-180 seconds (user dependent)
- Total request: 5-15 seconds (without elicitation)

### Resource Usage
- Memory: ~200MB per worker
- Redis: ~1MB per active elicitation
- PostgreSQL: ~5KB per checkpoint
- CPU: Low (mostly I/O bound)

## Future Enhancements

1. **Multiple Parallel Tools**
   - Track multiple tool executions per thread
   - Queue elicitations or show all at once

2. **Persistent Pub/Sub Reconnection**
   - Handle process restarts gracefully
   - Resume subscriptions from Redis state

3. **Metrics & Observability**
   - Prometheus metrics
   - OpenTelemetry tracing
   - Structured logging

4. **Rate Limiting**
   - Per-user limits
   - Per-organization limits
   - Redis-based counters

## License

MIT

## Support

For issues or questions, please refer to:
- LangChain docs: https://docs.langchain.com
- LangGraph docs: https://langchain-ai.github.io/langgraph/
- MCP spec: https://modelcontextprotocol.io
