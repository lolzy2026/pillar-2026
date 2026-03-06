# MCP Tool Execution with Elicitation - Mermaid Diagrams

## 1. System Architecture Overview

```mermaid
graph TB
    subgraph Presentation["PRESENTATION LAYER"]
        UI[Chat UI React<br/>- Chat Thread Display<br/>- Tool Mode Selector<br/>- Elicitation Form Modal<br/>- Multi-form Queue Display]
    end

    subgraph Application["APPLICATION LAYER - FastAPI"]
        API[API Endpoints<br/>POST /assist<br/>POST /elicitation/respond<br/>GET /session/status]
        
        subgraph LangGraph["LangGraph Agent Orchestrator"]
            Router[Router Node]
            MCPNode[MCP Tool Node<br/>State Machine:<br/>INITIATING → EXECUTING<br/>→ AWAITING_ELIC<br/>→ RESUMING → COMPLETED]
            LLM[LLM Node]
            Checkpoint[Checkpoint Mechanism<br/>Pause/Resume Graph]
        end
        
        SessionMgr[Elicitation Session Manager<br/>- create_elicitation<br/>- submit_response<br/>- handle_timeout<br/>- get_pending_elicitations]
    end

    subgraph Storage["DATA LAYER"]
        Redis[(Redis Hot Data<br/>- Sessions<br/>- Elicitations<br/>- Active Executions<br/>- Event Buffers<br/>TTL: 5-15 min)]
        Postgres[(PostgreSQL Cold Data<br/>- Conversation History<br/>- Tool Execution Audit<br/>- Elicitation Logs<br/>Permanent Storage)]
    end

    subgraph External["EXTERNAL SERVICES"]
        Gateway[MCP Gateway<br/>- Tool Discovery<br/>- Tool Execution SSE<br/>- Elicitation Response<br/>- Auth & Authorization<br/>- Namespace Filtering]
        MCPServers[MCP Servers<br/>Tool Providers]
    end

    UI -->|POST /assist| API
    UI -->|POST /elicitation/respond| API
    UI -->|GET /session/status| API
    
    API --> LangGraph
    Router --> MCPNode
    MCPNode --> LLM
    MCPNode -.->|Pause/Resume| Checkpoint
    
    LangGraph --> SessionMgr
    SessionMgr --> Redis
    SessionMgr -.->|Async Logging| Postgres
    MCPNode --> Redis
    
    MCPNode -->|SSE Connection| Gateway
    SessionMgr -->|Forward Response| Gateway
    Gateway --> MCPServers
    
    style UI fill:#e1f5ff
    style LangGraph fill:#fff4e1
    style SessionMgr fill:#ffe1f5
    style Redis fill:#ffe1e1
    style Postgres fill:#e1ffe1
    style Gateway fill:#f5e1ff
```

---

## 2. Component Responsibility Diagram

```mermaid
graph LR
    subgraph Components
        A[Chat UI]
        B[FastAPI Endpoints]
        C[LangGraph Agent]
        D[MCP Tool Node]
        E[Elicitation Manager]
        F[Redis]
        G[PostgreSQL]
        H[MCP Gateway]
    end
    
    A -->|User Interaction| B
    B -->|Route Requests| C
    C -->|Execute Nodes| D
    D -->|Create/Check| E
    D -->|Buffer Events| F
    E -->|Store State| F
    E -->|Audit Log| G
    D -->|SSE Stream| H
    E -->|Forward Response| H
    
    style A fill:#e3f2fd
    style B fill:#fff3e0
    style C fill:#f3e5f5
    style D fill:#e8f5e9
    style E fill:#fce4ec
    style F fill:#ffebee
    style G fill:#e0f2f1
    style H fill:#f1f8e9
```

---

## 3. Data Storage Strategy Decision Tree

