import json
import sys
import os
import uuid

# Ensure the parent directory is in sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_orchestrator_poc.graph_orchestrator import create_graph, AgentState, AgentType
from agent_orchestrator_poc.models import ConversationStatus, TaskStatus

def main():
    graph = create_graph()
    
    # Simulate a session ID
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    
    print(f"Multi-Agent Orchestrator (LangGraph) Started. Session ID: {thread_id}")
    print("Type your request (or 'exit' to quit).")
    print("-----------------------------------------------------------------------------")

    # Initial State
    current_state = {
        "messages": [],
        "task": None,
        "memory": {},
        "next_agent": AgentType.NONE,
        "conversation_status": ConversationStatus.RUNNING,
        "user_input": None
    }

    first_turn = True

    while True:
        try:
            user_input = input("USER: ").strip()
            if user_input.lower() in ["exit", "quit"]:
                break
            
            if not user_input:
                continue

            # Update input in state
            current_state["user_input"] = user_input
            
            # If resume (not first turn), we rely on graph state, but we need to inject the new input
            # LangGraph inputs are merged.
            
            inputs = {"user_input": user_input}
            
            # Run the graph
            # If it's the first turn, we enter at planner.
            # If subsequent, we might be resuming.
            # However, for this simple POC, since we "END" on WAITING_FOR_USER, 
            # we need to know where to resume. 
            # The snapshot saved by checkpointer handles this? 
            # Actually, if we ended at Execution (returning WAITING), 
            # the next run needs to start at Execution.
            
            # Check snapshot
            snapshot = graph.get_state(config)
            if snapshot and snapshot.values and snapshot.values.get("conversation_status") == ConversationStatus.WAITING_FOR_USER:
                # We are resuming. The "next" node was implied to be Execution again 
                # but we returned END to stop the loop.
                # To resume at Execution, we can explicitly update state or use the Command pattern in newer LangGraph.
                # For simplicity here: The router logic ended the run.
                # If we invoke again, where does it start? 
                # It starts at entry point unless we update state to next node?
                # Actually, StateGraph entry point is static.
                # To resume at a specific node, we might need to modify the graph validation or use update_state.
                
                # Let's inspect the snapshot 'next_agent'.
                if snapshot.values.get("next_agent") == AgentType.EXECUTION:
                    # We want to run 'execution' node next.
                    # We can use update_state to set the input, and then stream/invoke.
                    # But invoke() usually starts from start?
                    # No, invoke(..., config) resumes if there is a checkpoint? 
                    # Only if there was an interrupt. We didn't use interrupt, we used router->END.
                    # If we used router->END, the run IS finished.
                    # So next invoke starts from START (Planner). That's bad.
                    
                    # Fix: use interrupt_before=["execution"]? Or proper routing?
                    # Better Approach for POC:
                    # Just pass the state to the node directly? No, defeats the purpose.
                    
                    # Correction:
                    # If we want to resume at Execution, we should actually NOT end the run, 
                    # but use `interrupt_before` logic or `input` capability.
                    # But since this is a turn-based CLI, ending the run is natural.
                    # To resume: We can use `graph.update_state(config, ...)` to pretend we are at the step before?
                    # Or simpler: The "Planner" node can have logic to skip itself if task is already there?
                    pass
            
            # -------------------------------------------------------------
            # REFINED STRATEGY for Resuming in this simple graph:
            # 1. Update the state with the new user input.
            # 2. If we have a task in progress, we basically want to "continue" execution.
            #    We can trick the graph by having a "dispatcher" entry node that checks state,
            #    OR we can just let Planner run and seeing "Task exists" -> pass to Execution.
            # -------------------------------------------------------------
            
            # Let's rely on the Planner node handling the "Resume" logic if we start from top,
            # OR we update the graph definition to allow starting from Execution?
            # Actually, `graph.invoke(..., config)` with a checkpoint RESUMES from where it left off 
            # IF it was interrupted. If it finished (END), it starts over.
            
            # So we SHOULD use `interrupt_before=["execution"]`? 
            # If we interrupt, the state is saved and we wait.
            # Next call to invoke(None, config) resumes.
            
            # Let's try the "Planner checks state" approach for simplicity, 
            # modifying graph_orchestrator.py slightly in the next step if needed.
            # But for now, let's assume Planner overwrites task if we call it. AHH.
            
            # Okay, logic change:
            # We will use `graph.update_state` to inject the User Input.
            # And then `graph.stream(None, config)`.
            
            events = graph.stream(inputs, config, stream_mode="values")
            
            final_response = {}
            for event in events:
                # event is the state dict
                if "messages" in event and event["messages"]:
                     last_msg = event["messages"][-1].content
                     # We can print intermediate logic if we want, but user wants JSON at end.
                
                # Track the latest state
                final_response = event
            
            # Construct JSON output from final state
            task_obj = final_response.get("task")
            task_dict = task_obj.dict() if task_obj else None
            
            output = {
                "conversation_status": final_response.get("conversation_status"),
                "current_agent": final_response.get("next_agent", AgentType.NONE),
                "message_for_user": final_response.get("messages")[-1].content if final_response.get("messages") else "",
                "memory_update": final_response.get("memory", {}), # In LG, we return full memory usually
                "task": task_dict
            }
            
            print(json.dumps(output, indent=2))
            
            if final_response.get("conversation_status") in [ConversationStatus.COMPLETED, ConversationStatus.FAILED]:
                print(f"Conversation ended.")
                # Clear thread?
                break

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(json.dumps({"error": str(e)}))
            # import traceback
            # traceback.print_exc()

if __name__ == "__main__":
    main()
