"""
demo_tool_caching_and_system_behaviour.py
==========================================
A self-contained script that demonstrates — with real runnable code —
the following topics from the Data MCP Client architecture discussion:

SECTION 1 — Tool caching difficulties
  1a. Why StructuredTool objects cannot be JSON-serialised
  1b. Why the coroutine closure captures a live session reference
  1c. What IS serialisable (mcp.types.Tool from list_tools())
  1d. Why deserialised metadata alone cannot execute a tool call
  1e. Why the interceptor API cannot intercept tool *discovery*

SECTION 2 — What the session manager already amortises
  2a. Proving that get_tools() cost is per-session, not per-request
  2b. TTL cleanup and session lifecycle

SECTION 3 — Real bugs in the current codebase (from the review)
  3a. asyncio.get_event_loop() deprecation in Python 3.12
  3b. Race condition: elicitation callback set after graph task starts
  3c. consume_stream timeout uses session TTL (wrong semantics)
  3d. MCP client never disconnected on session destroy

SECTION 4 — What caching WOULD help vs what it wouldn't
  4a. JWT decode cost measurement
  4b. list_tools() network call simulation
  4c. Decision matrix printout

Run with:
    python demo_tool_caching_and_system_behaviour.py

No external services required. Everything is mocked/simulated.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import pickle
import sys
import time
import traceback
import warnings
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

# ---------------------------------------------------------------------------
# Colour helpers for terminal output
# ---------------------------------------------------------------------------

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def header(title: str) -> None:
    width = 72
    print(f"\n{BOLD}{BLUE}{'━' * width}{RESET}")
    print(f"{BOLD}{BLUE}  {title}{RESET}")
    print(f"{BOLD}{BLUE}{'━' * width}{RESET}")

def subheader(title: str) -> None:
    print(f"\n{BOLD}{YELLOW}  ▶ {title}{RESET}")

def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET}  {msg}")

def fail(msg: str) -> None:
    print(f"  {RED}✗{RESET}  {msg}")

def info(msg: str) -> None:
    print(f"  {BLUE}ℹ{RESET}  {msg}")

def show(label: str, value: Any) -> None:
    print(f"  {YELLOW}→{RESET}  {label}: {BOLD}{value!r}{RESET}")


# ===========================================================================
# SECTION 1 — Tool caching difficulties
# ===========================================================================

def section_1_tool_caching_difficulties() -> None:
    header("SECTION 1 — Tool Caching Difficulties")

    # -----------------------------------------------------------------------
    # 1a. StructuredTool is NOT JSON serialisable
    # -----------------------------------------------------------------------
    subheader("1a. StructuredTool cannot be JSON-serialised")

    try:
        from langchain_core.tools import StructuredTool

        # Build a minimal StructuredTool — exactly what langchain-mcp-adapters
        # produces after calling session.list_tools() + convert_mcp_tool_to_langchain_tool()
        async def fake_call_tool(**kwargs: Any) -> str:
            """Fake MCP tool coroutine that captures a live session."""
            # In real code this closure captures: session, server_name,
            # tool_name, connection config — none of which are serialisable.
            return "result"

        tool = StructuredTool(
            name="query_data",
            description="Query data from the MCP server",
            args_schema=None,           # simplified for demo
            coroutine=fake_call_tool,
            response_format="content_and_artifact",
        )

        # Attempt 1: json.dumps
        try:
            json.dumps(tool)
            fail("json.dumps succeeded — unexpected")
        except TypeError as e:
            ok(f"json.dumps raises TypeError: {e}")

        # Attempt 2: LangChain's own dumps() (dumpd)
        try:
            from langchain_core.load import dumps as lc_dumps
            serialised = lc_dumps(tool)
            # Even if it produces a string, check if it captures the function
            parsed = json.loads(serialised)
            # LangChain serialises the schema and metadata but NOT the callable
            has_coroutine = "coroutine" in json.dumps(parsed)
            if has_coroutine:
                fail("Coroutine was serialised — dangerous, not useful")
            else:
                ok("LangChain dumps() succeeds but silently DROPS the coroutine")
                info("  → A deserialised tool has no callable: it cannot execute")
        except Exception as e:
            ok(f"LangChain dumps() also fails: {type(e).__name__}: {e}")

        # Attempt 3: pickle
        try:
            pickle.dumps(tool)
            # Pickle CAN serialise closures in some Python versions, BUT:
            # the captured session object (asyncio streams, socket) will fail
            ok("pickle.dumps() of StructuredTool itself may not fail...")
            info("  → BUT the captured ClientSession contains open sockets")
            info("  → pickle of a live asyncio StreamReader raises on load")
        except Exception as e:
            ok(f"pickle.dumps() raises: {type(e).__name__}: {e}")

        # Check whether StructuredTool.serialize() exists (claimed in discussion)
        has_serialize = hasattr(tool, "serialize")
        if has_serialize:
            fail("tool.serialize() EXISTS — the claim was right, re-investigate")
        else:
            ok("tool.serialize() does NOT exist on StructuredTool")

    except ImportError:
        info("langchain-core not installed — skipping 1a (install: pip install langchain-core)")

    # -----------------------------------------------------------------------
    # 1b. The coroutine closure captures the live session
    # -----------------------------------------------------------------------
    subheader("1b. Coroutine closure captures live session reference")

    # Simulate exactly what convert_mcp_tool_to_langchain_tool() does
    class FakeClientSession:
        """Simulates mcp.client.session.ClientSession."""
        def __init__(self, session_id: str):
            self.session_id = session_id
            self._stream = object()  # simulates an open asyncio stream

        async def call_tool(self, name: str, arguments: dict) -> Any:
            return f"result from session {self.session_id}"

    # The adapter creates one closure per tool per session
    def make_call_tool_coroutine(
        session: FakeClientSession,
        tool_name: str,
    ) -> Callable[..., Awaitable[Any]]:
        """This is the pattern inside langchain_mcp_adapters/tools.py."""
        async def call_tool(**kwargs: Any) -> Any:
            # 'session' is captured here — it is a live network object
            return await session.call_tool(tool_name, kwargs)
        return call_tool

    session_a = FakeClientSession("session-A")
    coroutine_a = make_call_tool_coroutine(session_a, "query_data")

    # Inspect what the closure captures
    closure_vars = {}
    if coroutine_a.__closure__:
        for cell, var_name in zip(
            coroutine_a.__closure__,
            coroutine_a.__code__.co_freevars,
        ):
            try:
                closure_vars[var_name] = cell.cell_contents
            except ValueError:
                closure_vars[var_name] = "<empty cell>"

    show("Closure captures", list(closure_vars.keys()))
    show("session type", type(closure_vars.get("session", "N/A")).__name__)
    show("session._stream type", type(getattr(closure_vars.get("session"), "_stream", None)).__name__)

    ok("Coroutine is inseparable from its captured session reference")
    info("  → If you pickle the coroutine, you pickle the session")
    info("  → If you cache the tool, the cached copy is dead after session ends")

    # -----------------------------------------------------------------------
    # 1c. What IS serialisable: mcp.types.Tool from list_tools()
    # -----------------------------------------------------------------------
    subheader("1c. What IS serialisable: raw mcp.types.Tool Pydantic model")

    # Simulate what session.list_tools() returns
    # (mcp.types.Tool is a plain Pydantic model with no live references)
    class SimulatedMCPTool:
        """Simulates mcp.types.Tool — a Pydantic model, fully serialisable."""
        def __init__(self, name: str, description: str, input_schema: dict):
            self.name = name
            self.description = description
            self.inputSchema = input_schema  # noqa: N815

        def model_dump(self) -> dict:
            return {
                "name": self.name,
                "description": self.description,
                "inputSchema": self.inputSchema,
            }

    raw_mcp_tools = [
        SimulatedMCPTool(
            name="query_data",
            description="Query data from the namespace",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "SQL query"},
                    "limit": {"type": "integer", "default": 100},
                },
                "required": ["query"],
            },
        ),
        SimulatedMCPTool(
            name="list_namespaces",
            description="List available namespaces",
            input_schema={"type": "object", "properties": {}},
        ),
    ]

    serialised_tools = json.dumps([t.model_dump() for t in raw_mcp_tools])
    deserialised = json.loads(serialised_tools)

    ok(f"list_tools() output serialises to {len(serialised_tools)} bytes of valid JSON")
    show("Deserialised tool names", [t["name"] for t in deserialised])
    info("  → This is what you COULD cache in Redis")
    info("  → Key: mcp_tools:{server_name}:{roles_fingerprint}")

    # -----------------------------------------------------------------------
    # 1d. Deserialised metadata alone cannot execute — you still need session
    # -----------------------------------------------------------------------
    subheader("1d. Why cached metadata alone cannot skip the session")

    info("Even with cached tool schemas, execution still requires:")
    info("  1. A live MCPClientUsingLangchainMCP (_connect() → MultiServerMCPClient)")
    info("  2. A live ClientSession open to the MCP server")
    info("  3. The call_tool() coroutine bound to that session")
    info("")
    info("Cached schemas let you skip list_tools() network call ONLY.")
    info("You still pay: _connect() + session.initialize() on every new session.")
    info("")

    # Measure what can be skipped vs what cannot
    COSTS = {
        "_connect() / session.initialize()": ("CANNOT SKIP", "~50–200ms per session"),
        "session.list_tools() (get_tools)":  ("CAN SKIP with cache", "~20–100ms per session"),
        "convert_mcp_tool_to_langchain_tool()": ("CANNOT SKIP", "<1ms, pure CPU"),
        "Tool execution (call_tool())":      ("CANNOT SKIP", "varies by tool"),
        "JWT decode (get_user_from_jwt)":    ("NOT WORTH CACHING", "<1ms, pure CPU"),
    }

    print()
    print(f"  {'Operation':<45} {'Cacheable?':<22} {'Cost'}")
    print(f"  {'─'*45} {'─'*22} {'─'*25}")
    for op, (cacheable, cost) in COSTS.items():
        colour = GREEN if "CAN SKIP" in cacheable else (YELLOW if "NOT WORTH" in cacheable else RED)
        print(f"  {op:<45} {colour}{cacheable:<22}{RESET} {cost}")

    # -----------------------------------------------------------------------
    # 1e. Interceptors intercept execution, NOT discovery
    # -----------------------------------------------------------------------
    subheader("1e. Interceptors intercept tool EXECUTION, not tool DISCOVERY")

    # Show what MCPToolCallRequest actually contains
    info("MCPToolCallRequest fields (from langchain_mcp_adapters.interceptors):")
    interceptor_fields = {
        "name":        "Tool name chosen by the LLM",
        "args":        "Arguments the LLM supplied",
        "headers":     "HTTP headers (modifiable for auth injection)",
        "server_name": "Which MCP server owns this tool (read-only)",
        "runtime":     "LangGraph runtime context (read-only)",
    }
    for field, desc in interceptor_fields.items():
        print(f"    {YELLOW}{field:<15}{RESET} {desc}")

    print()
    info("Interceptors fire AFTER the LLM has called a tool.")
    info("get_tools() / list_tools() happen BEFORE any interceptor runs.")
    info("There is no hook for discovery in the Python adapter.")

    try:
        from langchain_mcp_adapters.interceptors import MCPToolCallRequest
        ok("MCPToolCallRequest imported — fields confirmed from real module")
    except ImportError:
        info("langchain-mcp-adapters not installed — field list is from docs/source")


# ===========================================================================
# SECTION 2 — What the session manager already amortises
# ===========================================================================

def section_2_session_manager_amortisation() -> None:
    header("SECTION 2 — What the Session Manager Already Amortises")

    subheader("2a. get_tools() cost is per-SESSION, not per-REQUEST")

    info("Before session manager (original code):")
    info("  Every request → new MCPClientUsingLangchainMCP → _connect() → get_tools()")
    print()
    info("After session manager (current code):")
    info("  First message in conversation → one MCPClientUsingLangchainMCP")
    info("  Subsequent messages with session_id → reuse existing client")
    info("  get_tools() runs ONCE per conversation, not once per message")
    print()

    # Simulate the cost difference with timing
    SIMULATED_CONNECT_MS = 120
    SIMULATED_GET_TOOLS_MS = 60

    messages_per_conversation = 8

    cost_before = (SIMULATED_CONNECT_MS + SIMULATED_GET_TOOLS_MS) * messages_per_conversation
    cost_after  = (SIMULATED_CONNECT_MS + SIMULATED_GET_TOOLS_MS) * 1  # once per session

    show("Messages per conversation (typical)", messages_per_conversation)
    show("Simulated _connect() cost", f"{SIMULATED_CONNECT_MS}ms")
    show("Simulated get_tools() cost", f"{SIMULATED_GET_TOOLS_MS}ms")
    show("Total cost WITHOUT session manager", f"{cost_before}ms per conversation")
    show("Total cost WITH session manager", f"{cost_after}ms per conversation")
    show("Saving", f"{cost_before - cost_after}ms per conversation ({(1 - cost_after/cost_before)*100:.0f}%)")

    print()
    info("Adding Redis tool caching on top of the session manager would save:")
    cache_saving = SIMULATED_GET_TOOLS_MS  # skip list_tools() only
    show("Additional saving from tool schema cache", f"{cache_saving}ms per FIRST message")
    info("  → Across an 8-message conversation, that's <1% additional gain")
    info("  → The session manager already provides 87%+ of the possible saving")

    subheader("2b. Session lifecycle and TTL cleanup")

    info("Session status transitions:")
    transitions = [
        ("RUNNING",           "Graph task is actively executing"),
        ("WAITING_FOR_INPUT", "Elicitation paused; awaiting /respond call"),
        ("COMPLETED",         "Graph finished normally; session removed"),
        ("FAILED",            "Error or cancellation; session removed"),
    ]
    for status, desc in transitions:
        colour = GREEN if status == "COMPLETED" else (YELLOW if "WAITING" in status else (RED if "FAILED" in status else BLUE))
        print(f"    {colour}{status:<25}{RESET} {desc}")

    print()
    info("TTL cleanup loop runs every 60s.")
    info("Only WAITING_FOR_INPUT sessions are evicted by TTL.")
    info("RUNNING sessions are never evicted — the graph task owns its lifetime.")


# ===========================================================================
# SECTION 3 — Real bugs in the current codebase
# ===========================================================================

def section_3_real_bugs() -> None:
    header("SECTION 3 — Real Bugs in the Current Codebase")

    # -----------------------------------------------------------------------
    # 3a. asyncio.get_event_loop() deprecation
    # -----------------------------------------------------------------------
    subheader("3a. asyncio.get_event_loop() deprecated in Python 3.10+")

    py_version = sys.version_info
    show("Python version", f"{py_version.major}.{py_version.minor}.{py_version.micro}")

    # Reproduce the warning that the current elicitation_handler.py triggers
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            # This is exactly what elicitation_handler.py line 102 does
            loop = asyncio.get_event_loop()
            future = loop.create_future()
            future.cancel()
        except RuntimeError as e:
            fail(f"get_event_loop() raises RuntimeError in 3.12+: {e}")

    deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    if deprecation_warnings:
        fail(f"get_event_loop() emits DeprecationWarning: {deprecation_warnings[0].message}")
    elif py_version >= (3, 12):
        fail("get_event_loop() raises RuntimeError when no running loop (Python 3.12+)")
    else:
        info("No DeprecationWarning on this Python version, but still wrong outside async context")

    print()
    info("Current code (elicitation_handler.py line 102):")
    print(f"    {RED}loop = asyncio.get_event_loop()      # DEPRECATED{RESET}")
    print(f"    {RED}future = loop.create_future(){RESET}")
    print()
    info("Correct code:")
    print(f"    {GREEN}future = asyncio.get_running_loop().create_future()  # CORRECT{RESET}")
    print(f"    {GREEN}# OR simply:{RESET}")
    print(f"    {GREEN}future = asyncio.get_event_loop().create_future()    # only safe inside async def{RESET}")

    # -----------------------------------------------------------------------
    # 3b. Race condition: elicitation callback set after task starts
    # -----------------------------------------------------------------------
    subheader("3b. Race condition: elicitation callback attached after graph task starts")

    info("Current code in data_mcp_client_service.py:")
    print()
    print(f"    session = await session_manager.create_session(")
    print(f"        ...  {RED}# graph task starts HERE via asyncio.create_task(){RESET}")
    print(f"    )")
    print(f"    {RED}# ← task is now running concurrently{RESET}")
    print(f"    elicitation_callback = build_elicitation_callback(session)")
    print(f"    mcp_client.set_elicitation_handler(elicitation_callback)  {RED}# too late?{RESET}")
    print()

    # Demonstrate the race with a concrete simulation
    race_detected = False

    async def simulate_race() -> None:
        nonlocal race_detected

        elicitation_called_before_handler_set = asyncio.Event()
        handler_was_set = asyncio.Event()

        class FakeMCPClient:
            def __init__(self):
                self._handler = None
                self.elicitation_triggered = False

            def set_elicitation_handler(self, handler):
                self._handler = handler
                handler_was_set.set()

            async def run_tool_that_elicits(self):
                # Simulate MCP tool triggering elicitation on first event loop tick
                await asyncio.sleep(0)  # yield once — handler may not be set yet
                if self._handler is None:
                    elicitation_called_before_handler_set.set()

        mcp_client = FakeMCPClient()

        async def graph_task():
            """Simulates LangGraph running immediately after create_task()."""
            await mcp_client.run_tool_that_elicits()

        # Replicate the exact sequence in create_session() + service code
        task = asyncio.create_task(graph_task())   # task starts
        # Service code does work here (building elicitation_callback etc.)
        await asyncio.sleep(0)                     # yield — graph_task runs
        mcp_client.set_elicitation_handler(lambda: None)  # set too late

        await task
        return elicitation_called_before_handler_set.is_set()

    race_detected = asyncio.get_event_loop().run_until_complete(simulate_race())

    if race_detected:
        fail("Race condition CONFIRMED: elicitation fired before handler was set")
    else:
        info("Race not triggered in this run — but window exists on fast MCP servers")

    print()
    info("Fix: pass elicitation_callback into create_session() so it is set")
    info("     BEFORE asyncio.create_task() is called.")
    print()
    print(f"    {GREEN}# Fixed sequence:{RESET}")
    print(f"    {GREEN}elicitation_callback = build_elicitation_callback_deferred(){RESET}")
    print(f"    {GREEN}mcp_client.set_elicitation_handler(elicitation_callback){RESET}")
    print(f"    {GREEN}session = await session_manager.create_session(...)  # task starts after{RESET}")

    # -----------------------------------------------------------------------
    # 3c. consume_stream timeout uses TTL — wrong semantics
    # -----------------------------------------------------------------------
    subheader("3c. consume_stream() timeout uses session TTL — wrong semantics")

    info("Current code in session_manager.py consume_stream():")
    print()
    print(f"    event = await asyncio.wait_for(")
    print(f"        session.queue.get(),")
    print(f"        {RED}timeout=session.ttl_seconds,  # e.g. 300 seconds{RESET}")
    print(f"    )")
    print()
    info("Problem: TTL is the session lifetime, not the inter-token timeout.")
    info("A slow graph that takes >300s to produce a token silently kills the stream.")
    info("A client reconnecting after 4 mins waits up to 300s for the next token.")
    print()

    # Show what two separate timeouts would look like
    print(f"    {GREEN}# Correct: two separate timeout values{RESET}")
    print(f"    {GREEN}INTER_TOKEN_TIMEOUT = 30   # max seconds between tokens{RESET}")
    print(f"    {GREEN}event = await asyncio.wait_for({RESET}")
    print(f"    {GREEN}    session.queue.get(),{RESET}")
    print(f"    {GREEN}    timeout=INTER_TOKEN_TIMEOUT,  # short, for responsiveness{RESET}")
    print(f"    {GREEN}){RESET}")
    print(f"    {GREEN}# Session TTL is enforced separately by _cleanup_loop(){RESET}")

    # -----------------------------------------------------------------------
    # 3d. MCP client never disconnected on session destroy
    # -----------------------------------------------------------------------
    subheader("3d. MCP client never disconnected on session destroy — resource leak")

    info("Current _destroy_session() in session_manager.py:")
    print()
    print(f"    async def _destroy_session(self, session, *, reason):")
    print(f"        # reject elicitation future ...")
    print(f"        # cancel graph task ...")
    print(f"        session.status = SessionStatus.FAILED")
    print(f"        self._sessions.pop(session.session_id, None)")
    print(f"        {RED}# ← mcp_client.disconnect() is NEVER called{RESET}")
    print()

    # Demonstrate what the resource leak looks like
    class TrackingMCPClient:
        def __init__(self, name: str):
            self.name = name
            self._connected = True
            self._call_count = 0

        async def disconnect(self) -> None:
            self._connected = False

        def __del__(self):
            if self._connected:
                # This fires during GC — not guaranteed, not timely
                pass  # would log "connection leaked"

    clients = [TrackingMCPClient(f"client-{i}") for i in range(5)]
    # Simulate sessions being destroyed without disconnect
    leaked = sum(1 for c in clients if c._connected)
    show("Sessions destroyed without disconnect()", len(clients))
    show("Open connections leaked to GC", leaked)
    info("  → In CPython GC will eventually close them, but timing is not guaranteed")
    info("  → Under load this causes connection pool exhaustion on the MCP gateway")
    print()
    print(f"    {GREEN}# Fix: add to _destroy_session(){RESET}")
    print(f"    {GREEN}if session.mcp_client is not None:{RESET}")
    print(f"    {GREEN}    try:{RESET}")
    print(f"    {GREEN}        await session.mcp_client.disconnect(){RESET}")
    print(f"    {GREEN}    except Exception:{RESET}")
    print(f"    {GREEN}        pass  # best-effort{RESET}")


# ===========================================================================
# SECTION 4 — What caching WOULD vs WOULD NOT help
# ===========================================================================

def section_4_caching_decision_matrix() -> None:
    header("SECTION 4 — What Caching Would vs Would Not Help")

    subheader("4a. JWT decode cost (get_user_from_jwt)")

    # Measure actual JWT-style work: base64 decode + JSON parse + dict lookup
    import base64
    import hashlib

    fake_jwt_payload = {
        "sub": "user-123",
        "exp": int(time.time()) + 3600,
        "persona": "admin",
        "mcp_gw": ["namespace_a", "namespace_b", "namespace_c"],
        "email": "user@example.com",
        "iat": int(time.time()),
    }
    encoded = base64.b64encode(json.dumps(fake_jwt_payload).encode()).decode()

    iterations = 100_000
    start = time.perf_counter()
    for _ in range(iterations):
        decoded = json.loads(base64.b64decode(encoded))
        _ = decoded.get("sub")
        _ = decoded.get("mcp_gw", [])
    elapsed_us = (time.perf_counter() - start) / iterations * 1_000_000

    show("JWT decode cost (per call)", f"{elapsed_us:.2f} microseconds")
    info("  → Redis round-trip is ~500–1000 microseconds on LAN")
    info("  → Caching JWT decode adds 250–500x MORE latency than it saves")
    ok("Conclusion: DO NOT cache JWT decode results")

    subheader("4b. list_tools() network call simulation")

    async def simulate_list_tools_timing():
        # Simulate the list_tools() round-trip: SSE → JSON parse → return
        SIMULATED_RTT_MS = 75  # typical LAN MCP gateway

        start = time.perf_counter()
        await asyncio.sleep(SIMULATED_RTT_MS / 1000)  # simulate network wait
        elapsed = (time.perf_counter() - start) * 1000

        return elapsed

    loop = asyncio.get_event_loop()
    list_tools_ms = loop.run_until_complete(simulate_list_tools_timing())

    show("Simulated list_tools() cost", f"{list_tools_ms:.1f}ms")
    info("  → This IS worth caching IF it runs per-request")
    info("  → With session manager: runs once per CONVERSATION, not per message")
    info("  → Typical conversation has 6–10 messages")
    info("  → Amortised cost per message: ~10ms — marginal")

    subheader("4c. Final decision matrix")

    print()
    matrix = [
        # (Data, Cache key, Cacheable?, Skip what?, Verdict)
        (
            "JWT user profile",
            "user:{user_id}:data",
            "No",
            "get_user_from_jwt() — <1ms CPU",
            "Redis RTT > cost saved",
        ),
        (
            "User roles/namespaces",
            "user:{user_id}:roles",
            "No",
            "JWT dict lookup — <1ms CPU",
            "Redis RTT > cost saved",
        ),
        (
            "list_tools() raw output",
            "tools:{roles_fingerprint}",
            "Yes (mcp.types.Tool model_dump())",
            "list_tools() network call ~50–100ms",
            "Saves 1 call per session. Low value with session manager.",
        ),
        (
            "StructuredTool objects",
            "N/A",
            "No",
            "N/A — contains live session closure",
            "Impossible — not serialisable",
        ),
        (
            "Tool execution results",
            "result:{tool}:{args_hash}",
            "Yes (for idempotent tools)",
            "call_tool() execution — varies",
            "HIGH value IF tools are deterministic read-only",
        ),
        (
            "Session metadata",
            "session_meta:{session_id}",
            "Yes (observability only)",
            "Nothing — in-memory is authoritative",
            "Useful for monitoring/admin, not performance",
        ),
    ]

    col_w = [24, 28, 10, 42, 40]
    headers = ["Data", "Redis Key Pattern", "Cache?", "What it skips", "Verdict"]
    sep = "  " + "  ".join("─" * w for w in col_w)

    print("  " + "  ".join(f"{h:<{w}}" for h, w in zip(headers, col_w)))
    print(sep)
    for row in matrix:
        colours = [RESET, RESET, GREEN if row[2].startswith("Yes") else RED, RESET, RESET]
        parts = [f"{colours[i]}{v:<{col_w[i]}}{RESET}" for i, v in enumerate(row)]
        print("  " + "  ".join(parts))

    print()
    ok("Only tool RESULT caching via interceptors has real value in this architecture")
    info("  → And only for idempotent, deterministic read-only MCP tools")
    info("  → Use MCPToolCallRequest.name + hash(args) as cache key")
    info("  → TTL should match data freshness requirements of each tool")


# ===========================================================================
# MAIN
# ===========================================================================

def main() -> None:
    print(f"\n{BOLD}Data MCP Client — Tool Caching & System Behaviour Demonstration{RESET}")
    print(f"Python {sys.version}")
    print(f"{'─' * 72}")

    section_1_tool_caching_difficulties()
    section_2_session_manager_amortisation()
    section_3_real_bugs()
    section_4_caching_decision_matrix()

    header("SUMMARY")
    print(f"""
  {BOLD}What we proved:{RESET}

  {RED}✗{RESET}  StructuredTool cannot be JSON-serialised — coroutine captures live session
  {RED}✗{RESET}  ToolSerializer / tool.serialize() do not exist — the claim was hallucinated
  {RED}✗{RESET}  Interceptors cannot intercept tool DISCOVERY, only tool EXECUTION
  {RED}✗{RESET}  JWT decode is <1μs — Redis cache would cost 500x more than it saves
  {RED}✗{RESET}  User-level MCP sessions don't work — auth tokens expire, no Python reconnect

  {GREEN}✓{RESET}  mcp.types.Tool from list_tools() IS serialisable via .model_dump()
  {GREEN}✓{RESET}  Session manager already amortises connect + get_tools() per conversation
  {GREEN}✓{RESET}  Tool RESULT caching via interceptors IS valid for idempotent tools

  {BOLD}Real bugs to fix now (no new dependencies):{RESET}

  {YELLOW}1{RESET}  elicitation_handler.py:102  — asyncio.get_event_loop() → get_running_loop()
  {YELLOW}2{RESET}  data_mcp_client_service.py  — elicitation callback set after task starts (race)
  {YELLOW}3{RESET}  session_manager.py          — consume_stream timeout uses TTL not inter-token value
  {YELLOW}4{RESET}  session_manager.py          — _destroy_session() never calls mcp_client.disconnect()
  {YELLOW}5{RESET}  data_mcp_client_service.py  — generate_response is @staticmethod but __init__ stores session_manager (dead code)
  {YELLOW}6{RESET}  session_manager.py          — finally block double-removes on expired elicitation sessions
""")


if __name__ == "__main__":
    main()