```mermaid
graph TD
    Start[Request Received] --> Question{Is it hot data needed<br/>for real-time operations?}
    
    Question -->|Yes| Redis[Store in REDIS]
    Question -->|No| Postgres[Store in POSTGRESQL]
    
    Redis --> R1[Session Data<br/>TTL: 5 min]
    Redis --> R2[Elicitation State<br/>TTL: 3 min wait<br/>5 min total]
    Redis --> R3[Active Executions<br/>TTL: 15 min]
    Redis --> R4[Event Buffers<br/>TTL: 15 min]
    
    Postgres --> P1[Conversation History<br/>Permanent]
    Postgres --> P2[Tool Execution Audit<br/>Permanent]
    Postgres --> P3[Elicitation Events<br/>Permanent]
    
    style Redis fill:#ffcdd2
    style Postgres fill:#c8e6c9
    style R1 fill:#ffebee
    style R2 fill:#ffebee
    style R3 fill:#ffebee
    style R4 fill:#ffebee
    style P1 fill:#e8f5e9
    style P2 fill:#e8f5e9
    style P3 fill:#e8f5e9
```

---

## 4. Flow 1: Tool Execution WITHOUT Elicitation

```mermaid
sequenceDiagram
    participant User
    participant UI as Chat UI
    participant API as FastAPI
    participant Agent as LangGraph Agent
    participant MCP as MCP Gateway

    User->>UI: 1. Type message<br/>"Get sales data"
    UI->>API: 2. POST /assist<br/>{message, thread_id, ...}
    API->>Agent: 3. Create initial state<br/>& invoke graph
    
    Agent->>Agent: 4. Router node<br/>determines MCP tool needed
    Agent->>Agent: 5. MCP Tool Node<br/>executes
    
    Agent->>MCP: 6. SSE POST<br/>/tools/execute
    MCP-->>Agent: SSE Stream opened
    
    MCP-->>Agent: data: {type: "progress",<br/>message: "Fetching..."}
    MCP-->>Agent: data: {type: "result",<br/>data: {...}}
    
    Agent->>Agent: 7. Tool node updates state
    Agent->>Agent: 8. Continue to LLM node
    
    Agent-->>API: 9. Return state<br/>{messages, ...}
    API-->>UI: 10. Response<br/>{response: "...",<br/>status: "completed"}
    UI-->>User: 11. Display assistant message
    
    Note over User,MCP: Components: UI, FastAPI, LangGraph, MCP Gateway<br/>Time: ~2-5 seconds
```

---

## 5. Flow 2: Tool Execution WITH Single Form Elicitation

```mermaid
sequenceDiagram
    participant User
    participant UI as Chat UI
    participant API as FastAPI
    participant Agent as LangGraph
    participant Session as Session Manager
    participant Redis
    participant MCP as MCP Gateway

    User->>UI: 1. Send message
    UI->>API: 2. POST /assist
    API->>Agent: 3. Invoke graph
    
    Agent->>Agent: 4. Router → MCP node
    Agent->>MCP: 5. SSE POST /tools/execute
    MCP-->>Agent: 6. Stream: {type: "progress"}
    
    MCP-->>Agent: 7. Stream: {type: "elicitation_required",<br/>schema: {...}}
    
    Agent->>Agent: 8. Detect elicitation
    Agent->>Session: 9. Create elicitation
    Session->>Redis: 10. Store in Redis
    Redis-->>Session: elicitation_id
    Session-->>Agent: elicitation_id
    
    Agent->>Session: 11. Start timeout task (3min)
    Agent->>Agent: 12. Raise ElicitationException
    Agent->>Agent: 13. Graph PAUSES &<br/>creates checkpoint
    
    Agent-->>API: 14. Return state with<br/>pending_elicitations
    API-->>UI: 15. Response {status: "awaiting",<br/>elicitations: [...]}
    UI-->>User: 16. Display form modal
    
    Note over User: 17. User fills form (~45 sec)
    
    User->>UI: 18. Submit form
    UI->>API: 19. POST /elicitation/respond
    API->>Session: 20. Submit response
    
    Session->>Redis: 21. Get elicitation data
    Redis-->>Session: data
    
    Session->>MCP: 22. Forward to MCP Gateway<br/>POST /executions/{id}/elicitation
    MCP-->>Session: 200 OK
    
    Session->>Redis: 23. Update Redis status
    Session-->>API: 24. Return resume context
    API-->>UI: 25. {status: "accepted"}
    UI-->>User: 26. Show "Processing..."
    
    API->>Agent: 27. Background: Resume graph
    Agent->>Agent: 28. Load checkpoint
    Agent->>Agent: 29. MCP node resumes
    
    Agent->>MCP: 30. SSE still open,<br/>continues listening
    MCP-->>Agent: 31. Stream: {type: "progress"}
    MCP-->>Agent: 32. Stream: {type: "result",<br/>data: {...}}
    
    Agent->>Agent: 33. Update state
    Agent->>Agent: 34. Continue to LLM node
    Agent->>Agent: 35. Complete
    
    User->>UI: 36. Poll /session/status
    UI->>API: 37. Check Redis
    API->>Redis: 38. Query pending
    Redis-->>API: No pending
    API-->>UI: 39. {has_pending: false}
    
    User->>UI: 40. Fetch final response
    UI-->>User: 41. Display result
    
    Note over User,MCP: Timeline: ~48-53 seconds total<br/>Initial: 500ms | User fills: 45s | Submit: 200ms | Resume: 2-5s
```

