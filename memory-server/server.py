#!/usr/bin/env python3
"""Gitmas Memory MCP server — scoped shared durable memory for agents/projects."""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

# Import Gitmas memory module from the canonical product path.
for module_dir in (
    os.path.expanduser("~/.gitmas/memory"),
):
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
from unified_memory import UnifiedMemory

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gitmas-memory")
mem = UnifiedMemory()

# Server boot metadata — used by memory_server_info() to detect when the
# source file on disk has been updated since the running process started.
# MCP clients (Claude Code) cache the tool list at session start, so a new
# @mcp.tool() only appears after `/mcp` reconnect.
_SERVER_START = time.time()
_SERVER_FILE = os.path.abspath(__file__)


def _sighup_handler(signum, frame):
    """SIGHUP: log a reload marker. MCP protocol doesn't support pushing a
    tool-list-changed notification from a signal handler over stdio, so the
    client must reconnect (`/mcp` in Claude Code) to pick up new tools."""
    sys.stderr.write(
        "[gitmas-memory] SIGHUP received — server file may have changed. "
        "Reconnect MCP session (/mcp in Claude Code) to load new tools.\n"
    )
    sys.stderr.flush()


try:
    signal.signal(signal.SIGHUP, _sighup_handler)
except (AttributeError, ValueError):
    pass  # Non-POSIX platforms or non-main-thread init


def _is_android_debian() -> bool:
    """Best-effort detection for Termux/proot Debian on Android."""
    markers = (
        "/system/build.prop",
        "/sdcard",
        "/data/data/com.termux",
    )
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
            value = open(path, "r").read().strip()
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
            or "gitmas-code").strip()


def _default_agent_id() -> str:
    if os.environ.get("GITMAS_AGENT_ID"):
        return os.environ["GITMAS_AGENT_ID"].strip()
    if os.environ.get("MEMORY_AGENT_ID"):
        return os.environ["MEMORY_AGENT_ID"].strip()
    for path in (
        os.path.expanduser("~/.gitmas/identity/agent-id"),
    ):
        try:
            value = open(path, "r").read().strip()
            if value:
                return value
        except OSError:
            pass
    return f"{_device_prefix()}-{_default_source()}"


def _identity(agent: str | None = None) -> tuple[str, str, str]:
    """Return (agent_id, user_id, source) for this memory client."""
    return agent or _default_agent_id(), _default_user_id(), _default_source()


_cli_session_id: str | None = None


def _session_from_ancestry() -> str | None:
    """Hooks/extensions write ~/.gitmas/session-map/<pid>.json for the owning
    CLI process; this server is a child of that same process, so walking our
    parent chain finds it — exact even with concurrent sessions."""
    map_dir = os.path.expanduser("~/.gitmas/session-map")
    try:
        pid = os.getpid()
        for _ in range(12):
            path = os.path.join(map_dir, f"{pid}.json")
            if os.path.exists(path):
                with open(path) as f:
                    sid = str(json.load(f).get("session_id") or "").strip()
                if sid:
                    return sid
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2)
            ppid_s = out.stdout.strip()
            if not ppid_s or int(ppid_s) <= 1:
                return None
            pid = int(ppid_s)
    except Exception:
        return None
    return None


