#!/usr/bin/env python3
"""
local_test_elicitation.py

Interactive terminal test for the elicitation URL mode flow.

Flow:
  1. Send "Login to server v2" to POST /assist  (streaming SSE)
  2. Stream and print all SSE events in real time
  3. When an "elicitation" event with mode="url" arrives:
       - Print the URL prominently
       - Prompt you to open it in your browser and complete the login
       - Wait for you to press Enter
  4. POST /assist with is_elicitation_response=True
  5. Continue streaming the original SSE connection
  6. Print the final answer

Usage:
  pip install httpx rich
  python local_test_elicitation.py

  Optional env overrides:
    BASE_URL=http://localhost:8000   (default)
    BEARER_TOKEN=your-token         (default: test-token-local)
    SESSION_ID=my-session-123       (default: auto-generated)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import AsyncGenerator

import httpx
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

console = Console()

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
BEARER_TOKEN = os.getenv("BEARER_TOKEN", "test-token-local")
SESSION_ID = os.getenv("SESSION_ID", f"test-session-{uuid.uuid4().hex[:8]}")
ASSIST_URL = f"{BASE_URL}/api/v1/assist"
HEADERS = {
    "Authorization": f"Bearer {BEARER_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
}


# ── SSE parser ────────────────────────────────────────────────────────────────

async def parse_sse(response: httpx.Response) -> AsyncGenerator[dict, None]:
    """
    Yield parsed JSON dicts from a streaming SSE response.
    Handles multi-line data fields and ignores comment/keep-alive lines.
    """
    buffer = ""
    async for line in response.aiter_lines():
        line = line.rstrip("\r")

        if line.startswith("data:"):
            buffer = line[len("data:"):].strip()
        elif line == "" and buffer:
            try:
                yield json.loads(buffer)
            except json.JSONDecodeError:
                console.print(f"[dim]SSE parse error: {buffer!r}[/dim]")
            buffer = ""
        # Ignore comment lines (starting with :) and empty lines without buffer


# ── Event renderer ────────────────────────────────────────────────────────────

def render_event(event: dict) -> bool:
    """
    Pretty-print a single SSE event.
    Returns True if this is an elicitation event (caller should pause and act).
    """
    event_type = event.get("type", "unknown")

    if event_type == "node_start":
        console.print(f"  [dim]▶ node:[/dim] [cyan]{event.get('node')}[/cyan]")

    elif event_type == "node_complete":
        console.print(f"  [dim]✓ node:[/dim] [green]{event.get('node')}[/green]")

    elif event_type == "token":
        # Print tokens inline without newline (streaming feel)
        console.print(event.get("content", ""), end="", highlight=False)

    elif event_type == "elicitation":
        return True  # Caller handles this specially

    elif event_type == "complete":
        console.print()  # newline after tokens
        console.print(Rule("[bold green]Complete[/bold green]"))
        console.print(Panel(
            event.get("answer", "(no answer)"),
            title="[bold]Final Answer[/bold]",
            border_style="green",
        ))
        elicit_count = event.get("elicitation_count", 0)
        if elicit_count:
            console.print(f"  [dim]Elicitations in this session: {elicit_count}[/dim]")

    elif event_type == "error":
        console.print(Panel(
            event.get("message", "Unknown error"),
            title="[bold red]Error[/bold red]",
            border_style="red",
        ))

    else:
        console.print(f"  [dim]event: {event}[/dim]")

    return False


def render_elicitation(event: dict) -> None:
    """Print the elicitation URL prominently."""
    console.print()
    console.print(Rule("[bold yellow]⚡ Elicitation Request[/bold yellow]"))

    mode = event.get("mode", "unknown")
    message = event.get("message", "")
    url = event.get("url")
    tool_name = event.get("tool_name", "")

    console.print(f"  [bold]Tool:[/bold]    [magenta]{tool_name}[/magenta]")
    console.print(f"  [bold]Mode:[/bold]    [yellow]{mode.upper()}[/yellow]")
    console.print(f"  [bold]Message:[/bold] {message}")

    if mode == "url" and url:
        console.print()
        console.print(Panel(
            Text(url, style="bold cyan underline"),
            title="[bold yellow]🔗 Open this URL in your browser[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        ))
    elif mode == "form":
        schema = event.get("schema")
        if schema:
            console.print(f"  [bold]Schema:[/bold]")
            console.print(json.dumps(schema, indent=4))

    console.print()


# ── Elicitation response sender ───────────────────────────────────────────────

async def send_elicitation_response(
    client: httpx.AsyncClient,
    elicitation_id: str,
    action: str = "accept",
    content: dict | None = None,
) -> None:
    """POST the elicitation response back to /assist."""
    payload = {
        "session_id": SESSION_ID,
        "message": "",
        "is_elicitation_response": True,
        "elicitation_id": elicitation_id,
        "elicitation_action": action,
        "elicitation_content": content or {},
    }

    console.print(f"  [dim]Sending elicitation response: action={action}...[/dim]")

    response = await client.post(
        ASSIST_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {BEARER_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=10.0,
    )

    if response.status_code == 200:
        data = response.json()
        console.print(f"  [green]✓ Elicitation ACK:[/green] {data.get('message')}")
    else:
        console.print(f"  [red]✗ Elicitation response failed {response.status_code}:[/red]")
        console.print(f"  {response.text}")


# ── Main flow ─────────────────────────────────────────────────────────────────

async def run_test() -> None:
    console.print(Rule("[bold blue]MCP Elicitation — Local Test[/bold blue]"))
    console.print(f"  [dim]BASE_URL:   {BASE_URL}[/dim]")
    console.print(f"  [dim]SESSION_ID: {SESSION_ID}[/dim]")
    console.print(f"  [dim]TOKEN:      {BEARER_TOKEN[:8]}...[/dim]")
    console.print()

    user_message = "Login to server v2"
    console.print(Panel(
        f"[bold]{user_message}[/bold]",
        title="[bold blue]👤 User Message[/bold blue]",
        border_style="blue",
    ))
    console.print()

    payload = {
        "session_id": SESSION_ID,
        "message": user_message,
        "is_elicitation_response": False,
    }

    # Use a long timeout — the SSE stream stays open during elicitation
    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:
        console.print("[dim]Connecting to /assist...[/dim]")

        async with client.stream(
            "POST",
            ASSIST_URL,
            json=payload,
            headers=HEADERS,
        ) as response:

            if response.status_code != 200:
                console.print(f"[red]Request failed: {response.status_code}[/red]")
                body = await response.aread()
                console.print(body.decode())
                return

            console.print(f"[green]Connected — streaming SSE events[/green]")
            console.print(Rule())

            # Track pending elicitation across the stream
            pending_elicitation_id: str | None = None

            async for event in parse_sse(response):
                is_elicitation = render_event(event)

                if is_elicitation:
                    elicitation_id = event.get("elicitation_id")
                    mode = event.get("mode", "form")
                    pending_elicitation_id = elicitation_id

                    render_elicitation(event)

                    if mode == "url":
                        # ── URL mode: user opens browser ──────────────────
                        console.print("[yellow]Steps:[/yellow]")
                        console.print("  1. Copy the URL above and open it in your browser")
                        console.print("  2. Complete the login / OAuth flow")
                        console.print("  3. Come back here and press [bold]Enter[/bold]")
                        console.print()

                        # Non-blocking input — run in thread so SSE loop isn't blocked
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: input("  Press Enter when you have completed the login... "),
                        )
                        console.print()

                        # Send the elicitation response (confirmed=True for URL mode)
                        await send_elicitation_response(
                            client=client,
                            elicitation_id=elicitation_id,
                            action="accept",
                            content={"confirmed": True},
                        )

                    elif mode == "form":
                        # ── Form mode: collect field values from terminal ──
                        schema = event.get("schema", {})
                        properties = schema.get("properties", {}) if schema else {}

                        console.print("[yellow]Fill in the form fields:[/yellow]")
                        form_data: dict = {}

                        for field_name, field_def in properties.items():
                            field_type = field_def.get("type", "string")
                            description = field_def.get("description", "")
                            prompt = f"  {field_name}"
                            if description:
                                prompt += f" ({description})"
                            prompt += f" [{field_type}]: "

                            raw = await asyncio.get_event_loop().run_in_executor(
                                None, lambda p=prompt: input(p)
                            )
                            # Basic type coercion
                            if field_type == "integer":
                                form_data[field_name] = int(raw) if raw.strip() else None
                            elif field_type == "boolean":
                                form_data[field_name] = raw.strip().lower() in ("y", "yes", "true", "1")
                            else:
                                form_data[field_name] = raw.strip()

                        console.print()
                        await send_elicitation_response(
                            client=client,
                            elicitation_id=elicitation_id,
                            action="accept",
                            content=form_data,
                        )

                    console.print()
                    console.print("[dim]Waiting for tool execution to resume...[/dim]")
                    console.print(Rule())

    console.print()
    console.print(Rule("[bold blue]Test Complete[/bold blue]"))


def main() -> None:
    try:
        asyncio.run(run_test())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        sys.exit(0)
    except httpx.ConnectError:
        console.print(f"\n[red]Could not connect to {BASE_URL}[/red]")
        console.print("[dim]Is the FastAPI server running?[/dim]")
        sys.exit(1)


if __name__ == "__main__":
    main()