---

## 6. Flow 3: Multiple Sequential Elicitations

```mermaid
graph TD
    Start[User sends message] --> Phase1[Phase 1: Initial Execution]
    
    Phase1 --> Exec1[Agent executes tool]
    Exec1 --> Elic1{MCP Gateway sends<br/>elicitation_required<br/>Form 1}
    Elic1 --> Pause1[Graph pauses,<br/>returns Form 1 to UI]
    Pause1 --> User1[User fills Form 1]
    
    User1 --> Phase2[Phase 2: First Resume]
    Phase2 --> Submit1[User submits Form 1]
    Submit1 --> Forward1[Response forwarded<br/>to MCP Gateway]
    Forward1 --> Resume1[Graph resumes execution]
    Resume1 --> Elic2{MCP Gateway sends<br/>elicitation_required<br/>Form 2}
    Elic2 --> Pause2[Graph pauses AGAIN,<br/>returns Form 2 to UI]
    Pause2 --> User2[User fills Form 2]
    
    User2 --> Phase3[Phase 3: Final Resume]
    Phase3 --> Submit2[User submits Form 2]
    Submit2 --> Forward2[Response forwarded<br/>to MCP Gateway]
    Forward2 --> Resume2[Graph resumes execution]
    Resume2 --> Result{MCP Gateway sends<br/>result}
    Result --> Complete[Graph completes]
    Complete --> End[Final response to user]
    
    style Phase1 fill:#e3f2fd
    style Phase2 fill:#fff3e0
    style Phase3 fill:#f3e5f5
    style Pause1 fill:#ffcdd2
    style Pause2 fill:#ffcdd2
    style Complete fill:#c8e6c9
```

---

## 7. Flow 4: Timeout Scenario

```mermaid
sequenceDiagram
    participant User
    participant UI
    participant Session as Session Manager
    participant Agent as LangGraph
    participant Redis
    participant MCP as MCP Gateway

    Note over User,UI: Elicitation form displayed
    UI-->>User: Form shown
    
    Session->>Session: Timeout task running (3 min)
    
    Note over Session: 180 seconds elapsed
    
    Session->>Redis: Check status
    Redis-->>Session: Still "pending"
    
    Session->>Redis: Update status to "timeout"
    Session->>MCP: Cancel MCP execution (optional)
    Session->>Agent: Notify graph
    Agent->>Agent: Mark execution failed
    
    User->>UI: Poll /session/status
    UI->>Redis: Check status
    Redis-->>UI: {status: "timeout"}
    UI-->>User: Show "Request timed out"
    
    Note over User,MCP: Components: UI, Session Manager, Redis, LangGraph, MCP Gateway<br/>Timing: 0s (form) → 180s (timeout) → 180.2s (cleanup)
```

---

## 8. MCP Tool Node State Machine

