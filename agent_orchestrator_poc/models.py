from enum import Enum
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

class ConversationStatus(str, Enum):
    RUNNING = "RUNNING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class AgentType(str, Enum):
    PLANNER = "PlannerAgent"
    EXECUTION = "ExecutionAgent"
    VALIDATION = "ValidationAgent"
    NONE = "NONE"

class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    DONE = "DONE"

class RequiredInput(BaseModel):
    field: str
    question: str

class Task(BaseModel):
    id: str
    status: TaskStatus = TaskStatus.PENDING
    required_inputs: List[RequiredInput] = Field(default_factory=list)
    output: Dict[str, Any] = Field(default_factory=dict)
    description: str = "" # Added to help agents know what the task is about

class AgentResponse(BaseModel):
    conversation_status: ConversationStatus
    current_agent: AgentType
    message_for_user: str
    memory_update: Dict[str, Any]
    task: Optional[Task] = None
