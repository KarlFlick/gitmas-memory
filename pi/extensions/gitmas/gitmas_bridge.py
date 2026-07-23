#!/usr/bin/env python3
"""Small JSON bridge for Pi's Gitmas extension."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path

HOME = Path.home()

os.environ.setdefault("MEMORY_DEVICE_ROLE", "mac")
os.environ.setdefault("MEMORY_USER_ID", os.environ.get("GITMAS_USER_ID", "karl-mac"))
os.environ.setdefault("MEMORY_SOURCE", os.environ.get("GITMAS_SOURCE", "pi-agent"))
os.environ.setdefault("MEMORY_AGENT_SOURCE", os.environ.get("GITMAS_AGENT_SOURCE", "pi-agent"))
os.environ.setdefault("MEMORY_AGENT_ID", os.environ.get("GITMAS_AGENT_ID", "mac-pi-agent"))

SERVER_PATH = Path(os.environ.get("GITMAS_MCP_SERVER", str(HOME / ".gitmas" / "memory-server" / "server.py"))).expanduser()


def load_server():
    spec = importlib.util.spec_from_file_location("gitmas_memory_server", SERVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {SERVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: gitmas_bridge.py <tool_name>", file=sys.stderr)
        return 2

    raw = sys.stdin.read() or "{}"
    try:
        params = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON input: {exc}", file=sys.stderr)
        return 2

    server = load_server()
    tool_name = sys.argv[1]
    func = getattr(server, tool_name, None)
    if func is None or not callable(func):
        print(f"Unknown Gitmas tool: {tool_name}", file=sys.stderr)
        return 2

    # Some Gitmas calls print diagnostics while importing/syncing. Keep stdout
    # reserved for the tool result so Pi receives clean text.
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            result = func(**params)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(result, str):
        result = json.dumps(result, indent=2)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