```mermaid
stateDiagram-v2
    [*] --> INITIATING: Tool execution requested
    
    INITIATING --> EXECUTING: SSE connection established
    note right of INITIATING
        - Create execution_id
        - Store in Redis
    end note
    
    EXECUTING --> EXECUTING: event: "progress"
    EXECUTING --> AWAITING_ELICITATION: event: "elicitation_required"
    EXECUTING --> COMPLETED: event: "result"
    EXECUTING --> FAILED: event: "error"
    
    note right of EXECUTING
        - Listen to SSE
        - Buffer events
    end note
    
    AWAITING_ELICITATION --> RESUMING: User submits response
    AWAITING_ELICITATION --> TIMEOUT: 3 minutes elapsed
    
    note right of AWAITING_ELICITATION
        - Create elicitation_id
        - Store in Redis
        - Raise Exception
        - Graph PAUSES
    end note
    
    RESUMING --> EXECUTING: Response forwarded to MCP
    note right of RESUMING
        - Load from checkpoint
        - Continue SSE
    end note
    
    COMPLETED --> [*]
    note right of COMPLETED
        - Store result
        - Update state
        - Set Redis TTL
    end note
    
    FAILED --> [*]
    TIMEOUT --> [*]
    CANCELLED --> [*]
    
    EXECUTING --> CANCELLED: User cancels
```

---

## 9. Elicitation Lifecycle State Machine

```mermaid
stateDiagram-v2
    [*] --> CREATED: Elicitation required
    
    note right of CREATED
        - Redis entry created
        - TTL: 5 min
        - Status: "pending"
        - Timeout task scheduled
    end note
    
    CREATED --> COMPLETED: User submits within 3 min
    CREATED --> TIMEOUT: 180 seconds elapsed
    
    note right of COMPLETED
        - Response stored
        - Forwarded to MCP Gateway
        - Status: "completed"
    end note
    
    note right of TIMEOUT
        - No response received
        - Execution cancelled
        - Graph notified
        - Status: "timeout"
    end note
    
    COMPLETED --> EXPIRED: TTL expires (5 min total)
    TIMEOUT --> EXPIRED: TTL expires (5 min total)
    
    note right of EXPIRED
        - Redis key deleted
        - Data moved to PostgreSQL
    end note
    
    EXPIRED --> [*]
```

---

## 10. Redis Data Model

```mermaid
graph TB
    subgraph Redis["Redis Key Structure"]
        E1[elicitation:{uuid}<br/>Type: Hash<br/>TTL: 300s]
        E2[elicitation:callback:{uuid}<br/>Type: Hash<br/>TTL: 300s]
        T1[thread:{thread_id}:elicitations<br/>Type: Set<br/>TTL: 300s]
        X1[execution:{execution_id}<br/>Type: Hash<br/>TTL: 900s]
        T2[thread:{thread_id}:executions<br/>Type: Set<br/>TTL: 900s]
    end
    
    subgraph ElicitationData["Elicitation Fields"]
        F1[thread_id]
        F2[checkpoint_id]
        F3[execution_id]
        F4[tool_name]
        F5[status]
        F6[elicitation_type]
        F7[schema JSON]
        F8[user_response JSON]
        F9[created_at]
        F10[expires_at]
        F11[mcp_gateway_token]
    end
    
    subgraph CallbackData["Callback Fields"]
        C1[thread_id]
        C2[checkpoint_id]
        C3[execution_id]
    end
    
    subgraph ExecutionData["Execution Fields"]
        D1[thread_id]
        D2[tool_name]
        D3[status]
        D4[started_at]
        D5[mcp_sse_events JSON]
        D6[current_elicitation_id]
    end
    
    E1 -.-> ElicitationData
    E2 -.-> CallbackData
    X1 -.-> ExecutionData
    
    style E1 fill:#ffcdd2
    style E2 fill:#ffcdd2
    style T1 fill:#f8bbd0
    style X1 fill:#e1bee7
    style T2 fill:#e1bee7
```

---

## 11. Component Interaction Matrix

```mermaid
graph LR
    UI[Chat UI]
    API[FastAPI Endpoints]
    Agent[LangGraph Agent]
    MCP_Node[MCP Tool Node]
    Session[Elicitation Manager]
    Redis[(Redis)]
    PG[(PostgreSQL)]
    Gateway[MCP Gateway]
    
    UI -->|HTTP POST/GET<br/>User messages, forms| API
    API -->|Function call<br/>State, config| Agent
    API -->|Function call<br/>Elicitation ID, response| Session
    Agent -->|State passing<br/>Graph state| MCP_Node
    MCP_Node -->|SSE<br/>Tool params, events| Gateway
    MCP_Node -->|Function call<br/>Create/check elicitation| Session
    MCP_Node -->|Redis commands<br/>Event buffering| Redis
    Session -->|Redis commands<br/>Elicitation state| Redis
    Session -->|HTTP POST<br/>User response| Gateway
    Session -.->|SQL INSERT<br/>Audit logging async| PG
    
    style UI fill:#e3f2fd
    style API fill:#fff3e0
    style Agent fill:#f3e5f5
    style MCP_Node fill:#e8f5e9
    style Session fill:#fce4ec
    style Redis fill:#ffebee
    style PG fill:#e0f2f1
    style Gateway fill:#f1f8e9
```

