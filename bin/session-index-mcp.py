#!/usr/bin/env python3
"""session-index-mcp: extract a compact session summary from a Claude Code .jsonl
transcript and store it as a Gitmas memory card via UnifiedMemory (local SQLite +
Gitmas sync queue + embeddings).

The card stores intent (first user prompt), key user prompts, tool usage, files
touched, duration, cwd — plus a path back to the encrypted backup on gitmas.com.
The full content stays in the .jsonl file (locally + on gitmas backup); the MCP
card is just a searchable pointer.

Usage:
  session-index-mcp.py <path-to-session.jsonl>             # store one
  session-index-mcp.py --backfill [<projects-root>]        # walk all jsonls
  session-index-mcp.py --dry-run <path>                    # parse only, no store

Selection rules (skip noise):
  - skip if fewer than MIN_USER_MSGS real user prompts
  - skip subagent transcripts (caller already filters them)
"""
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Wire UnifiedMemory in from canonical Gitmas path first, with repo paths as
# compatibility fallbacks for development worktrees.
sys.path.insert(0, str(Path.home() / ".gitmas" / "memory"))
sys.path.insert(0, str(Path.home() / "gitmas-config" / "memory"))
from unified_memory import UnifiedMemory, _vec_to_blob  # noqa: E402

MIN_USER_MSGS = 2
MAX_FIRST_PROMPT_CHARS = 240
MAX_USER_MSGS_IN_SUMMARY = 8


