#!/usr/bin/env python3
"""Gitmas Memory local HTTP gateway.

This lives beside the stdio Gitmas Memory MCP server (`server.py`). Claude Code
and Codex can use MCP; Hermes, Pi, and scripts can use this localhost-only JSON
API. All storage/search logic still goes through the internal UnifiedMemory
engine.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# Prefer the installed Gitmas product path.
MEMORY_DIR = Path(__file__).resolve().parents[1] / "memory"
for module_dir in (
    str(MEMORY_DIR),
    os.path.expanduser("~/.gitmas/memory"),
):
    if module_dir and module_dir not in sys.path:
        sys.path.insert(0, module_dir)
from unified_memory import UnifiedMemory  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TOKEN_PATH = os.path.expanduser(
    os.environ.get("GITMAS_HTTP_TOKEN_FILE", "~/.gitmas/memory-server/http.token")
)
def _is_android_debian() -> bool:
    markers = ("/system/build.prop", "/sdcard", "/data/data/com.termux")
    return any(os.path.exists(p) for p in markers)


def _device_prefix() -> str:
    if os.environ.get("GITMAS_DEVICE_ROLE"):
        return os.environ["GITMAS_DEVICE_ROLE"].strip()
    if os.environ.get("MEMORY_DEVICE_ROLE"):
        return os.environ["MEMORY_DEVICE_ROLE"].strip()
    if os.uname().sysname == "Darwin":
        return "mac"
    if _is_android_debian():
        return "android"
    return "device"


def _default_user_id() -> str:
    if os.environ.get("GITMAS_USER_ID"):
        return os.environ["GITMAS_USER_ID"].strip()
    if os.environ.get("MEMORY_USER_ID"):
        return os.environ["MEMORY_USER_ID"].strip()
    for path in (
        os.path.expanduser("~/.gitmas/identity/user-id"),
    ):
        try:
            value = Path(path).read_text().strip()
            if value:
                return value
        except OSError:
            pass
    prefix = _device_prefix()
    if prefix in {"mac", "android"}:
        return f"karl-{prefix}"
    return "karl"


def _default_source() -> str:
    return (os.environ.get("GITMAS_SOURCE")
            or os.environ.get("GITMAS_AGENT_SOURCE")
            or os.environ.get("MEMORY_SOURCE")
            or os.environ.get("MEMORY_AGENT_SOURCE")
            or "hermes").strip()


def _default_agent_id() -> str:
    if os.environ.get("GITMAS_AGENT_ID"):
        return os.environ["GITMAS_AGENT_ID"].strip()
    if os.environ.get("MEMORY_AGENT_ID"):
        return os.environ["MEMORY_AGENT_ID"].strip()
    for path in (
        os.path.expanduser("~/.gitmas/identity/agent-id"),
    ):
        try:
            value = Path(path).read_text().strip()
            if value:
                return value
        except OSError:
            pass
    return f"{_device_prefix()}-{_default_source()}"


DEFAULT_AGENT = _default_agent_id()
DEFAULT_SOURCE = _default_source()
DEFAULT_USER = _default_user_id()

PUBLIC_PATHS = {"/health"}


class HttpError(Exception):
    def __init__(self, status: int, error: str, message: str | None = None):
        self.status = status
        self.error = error
        self.message = message or error
        super().__init__(self.message)


def load_token(path: str = DEFAULT_TOKEN_PATH) -> str:
    """Load the bearer token from env or token file."""
    env_token = os.environ.get("MEMORY_HTTP_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def write_new_token(path: str = DEFAULT_TOKEN_PATH) -> str:
    """Create a local token file if the user asks for one."""
    token = secrets.token_urlsafe(32)
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return token


def _header(headers: dict[str, str] | Any, name: str) -> str:
    if hasattr(headers, "get"):
        return headers.get(name, "") or headers.get(name.lower(), "") or ""
    return ""


def _authorized(path: str, headers: dict[str, str] | Any, expected_token: str | None) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if not expected_token:
        return False
    auth = _header(headers, "Authorization")
    prefix = "Bearer "
    if not auth.startswith(prefix):
        return False
    return secrets.compare_digest(auth[len(prefix):].strip(), expected_token)


def _json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HttpError(400, "invalid_json", str(exc)) from exc
    if not isinstance(payload, dict):
        raise HttpError(400, "invalid_json", "JSON body must be an object")
    return payload


def _first(params: dict[str, list[str]], key: str, default: Any = None) -> Any:
    values = params.get(key)
    if not values:
        return default
    return values[0]


def _int_value(value: Any, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HttpError(400, "invalid_parameter", f"expected integer, got {value!r}") from exc


def _bool_value(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    raise HttpError(400, "invalid_parameter", f"expected boolean, got {value!r}")


def _tags(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    raise HttpError(400, "invalid_parameter", "tags must be a list or comma-separated string")


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        data = dict(row)
    elif hasattr(row, "keys"):
        data = {k: row[k] for k in row.keys()}
    else:
        data = dict(row)

    if "mem_id" in data and "id" not in data:
        data["id"] = data["mem_id"]
    if isinstance(data.get("tags"), str):
        try:
            data["tags"] = json.loads(data["tags"])
        except json.JSONDecodeError:
            pass
    return data


def _identity(payload: dict[str, Any], params: dict[str, list[str]] | None = None) -> dict[str, str | None]:
    params = params or {}
    agent = payload.get("agent") or _first(params, "agent") or DEFAULT_AGENT
    source = payload.get("source") or _first(params, "source") or DEFAULT_SOURCE
    user_id = payload.get("user_id") or _first(params, "user_id") or DEFAULT_USER
    device_id = payload.get("device_id") or _first(params, "device_id")
    return {
        "agent": agent,
        "source": source,
        "user_id": user_id,
        "device_id": device_id,
    }


def _require(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value in (None, ""):
        raise HttpError(400, "missing_field", f"missing required field: {key}")
    return value


def _hermes_session_guess() -> str | None:
    """Hermes talks to this gateway over HTTP (no process ancestry), and its
    upstream does not send a session id — the session file it is appending to
    right now is the freshest one in ~/.hermes/sessions."""
    import glob
    import time
    best_mtime = 0.0
    best_sid = None
    now = time.time()
    try:
        for f in glob.glob(os.path.expanduser("~/.hermes/sessions/session_*.json")):
            try:
                mt = os.path.getmtime(f)
            except OSError:
                continue
            if now - mt < 600 and mt > best_mtime:
                best_mtime = mt
                # canonical form matches the backup pipeline:
                # session_20260603_125519_5d3254.json → 20260603-125519-5d3254
                stem = os.path.splitext(os.path.basename(f))[0]
                best_sid = stem.removeprefix("session_").replace("_", "-")
    except Exception:
        return None
    return best_sid


def _store(mem: UnifiedMemory, payload: dict[str, Any], *, force_type: str | None = None) -> dict[str, Any]:
    ident = _identity(payload)
    session_id = payload.get("session_id")
    if not session_id and "hermes" in (str(ident.get("source") or "") + " " + str(ident.get("agent") or "")).lower():
        session_id = _hermes_session_guess()
    mem_type = force_type or payload.get("type") or "project"
    scope = payload.get("scope") or ("global" if mem_type == "feedback" else "global")
    project = payload.get("project") or payload.get("project_id")
    owner_id = payload.get("owner_id") or payload.get("agent") or ident["agent"]
    mem_id = mem.store(
        content=_require(payload, "content"),
        description=payload.get("description") or str(payload.get("content", ""))[:120],
        type=mem_type,
        tags=_tags(payload.get("tags")),
        session_id=session_id,
        source=ident["source"] or DEFAULT_SOURCE,
        scope=scope,
        owner_id=owner_id,
        project_id=project,
        visibility=payload.get("visibility") or "all",
    )
    return {"ok": True, "id": mem_id, "type": mem_type, "scope": scope, "project": project}


def _search(mem: UnifiedMemory, payload: dict[str, Any]) -> dict[str, Any]:
    ident = _identity(payload)
    mem.sync_from_remote()
    results = mem.search(
        query=_require(payload, "query"),
        limit=_int_value(payload.get("limit"), 5),
        type=payload.get("type"),
        min_score=float(payload.get("min_score", 0.0)),
        agent_id=payload.get("agent") or ident["agent"],
        user_id=payload.get("user_id") or ident["user_id"],
        project_id=payload.get("project") or payload.get("project_id"),
        since=payload.get("since"),
        until=payload.get("until"),
        chronological=_bool_value(payload.get("chronological")),
    )
    scope = payload.get("scope")
    rows = [_row_to_dict(r) for r in results]
    if scope:
        rows = [r for r in rows if r.get("scope") == scope]
    return {"ok": True, "results": rows, "count": len(rows)}


def _list(mem: UnifiedMemory, params: dict[str, list[str]]) -> dict[str, Any]:
    ident = _identity({}, params)
    mem.sync_from_remote()
    rows = [_row_to_dict(r) for r in mem.list_memories(
        type=_first(params, "type"),
        agent_id=_first(params, "agent", ident["agent"]),
        user_id=_first(params, "user_id", ident["user_id"]),
        project_id=_first(params, "project") or _first(params, "project_id"),
    )]
    scope = _first(params, "scope")
    if scope:
        rows = [r for r in rows if r.get("scope") == scope]
    limit = _int_value(_first(params, "limit"), len(rows) or 20)
    return {"ok": True, "results": rows[:limit], "count": len(rows[:limit])}


def _recent(mem: UnifiedMemory, params: dict[str, list[str]]) -> dict[str, Any]:
    listed = _list(mem, params)["results"]
    listed.sort(key=lambda r: r.get("created") or "", reverse=True)
    limit = _int_value(_first(params, "limit"), 20)
    rows = listed[:limit]
    return {"ok": True, "results": rows, "count": len(rows)}


def _update(mem: UnifiedMemory, payload: dict[str, Any]) -> dict[str, Any]:
    ok = bool(mem.update(
        _require(payload, "id"),
        content=payload.get("content"),
        description=payload.get("description"),
    ))
    return {"ok": ok, "updated": ok, "id": payload.get("id")}


def _delete(mem: UnifiedMemory, payload: dict[str, Any]) -> dict[str, Any]:
    ok = bool(mem.delete(_require(payload, "id")))
    return {"ok": ok, "deleted": ok, "id": payload.get("id")}


def _agents(mem: UnifiedMemory) -> dict[str, Any]:
    if hasattr(mem, "list_agents"):
        rows = [_row_to_dict(r) for r in mem.list_agents()]
    else:
        conn, _ = mem._connect()
        try:
            rows = [_row_to_dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY created").fetchall()]
        finally:
            conn.close()
    return {"ok": True, "results": rows, "count": len(rows)}


def _projects(mem: UnifiedMemory) -> dict[str, Any]:
    mem.sync_from_remote()
    conn, _ = mem._connect()
    try:
        rows = [_row_to_dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY created").fetchall()]
    finally:
        conn.close()
    return {"ok": True, "results": rows, "count": len(rows)}


def _sync_status(mem: UnifiedMemory) -> dict[str, Any]:
    stats = mem.stats() if hasattr(mem, "stats") else {}
    return {"ok": True, **_row_to_dict(stats)}


def _sync_drain(mem: UnifiedMemory) -> dict[str, Any]:
    drained = int(mem.drain_sync_queue())
    stats = mem.stats() if hasattr(mem, "stats") else {}
    return {"ok": True, "drained": drained, **_row_to_dict(stats)}


def dispatch_request(
    method: str,
    path: str,
    headers: dict[str, str] | Any,
    body: bytes,
    mem: UnifiedMemory,
    expected_token: str | None,
) -> tuple[int, dict[str, Any]]:
    """Pure dispatch function used by tests and the HTTP handler."""
    parsed = urlparse(path)
    route = parsed.path.rstrip("/") or "/"
    params = parse_qs(parsed.query)

    try:
        if not _authorized(route, headers, expected_token):
            raise HttpError(401, "unauthorized", "missing or invalid bearer token")

        if method == "GET" and route == "/health":
            return 200, {"ok": True, "service": "memory-http"}
        if method == "GET" and route == "/sync/status":
            return 200, _sync_status(mem)
        if method == "POST" and route == "/sync/drain":
            return 200, _sync_drain(mem)
        if method == "POST" and route == "/memory/store":
            return 200, _store(mem, _json_body(body))
        if method == "POST" and route == "/memory/feedback":
            return 200, _store(mem, _json_body(body), force_type="feedback")
        if method == "POST" and route == "/memory/search":
            return 200, _search(mem, _json_body(body))
        if method == "GET" and route == "/memory/list":
            return 200, _list(mem, params)
        if method == "GET" and route == "/memory/recent":
            return 200, _recent(mem, params)
        if method == "POST" and route == "/memory/update":
            return 200, _update(mem, _json_body(body))
        if method == "POST" and route == "/memory/delete":
            return 200, _delete(mem, _json_body(body))
        if method == "GET" and route == "/agents":
            return 200, _agents(mem)
        if method == "GET" and route == "/projects":
            return 200, _projects(mem)
        raise HttpError(404, "not_found", f"unknown route: {method} {route}")
    except HttpError as exc:
        return exc.status, {"ok": False, "error": exc.error, "message": exc.message}
    except Exception as exc:
        return 500, {"ok": False, "error": "internal_error", "message": str(exc)}


class MemoryHttpHandler(BaseHTTPRequestHandler):
    mem: UnifiedMemory | None = None
    expected_token: str = ""

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        status, payload = dispatch_request(
            method=self.command,
            path=self.path,
            headers=self.headers,
            body=body,
            mem=self.mem or build_memory(),
            expected_token=self.expected_token,
        )
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"memory-http: {self.address_string()} - {fmt % args}", file=sys.stderr)


def build_memory() -> UnifiedMemory:
    """Create the shared memory backend for the HTTP process.

    `GITMAS_MEMORY_DB` / `MEMORY_DB_PATH` keep smoke tests away from the real
    DB. When `MEMORY_HTTP_DISABLE_REMOTE_SYNC=1`, list/search skip remote pulls
    and store operations queue locally instead of attempting an immediate remote
    POST.
    """
    db_path = os.environ.get("GITMAS_MEMORY_DB") or os.environ.get("MEMORY_DB_PATH")
    mem = UnifiedMemory(db_path) if db_path else UnifiedMemory()
    if os.environ.get("MEMORY_HTTP_DISABLE_REMOTE_SYNC") == "1":
        mem.sync_from_remote = lambda: None  # type: ignore[method-assign]
        mem._is_remote_available = lambda: False  # type: ignore[method-assign]
    return mem


def run_server(host: str, port: int, token: str) -> None:
    if host != "127.0.0.1" and os.environ.get("MEMORY_HTTP_ALLOW_NONLOCAL") != "1":
        raise SystemExit("Refusing non-local bind. Set MEMORY_HTTP_ALLOW_NONLOCAL=1 to override explicitly.")
    if not token:
        raise SystemExit(
            "No HTTP token found. Set MEMORY_HTTP_TOKEN or create one with: "
            "python3 memory-server/http_server.py --init-token"
        )
    MemoryHttpHandler.expected_token = token
    MemoryHttpHandler.mem = build_memory()
    httpd = ThreadingHTTPServer((host, port), MemoryHttpHandler)
    print(f"gitmas-http listening on http://{host}:{port}", file=sys.stderr)
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gitmas localhost HTTP memory server")
    parser.add_argument("--host", default=os.environ.get("GITMAS_HTTP_HOST", os.environ.get("MEMORY_HTTP_HOST", DEFAULT_HOST)))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GITMAS_HTTP_PORT", os.environ.get("MEMORY_HTTP_PORT", DEFAULT_PORT))))
    parser.add_argument("--token-file", default=os.environ.get("GITMAS_HTTP_TOKEN_FILE", os.environ.get("MEMORY_HTTP_TOKEN_FILE", DEFAULT_TOKEN_PATH)))
    parser.add_argument("--init-token", action="store_true", help="create a new bearer token file and exit")
    args = parser.parse_args(argv)

    if args.init_token:
        token = write_new_token(args.token_file)
        print(f"Wrote token to {args.token_file}")
        print(token)
        return 0

    run_server(args.host, args.port, load_token(args.token_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