---

## 12. Deployment Architecture

```mermaid
graph TB
    subgraph Internet
        Users[Users]
    end
    
    subgraph LoadBalancer["Load Balancer - NGINX/AWS ALB"]
        LB[Load Balancer]
    end
    
    subgraph AppLayer["Application Layer"]
        W1[FastAPI Worker 1<br/>uvicorn --workers 1]
        W2[FastAPI Worker 2<br/>uvicorn --workers 1]
        W3[FastAPI Worker 3<br/>uvicorn --workers 1]
    end
    
    subgraph DataLayer["Data Layer"]
        subgraph RedisCluster["Redis Cluster"]
            RM[Redis Master]
            RR1[Redis Replica 1]
            RR2[Redis Replica 2]
            RS[Redis Sentinel]
        end
        
        subgraph PostgreSQLCluster["PostgreSQL"]
            PGP[Primary]
            PGR[Read Replica]
        end
    end
    
    subgraph ExternalServices["External Services"]
        MCPGW[MCP Gateway]
    end
    
    Users --> LB
    LB --> W1
    LB --> W2
    LB --> W3
    
    W1 --> RM
    W2 --> RM
    W3 --> RM
    
    W1 --> PGP
    W2 --> PGP
    W3 --> PGP
    
    RM --> RR1
    RM --> RR2
    RS -.->|Monitor| RM
    RS -.->|Monitor| RR1
    RS -.->|Monitor| RR2
    
    PGP --> PGR
    
    W1 --> MCPGW
    W2 --> MCPGW
    W3 --> MCPGW
    
    style Users fill:#e3f2fd
    style LB fill:#fff3e0
    style W1 fill:#f3e5f5
    style W2 fill:#f3e5f5
    style W3 fill:#f3e5f5
    style RM fill:#ffcdd2
    style RR1 fill:#ffebee
    style RR2 fill:#ffebee
    style RS fill:#ffebee
    style PGP fill:#c8e6c9
    style PGR fill:#e8f5e9
    style MCPGW fill:#f1f8e9
```

---

## 13. Scaling Strategy

```mermaid
graph TD
    Current[Current Scale<br/>50 Concurrent Users]
    
    Current --> Resources[Resources:<br/>3 FastAPI workers<br/>1 Redis instance<br/>1 PostgreSQL instance]
    
    Resources --> Check{Scaling Needed?}
    
    Check -->|Yes, 500+ users| Scale[Future Scaling]
    Check -->|No| Optimize[Current Optimization]
    
    Scale --> S1[FastAPI: 10-20 workers<br/>Auto-scaling]
    Scale --> S2[Redis: 6+ node cluster]
    Scale --> S3[PostgreSQL: Read replicas]
    Scale --> S4[Add CDN for static content]
    Scale --> S5[WebSocket for result push]
    
    Optimize --> O1[Connection pooling]
    Optimize --> O2[Batch PostgreSQL writes]
    Optimize --> O3[Redis pipeline commands]
    
    style Current fill:#e3f2fd
    style Scale fill:#fff3e0
    style Optimize fill:#f3e5f5
    style S1 fill:#c8e6c9
    style S2 fill:#c8e6c9
    style S3 fill:#c8e6c9
    style S4 fill:#c8e6c9
    style S5 fill:#c8e6c9
```

---

## 14. Monitoring & Observability

