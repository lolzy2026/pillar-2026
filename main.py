"""
app/main.py

FastAPI application entry point.

Startup sequence (lifespan):
1. Create ElicitationBridge, start TTL cleanup
2. Initialize MCPSessionManager (connects to MCP server, loads tools)
3. Set up PostgreSQL checkpointer for LangGraph
4. Compile LangGraph with tools + checkpointer
5. Create GraphRunner (wires bridge + compiled graph)
6. Store all components in app.state for dependency injection

Shutdown sequence (lifespan):
1. Cancel all pending elicitations
2. Stop MCPSessionManager (close MCP SSE connection)
3. Close PostgreSQL connection pool
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.api.assist import router as assist_router
from app.core.elicitation_bridge import ElicitationBridge
from app.core.graph_runner import GraphRunner
from app.core.mcp_session_manager import MCPSessionManager
from app.graph.builder import build_graph

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.
    All startup happens before yield, all teardown after.
    """

    logger.info("=== Application startup ===")

    # ── 1. Elicitation Bridge ─────────────────────────────────────────────────
    bridge = ElicitationBridge(
        ttl_seconds=int(os.getenv("ELICITATION_TTL_SECONDS", "600"))
    )
    await bridge.start()
    app.state.elicitation_bridge = bridge
    logger.info("ElicitationBridge started")

    # ── 2. MCP Session Manager ────────────────────────────────────────────────
    mcp_manager = MCPSessionManager(
        server_url=os.environ["MCP_SERVER_URL"],
        bridge=bridge,
        server_name=os.getenv("MCP_SERVER_NAME", "default"),
        transport=os.getenv("MCP_TRANSPORT", "streamable_http"),
        extra_headers=(
            {"Authorization": f"Bearer {os.environ['MCP_API_KEY']}"}
            if os.getenv("MCP_API_KEY")
            else None
        ),
    )
    await mcp_manager.start()
    app.state.mcp_manager = mcp_manager
    logger.info("MCPSessionManager started")

    # ── 3. PostgreSQL + LangGraph Checkpointer ────────────────────────────────
    pg_dsn = os.environ["POSTGRES_DSN"]

    # Build the checkpointer using asyncpg connection pool
    # Pool sizing: min 5, max 20 connections (supports 50 concurrent users
    # since most requests are async and don't hold connections continuously)
    pool = await asyncpg.create_pool(
        pg_dsn,
        min_size=5,
        max_size=20,
        command_timeout=30,
    )

    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()  # Creates checkpoint tables if they don't exist
    app.state.checkpointer = checkpointer
    app.state.pg_pool = pool
    logger.info("PostgreSQL checkpointer ready")

    # ── 4. Compile LangGraph ──────────────────────────────────────────────────
    compiled_graph = build_graph(
        checkpointer=checkpointer,
        mcp_tools=mcp_manager.tools,
    )
    app.state.compiled_graph = compiled_graph
    logger.info("LangGraph compiled")

    # ── 5. Graph Runner ───────────────────────────────────────────────────────
    graph_runner = GraphRunner(
        compiled_graph=compiled_graph,
        bridge=bridge,
    )
    app.state.graph_runner = graph_runner
    logger.info("GraphRunner ready")

    logger.info("=== Application ready ===")

    yield  # ── Application runs ──────────────────────────────────────────────

    # ── Teardown ──────────────────────────────────────────────────────────────
    logger.info("=== Application shutdown ===")

    await bridge.stop()
    logger.info("ElicitationBridge stopped")

    await mcp_manager.stop()
    logger.info("MCPSessionManager stopped")

    await pool.close()
    logger.info("PostgreSQL pool closed")

    logger.info("=== Shutdown complete ===")


def create_app() -> FastAPI:
    app = FastAPI(
        title="MCP Assist Service",
        description=(
            "AI-powered chat service with MCP tool integration and "
            "interactive elicitation support."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Configure origins for your actual UI domain in production
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
        allow_credentials=True,
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["*"],
    )

    # ── Routes ────────────────────────────────────────────────────────────────
    app.include_router(assist_router, prefix="/api/v1", tags=["assist"])

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["ops"])
    async def health():
        bridge: ElicitationBridge = app.state.elicitation_bridge
        return {
            "status": "ok",
            "pending_elicitations": bridge.pending_count(),
        }

    return app


app = create_app()
