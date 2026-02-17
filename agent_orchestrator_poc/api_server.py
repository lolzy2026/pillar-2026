
import sys
import os
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Dict, Any, Optional
import uuid

# Ensure the parent directory is in sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_orchestrator_poc.graph_orchestrator import create_graph, AgentState, AgentType
from agent_orchestrator_poc.models import ConversationStatus, TaskStatus

app = FastAPI(title="Multi-Agent Orchestrator API")

# Define Models
class ChatRequest(BaseModel):
    session_id: str
    message: str

class ChatResponse(BaseModel):
    conversation_status: ConversationStatus
    current_agent: AgentType
    message_for_user: str
    memory_update: Dict[str, Any]
    task: Optional[Dict[str, Any]] = None

# Initialize Graph
graph = create_graph()

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    try:
        config = {"configurable": {"thread_id": request.session_id}}
        
        # Prepare Inputs
        inputs = {"user_input": request.message}
        
        # Run Graph
        # We stream values to get the final state
        # (Same logic as CLI loop but single turn)
        
        # Check if we need to resume?
        # LangGraph handles resumption via thread_id automatically. 
        # If there is saved state for this thread_id, it uses it.
        
        events = graph.stream(inputs, config, stream_mode="values")
        
        final_state = None
        for event in events:
            final_state = event
            
        if not final_state:
            raise HTTPException(status_code=500, detail="No state returned from graph.")

        # Construct Response
        task_obj = final_state.get("task")
        task_dict = task_obj.dict() if task_obj else None
        
        # Determine message to return
        # If messages list exists, get the last AI message
        messages = final_state.get("messages", [])
        last_msg = ""
        if messages:
            last_msg = messages[-1].content
            
        return ChatResponse(
            conversation_status=final_state.get("conversation_status", ConversationStatus.RUNNING),
            current_agent=final_state.get("next_agent", AgentType.NONE),
            message_for_user=last_msg,
            memory_update=final_state.get("memory", {}),
            task=task_dict
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