```mermaid
graph TB
    subgraph Metrics["Application Metrics"]
        M1[elicitation_created_total<br/>Counter, by type]
        M2[elicitation_response_time_seconds<br/>Histogram]
        M3[elicitation_timeout_total<br/>Counter]
        M4[elicitation_completion_rate<br/>Gauge]
        M5[tool_execution_duration_seconds<br/>Histogram]
        M6[graph_pause_resume_total<br/>Counter]
        M7[active_elicitations<br/>Gauge]
    end
    
    subgraph Infrastructure["Infrastructure Metrics"]
        I1[Redis: connection_pool_usage]
        I2[Redis: memory_usage]
        I3[PostgreSQL: connection_count]
        I4[PostgreSQL: query_duration]
        I5[FastAPI: request_latency]
        I6[FastAPI: error_rate]
        I7[MCP Gateway: sse_connection_duration]
    end
    
    subgraph Dashboards["Monitoring Dashboards"]
        D1[Real-time Elicitation Queue]
        D2[Response Time by Type]
        D3[Timeout Rate Trends]
        D4[Success/Failure Rates]
        D5[User Journey Funnel]
    end
    
    subgraph Alerts["Alert Rules"]
        A1[Timeout rate > 10%]
        A2[Redis pool exhausted]
        A3[Checkpoint save failures]
        A4[MCP Gateway unavailable]
    end
    
    Metrics --> Dashboards
    Infrastructure --> Dashboards
    Dashboards --> Alerts
    
    style Metrics fill:#e3f2fd
    style Infrastructure fill:#fff3e0
    style Dashboards fill:#f3e5f5
    style Alerts fill:#ffcdd2
```

---

## 15. Error Handling Flow

```mermaid
flowchart TD
    Start[Error Occurred] --> Type{Error Type?}
    
    Type -->|User Timeout| T1[Mark elicitation as timeout]
    T1 --> T2[Cancel MCP execution]
    T2 --> T3[Notify user: Request timed out]
    
    Type -->|Backend Crash| B1[Check Redis for state]
    B1 --> B2{State exists?}
    B2 -->|Yes| B3[Restore from checkpoint]
    B2 -->|No| B4[Notify user: Please retry]
    
    Type -->|MCP Gateway Down| M1[Return 503 to user]
    M1 --> M2[Queue for retry]
    M2 --> M3[Alert ops team]
    
    Type -->|Redis Failure| R1[Activate Redis failover]
    R1 --> R2{Failover successful?}
    R2 -->|Yes| R3[Continue operations]
    R2 -->|No| R4[Graceful degradation mode]
    
    Type -->|Network Drop| N1[Buffer events in Redis]
    N1 --> N2[Allow reconnection]
    N2 --> N3[Resume from last event ID]
    
    T3 --> End[Log & Report]
    B3 --> End
    B4 --> End
    M3 --> End
    R3 --> End
    R4 --> End
    N3 --> End
    
    style T1 fill:#ffcdd2
    style B1 fill:#fff3e0
    style M1 fill:#f3e5f5
    style R1 fill:#e1bee7
    style N1 fill:#c8e6c9
```

---

## 16. Query Patterns on Redis

```mermaid
graph LR
    subgraph Queries["Common Query Patterns"]
        Q1[Get Pending Elicitations<br/>for Thread]
        Q2[Resume from Elicitation<br/>Response]
        Q3[Check Execution<br/>Status]
        Q4[Get Buffered SSE<br/>Events]
    end
    
    Q1 --> Q1S1[SMEMBERS thread:ID:elicitations]
    Q1S1 --> Q1S2[For each: HGETALL elicitation:ID]
    Q1S2 --> Q1S3[Filter where status == pending]
    
    Q2 --> Q2S1[HGETALL elicitation:callback:ID]
    Q2S1 --> Q2S2[Extract thread_id, checkpoint_id]
    
    Q3 --> Q3S1[HGET execution:ID status]
    Q3S1 --> Q3S2[Returns current state]
    
    Q4 --> Q4S1[HGET execution:ID mcp_sse_events]
    Q4S1 --> Q4S2[Parse JSON array]
    
    style Q1 fill:#e3f2fd
    style Q2 fill:#fff3e0
    style Q3 fill:#f3e5f5
    style Q4 fill:#e8f5e9
```

---

## 17. Critical Path Timeline

