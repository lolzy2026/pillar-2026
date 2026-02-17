import sqlite3
import json
from typing import TypedDict, Annotated, List, Dict, Any, Optional, Union
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage

# Reuse existing models
from .models import AgentType, Task, TaskStatus, ConversationStatus, RequiredInput

# Define AgentState
class AgentState(TypedDict):
    messages: List[BaseMessage]
    task: Optional[Task]
    memory: Dict[str, Any]
    next_agent: AgentType
    conversation_status: ConversationStatus
    user_input: Optional[str] # To persist latest input

# --- Node Functions ---

def planner_node(state: AgentState) -> AgentState:
    # If we already have a task that is incomplete or waiting, we should probably not create a new one
    # unless the user intent completely changes. For this POC, we resume the existing task.
    current_task = state.get('task')
    if current_task and current_task.status in [TaskStatus.WAITING_FOR_USER, TaskStatus.RUNNING]:
        return {
            "next_agent": AgentType.EXECUTION,
            "conversation_status": ConversationStatus.RUNNING
        }

    user_msg = state['user_input']
    
    # Simple heuristic (same as before)
    new_task = Task(
        id="task_001",
        status=TaskStatus.PENDING,
        description=user_msg if user_msg else "Processing...",
        required_inputs=[],
        output={}
    )
    
    return {
        "task": new_task,
        "next_agent": AgentType.EXECUTION,
        "conversation_status": ConversationStatus.RUNNING,
        "messages": [AIMessage(content="I have analyzed your request. Proceeding to execution.")]
    }

def execution_node(state: AgentState) -> AgentState:
    current_task = state['task']
    memory = state.get('memory', {})
    user_input = state.get('user_input')
    
    if not current_task:
        return {
            "conversation_status": ConversationStatus.FAILED,
            "next_agent": AgentType.NONE,
            "messages": [AIMessage(content="Error: No task found.")]
        }

    # Handle Input
    memory_update = {}
    local_memory = memory.copy()
    
    if current_task.status == TaskStatus.WAITING_FOR_USER and user_input:
        if current_task.required_inputs:
            field_to_update = current_task.required_inputs[0].field
            local_memory[field_to_update] = user_input
            memory_update[field_to_update] = user_input

    # Requirements Logic (same as before)
    description = current_task.description.lower()
    required_fields = []
    if "flight" in description or "book" in description:
        required_fields = [
            {"field": "origin", "question": "Where are you flying from?"},
            {"field": "destination", "question": "Where are you flying to?"},
            {"field": "date", "question": "When do you want to fly?"}
        ]
    elif "meeting" in description:
        required_fields = [
            {"field": "time", "question": "What time is the meeting?"},
            {"field": "attendees", "question": "Who is attending?"}
        ]

    missing_inputs = []
    for req in required_fields:
        if req["field"] not in local_memory:
            missing_inputs.append(RequiredInput(field=req["field"], question=req["question"]))
            
    if missing_inputs:
        current_task.status = TaskStatus.WAITING_FOR_USER
        current_task.required_inputs = missing_inputs
        
        # We need to interrupt here or ensure the graph stops to wait for input.
        # In this design, we just return the state. Main loop handles the "WAITING" status.
        return {
            "task": current_task,
            "memory": local_memory, # Apply updates
            "next_agent": AgentType.EXECUTION, # Stay here
            "conversation_status": ConversationStatus.WAITING_FOR_USER,
            "messages": [AIMessage(content=missing_inputs[0].question)]
        }
    
    # Success
    current_task.status = TaskStatus.DONE
    current_task.required_inputs = []
    current_task.output = {
        "result": "Success",
        "details": f"Booked {description} with parameters: {local_memory}"
    }
    
    return {
        "task": current_task,
        "memory": local_memory,
        "next_agent": AgentType.VALIDATION,
        "conversation_status": ConversationStatus.RUNNING,
        "messages": [AIMessage(content="Execution complete. Validating results.")]
    }

def validation_node(state: AgentState) -> AgentState:
    current_task = state['task']
    if current_task and current_task.output.get("result") == "Success":
        return {
            "conversation_status": ConversationStatus.COMPLETED,
            "next_agent": AgentType.NONE,
            "messages": [AIMessage(content="Task completed successfully.")]
        }
    else:
         return {
            "conversation_status": ConversationStatus.FAILED,
            "next_agent": AgentType.NONE,
             "messages": [AIMessage(content="Validation Failed.")]
        }

# --- Conditional Logic ---

def router(state: AgentState) -> str:
    # If waiting for user, we conceptually "end" the graph run to yield control back to CLI
    if state['conversation_status'] == ConversationStatus.WAITING_FOR_USER:
        return END
    
    if state['next_agent'] == AgentType.EXECUTION:
        return "execution"
    elif state['next_agent'] == AgentType.VALIDATION:
        return "validation"
    elif state['next_agent'] == AgentType.NONE:
        return END
    return END

# --- Graph Construction ---

def create_graph():
    # Helper to check if DB exists, if not create connection
    conn = sqlite3.connect("conversations.db", check_same_thread=False)
    
    workflow = StateGraph(AgentState)
    
    workflow.add_node("planner", planner_node)
    workflow.add_node("execution", execution_node)
    workflow.add_node("validation", validation_node)
    
    workflow.set_entry_point("planner")
    
    workflow.add_conditional_edges(
        "planner",
        router,
        {
            "execution": "execution",
            END: END
        }
    )
    
    workflow.add_conditional_edges(
        "execution",
        router,
        {
            "execution": "execution", # Loop back if we re-enter (though router handles the wait)
            "validation": "validation",
            END: END # Ends graph run if WAITING_FOR_USER
        }
    )
    
    workflow.add_edge("validation", END)
    
    checkpointer = SqliteSaver(conn)
    return workflow.compile(checkpointer=checkpointer)
