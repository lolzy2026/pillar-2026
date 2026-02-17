# Multi-Agent Orchestrator POC Walkthrough

## Overview
This POC demonstrates a Multi-Agent Orchestrator system where a central orchestrator delegates tasks to specialized agents (Planner, Execution, Validation) and manages state and memory.

## Architecture
The project uses **LangGraph** to define the workflow and **SQLite** to persist conversation state.

- **Nodes**: `planner`, `execution`, `validation`.
- **Persistence**: State is saved after every step to `conversations.db` using `sqlite`.
- **Resumption**: The system checks existing state to resume tasks (e.g., providing missing inputs across multiple turns).

## How to Run
1.  Navigate to the project root.
2.  Run the script:
    ```bash
    python agent_orchestrator_poc/main.py
    ```
3.  Interact with the CLI.

## Verification Log
The following interaction demonstrates the "Book a flight" scenario with persistence:

1.  **User**: "I want to book a flight"
    - **System**: Planner creates task. Execution asks "Where are you flying from?"
2.  **User**: "New York"
    - **System**: Execution updates state (Origin: NY), asks "Where are you flying to?"
3.  **User**: "Paris"
    - **System**: Execution updates state (Dest: Paris), asks "When do you want to fly?"
4.  **User**: "Tomorrow"
    - **System**: Execution updates state (Date: Tomorrow), executes task. Validation confirms success.

### Sample Output
```json
{
  "conversation_status": "COMPLETED",
  "current_agent": "NONE",
  "message_for_user": "Task completed successfully.",
  "memory_update": {
    "origin": "New York",
    "destination": "Paris",
    "date": "Tomorrow"
  },
  "task": {
    "id": "task_001",
    "status": "DONE",
    ...
  }
}
```