def _session_from_transcripts() -> str | None:
    """Last-resort guess for agents without a hook/extension writer (codex,…):
    the transcript file the calling session just appended to is the most
    recently modified one across the known agent session dirs."""
    import glob
    import re
    patterns = (
        ("~/.claude/projects/*/*.jsonl", r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$"),
        ("~/.pi/agent/sessions/*/*.jsonl", r"_([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$"),
        ("~/.codex/sessions/*/*/*/rollout-*.jsonl", r"-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$"),
    )
    best_mtime = 0.0
    best_sid = None
    now = time.time()
    try:
        for pat, rx in patterns:
            for f in glob.glob(os.path.expanduser(pat)):
                try:
                    mt = os.path.getmtime(f)
                except OSError:
                    continue
                if now - mt < 600 and mt > best_mtime:
                    m = re.search(rx, os.path.basename(f))
                    if m:
                        best_mtime = mt
                        best_sid = m.group(1)
    except Exception:
        return None
    return best_sid


def _resolve_cli_session_id(explicit: str | None = None) -> str | None:
    """Real CLI session id for the calling agent.

    Order: explicit param → GITMAS_SESSION_ID/CLAUDE_SESSION_ID env (set by
    agent extensions) → pid-map ancestry walk (claude hooks, pi extension) →
    newest-transcript guess. Only certain answers are cached."""
    global _cli_session_id
    if explicit:
        return explicit.strip()
    if _cli_session_id:
        return _cli_session_id
    sid = (os.environ.get("GITMAS_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or "").strip()
    if not sid:
        sid = _session_from_ancestry()
    if sid:
        _cli_session_id = sid
        return sid
    return _session_from_transcripts()


# ─── Memory Tools (v3 scope-aware) ──────────────────────────────────────────

@mcp.tool()
def memory_store(content: str, description: str, type: str,
                 tags: list[str] | None = None,
                 scope: str = "global",
                 project: str | None = None,
                 agent: str | None = None,
                 session_id: str | None = None) -> str:
    """Store a new Gitmas Memory entry with scope control.

    Args:
        content: The Gitmas Memory content (will be embedded for semantic search)
        description: One-line description for indexing
        type: Memory type — user, feedback, project, or reference
        tags: Optional tags for filtering
        scope: Memory scope — global, project, agent, or user (default: global)
        project: Project ID for project-scoped memories (e.g. "tetris")
        agent: Agent ID for agent-scoped memories (e.g. "atlas")
        session_id: CLI session id of the writer; auto-resolved when omitted
    """
    owner_id, user_id, source = _identity(agent)
    mem_id = mem.store(content, description, type, tags,
                       session_id=_resolve_cli_session_id(session_id),
                       source=source,
                       scope=scope, owner_id=owner_id,
                       project_id=project)
    scope_info = f" scope={scope}"
    if project:
        scope_info += f" project={project}"
    scope_info += f" agent={owner_id} user={user_id} source={source}"
    return f"Stored [{mem_id}] ({type}){scope_info} {description}"


@mcp.tool()
def memory_search(query: str, limit: int = 5, type: str | None = None,
                  scope: str | None = None,
                  project: str | None = None,
                  agent: str | None = None,
                  since: str | None = None,
                  until: str | None = None,
                  chronological: bool | None = None,
                  debug: bool = False) -> str:
    """Search Gitmas Memory with semantic, scope, and time filtering.

    Args:
        query: Natural language search query
        limit: Max results to return (default 5)
        type: Optional filter by type (user, feedback, project, reference)
        scope: Optional scope filter (global, project, agent, user)
        project: Optional project ID filter
        agent: Optional agent ID filter (defaults to MEMORY_AGENT_ID or device-source identity)
        since: ISO date/datetime — only memories created on/after this instant
        until: ISO date/datetime — only memories created on/before this instant
        chronological: force newest-first sort; None auto-detects when the
            query contains time keywords (vakar/prieš/po/yesterday/…).
        debug: When True, append per-hit score breakdown (base, trust_mult,
            decay, echo, retrieval_count, helpful_count) — useful for
            diagnosing why a memory did/didn't rank.

    Results always include age (e.g. "prieš 2 d") and full ISO timestamp
    so the LLM can reason about when events actually happened.
    """
    mem.sync_from_remote()

    search_agent, search_user, _source = _identity(agent)

    results = mem.search(query, limit, type, min_score=0.25,
                         agent_id=search_agent, user_id=search_user,
                         project_id=project,
                         since=since, until=until,
                         chronological=chronological)

    # Post-filter by scope if explicitly requested
    if scope:
        results = [r for r in results if r.get("scope") == scope]

    if not results:
        return "No memories found."

    lines = []
    for r in results:
        tags = json.loads(r.get("tags", "[]")) if isinstance(r.get("tags"), str) else []
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        created = (r.get("created") or "")[:16].replace("T", " ")
        age = r.get("age_human") or ""
        when = ""
        if age and created:
            when = f" [{age} · {created} UTC]"
        elif created:
            when = f" [{created} UTC]"
        score_pct = f"{r['score']:.0%} match"
        scope_str = f" scope={r.get('scope', 'global')}"
        proj_str = f" project={r['project_id']}" if r.get("project_id") else ""
        debug_str = ""
        if debug:
            parts = r.get("_score_parts") or {}
            debug_str = (
                f"\n  [debug] base={parts.get('base', 0):.3f} "
                f"trust_mult={parts.get('trust_mult', 0):.3f} "
                f"decay={parts.get('decay', 0):.3f} "
                f"echo={parts.get('echo', 0):.3f} "
                f"retr={r.get('_retrieval', 0)} "
                f"helpful={r.get('_helpful', 0)}"
            )
        lines.append(
            f"**[{r['mem_id']}]** ({r['type']}){scope_str}{proj_str} "
            f"{r['description']} ({score_pct}){tag_str}{when}{debug_str}\n{r['content']}"
        )

    return "\n\n---\n\n".join(lines)


@mcp.tool()
def memory_list(type: str | None = None,
                scope: str | None = None,
                project: str | None = None) -> str:
    """List Gitmas Memory entries (IDs, types, descriptions only).

    Args:
        type: Optional filter by type (user, feedback, project, reference)
        scope: Optional scope filter (global, project, agent, user)
        project: Optional project ID filter
    """
    mem.sync_from_remote()
    search_agent, search_user, _source = _identity()
    results = mem.list_memories(type, agent_id=search_agent, user_id=search_user,
                                project_id=project)

    # Post-filter by scope if requested
    if scope:
        results = [r for r in results if r.get("scope") == scope]

    if not results:
        return "No memories stored."

    lines = []
    for r in results:
        created = (r.get("created") or "")[:16].replace("T", " ")
        s = r.get("scope", "global")
        p = f" project={r['project_id']}" if r.get("project_id") else ""
        lines.append(f"- **{r['id']}** ({r['type']}) [{s}{p}] {r['description']}  [{created} UTC]")

    return f"{len(lines)} memories:\n" + "\n".join(lines)


@mcp.tool()
def memory_identity() -> str:
    """Show the active Gitmas Memory identity for this agent process."""
    agent_id, user_id, source = _identity()
    return (f"agent={agent_id}\n"
            f"user={user_id}\n"
            f"source={source}\n"
            f"device_role={_device_prefix()}\n"
            f"api_url={os.environ.get('GITMAS_API_URL') or os.environ.get('MEMORY_API_URL', 'https://gitmas.com/memory')}")


@mcp.tool()
def memory_update(id: str, content: str, description: str | None = None) -> str:
    """Update an existing Gitmas Memory entry's content.

    Args:
        id: Memory ID to update
        content: New content
        description: New description (optional, keeps existing if omitted)
    """
    if mem.update(id, content, description):
        return f"Updated [{id}]"
    return f"Memory {id} not found."


@mcp.tool()
def memory_delete(id: str) -> str:
    """Delete a Gitmas Memory entry by ID.

    Args:
        id: Memory ID to delete
    """
    if mem.delete(id):
        return f"Deleted [{id}]"
    return f"Memory {id} not found."


@mcp.tool()
def memory_server_info() -> str:
    """Show live Gitmas Memory MCP server state — uptime, loaded tools, and whether a reload is needed.

    `restart_needed=true` means the source file on disk is newer than the
    running server. Run `/mcp` (Claude Code) to reconnect and load new tools.
    """
    try:
        file_mtime = os.path.getmtime(_SERVER_FILE)
    except OSError:
        file_mtime = 0.0
    tools = sorted(getattr(mcp._tool_manager, "_tools", {}).keys())
    # Probe Ollama lazily — _ollama_ok is None until first embed call.
    if mem._ollama_ok is None:
        try:
            mem._embed_sync("ping")
        except Exception:
            pass
    info = {
        "pid": os.getpid(),
        "uptime_seconds": int(time.time() - _SERVER_START),
        "server_file": _SERVER_FILE,
        "start_time_utc": datetime.fromtimestamp(_SERVER_START, tz=timezone.utc).isoformat(),
        "file_mtime_utc": datetime.fromtimestamp(file_mtime, tz=timezone.utc).isoformat() if file_mtime else None,
        "restart_needed": file_mtime > _SERVER_START + 1.0,
        "vec_ok": bool(mem._vec_ok),
        "ollama_ok": bool(mem._ollama_ok),
        "tool_count": len(tools),
        "tools": tools,
    }
    return json.dumps(info, indent=2)


@mcp.tool()
def memory_mark_helpful(id: str) -> str:
    """Mark a memory as helpful — bumps trust_score by +0.05 (cap 1.0) and increments helpful_count.

    Trust acts as a multiplier (0.5–1.0) on search relevance, so this lifts future ranking of the memory.

    Args:
        id: Memory ID to mark helpful
    """
    if mem.mark_helpful(id):
        return f"Marked helpful [{id}]"
    return f"Memory {id} not found."


@mcp.tool()
def memory_quality_report(days: int = 7) -> str:
    """Gitmas Memory search quality drift report — aggregates top-hit scores over recent days.

    Use this to spot when ranking quality has regressed (e.g. after a scoring
    change). Surfaces avg/min/max top_score, empty-result rate, and the
    weakest queries so you can see what's failing.

    Args:
        days: Lookback window in days (default 7).
    """
    report = mem.quality_report(days=days)
    return json.dumps(report, indent=2)


@mcp.tool()
def memory_entity_add(canonical_name: str,
                      aliases: list[str] | None = None,
                      entity_type: str = "unknown") -> str:
    """Register or update a Gitmas Memory entity + aliases for query expansion.

    Aliases registered here are used by every memory_search query to
    expand the query before scoring — e.g. registering canonical="Karl Flick"
    with aliases=["kf","karlui"] makes "kf bug" search expand to include
    "karl flick".

    Args:
        canonical_name: The canonical/preferred form of the entity.
        aliases: Other strings that should resolve to canonical_name.
        entity_type: Free-form classifier (person, project, tool, etc.).
    """
    ok = mem.entity_add(canonical_name, aliases or [], entity_type)
    return f"Entity '{canonical_name}' registered with {len(aliases or [])} aliases." if ok else "Failed."


@mcp.tool()
def memory_entity_list() -> str:
    """List all entities (hardcoded builtins + DB-registered)."""
    entities = mem.entity_list()
    if not entities:
        return "No entities registered."
    lines = []
    for e in entities:
        src = f" [{e.get('source','?')}/{e.get('entity_type','')}]"
        alias_str = ", ".join(e["aliases"]) if e["aliases"] else "(no aliases)"
        lines.append(f"- **{e['canonical_name']}**{src} → {alias_str}")
    return f"{len(entities)} entities:\n" + "\n".join(lines)


@mcp.tool()
def memory_mark_canonical(id: str, canonical: bool = True) -> str:
    """Tag a memory as `canonical` — a curated arch/reference doc.

    Canonical memories get a 1.3× score boost during search AND are
    exempt from temporal decay. Use this for the foundational docs you
    want search to surface reliably (architecture, design decisions,
    convention docs) — not for chatty summaries.

    Pass canonical=False to remove the tag.

    Args:
        id: Memory ID to (un)mark as canonical
        canonical: True to add the tag, False to remove it (default True)
    """
    if mem.mark_canonical(id, canonical):
        verb = "Marked canonical" if canonical else "Removed canonical tag"
        return f"{verb} [{id}]"
    return f"Memory {id} not found."


@mcp.tool()
def memory_mark_irrelevant(id: str) -> str:
    """Mark a memory as irrelevant — drops trust_score by 0.05 (floor 0.0) and increments irrelevant_count.

    Use when a search returned this memory but it wasn't what you wanted; future ranking learns from the miss.

    Args:
        id: Memory ID to mark irrelevant
    """
    if mem.mark_irrelevant(id):
        return f"Marked irrelevant [{id}]"
    return f"Memory {id} not found."


# ─── Agent Management Tools ─────────────────────────────────────────────────

@mcp.tool()
def agent_list() -> str:
    """List all registered agents in the MAS system."""
    conn, _ = mem._connect()
    try:
        rows = conn.execute(
            "SELECT id, name, role, capabilities, parent_id, status, created "
            "FROM agents ORDER BY created"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return "No agents registered."

    lines = []
    for r in rows:
        caps = json.loads(r["capabilities"]) if r["capabilities"] else []
        parent = f" (sub of {r['parent_id']})" if r["parent_id"] else ""
        lines.append(
            f"- **{r['id']}** [{r['status']}]{parent} — {r['name']}\n"
            f"  Role: {r['role']} | Caps: {', '.join(caps)}"
        )

    return f"{len(rows)} agents:\n" + "\n".join(lines)


@mcp.tool()
def project_list() -> str:
    """List all registered projects in the MAS system."""
    mem.sync_from_remote()
    conn, _ = mem._connect()
    try:
        rows = conn.execute(
            "SELECT id, name, description, assigned_agents, owner_user, status, created "
            "FROM projects ORDER BY created"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return "No projects registered."

    lines = []
    for r in rows:
        agents = json.loads(r["assigned_agents"]) if r["assigned_agents"] else []
        lines.append(
            f"- **{r['id']}** [{r['status']}] — {r['name']}\n"
            f"  {r['description']}\n"
            f"  Agents: {', '.join(agents)} | Owner: {r['owner_user']}"
        )

    return f"{len(rows)} projects:\n" + "\n".join(lines)


@mcp.tool()
def agent_send_message(from_agent: str, content: str,
                       to_agent: str | None = None,
                       project: str | None = None,
                       message_type: str = "request",
                       priority: str = "normal") -> str:
    """Send an inter-agent message. Omit to_agent for broadcast.

    Args:
        from_agent: Sending agent ID (e.g. "atlas")
        content: Message content
        to_agent: Target agent ID (omit for broadcast to all project agents)
        project: Project context for the message
        message_type: Type — request, response, broadcast, alert
        priority: Priority — low, normal, high, urgent
    """
    msg_id = mem.send_message(
        from_agent=from_agent,
        to_agent=to_agent,
        project_id=project,
        message_type=message_type,
        content=content,
        priority=priority,
    )
    target = to_agent or f"broadcast (project={project or 'all'})"
    return f"Message [{msg_id}] sent from {from_agent} to {target}"


@mcp.tool()
def agent_messages(agent_id: str = "atlas",
                   project: str | None = None,
                   include_read: bool = False,
                   limit: int = 20) -> str:
    """View messages for an agent.

    Args:
        agent_id: Agent ID to check messages for (default: atlas)
        project: Optional project filter. Prefer passing this for project mailboxes.
        include_read: Include already-read messages (default: false, pending only)
        limit: Maximum messages to return (default 20, max 100)
    """
    limit = max(1, min(int(limit), 100))
    if include_read:
        conn, _ = mem._connect()
        try:
            sql = """SELECT id, from_agent, to_agent, project_id, message_type,
                     content, status, priority, created, read_at
                     FROM agent_messages WHERE (to_agent=? OR to_agent IS NULL)"""
            params = [agent_id]
            if project:
                sql += " AND project_id=?"
                params.append(project)
            sql += " ORDER BY created DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        if not rows:
            return f"No messages for {agent_id}."

        lines = []
        for r in rows:
            prio = f" [{r['priority'].upper()}]" if r["priority"] != "normal" else ""
            target = "broadcast" if r["to_agent"] is None else f"to {r['to_agent']}"
            status = r["status"]
            lines.append(
                f"- [{r['id']}] {r['message_type']}{prio} from {r['from_agent']} ({target}) "
                f"[{status}] {r['created'][:16]}\n  {r['content'][:300]}"
            )

        return f"{len(rows)} messages:\n" + "\n".join(lines)
    else:
        msgs = mem.get_pending_messages(agent_id, project_id=project, limit=limit)
        if not msgs:
            return f"No pending messages for {agent_id}."

        lines = []
        for m in msgs:
            prio = f" [{m['priority'].upper()}]" if m["priority"] != "normal" else ""
            target = "broadcast" if m["to_agent"] is None else f"to {m['to_agent']}"
            lines.append(
                f"- [{m['id']}] {m['message_type']}{prio} from {m['from_agent']} ({target})\n"
                f"  {m['content'][:300]}"
            )

        return f"{len(msgs)} pending messages:\n" + "\n".join(lines)


# ─── Session Tools ───────────────────────────────────────────────────────────

@mcp.tool()
def session_list(device_id: str | None = None, days: int = 7, limit: int = 20) -> str:
    """List sessions synced to Gitmas.

    Args:
        device_id: Optional filter by real Gitmas device ID (not display name)
        days: How many days back to look (default 7)
        limit: Max results (default 20)
    """
    import subprocess
    from urllib.parse import quote as url_quote

    from unified_memory import _get_memory_api_key, _get_memory_api_url
    api_key = _get_memory_api_key()
    if not api_key:
        return "No MEMORY_API_KEY set."

    url = f"{_get_memory_api_url()}/sessions-list.php?days={days}&limit={limit}"
    if device_id:
        url += f"&device_id={url_quote(device_id)}"

    try:
        result = subprocess.run(
            ["curl", "-s", "-H", f"X-API-Key: {api_key}", "-m", "5", url],
            capture_output=True, text=True, timeout=8,
        )
        sessions = json.loads(result.stdout)
        if not isinstance(sessions, list) or not sessions:
            return "No sessions found."
    except Exception as e:
        return f"Error: {e}"

    lines = []
    for s in sessions:
        orig_kb = int(s.get("original_size") or 0) / 1024
        fmt = s.get("format") or "gz"
        device_name = s.get("device_name") or "?"
        real_device_id = s.get("device_id") or "?"
        project = s.get("project_dir") or "?"
        lines.append(
            f"- **{s['session_id']}** | {device_name} | {orig_kb:.0f}KB | {s.get('uploaded_at')}\n"
            f"  device_id={real_device_id} project_dir={project} format={fmt}"
        )

    return f"{len(sessions)} sessions:\n" + "\n".join(lines)


def _session_text_from_jsonl(raw: str) -> list[str]:
    """Extract a compact readable transcript from Pi/Claude-style JSONL."""
    def _text_from_content(content) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"].strip())
                elif isinstance(part.get("content"), str):
                    parts.append(part["content"].strip())
            return " ".join(p for p in parts if p).strip()
        return ""

    lines = []
    for line in raw.strip().split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue

        msg = obj.get("message") if isinstance(obj, dict) else None
        if isinstance(msg, dict):
            role = msg.get("role", "")
            text = _text_from_content(msg.get("content", ""))
        else:
            # Some agent JSONL formats put role/content at the top level.
            role = obj.get("role", "") if isinstance(obj, dict) else ""
            text = _text_from_content(obj.get("content", "")) if isinstance(obj, dict) else ""

        if role in ("user", "assistant") and text:
            label = "USER" if role == "user" else "ASSISTANT"
            lines.append(f"**{label}:** {text[:800]}")
    return lines


def _download_session_blob(api_base: str, api_key: str, session_id: str,
                           device_id: str, project_dir: str) -> bytes:
    import urllib.parse
    import urllib.request

    params = urllib.parse.urlencode({
        "session_id": session_id,
        "device_id": device_id,
        "project_dir": project_dir,
    })
    req = urllib.request.Request(
        f"{api_base}/sessions-download.php?{params}",
        headers={"X-API-Key": api_key},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


@mcp.tool()
def session_read(session_id: str, device_id: str | None = None, project_dir: str | None = None) -> str:
    """Read a full Gitmas-synced session from the remote store.

    If device_id/project_dir are omitted, metadata is resolved from session_list.
    Encrypted `gz.age` archives are downloaded, decrypted locally with
    `~/.age/mas.key`, gunzipped, then parsed into a readable transcript.

    Args:
        session_id: Session UUID to read
        device_id: Optional real device ID, or display name from session_list
        project_dir: Optional project directory key from session_list
    """
    import gzip
    import subprocess
    import urllib.parse
    import urllib.request

    from unified_memory import _get_memory_api_key, _get_memory_api_url
    api_key = _get_memory_api_key()
    if not api_key:
        return "No MEMORY_API_KEY set."
    api_base = _get_memory_api_url()

    # Resolve exact device_id/project_dir/format from metadata. Older callers often
    # passed device_name instead of device_id; accepting both keeps the tool simple.
    meta = None
    try:
        list_url = f"{api_base}/sessions-list.php?days=9999&limit=500"
        req = urllib.request.Request(list_url, headers={"X-API-Key": api_key})
        with urllib.request.urlopen(req, timeout=20) as resp:
            sessions = json.loads(resp.read().decode("utf-8"))
        for row in sessions if isinstance(sessions, list) else []:
            if row.get("session_id") != session_id:
                continue
            if device_id and device_id not in (row.get("device_id"), row.get("device_name")):
                continue
            if project_dir and project_dir != row.get("project_dir"):
                continue
            meta = row
            break
    except Exception:
        meta = None

    if meta:
        device_id = meta.get("device_id") or device_id
        project_dir = meta.get("project_dir") or project_dir
        fmt = meta.get("format") or "gz"
        device_name = meta.get("device_name") or "?"
    else:
        fmt = "gz.age"
        device_name = "?"
        if not device_id or not project_dir:
            return (
                "Session metadata not found. Run session_list and retry with "
                "session_id, real device_id, and project_dir."
            )

    try:
        blob = _download_session_blob(api_base, api_key, session_id, device_id, project_dir)
    except Exception as e:
        return f"Session download failed: {type(e).__name__}: {e}"

    # A JSON error can arrive with HTTP 200/4xx depending on PHP/auth path.
    if blob.lstrip().startswith(b'{"status":"error"'):
        return f"Session not found or error: {blob[:300].decode('utf-8', 'replace')}"

    try:
        if fmt == "gz.age":
            age_key = os.path.expanduser("~/.age/mas.key")
            if not os.path.exists(age_key):
                return f"Session is encrypted (gz.age), but age key not found at {age_key}."
            proc = subprocess.run(
                ["age", "-d", "-i", age_key],
                input=blob,
                capture_output=True,
                timeout=120,
            )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", "replace")[:300]
                return f"Session decrypt failed: rc={proc.returncode} {err}"
            raw_bytes = gzip.decompress(proc.stdout)
        elif fmt == "gz":
            raw_bytes = gzip.decompress(blob)
        else:
            return f"Unsupported session format: {fmt}"
    except Exception as e:
        return f"Session decode failed ({fmt}): {type(e).__name__}: {e}"

    raw = raw_bytes.decode("utf-8", "replace")
    lines = _session_text_from_jsonl(raw)
    if not lines:
        return "Session found/decrypted, but no readable user/assistant messages were detected."

    header = (
        f"Session {session_id} — {len(lines)} messages\n"
        f"device={device_name} ({device_id}) project_dir={project_dir} format={fmt}\n"
    )
    return header + "\n" + "\n\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