```mermaid
gantt
    title Tool Execution with Elicitation - Timeline
    dateFormat X
    axisFormat %S
    
    section Initial Request
    User sends message           :0, 1
    API processing               :1, 2
    LangGraph starts             :2, 3
    MCP connection               :3, 4
    
    section Tool Execution
    Tool executing               :4, 10
    Progress events              :4, 10
    Elicitation required event   :10, 11
    
    section Pause & Display
    Create elicitation           :11, 12
    Graph pauses                 :12, 13
    Return to UI                 :13, 14
    Display form                 :14, 15
    
    section User Action
    User fills form              :15, 60
    
    section Resume
    Submit response              :60, 61
    Forward to MCP Gateway       :61, 62
    Background resume            :62, 63
    
    section Completion
    Tool continues               :63, 68
    Final result                 :68, 69
    Display to user              :69, 70
```

---

## 18. Decision Matrix

```mermaid
graph TD
    Start{Design Decision<br/>Required}
    
    Start --> D1{Where to handle<br/>elicitation?}
    D1 -->|Same node| D1A[✓ Atomic operation<br/>✓ Maintains context<br/>✓ Cleaner graph]
    D1 -->|Separate node| D1B[✗ Complex state passing<br/>✗ Edge explosion<br/>✗ Lost context]
    D1A -->|CHOSEN| Next1
    
    Next1 --> D2{What API endpoint<br/>for response?}
    D2 -->|Dedicated endpoint| D2A[✓ Clear separation<br/>✓ Better errors<br/>✓ Easier audit]
    D2 -->|Reuse /assist| D2B[✗ Complex routing<br/>✗ Unclear semantics]
    D2A -->|CHOSEN| Next2
    
    Next2 --> D3{Session storage?}
    D3 -->|Redis hot + PG cold| D3A[✓ Performance<br/>✓ Persistence<br/>✓ Balanced]
    D3 -->|Only Redis| D3B[✗ No audit trail<br/>✗ Data loss risk]
    D3 -->|Only PostgreSQL| D3C[✗ Slow lookups<br/>✗ Latency issues]
    D3A -->|CHOSEN| Next3
    
    Next3 --> D4{Timeout duration?}
    D4 -->|3 minutes| D4A[✓ UX balance<br/>✓ Resource efficient]
    D4 -->|1 minute| D4B[✗ Too short]
    D4 -->|10 minutes| D4C[✗ Resource waste]
    D4A -->|CHOSEN| End
    
    style D1A fill:#c8e6c9
    style D2A fill:#c8e6c9
    style D3A fill:#c8e6c9
    style D4A fill:#c8e6c9
    style D1B fill:#ffcdd2
    style D2B fill:#ffcdd2
    style D3B fill:#ffcdd2
    style D3C fill:#ffcdd2
    style D4B fill:#ffcdd2
    style D4C fill:#ffcdd2
```

---

## How to Use These Diagrams

1. **Copy any diagram** - Each is standalone Mermaid syntax
2. **Paste into**:
   - GitHub/GitLab Markdown (renders automatically)
   - Mermaid Live Editor (https://mermaid.live)
   - Confluence (with Mermaid plugin)
   - Notion (with Mermaid support)
   - VS Code (with Mermaid extension)

3. **Customize easily** - All text is editable
4. **Export** - From Mermaid Live Editor to PNG/SVG/PDF

---

## Diagram Quick Reference

| Diagram | Purpose | Key Audience |
|---------|---------|--------------|
| 1. System Architecture | Overall system view | Lead, Architect |
| 2. Component Responsibility | Who does what | Team, New developers |
| 3. Data Storage Strategy | Redis vs PostgreSQL decisions | Architect, DBA |
| 4-7. Flows | Step-by-step execution paths | All stakeholders |
| 8-9. State Machines | Internal state transitions | Developers |
| 10. Redis Data Model | Key structure & queries | Backend developers |
| 11. Interaction Matrix | Component communication | Integration team |
| 12. Deployment | Production setup | DevOps, SRE |
| 13. Scaling | Growth strategy | Architect, Leadership |
| 14. Monitoring | Observability setup | DevOps, SRE |
| 15. Error Handling | Failure scenarios | Developers, QA |
| 16. Query Patterns | Redis access patterns | Backend developers |
| 17. Timeline | Performance expectations | Product, QA |
| 18. Decision Matrix | Design rationale | Lead, Architect |
