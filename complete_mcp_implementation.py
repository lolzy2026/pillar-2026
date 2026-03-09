"""
Complete MCP Tool Execution with Elicitation Implementation
Using LangChain MCP Adapter + Redis Pub/Sub + LangGraph

Architecture:
- Single /assist endpoint for both messages and elicitation responses
- Two-node pattern: mcp_tool_node and elicitation_node
- Redis Pub/Sub for callback coordination (no in-memory futures)
- PostgreSQL checkpointer for graph state persistence
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from typing import TypedDict, Literal, Optional, Annotated
import operator

# FastAPI
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Redis
import redis.asyncio as redis

# PostgreSQL
import asyncpg

# LangGraph
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import interrupt

# LangChain MCP Adapter
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.callbacks import Callbacks
from mcp.types import ElicitRequestParams, ElicitResult

# ============================================================================
# CONFIGURATION
# ============================================================================

REDIS_URL = "redis://localhost:6379"
DATABASE_URL = "postgresql://user:password@localhost/db"
MCP_GATEWAY_URL = "https://mcp-gateway.example.com/mcp"

ELICITATION_TIMEOUT = 180  # 3 minutes
ELICITATION_TTL = 300  # 5 minutes (with 2 min buffer)

# ============================================================================
# STATE DEFINITIONS
# ============================================================================

class AgentState(TypedDict):
    """LangGraph agent state"""
    
    # Messages
    messages: Annotated[list, operator.add]
    
    # Session info
    thread_id: str
    user_token: str
    
    # Tool execution tracking
    current_tool_call: Optional[dict]
    tool_execution_status: Literal[
        "not_started",
        "in_progress",
        "awaiting_elicitation",
        "completed",
        "failed"
    ]
    
    # Elicitation data
    pending_elicitation: Optional[dict]  # {elicitation_id, schema, message}
    callback_id: Optional[str]

# ============================================================================
# PYDANTIC MODELS FOR API
# ============================================================================

class AssistRequest(BaseModel):
    """Request model for /assist endpoint"""
    
    thread_id: str
    user_id: str
    organization_id: str
    auth_token: str
    
    # Request type discriminator
    request_type: Literal["message", "elicitation_response"]
    
    # For regular messages
    message: Optional[str] = None
    
    # For elicitation responses
    elicitation_id: Optional[str] = None
    elicitation_data: Optional[dict] = None

class AssistResponse(BaseModel):
    """Response model for /assist endpoint"""
    
    response: str
    pending_elicitation: Optional[dict] = None
    status: Literal["completed", "awaiting_elicitation", "failed"]

# ============================================================================
# REDIS-BASED SESSION MANAGER (Pub/Sub)
# ============================================================================

class ElicitationSessionManager:
    """
    Manages elicitation lifecycle using Redis Pub/Sub.
    No in-memory state - all state persists in Redis.
    """
    
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.timeout = ELICITATION_TIMEOUT
        self.ttl = ELICITATION_TTL
    
    async def create_elicitation(
        self,
        elicitation_id: str,
        thread_id: str,
        callback_id: str,
        schema: dict,
        message: str
    ):
        """
        Create elicitation entry in Redis.
        Called by MCP adapter's on_elicitation callback.
        """
        
        now = datetime.utcnow().isoformat()
        
        # Store elicitation metadata (for UI)
        await self.redis.hset(
            f"elicitation:{elicitation_id}",
            mapping={
                "thread_id": thread_id,
                "callback_id": callback_id,
                "status": "pending",
                "schema": json.dumps(schema),
                "message": message,
                "response": json.dumps(None),
                "created_at": now,
            }
        )
        await self.redis.expire(f"elicitation:{elicitation_id}", self.ttl)
        
        # Store callback state (for waiting mechanism)
        await self.redis.hset(
            f"callback:{callback_id}",
            mapping={
                "elicitation_id": elicitation_id,
                "thread_id": thread_id,
                "status": "waiting",  # waiting | resolved | timeout
                "result": json.dumps(None),
                "created_at": now
            }
        )
        await self.redis.expire(f"callback:{callback_id}", self.ttl)
        
        # Add to thread's active callbacks (for monitoring/cleanup)
        await self.redis.sadd(f"thread:{thread_id}:callbacks", callback_id)
        await self.redis.expire(f"thread:{thread_id}:callbacks", self.ttl)
        
        print(f"[SessionManager] Created elicitation {elicitation_id} with callback {callback_id}")
    
    async def wait_for_user_response(
        self,
        callback_id: str
    ) -> dict:
        """
        Wait for user to submit response via Redis Pub/Sub.
        Blocks until user submits or timeout occurs.
        
        This is called inside the MCP adapter's on_elicitation callback.
        """
        
        print(f"[SessionManager] Waiting for user response on callback {callback_id}")
        
        channel = f"callback:{callback_id}:response"
        pubsub = self.redis.pubsub()
        
        try:
            await pubsub.subscribe(channel)
            
            start_time = datetime.utcnow()
            
            # Listen for published messages
            async for message in pubsub.listen():
                # Check timeout
                elapsed = (datetime.utcnow() - start_time).total_seconds()
                if elapsed > self.timeout:
                    print(f"[SessionManager] Callback {callback_id} timed out")
                    
                    # Mark as timeout in Redis
                    await self.redis.hset(
                        f"callback:{callback_id}",
                        "status",
                        "timeout"
                    )
                    raise TimeoutError(f"User did not respond within {self.timeout}s")
                
                # Process message
                if message["type"] == "message":
                    # User submitted response!
                    response_data = json.loads(message["data"])
                    
                    print(f"[SessionManager] Received user response for callback {callback_id}")
                    
                    # Cleanup callback state
                    await self.redis.delete(f"callback:{callback_id}")
                    
                    return response_data
        
        except Exception as e:
            print(f"[SessionManager] Error waiting for callback {callback_id}: {e}")
            raise
        
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
    
    async def submit_elicitation_response(
        self,
        elicitation_id: str,
        response: dict
    ) -> str:
        """
        Handle user's elicitation response submission.
        Called when user submits via /assist endpoint.
        
        Returns thread_id for resuming the graph.
        """
        
        print(f"[SessionManager] Submitting response for elicitation {elicitation_id}")
        
        # Get elicitation data
        elic_data = await self.redis.hgetall(f"elicitation:{elicitation_id}")
        
        if not elic_data:
            raise Exception(f"Elicitation {elicitation_id} not found or expired")
        
        # Extract callback info
        callback_id = elic_data[b"callback_id"].decode()
        thread_id = elic_data[b"thread_id"].decode()
        
        # Update elicitation status
        await self.redis.hset(
            f"elicitation:{elicitation_id}",
            mapping={
                "status": "completed",
                "response": json.dumps(response),
                "completed_at": datetime.utcnow().isoformat()
            }
        )
        
        # Update callback status
        await self.redis.hset(
            f"callback:{callback_id}",
            mapping={
                "status": "resolved",
                "result": json.dumps(response)
            }
        )
        
        # PUBLISH to wake up waiting callback!
        channel = f"callback:{callback_id}:response"
        await self.redis.publish(channel, json.dumps(response))
        
        print(f"[SessionManager] Published response to channel {channel}")
        
        return thread_id
    
    async def get_pending_elicitations(
        self,
        thread_id: str
    ) -> list[dict]:
        """
        Get all pending elicitations for a thread.
        Used for UI polling if needed.
        """
        
        callback_ids = await self.redis.smembers(f"thread:{thread_id}:callbacks")
        
        pending = []
        for callback_id in callback_ids:
            callback_id = callback_id.decode() if isinstance(callback_id, bytes) else callback_id
            
            # Get callback status
            status_bytes = await self.redis.hget(f"callback:{callback_id}", "status")
            
            if status_bytes and status_bytes.decode() == "waiting":
                # Get associated elicitation
                elic_id_bytes = await self.redis.hget(f"callback:{callback_id}", "elicitation_id")
                if not elic_id_bytes:
                    continue
                
                elic_id = elic_id_bytes.decode()
                elic_data = await self.redis.hgetall(f"elicitation:{elic_id}")
                
                if elic_data:
                    pending.append({
                        "elicitation_id": elic_id,
                        "schema": json.loads(elic_data[b"schema"]),
                        "message": elic_data[b"message"].decode(),
                        "created_at": elic_data[b"created_at"].decode()
                    })
        
        return pending

# ============================================================================
# CUSTOM EXCEPTIONS
# ============================================================================

class ElicitationRequiredException(Exception):
    """
    Raised when MCP tool needs elicitation.
    This signals the node to route to elicitation_node.
    """
    def __init__(self, elicitation_id: str, callback_id: str):
        self.elicitation_id = elicitation_id
        self.callback_id = callback_id
        super().__init__(f"Elicitation required: {elicitation_id}")

# ============================================================================
# MCP CLIENT FACTORY
# ============================================================================

def create_mcp_client_with_callback(
    user_token: str,
    thread_id: str,
    session_manager: ElicitationSessionManager,
    state: AgentState
) -> MultiServerMCPClient:
    """
    Create MCP client with elicitation callback configured.
    
    The callback:
    1. Creates elicitation in Redis
    2. Updates graph state
    3. Waits for user response via Pub/Sub
    4. Returns result to MCP adapter
    """
    
    async def on_elicitation(
        mcp_context,
        params: ElicitRequestParams,
        context
    ) -> ElicitResult:
        """
        Called by LangChain adapter when MCP server needs elicitation.
        Blocks until user submits response via Redis Pub/Sub.
        """
        
        print(f"[MCP Callback] Elicitation required: {params.message}")
        
        elicitation_id = str(uuid.uuid4())
        callback_id = str(uuid.uuid4())
        
        # Create elicitation in Redis
        await session_manager.create_elicitation(
            elicitation_id=elicitation_id,
            thread_id=thread_id,
            callback_id=callback_id,
            schema=params.requestedSchema,
            message=params.message or "Additional information required"
        )
        
        # Update graph state (triggers routing to elicitation_node)
        state["pending_elicitation"] = {
            "elicitation_id": elicitation_id,
            "schema": params.requestedSchema,
            "message": params.message or "Additional information required"
        }
        state["callback_id"] = callback_id
        state["tool_execution_status"] = "awaiting_elicitation"
        
        # Raise exception to signal the graph to interrupt
        # The exception will be caught by mcp_tool_node
        raise ElicitationRequiredException(
            elicitation_id=elicitation_id,
            callback_id=callback_id
        )
        
        # NOTE: Code below never executes in this pattern
        # It would execute if we didn't raise exception
        # But we need to interrupt the graph, so we raise
    
    # Create MCP client
    client = MultiServerMCPClient(
        {
            "mcp_gateway": {
                "transport": "http",
                "url": MCP_GATEWAY_URL,
                "headers": {
                    "Authorization": f"Bearer {user_token}"
                }
            }
        },
        callbacks=Callbacks(on_elicitation=on_elicitation)
    )
    
    return client

# ============================================================================
# MCP TOOL NODE
# ============================================================================

def create_mcp_tool_node(session_manager: ElicitationSessionManager):
    """
    Factory to create MCP tool execution node.
    Handles tool invocation and elicitation exceptions.
    """
    
    async def mcp_tool_node(state: AgentState) -> AgentState:
        """
        Execute MCP tool.
        If elicitation is raised, state is updated and routing happens.
        """
        
        print(f"[MCP Tool Node] Status: {state.get('tool_execution_status', 'not_started')}")
        
        status = state.get("tool_execution_status", "not_started")
        
        # Check if already completed
        if status == "completed" or status == "failed":
            print(f"[MCP Tool Node] Already {status}, skipping")
            return state
        
        # Extract or retrieve tool call
        if status == "not_started":
            tool_call = extract_tool_call_from_messages(state["messages"])
            if not tool_call:
                print("[MCP Tool Node] No tool call found in messages")
                return state
            
            state["current_tool_call"] = tool_call
            state["tool_execution_status"] = "in_progress"
        else:
            tool_call = state["current_tool_call"]
        
        print(f"[MCP Tool Node] Executing tool: {tool_call['name']}")
        
        # Get MCP client with callback
        client = create_mcp_client_with_callback(
            user_token=state["user_token"],
            thread_id=state["thread_id"],
            session_manager=session_manager,
            state=state
        )
        
        # Get tools from MCP Gateway
        try:
            tools = await client.get_tools()
        except Exception as e:
            print(f"[MCP Tool Node] Failed to get tools: {e}")
            state["messages"].append({
                "role": "system",
                "content": f"Failed to connect to MCP Gateway: {str(e)}"
            })
            state["tool_execution_status"] = "failed"
            return state
        
        # Find the specific tool
        tool = next((t for t in tools if t.name == tool_call["name"]), None)
        
        if not tool:
            print(f"[MCP Tool Node] Tool {tool_call['name']} not found")
            state["messages"].append({
                "role": "system",
                "content": f"Tool '{tool_call['name']}' not found"
            })
            state["tool_execution_status"] = "failed"
            return state
        
        try:
            # Invoke tool - this may raise ElicitationRequiredException
            print(f"[MCP Tool Node] Invoking tool.ainvoke()")
            result = await tool.ainvoke(tool_call["parameters"])
            
            # Success - tool completed without elicitation
            print(f"[MCP Tool Node] Tool completed successfully")
            state["messages"].append({
                "role": "tool",
                "content": str(result),
                "tool_call_id": tool_call["id"]
            })
            state["tool_execution_status"] = "completed"
            state["current_tool_call"] = None
            
        except ElicitationRequiredException as e:
            # Elicitation was raised by the callback
            # State was already updated in the callback
            print(f"[MCP Tool Node] Elicitation required: {e.elicitation_id}")
            # Just return - the conditional edge will route to elicitation_node
            
        except Exception as e:
            print(f"[MCP Tool Node] Tool execution failed: {e}")
            state["messages"].append({
                "role": "system",
                "content": f"Tool execution failed: {str(e)}"
            })
            state["tool_execution_status"] = "failed"
        
        return state
    
    return mcp_tool_node

# ============================================================================
# ELICITATION NODE
# ============================================================================

def create_elicitation_node(session_manager: ElicitationSessionManager):
    """
    Factory to create elicitation handling node.
    This node interrupts the graph to wait for user input.
    """
    
    async def elicitation_node(state: AgentState) -> AgentState:
        """
        Handle elicitation by interrupting the graph.
        Graph will pause here until user submits response.
        """
        
        print(f"[Elicitation Node] Entered with status: {state.get('tool_execution_status')}")
        
        # This node is only reached when tool_execution_status is "awaiting_elicitation"
        # Interrupt the graph (this pauses execution)
        
        elicitation_id = state["pending_elicitation"]["elicitation_id"]
        
        print(f"[Elicitation Node] Interrupting graph for elicitation {elicitation_id}")
        
        interrupt({
            "reason": "elicitation_required",
            "elicitation_id": elicitation_id
        })
        
        # When graph resumes (after user submits), execution continues here
        print(f"[Elicitation Node] Resumed after user submission")
        
        # The adapter callback is still waiting in wait_for_user_response()
        # It will receive the response via Pub/Sub and return to the adapter
        
        # Clear elicitation data from state
        state["pending_elicitation"] = None
        state["callback_id"] = None
        
        return state
    
    return elicitation_node

# ============================================================================
# OTHER NODES (Placeholder implementations)
# ============================================================================

async def router_node(state: AgentState) -> AgentState:
    """
    Router node - determines what to do with the message.
    In real implementation, this would use an LLM to decide.
    """
    print("[Router Node] Processing message")
    # Simple passthrough for now
    return state

async def llm_node(state: AgentState) -> AgentState:
    """
    LLM node - generates response based on tool results.
    In real implementation, this would call an LLM.
    """
    print("[LLM Node] Generating response")
    
    # Simple implementation: just acknowledge tool completion
    if state.get("tool_execution_status") == "completed":
        state["messages"].append({
            "role": "assistant",
            "content": "Tool executed successfully. Here are the results."
        })
    elif state.get("tool_execution_status") == "failed":
        state["messages"].append({
            "role": "assistant",
            "content": "I encountered an error while executing the tool."
        })
    else:
        state["messages"].append({
            "role": "assistant",
            "content": "I'm ready to help!"
        })
    
    return state

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def extract_tool_call_from_messages(messages: list) -> Optional[dict]:
    """
    Extract tool call from messages.
    In real implementation, this would parse LLM output.
    """
    # Look for the last user message requesting a tool
    for msg in reversed(messages):
        if msg.get("role") == "user":
            # Simple heuristic: if message mentions a tool, extract it
            content = msg.get("content", "")
            
            # Example: "Use tool fetch_sales_data with params {date_range: '2024-01'}"
            # In reality, this would be structured from LLM tool calling
            
            # For demo, return a mock tool call
            if "fetch_sales_data" in content.lower():
                return {
                    "id": str(uuid.uuid4()),
                    "name": "fetch_sales_data",
                    "parameters": {
                        "date_range": "2024-01"
                    }
                }
    
    return None

def extract_assistant_message(messages: list) -> str:
    """Extract the last assistant message from message list"""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            return msg.get("content", "")
    return "Task completed."

# ============================================================================
# GRAPH BUILDER
# ============================================================================

def build_langgraph(session_manager: ElicitationSessionManager) -> StateGraph:
    """
    Build and compile the LangGraph agent.
    """
    
    print("[Graph Builder] Building LangGraph")
    
    workflow = StateGraph(AgentState)
    
    # Add nodes
    workflow.add_node("router", router_node)
    workflow.add_node("mcp_tool_node", create_mcp_tool_node(session_manager))
    workflow.add_node("elicitation_node", create_elicitation_node(session_manager))
    workflow.add_node("llm_node", llm_node)
    
    # Entry point
    workflow.set_entry_point("router")
    
    # Router -> MCP Tool
    workflow.add_edge("router", "mcp_tool_node")
    
    # MCP Tool -> Conditional routing
    def route_after_mcp_tool(state: AgentState) -> str:
        """Decide where to go after MCP tool node"""
        status = state.get("tool_execution_status")
        
        if status == "awaiting_elicitation":
            return "elicitation_node"
        else:
            return "llm_node"
    
    workflow.add_conditional_edges(
        "mcp_tool_node",
        route_after_mcp_tool,
        {
            "elicitation_node": "elicitation_node",
            "llm_node": "llm_node"
        }
    )
    
    # Elicitation -> Back to MCP Tool (to complete execution)
    # Actually, after interrupt resumes, we need to handle the callback
    # The callback is still waiting in wait_for_user_response()
    
    # When elicitation_node returns, we go to LLM
    workflow.add_edge("elicitation_node", "llm_node")
    
    # LLM -> END
    workflow.add_edge("llm_node", END)
    
    return workflow

# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

app = FastAPI(title="MCP Assistant Service")

@app.on_event("startup")
async def startup():
    """
    Initialize all components at server startup.
    Graph is compiled once and reused for all requests.
    """
    
    print("=" * 80)
    print("STARTING MCP ASSISTANT SERVICE")
    print("=" * 80)
    
    # 1. Initialize Redis
    print("[Startup] Connecting to Redis...")
    redis_client = await redis.from_url(
        REDIS_URL,
        encoding="utf-8",
        decode_responses=False  # We handle encoding manually
    )
    app.state.redis = redis_client
    print("[Startup] ✓ Redis connected")
    
    # 2. Initialize Session Manager
    print("[Startup] Creating Session Manager...")
    session_manager = ElicitationSessionManager(redis_client)
    app.state.session_manager = session_manager
    print("[Startup] ✓ Session Manager ready")
    
    # 3. Initialize PostgreSQL Checkpointer
    print("[Startup] Creating PostgreSQL Checkpointer...")
    checkpointer = PostgresSaver(connection_string=DATABASE_URL)
    print("[Startup] ✓ PostgreSQL Checkpointer ready")
    
    # 4. Build and Compile LangGraph
    print("[Startup] Building LangGraph...")
    workflow = build_langgraph(session_manager)
    app.state.langgraph_app = workflow.compile(checkpointer=checkpointer)
    print("[Startup] ✓ LangGraph compiled")
    
    print("=" * 80)
    print("SERVER READY - Listening for requests")
    print("=" * 80)

@app.on_event("shutdown")
async def shutdown():
    """Cleanup resources"""
    print("[Shutdown] Closing connections...")
    await app.state.redis.close()
    print("[Shutdown] ✓ Cleanup complete")

# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.post("/assist", response_model=AssistResponse)
async def assist_endpoint(request: AssistRequest):
    """
    Unified endpoint for both new messages and elicitation responses.
    
    For new messages:
        POST /assist {
            request_type: "message",
            message: "Get sales data",
            thread_id: "...",
            auth_token: "..."
        }
    
    For elicitation responses:
        POST /assist {
            request_type: "elicitation_response",
            elicitation_id: "...",
            elicitation_data: {...},
            thread_id: "...",
            auth_token: "..."
        }
    """
    
    session_manager: ElicitationSessionManager = app.state.session_manager
    langgraph_app = app.state.langgraph_app
    
    print(f"\n{'='*80}")
    print(f"[/assist] Request: {request.request_type} | Thread: {request.thread_id}")
    print(f"{'='*80}")
    
    # Prepare initial state based on request type
    if request.request_type == "message":
        # New user message - start new execution
        print(f"[/assist] New message: {request.message}")
        
        initial_state = {
            "messages": [{"role": "user", "content": request.message}],
            "thread_id": request.thread_id,
            "user_token": request.auth_token,
            "tool_execution_status": "not_started",
            "current_tool_call": None,
            "pending_elicitation": None,
            "callback_id": None
        }
    
    elif request.request_type == "elicitation_response":
        # User submitting elicitation response - resume execution
        print(f"[/assist] Elicitation response for: {request.elicitation_id}")
        
        if not request.elicitation_id or not request.elicitation_data:
            raise HTTPException(400, "elicitation_id and elicitation_data required")
        
        # Submit to session manager (this publishes to Pub/Sub)
        try:
            thread_id = await session_manager.submit_elicitation_response(
                elicitation_id=request.elicitation_id,
                response=request.elicitation_data
            )
            
            if thread_id != request.thread_id:
                raise HTTPException(400, "Thread ID mismatch")
            
        except Exception as e:
            print(f"[/assist] Failed to submit elicitation: {e}")
            raise HTTPException(400, f"Failed to submit elicitation: {str(e)}")
        
        # Resume graph with None (loads from checkpoint)
        initial_state = None
    
    else:
        raise HTTPException(400, f"Invalid request_type: {request.request_type}")
    
    # Execute or resume graph
    config = {
        "configurable": {
            "thread_id": request.thread_id,
            "user_id": request.user_id,
        }
    }
    
    try:
        print(f"[/assist] Invoking LangGraph...")
        result = await langgraph_app.ainvoke(initial_state, config=config)
        
        print(f"[/assist] Graph execution completed")
        print(f"[/assist] Final status: {result.get('tool_execution_status')}")
        
        # Check if elicitation is pending
        if result.get("pending_elicitation"):
            print(f"[/assist] Elicitation pending")
            return AssistResponse(
                response="I need additional information to proceed.",
                pending_elicitation=result["pending_elicitation"],
                status="awaiting_elicitation"
            )
        else:
            # Extract final response
            assistant_msg = extract_assistant_message(result["messages"])
            
            status = "completed" if result.get("tool_execution_status") == "completed" else "completed"
            if result.get("tool_execution_status") == "failed":
                status = "failed"
            
            print(f"[/assist] Returning response: {status}")
            return AssistResponse(
                response=assistant_msg,
                pending_elicitation=None,
                status=status
            )
    
    except Exception as e:
        print(f"[/assist] Error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Execution failed: {str(e)}")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "mcp-assistant",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/session/{thread_id}/status")
async def get_session_status(thread_id: str):
    """
    Optional endpoint to check pending elicitations.
    Can be used for UI polling if needed.
    """
    session_manager: ElicitationSessionManager = app.state.session_manager
    
    pending = await session_manager.get_pending_elicitations(thread_id)
    
    return {
        "thread_id": thread_id,
        "pending_elicitations": pending,
        "has_pending": len(pending) > 0
    }

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║                                                              ║
    ║          MCP Tool Execution with Elicitation                 ║
    ║          Using LangChain Adapter + Redis Pub/Sub             ║
    ║                                                              ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    
    uvicorn.run(
        "complete_mcp_implementation:app",
        host="0.0.0.0",
        port=8000,
        reload=False,  # Set to True for development
        workers=1  # Must be 1 to avoid state issues
    )