def parse_session(path: Path) -> dict | None:
    user_msgs: list[str] = []
    tool_counts: Counter = Counter()
    files_touched: set[str] = set()
    timestamps: list[str] = []
    cwd = None

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = evt.get("timestamp")
            if ts:
                timestamps.append(ts)
            if cwd is None and evt.get("cwd"):
                cwd = evt["cwd"]

            msg = evt.get("message", {}) if isinstance(evt.get("message"), dict) else {}
            payload = evt.get("payload", {}) if isinstance(evt.get("payload"), dict) else {}
            if cwd is None and evt.get("type") == "session_meta" and isinstance(payload, dict):
                cwd = payload.get("cwd")

            is_user = evt.get("type") == "user" or (evt.get("type") == "message" and msg.get("role") == "user")
            is_codex_user = evt.get("type") == "event_msg" and payload.get("type") == "user_message"
            if is_user or is_codex_user:
                content = payload.get("message") if is_codex_user else msg.get("content")
                # Claude real user prompts are plain strings; tool_result arrays start with "[".
                if isinstance(content, str) and content and not content.startswith("[") and not content.startswith("<environment_context>"):
                    user_msgs.append(content)
                # Pi/Codex can store message content as blocks.
                elif isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            text = block.get("text") or block.get("content")
                            if isinstance(text, str) and not text.startswith("<"):
                                parts.append(text)
                    if parts:
                        user_msgs.append(" ".join(parts))

            elif evt.get("type") == "assistant" or (evt.get("type") == "message" and msg.get("role") == "assistant"):
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            tool_counts[block.get("name", "?")] += 1
                            ti = block.get("input", {})
                        elif block.get("type") == "toolCall":
                            tool_counts[block.get("name", "?")] += 1
                            ti = block.get("arguments", {})
                        else:
                            continue
                        if isinstance(ti, dict):
                            fp = ti.get("file_path") or ti.get("path")
                            if fp and isinstance(fp, str):
                                files_touched.add(fp)
            elif evt.get("type") == "response_item" and isinstance(payload, dict):
                if payload.get("type") in ("function_call", "custom_tool_call"):
                    tool_counts[payload.get("name", "?")] += 1
                    args = payload.get("arguments") or payload.get("input") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if isinstance(args, dict):
                        fp = args.get("file_path") or args.get("path") or args.get("cmd")
                        if fp and isinstance(fp, str):
                            files_touched.add(fp[:240])

    if len(user_msgs) < MIN_USER_MSGS:
        return None

    duration_min = None
    if len(timestamps) >= 2:
        try:
            start = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
            end = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
            duration_min = round((end - start).total_seconds() / 60, 1)
        except ValueError:
            pass

    m = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$", path.stem)
    session_id = m.group(1) if m else path.stem
    project_dir = path.parent.name
    if ".codex" in path.parts and cwd:
        base = cwd.strip("/").replace("/", "-") or "root"
        project_dir = "codex-" + re.sub(r"[^A-Za-z0-9_-]", "-", base)[:220]

    first_prompt = user_msgs[0]
    if len(first_prompt) > MAX_FIRST_PROMPT_CHARS:
        first_prompt = first_prompt[:MAX_FIRST_PROMPT_CHARS] + "…"

    date_part = timestamps[0][:10] if timestamps else "unknown"
    snippet = re.sub(r"\s+", " ", first_prompt)[:80]
    description = f"Session {session_id[:8]} {date_part} — {snippet}"

    lines = [
        f"Session {session_id}",
        f"Date: {timestamps[0] if timestamps else 'unknown'}",
        f"Cwd: {cwd or project_dir}",
        f"Duration: {duration_min}min" if duration_min else "Duration: unknown",
        f"User prompts: {len(user_msgs)}",
        "",
        f"First request: {first_prompt}",
        "",
        f"Subsequent prompts (up to {MAX_USER_MSGS_IN_SUMMARY - 1} shown):",
    ]
    for i, m in enumerate(user_msgs[1:MAX_USER_MSGS_IN_SUMMARY], start=2):
        snip = re.sub(r"\s+", " ", m)[:200]
        if len(m) > 200:
            snip += "…"
        lines.append(f"  {i}. {snip}")

    if tool_counts:
        tools_str = ", ".join(f"{t}×{n}" for t, n in tool_counts.most_common(8))
        lines.append("")
        lines.append(f"Tools: {tools_str}")

    if files_touched:
        flist = sorted(files_touched)[:12]
        lines.append("")
        lines.append("Files touched (up to 12):")
        for fp in flist:
            lines.append(f"  - {fp}")
        if len(files_touched) > 12:
            lines.append(f"  …and {len(files_touched) - 12} more")

    lines.append("")
    lines.append(f"Local transcript: {path}")
    lines.append(f"Encrypted backup: gitmas.com sessions/{session_id} (format=gz.age)")

    return {
        "session_id": session_id,
        "project_dir": project_dir,
        "description": description,
        "content": "\n".join(lines),
        "user_msg_count": len(user_msgs),
        "last_seen": timestamps[-1] if timestamps else None,
    }


def find_existing_session_card(mem: UnifiedMemory, session_id: str) -> dict | None:
    """Return existing memory row for this session card, if any."""
    import sqlite3
    conn = sqlite3.connect(mem._db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, content, description, created, source, agent_id, layer FROM memories WHERE deleted=0 AND description LIKE ? ORDER BY created DESC LIMIT 1",
            (f"Session {session_id[:8]}%",),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_session_card(mem: UnifiedMemory, mem_id: str, card: dict) -> None:
    """Refresh an already-indexed session card and move it to the current timeline.

    Session backups are incremental while a conversation is active. Treat an
    update as a fresh card timestamp so /checkpoints reflects the latest backup,
    instead of hiding the new upload behind the initial partial-session index.
    """
    now = datetime.now(timezone.utc).isoformat()
    created = card.get("last_seen") or now
    source, agent_id, layer = mem._resolve_writer_identity("cli")
    tags = json.dumps(["session", "transcript", card["project_dir"]])
    conn, vec_ok = mem._connect()
    try:
        row = conn.execute("SELECT rowid FROM memories WHERE id=? AND deleted=0", (mem_id,)).fetchone()
        if not row:
            raise RuntimeError(f"missing memory {mem_id}")
        rowid = row[0]
        conn.execute(
            """UPDATE memories
               SET content=?, description=?, updated=?, created=?, type='reference',
                   session_id=?, source=?, agent_id=?, layer=?, tags=?, scope='global',
                   project_id=NULL, visibility='all'
               WHERE id=?""",
            (card["content"], card["description"], now, created, card["session_id"], source, agent_id, layer, tags, mem_id),
        )
        embedding = mem._embed_sync(card["content"])
        if vec_ok and embedding:
            conn.execute("DELETE FROM memories_vec WHERE rowid=?", (rowid,))
            conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (rowid, _vec_to_blob(embedding)))
        conn.execute("DELETE FROM memories_fts WHERE rowid=?", (rowid,))
        conn.execute(
            "INSERT INTO memories_fts(rowid, content, description) VALUES (?, ?, ?)",
            (rowid, card["content"], card["description"]),
        )
        conn.commit()
    finally:
        conn.close()
    # Let UnifiedMemory read metadata from the just-updated row, including
    # source/agent_id/layer and the refreshed created timestamp.
    mem._queue_remote_sync(mem_id, card["content"], card["description"], "reference", "update")


