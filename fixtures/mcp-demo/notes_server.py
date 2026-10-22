"""DEMO FIXTURE: a tiny MCP server (stdio) that keeps notes in a local JSON file.

Used by NEXUS tests and the offline demo to exercise a real MCP connection end to end.
Run: python notes_server.py <notes.json>
"""
import json
import sys
from pathlib import Path

import mcp_types as types
from mcp.server.mcpserver import MCPServer

STORE = Path(sys.argv[1] if len(sys.argv) > 1 else "notes.json")
server = MCPServer("demo-notes")


def _load() -> list:
    return json.loads(STORE.read_text()) if STORE.exists() else []


@server.tool(annotations=types.ToolAnnotations(read_only_hint=True))
def list_notes() -> list[str]:
    """List saved notes."""
    return _load()


@server.tool(annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False))
def add_note(text: str) -> str:
    """Save a note."""
    notes = _load() + [text]
    STORE.write_text(json.dumps(notes))
    return f"saved note #{len(notes)}"


@server.tool(annotations=types.ToolAnnotations(destructive_hint=True))
def clear_notes() -> str:
    """Delete every note."""
    STORE.write_text("[]")
    return "cleared"


if __name__ == "__main__":
    server.run("stdio")