def index_one(path: Path, mem: UnifiedMemory, dry_run: bool = False) -> str:
    m = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$", path.stem)
    sid_short = (m.group(1) if m else path.stem)[:8]
    card = parse_session(path)
    if card is None:
        return f"  skip   {sid_short} (too few user prompts)"
    existing_id = None if dry_run else find_existing_session_card(mem, card["session_id"])
    if dry_run:
        return f"  WOULD  {sid_short} ({card['user_msg_count']} prompts, {len(card['content'])}b)"
    try:
        if existing_id:
            desired_created = card.get("last_seen") or ""
            expected_source, expected_agent_id, expected_layer = mem._resolve_writer_identity("cli")
            metadata_current = (
                existing_id.get("source") == expected_source
                and existing_id.get("agent_id") == expected_agent_id
                and existing_id.get("layer") == expected_layer
            )
            created_current = bool(desired_created) and str(existing_id.get("created") or "").startswith(desired_created[:19])
            if (existing_id.get("content") == card["content"]
                    and existing_id.get("description") == card["description"]
                    and metadata_current
                    and created_current):
                return f"  exists {sid_short} (already current)"
            update_session_card(mem, existing_id["id"], card)
            return f"  update {sid_short} → {existing_id['id']} ({card['user_msg_count']} prompts)"
        mem_id = mem.store(
            content=card["content"],
            description=card["description"],
            type="reference",
            scope="global",
            tags=["session", "transcript", card["project_dir"]],
        )
        return f"  index  {sid_short} → {mem_id} ({card['user_msg_count']} prompts)"
    except Exception as e:
        return f"  ERROR  {sid_short}: {type(e).__name__}: {e}"


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    dry_run = False
    if args[0] == "--dry-run":
        dry_run = True
        args = args[1:]
        if not args:
            print("--dry-run requires a path", file=sys.stderr)
            sys.exit(64)

    mem = UnifiedMemory()

    if args[0] == "--backfill":
        root = Path(args[1] if len(args) > 1 else Path.home() / ".claude/projects")
        files = sorted(p for p in root.rglob("*.jsonl") if "subagents" not in p.parts)
        print(f"Indexing {len(files)} sessions from {root}{' (DRY-RUN)' if dry_run else ''}")
        ok = skip = exists = err = 0
        for i, p in enumerate(files, 1):
            print(f"[{i:3d}/{len(files)}]", end=" ")
            res = index_one(p, mem, dry_run=dry_run)
            print(res)
            if "index "  in res: ok += 1
            elif "skip "   in res: skip += 1
            elif "exists " in res: exists += 1
            elif "ERROR "  in res: err += 1
            elif "WOULD "  in res: ok += 1
        print(f"\nDone. indexed={ok}, skipped={skip}, exists={exists}, errors={err}")
    else:
        path = Path(args[0])
        if not path.exists():
            print(f"not found: {path}", file=sys.stderr)
            sys.exit(1)
        print(index_one(path, mem, dry_run=dry_run))


if __name__ == "__main__":
    main()
