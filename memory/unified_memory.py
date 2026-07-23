"""Gitmas Memory — local-first shared memory for humans and agents.

Single SQLite backend for Gitmas clients, CLI integrations, voice tools, and
MCP servers. Hybrid search: 60% vector (BGE-M3 via Ollama) + 40% keyword
(FTS5). Offline-first with Gitmas API sync.

Usage:
    from unified_memory import UnifiedMemory
    mem = UnifiedMemory()
    mem.store("fact", "description", "project")
    results = mem.search("query")
"""

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import subprocess
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen

GITMAS_HOME = os.path.expanduser(os.environ.get("GITMAS_HOME", "~/.gitmas"))
DB_PATH = os.path.expanduser(
    os.environ.get("GITMAS_MEMORY_DB")
    or os.environ.get("MEMORY_DB_PATH", os.path.join(GITMAS_HOME, "memory", "unified.db"))
)
OLLAMA_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "bge-m3"
EMBED_DIM = 1024
HRR_DIM = 1024
SYNC_STATE_FILE = os.path.expanduser(
    os.environ.get("GITMAS_SYNC_STATE_FILE", os.path.join(GITMAS_HOME, "memory", ".last-sync"))
)
HALF_LIFE_DAYS = 180  # Temporal decay half-life for project memories

# Memory types exempt from temporal decay
NO_DECAY_TYPES = {"user", "feedback", "reference"}

# Tags that mark a memory as a transcript/echo of other content.
# Search applies a penalty so canonical docs outrank their own echoes.
ECHO_TAGS = {"session", "transcript"}
ECHO_PENALTY = 0.85

# Auto-flush session summaries are chatty prose memories that happen to
# match keywords across many topics. They drown out canonical docs on
# broad queries (measured: 4-5/10 top-1 misses on the field benchmark
# were auto-flush memories). Heavier penalty than ECHO.
NOISE_TAGS = {"auto-flush"}
NOISE_PENALTY = 0.65

# Boosts applied during search scoring (final score still capped at 1.0
# so it stays readable as a percentage).
PROJECT_BOOST = 1.3   # memory's project_id (or tag) matches a project named in the query
CANONICAL_BOOST = 1.3  # memory tagged `canonical` — curated arch docs
# When canonical AND project both match, the canonical signal is the
# strongest possible — "this is the official doc for this project".
# Lift canonical boost to 1.5 in that case so the canonical reliably
# outranks non-canonical project memories with stronger keyword density.
CANONICAL_PROJECT_BOOST = 1.5
DESC_MATCH_BOOST_PER_WORD = 0.05  # per query word also present in description
DESC_MATCH_BOOST_MAX = 0.20       # cap on description-match boost
CANONICAL_TAG = "canonical"

# ─── Temporal helpers ────────────────────────────────────────────────
# ISO 8601 validator for since/until filters (prevents SQL injection on
# the already-string-interpolated where_clause).
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?$"
)
# Keywords that make a query *about time* — when found, results get
# re-sorted chronologically (newest first) instead of by pure score.
_TEMPORAL_RE = re.compile(
    r"\b(vakar|šiandien|rytoj|prieš|po|tada|anksčiau|vėliau|kada|"
    r"pirmiau|seniau|ankstesn\w+|"
    r"yesterday|today|tomorrow|before|after|earlier|later|when|then|"
    r"ago|recent|recently)\b",
    re.IGNORECASE,
)


_SECRETS_PATH = os.path.expanduser("~/.secrets")
_SECRETS_CACHE = {"mtime": 0.0, "key": ""}


def _get_memory_api_url() -> str:
    """Base URL for the Gitmas Memory API.

    Prefer canonical GITMAS_API_URL; MEMORY_API_URL remains a compatibility
    alias for older clients.
    """
    return (os.environ.get("GITMAS_API_URL")
            or os.environ.get("MEMORY_API_URL")
            or "https://gitmas.com/memory").rstrip("/")


def _get_memory_api_key() -> str:
    """Read GITMAS_API_KEY / MEMORY_API_KEY from ~/.secrets or env.

    Prefer the file over os.environ so long-running processes pick up a
    rotated key without needing a restart.
    """
    try:
        mtime = os.path.getmtime(_SECRETS_PATH)
    except OSError:
        return os.environ.get("GITMAS_API_KEY") or os.environ.get("MEMORY_API_KEY", "")
    if mtime != _SECRETS_CACHE["mtime"]:
        key = ""
        try:
            with open(_SECRETS_PATH, "r") as f:
                for line in f:
                    if line.startswith("export GITMAS_API_KEY=") or line.startswith("export MEMORY_API_KEY="):
                        v = line.split("=", 1)[1].strip()
                        if v.startswith(('"', "'")) and v.endswith(v[0]):
                            v = v[1:-1]
                        key = v
                        break
        except OSError:
            pass
        _SECRETS_CACHE["mtime"] = mtime
        _SECRETS_CACHE["key"] = key
    return _SECRETS_CACHE["key"] or os.environ.get("GITMAS_API_KEY") or os.environ.get("MEMORY_API_KEY", "")


def _validate_iso(s: str) -> str:
    if not _ISO_RE.match(s):
        raise ValueError(f"invalid ISO date: {s!r}")
    return s


def _is_temporal_query(q: str) -> bool:
    return bool(_TEMPORAL_RE.search(q or ""))


def _relative_time_lt(created_iso: str) -> str:
    """Human-friendly LT relative time: 'ką tik', 'prieš 5 min', 'prieš 2 d'."""
    try:
        dt = datetime.fromisoformat((created_iso or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 60:
            return "ką tik"
        mins = secs // 60
        if mins < 60:
            return f"prieš {mins} min"
        hours = mins // 60
        if hours < 24:
            return f"prieš {hours} val"
        days = hours // 24
        if days < 7:
            return f"prieš {days} d"
        weeks = days // 7
        if weeks < 5:
            return f"prieš {weeks} sav"
        months = days // 30
        if months < 12:
            return f"prieš {months} mėn"
        return f"prieš {days // 365} m"
    except Exception:
        return ""

# Remote sync state
_remote_available = True
_last_connectivity_check = 0.0
_CONNECTIVITY_RETRY_INTERVAL = 60

# Known entity aliases (canonical_name → set of aliases)
ENTITY_ALIASES = {
    "Karl Flick": {"karl", "karlas", "karlui", "karlo", "karlą", "user", "aš", "man", "mano"},
    "Atlas": {"atlas", "atlasas", "atlasui", "cli atlas", "voice atlas"},
    "MAS": {"mas", "multi-agentic system", "multi agentic system", "voice pipeline"},
    "Tetris": {"tetris", "tetris unity", "tetris game"},
    "Upsnake": {"upsnake", "snake game", "snake unity"},
}
# Build reverse lookup: alias → canonical
_ALIAS_TO_CANONICAL = {}
for canonical, aliases in ENTITY_ALIASES.items():
    for alias in aliases:
        _ALIAS_TO_CANONICAL[alias.lower()] = canonical


# --- HRR (Holographic Reduced Representations) ---

_TWO_PI = 2.0 * math.pi


def _hrr_encode(text: str, dim: int = HRR_DIM) -> bytes:
    """Encode text as HRR phase vector blob. Pure Python, no dependencies."""
    words = re.findall(r'\b\w{2,}\b', text.lower())
    if not words:
        return _vec_to_blob([0.0] * dim)

    word_phases = []
    blocks_needed = math.ceil(dim / 16)
    for word in set(words):
        uint16s = []
        for i in range(blocks_needed):
            digest = hashlib.sha256(f"{word}:{i}".encode()).digest()
            uint16s.extend(struct.unpack("<16H", digest))
        phases = [v * (_TWO_PI / 65536.0) for v in uint16s[:dim]]
        word_phases.append(phases)

    # Bundle: circular mean of all word vectors
    if len(word_phases) == 1:
        return _vec_to_blob(word_phases[0])

    result = []
    for j in range(dim):
        sin_sum = sum(math.sin(wp[j]) for wp in word_phases)
        cos_sum = sum(math.cos(wp[j]) for wp in word_phases)
        result.append(math.atan2(sin_sum, cos_sum) % _TWO_PI)
    return _vec_to_blob(result)


def _hrr_similarity(a_blob: bytes, b_blob: bytes, dim: int = HRR_DIM) -> float:
    """Phase similarity between two HRR vectors. Returns 0-1."""
    a = struct.unpack(f"{dim}f", a_blob)
    b = struct.unpack(f"{dim}f", b_blob)
    return sum(math.cos(a[i] - b[i]) for i in range(dim)) / dim


def _vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


class UnifiedMemory:
    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._vec_ok = False
        self._ollama_ok = None
        self._init_db()
        # Instance-level alias map = hardcoded ENTITY_ALIASES + entities table.
        # Refreshed when entity_add() is called; resolve_query() reads this.
        self._alias_map: dict[str, str] = {}
        self._load_alias_map()
        self._maybe_backfill_hrr()

    def _maybe_backfill_hrr(self) -> None:
        """Run HRR backfill once per DB if the meta marker is missing.
        Cheap: a single SELECT when already done."""
        conn, _ = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='hrr_backfill_done'"
            ).fetchone()
        finally:
            conn.close()
        if row and row["value"]:
            return
        n = self.backfill_hrr_vectors()
        conn, _ = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("hrr_backfill_done", datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        if n:
            sys_stderr_write = getattr(__import__("sys").stderr, "write", None)
            if sys_stderr_write:
                sys_stderr_write(f"[gitmas-memory] HRR backfill: {n} rows\n")

    def _load_project_names(self) -> set[str]:
        """Return lowercase set of project IDs registered locally.
        Used by search to detect when a query is project-scoped."""
        try:
            conn, _ = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id FROM projects"
                ).fetchall()
            finally:
                conn.close()
            return {r["id"].lower() for r in rows if r["id"]}
        except Exception:
            return set()

    def _match_projects_in_query(self, query: str) -> set[str]:
        """Find registered project names mentioned in the query.

        A project matches when:
          1. Its full id appears as a word in the query, OR
          2. It is a hyphen-compound (e.g. `voice-pipeline`) and ALL
             of its hyphen-split parts appear as words.

        The second rule lets compound names work even though the
        word-boundary regex splits on hyphens. e.g. `voice-pipeline`
        matches the query "voice pipeline tts" because both 'voice'
        and 'pipeline' are present.
        """
        expanded = self.resolve_query(query).lower()
        words = set(re.findall(r"\b\w+\b", expanded))
        matched: set[str] = set()
        for p in self._load_project_names():
            if p in words:
                matched.add(p)
                continue
            parts = [s for s in re.split(r"[-_]", p) if s]
            if len(parts) > 1 and all(s in words for s in parts):
                matched.add(p)
        return matched

    def _load_alias_map(self) -> None:
        """Rebuild self._alias_map from hardcoded ENTITY_ALIASES + entities table."""
        m: dict[str, str] = {}
        for canonical, aliases in ENTITY_ALIASES.items():
            for a in aliases:
                m[a.lower()] = canonical
        try:
            conn, _ = self._connect()
            try:
                rows = conn.execute(
                    "SELECT canonical_name, aliases FROM entities"
                ).fetchall()
            finally:
                conn.close()
            for r in rows:
                try:
                    aliases = json.loads(r["aliases"] or "[]")
                except Exception:
                    aliases = []
                # The canonical name itself is also an alias for itself.
                m[r["canonical_name"].lower()] = r["canonical_name"]
                for a in aliases:
                    if isinstance(a, str) and a.strip():
                        m[a.lower().strip()] = r["canonical_name"]
        except Exception:
            pass
        self._alias_map = m

    def entity_add(self, canonical_name: str,
                   aliases: list[str] | None = None,
                   entity_type: str = "unknown",
                   sync: bool = True) -> bool:
        """Register or update an entity + its aliases. Refreshes the alias map.

        When sync=True, also writes a reference-type memory tagged
        `entity_alias` so the registration propagates to other devices via
        the existing memory sync path (no remote API changes required).
        """
        canonical_name = (canonical_name or "").strip()
        if not canonical_name:
            return False
        aliases = [a.strip() for a in (aliases or []) if a and a.strip()]
        now = datetime.now(timezone.utc).isoformat()
        merged_aliases: list[str] = aliases[:]
        conn, _ = self._connect()
        try:
            existing = conn.execute(
                "SELECT id, aliases FROM entities WHERE canonical_name=?",
                (canonical_name,),
            ).fetchone()
            if existing:
                try:
                    cur_aliases = json.loads(existing["aliases"] or "[]")
                except Exception:
                    cur_aliases = []
                merged_aliases = sorted({*cur_aliases, *aliases}, key=str.lower)
                conn.execute(
                    "UPDATE entities SET aliases=?, entity_type=? WHERE id=?",
                    (json.dumps(merged_aliases), entity_type, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO entities(canonical_name, entity_type, aliases, created) "
                    "VALUES (?, ?, ?, ?)",
                    (canonical_name, entity_type, json.dumps(merged_aliases), now),
                )
            conn.commit()
        finally:
            conn.close()
        self._load_alias_map()
        if sync:
            try:
                payload = json.dumps({
                    "canonical_name": canonical_name,
                    "entity_type": entity_type,
                    "aliases": merged_aliases,
                })
                self.store(
                    content=payload,
                    description=f"entity:{canonical_name}",
                    type="reference",
                    tags=["entity_alias", f"entity_type:{entity_type}"],
                )
            except Exception:
                pass
        return True

    def _ingest_entity_memory(self, content: str) -> bool:
        """Parse a sync'd entity_alias memory and merge into entities table."""
        try:
            obj = json.loads(content)
            name = (obj.get("canonical_name") or "").strip()
            if not name:
                return False
            aliases = obj.get("aliases") or []
            entity_type = obj.get("entity_type") or "unknown"
            self.entity_add(name, aliases, entity_type, sync=False)
            return True
        except Exception:
            return False

    def register_project(self, project_id: str, name: str | None = None,
                         description: str = "", sync: bool = True) -> bool:
        """Register a project locally. With sync=True, also pushes a
        `project_registration`-tagged memory so other devices pick up
        the project name via the existing memory sync path."""
        project_id = (project_id or "").strip()
        if not project_id:
            return False
        name = (name or project_id).strip()
        now = datetime.now(timezone.utc).isoformat()
        conn, _ = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO projects(id, name, description, created) "
                "VALUES (?, ?, ?, ?)",
                (project_id, name, description, now),
            )
            conn.commit()
        finally:
            conn.close()
        if sync:
            try:
                self.store(
                    content=json.dumps({"project_id": project_id, "name": name,
                                        "description": description}),
                    description=f"project:{project_id}",
                    type="reference",
                    tags=["project_registration"],
                )
            except Exception:
                pass
        return True

    def _ingest_project_memory(self, content: str) -> bool:
        """Parse a sync'd project_registration memory into local projects table."""
        try:
            obj = json.loads(content)
            pid = (obj.get("project_id") or "").strip()
            if not pid:
                return False
            name = obj.get("name") or pid
            desc = obj.get("description") or ""
            self.register_project(pid, name, desc, sync=False)
            return True
        except Exception:
            return False

    def backfill_hrr_vectors(self, limit: int | None = None) -> int:
        """Compute HRR vectors for any memory rows missing one. Idempotent.

        Old rows pre-date the hrr_vector column and need a one-time backfill
        so the always-on HRR co-ranker can score them.
        """
        conn, _ = self._connect()
        try:
            sql = ("SELECT id, content FROM memories "
                   "WHERE hrr_vector IS NULL AND deleted=0")
            if limit:
                sql += f" LIMIT {int(limit)}"
            rows = conn.execute(sql).fetchall()
            n = 0
            for r in rows:
                hrr = _hrr_encode(r["content"] or "")
                conn.execute(
                    "UPDATE memories SET hrr_vector=? WHERE id=?",
                    (hrr, r["id"]),
                )
                n += 1
            conn.commit()
            return n
        finally:
            conn.close()

    def entity_list(self) -> list[dict]:
        """List all entities (hardcoded + DB-registered)."""
        result: list[dict] = []
        for canonical, aliases in ENTITY_ALIASES.items():
            result.append({
                "canonical_name": canonical,
                "entity_type": "builtin",
                "aliases": sorted(aliases, key=str.lower),
                "source": "hardcoded",
            })
        try:
            conn, _ = self._connect()
            try:
                rows = conn.execute(
                    "SELECT canonical_name, entity_type, aliases, created "
                    "FROM entities ORDER BY canonical_name"
                ).fetchall()
            finally:
                conn.close()
            for r in rows:
                try:
                    aliases = json.loads(r["aliases"] or "[]")
                except Exception:
                    aliases = []
                result.append({
                    "canonical_name": r["canonical_name"],
                    "entity_type": r["entity_type"] or "unknown",
                    "aliases": aliases,
                    "created": r["created"],
                    "source": "db",
                })
        except Exception:
            pass
        return result

    def _connect(self) -> tuple[sqlite3.Connection, bool]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        vec_ok = _load_sqlite_vec(conn)
        return conn, vec_ok

    def _init_db(self):
        conn, vec_ok = self._connect()
        self._vec_ok = vec_ok
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    content TEXT NOT NULL,
                    session_id TEXT,
                    source TEXT NOT NULL DEFAULT 'cli',
                    device_id TEXT,
                    device_name TEXT,
                    tags TEXT DEFAULT '[]',
                    created TEXT NOT NULL,
                    updated TEXT,
                    synced_at TEXT,
                    deleted INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    session_type TEXT NOT NULL DEFAULT 'voice',
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    device_id TEXT
                );

                CREATE TABLE IF NOT EXISTS session_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    session_type TEXT NOT NULL DEFAULT 'voice',
                    note_type TEXT NOT NULL DEFAULT 'manual',
                    content TEXT NOT NULL,
                    created TEXT NOT NULL,
                    promoted INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS sync_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_name TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    synced_at TEXT
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)

            if vec_ok:
                try:
                    conn.execute(f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec
                        USING vec0(embedding float[{EMBED_DIM}] distance_metric=cosine)
                    """)
                    conn.execute(f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS turns_vec
                        USING vec0(embedding float[{EMBED_DIM}] distance_metric=cosine)
                    """)
                    conn.execute(f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS notes_vec
                        USING vec0(embedding float[{EMBED_DIM}] distance_metric=cosine)
                    """)
                except Exception:
                    pass

            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(content, description, tokenize='porter unicode61')
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts
                USING fts5(text, tokenize='porter unicode61')
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
                USING fts5(content, tokenize='porter unicode61')
            """)

            # Entities table for entity resolution
            conn.execute("""
                CREATE TABLE IF NOT EXISTS entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_name TEXT NOT NULL UNIQUE,
                    entity_type TEXT DEFAULT 'unknown',
                    aliases TEXT DEFAULT '[]',
                    created TEXT NOT NULL
                )
            """)

            # Search quality log — one row per search() call. Used by
            # memory_quality_report to detect drift in top-hit scores.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS search_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    query TEXT NOT NULL,
                    hit_count INTEGER NOT NULL,
                    top_score REAL,
                    top_id TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_search_log_ts ON search_log(timestamp)"
            )

            # --- v3 tables ---

            conn.execute("""
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT,
                    capabilities TEXT DEFAULT '[]',
                    parent_id TEXT,
                    created_by TEXT,
                    status TEXT DEFAULT 'active',
                    config TEXT DEFAULT '{}',
                    created TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT,
                    aliases TEXT DEFAULT '[]',
                    preferences TEXT DEFAULT '{}',
                    created TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    assigned_agents TEXT DEFAULT '[]',
                    owner_user TEXT,
                    status TEXT DEFAULT 'active',
                    created TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_agent TEXT NOT NULL,
                    to_agent TEXT,
                    project_id TEXT,
                    message_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    priority TEXT DEFAULT 'normal',
                    created TEXT NOT NULL,
                    read_at TEXT,
                    responded_at TEXT
                )
            """)

            # Agent messages are a shared mailbox in one SQLite DB, but they
            # must behave like separate project mailboxes at scale. These
            # indexes keep project/agent/status/time queries bounded even when
            # the global table contains many projects and millions of rows.
            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_agent_messages_project ON agent_messages(project_id)",
                "CREATE INDEX IF NOT EXISTS idx_agent_messages_to_status ON agent_messages(to_agent, status)",
                "CREATE INDEX IF NOT EXISTS idx_agent_messages_project_status_created ON agent_messages(project_id, status, created)",
                "CREATE INDEX IF NOT EXISTS idx_agent_messages_project_to_status_created ON agent_messages(project_id, to_agent, status, created)",
                "CREATE INDEX IF NOT EXISTS idx_agent_messages_project_broadcast_status_created ON agent_messages(project_id, status, created) WHERE to_agent IS NULL",
            ]:
                conn.execute(idx_sql)

            # Schema migrations — add columns if missing
            for col_sql in [
                # v2 columns
                "ALTER TABLE memories ADD COLUMN trust_score REAL DEFAULT 0.5",
                "ALTER TABLE memories ADD COLUMN retrieval_count INTEGER DEFAULT 0",
                "ALTER TABLE memories ADD COLUMN helpful_count INTEGER DEFAULT 0",
                "ALTER TABLE memories ADD COLUMN irrelevant_count INTEGER DEFAULT 0",
                "ALTER TABLE memories ADD COLUMN hrr_vector BLOB",
                "ALTER TABLE turns ADD COLUMN hrr_vector BLOB",
                "ALTER TABLE session_notes ADD COLUMN hrr_vector BLOB",
                # v3 columns — memories
                "ALTER TABLE memories ADD COLUMN scope TEXT DEFAULT 'global'",
                "ALTER TABLE memories ADD COLUMN owner_id TEXT",
                "ALTER TABLE memories ADD COLUMN project_id TEXT",
                "ALTER TABLE memories ADD COLUMN visibility TEXT DEFAULT 'all'",
                # writer provenance columns
                "ALTER TABLE memories ADD COLUMN agent_id TEXT",
                "ALTER TABLE memories ADD COLUMN layer TEXT",
                # v3 columns — turns
                "ALTER TABLE turns ADD COLUMN agent_id TEXT DEFAULT 'atlas'",
                "ALTER TABLE turns ADD COLUMN user_id TEXT DEFAULT 'karl'",
                # v3 columns — session_notes
                "ALTER TABLE session_notes ADD COLUMN agent_id TEXT DEFAULT 'atlas'",
            ]:
                try:
                    conn.execute(col_sql)
                except sqlite3.OperationalError:
                    pass  # Column already exists

            # Set metadata
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("schema_version", "3"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                ("embed_model", EMBED_MODEL),
            )
            conn.commit()
        finally:
            conn.close()

    # --- Embedding ---

    def _embed_sync(self, text: str) -> list[float] | None:
        timeout = 30 if self._ollama_ok is None else 5
        try:
            payload = json.dumps({"model": EMBED_MODEL, "prompt": text[:4096]}).encode()
            req = Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
                emb = result.get("embedding")
                if emb and len(emb) == EMBED_DIM:
                    self._ollama_ok = True
                    return emb
            return None
        except Exception:
            if self._ollama_ok is None:
                self._ollama_ok = False
            return None

    async def _embed(self, text: str) -> list[float] | None:
        return await asyncio.to_thread(self._embed_sync, text)

    # --- Device Info ---

    def _get_device_info(self) -> tuple[str, str]:
        device_id = os.environ.get("MEMORY_DEVICE_ID", "").strip()
        device_name_override = os.environ.get("MEMORY_DEVICE_NAME", "").strip()

        device_id_file = os.path.expanduser("~/.gitmas/identity/device-id")
        if not device_id and os.path.exists(device_id_file):
            with open(device_id_file) as f:
                device_id = f.read().strip()

        # Manual override wins. One-line file like `android-wm-v2`.
        name_file = os.path.expanduser("~/.gitmas/identity/device-name")
        if device_name_override:
            return device_id, device_name_override
        if os.path.exists(name_file):
            try:
                with open(name_file) as f:
                    n = f.read().strip()
                if n:
                    return device_id, n
            except OSError:
                pass

        device_name = ""
        try:
            device_name = subprocess.check_output(
                ["scutil", "--get", "ComputerName"], text=True, timeout=2
            ).strip()
        except Exception:
            pass
        if not device_name:
            import socket
            try:
                device_name = socket.gethostname()
            except Exception:
                device_name = ""
            # proot chroots report "localhost" — unhelpful, prefer empty so
            # the caller or override file fills it.
            if device_name.startswith("localhost"):
                device_name = ""
        return device_id, device_name

    # --- Structured Memories (MCP interface) ---

    def _resolve_writer_identity(self, source: str,
                                 agent_id: str | None = None,
                                 layer: str | None = None) -> tuple[str, str, str | None]:
        """Return canonical (source, agent_id, layer) for audit provenance.

        `source` should identify the real agent/client. Generic transport
        names such as mcp/voice/cli are preserved as `layer` when an explicit
        MEMORY_SOURCE is available.
        """
        generic_layers = {"mcp", "voice", "cli", "api", "http", "server", "remote"}
        env_source = os.environ.get("MEMORY_SOURCE", "").strip()
        env_agent_id = os.environ.get("MEMORY_AGENT_ID", "").strip()
        env_layer = os.environ.get("MEMORY_LAYER", "").strip()

        file_agent_id = ""
        for p in ("~/.gitmas/identity/agent-id", "~/.claude/memory-agent-id"):
            try:
                file_agent_id = open(os.path.expanduser(p), "r").read().strip()
                if file_agent_id:
                    break
            except OSError:
                pass

        resolved_source = (source or env_source or "cli").strip()
        resolved_layer = (layer or env_layer or "").strip() or None
        if resolved_source in generic_layers and env_source and env_source not in generic_layers:
            resolved_layer = resolved_layer or resolved_source
            resolved_source = env_source

        resolved_agent_id = (agent_id or env_agent_id or file_agent_id or "").strip()
        if resolved_source in generic_layers and not env_source and resolved_agent_id:
            low_agent = resolved_agent_id.lower()
            if "pi-agent" in low_agent or "pi_agent" in low_agent:
                resolved_layer = resolved_layer or resolved_source
                resolved_source = "pi-agent"
            elif "codex" in low_agent:
                resolved_layer = resolved_layer or resolved_source
                resolved_source = "codex"
            elif "claude" in low_agent:
                resolved_layer = resolved_layer or resolved_source
                resolved_source = "claude-code"
        if not resolved_agent_id:
            device_id, device_name = self._get_device_info()
            device = device_id or device_name or os.environ.get("MEMORY_DEVICE_ROLE", "device")
            resolved_agent_id = f"{device}-{resolved_source}"

        return resolved_source, resolved_agent_id, resolved_layer

    def store(self, content: str, description: str, type: str,
              tags: list[str] | None = None, session_id: str | None = None,
              source: str = "cli",
              scope: str = "global", owner_id: str | None = None,
              project_id: str | None = None,
              visibility: str = "all",
              agent_id: str | None = None,
              layer: str | None = None) -> str:
        mem_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        device_id, device_name = self._get_device_info()
        source, agent_id, layer = self._resolve_writer_identity(source, agent_id, layer)
        embedding = self._embed_sync(content)
        hrr = _hrr_encode(content)

        conn, vec_ok = self._connect()
        try:
            conn.execute(
                """INSERT INTO memories(id, type, description, content, session_id,
                   source, device_id, device_name, tags, created, hrr_vector,
                   scope, owner_id, project_id, visibility, agent_id, layer)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mem_id, type, description, content, session_id,
                 source, device_id, device_name, json.dumps(tags or []), now, hrr,
                 scope, owner_id, project_id, visibility, agent_id, layer),
            )
            if vec_ok and embedding:
                conn.execute(
                    "INSERT INTO memories_vec(rowid, embedding) VALUES ((SELECT rowid FROM memories WHERE id=?), ?)",
                    (mem_id, _vec_to_blob(embedding)),
                )
            conn.execute(
                "INSERT INTO memories_fts(rowid, content, description) VALUES ((SELECT rowid FROM memories WHERE id=?), ?, ?)",
                (mem_id, content, description),
            )
            conn.commit()
        finally:
            conn.close()

        self._queue_remote_sync(mem_id, content, description, type, "store")
        return mem_id

    def _build_scope_clause(self, agent_id: str | None = None,
                            user_id: str | None = None,
                            project_id: str | None = None,
                            user_role: str | None = None,
                            extra_project_ids: set[str] | None = None) -> str:
        """Build SQL WHERE clause for scope-aware filtering.

        Access rules by user role:
          owner:        sees everything (no filter)
          collaborator: global + assigned projects + own user scope
          viewer:       global + assigned projects (read-only)

        An agent sees:
          1. Global memories (scope='global')
          2. Project memories for their project (scope='project', project_id=X)
          3. Their own private memories (scope='agent', owner_id=self)
          4. User memories for the current user (scope='user', owner_id=user)

        extra_project_ids widens (2) when the query mentions registered
        project names — search results then include those projects even
        if the caller didn't pass project_id explicitly.
        """
        # Resolve user role if not provided
        if user_role is None and user_id:
            user = self.get_user(user_id)
            user_role = user["role"] if user else None

        # Owner sees everything
        if user_role == "owner":
            return ""

        if not agent_id and not user_id:
            return ""  # No scope filtering

        parts = ["m.scope = 'global'"]
        all_projects: set[str] = set()
        if project_id:
            all_projects.add(project_id)
        if extra_project_ids:
            all_projects.update(extra_project_ids)
        # SQL-escape single quotes in project ids
        safe_projects = {p.replace("'", "''") for p in all_projects if p}
        if safe_projects:
            in_clause = ",".join(f"'{p}'" for p in safe_projects)
            parts.append(
                f"(m.scope = 'project' AND m.project_id IN ({in_clause}))"
            )
        if agent_id:
            parts.append(f"(m.scope = 'agent' AND m.owner_id = '{agent_id}')")
        if user_id:
            parts.append(f"(m.scope = 'user' AND m.owner_id = '{user_id}')")

        # Viewer cannot see agent-private memories
        if user_role == "viewer":
            parts = [p for p in parts if "scope = 'agent'" not in p]

        return f"AND ({' OR '.join(parts)})"

    def search(self, query: str, limit: int = 5, type: str | None = None,
               min_score: float = 0.35,
               agent_id: str | None = None, user_id: str | None = None,
               project_id: str | None = None,
               since: str | None = None, until: str | None = None,
               chronological: bool | None = None) -> list[dict]:
        """Hybrid semantic + keyword memory search.

        Time-aware extensions:
          since, until        ISO 8601 date/datetime — filter by m.created.
          chronological=None  auto-detect via temporal keywords in query;
                              True forces chronological (newest first) sort;
                              False keeps pure score ranking.

        Every returned hit carries `created` (ISO) and `age_human` (LT
        relative, e.g. "prieš 3 d") so the LLM can reason about when
        events actually happened.
        """
        where_parts = []
        if type:
            where_parts.append(f"AND m.type='{type}'")
        # Auto-widen scope when the query mentions registered project
        # names — otherwise scope='project' memories are filtered out
        # whenever the caller passes agent_id/user_id (default for MCP).
        extra_projects = self._match_projects_in_query(query)
        scope_clause = self._build_scope_clause(
            agent_id, user_id, project_id, extra_project_ids=extra_projects,
        )
        if scope_clause:
            where_parts.append(scope_clause)
        if since:
            where_parts.append(f"AND m.created >= '{_validate_iso(since)}'")
        if until:
            where_parts.append(f"AND m.created <= '{_validate_iso(until)}'")

        results = self._hybrid_search(
            query=query,
            main_table="memories",
            vec_table="memories_vec",
            fts_table="memories_fts",
            text_col="content",
            top_k=limit,
            min_score=min_score,
            where_clause=" ".join(where_parts),
            extra_cols="m.id as mem_id, m.type, m.description, m.tags, m.created, m.scope, m.owner_id, m.project_id, m.source, m.device_id, m.device_name, m.agent_id, m.layer",
        )

        # Chronological re-sort when the query is about time.
        if chronological is None:
            chronological = _is_temporal_query(query)
        if chronological:
            results.sort(key=lambda r: r.get("created") or "", reverse=True)

        # Stamp every result with a LT relative-time hint for LLM context.
        for r in results:
            if r.get("created"):
                r["age_human"] = _relative_time_lt(r["created"])

        # Telemetry — record top-hit score for drift detection.
        try:
            top_score = results[0]["score"] if results else None
            top_id = results[0].get("mem_id") if results else None
            conn, _ = self._connect()
            try:
                conn.execute(
                    "INSERT INTO search_log(timestamp, query, hit_count, "
                    "top_score, top_id) VALUES (?, ?, ?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(),
                     query[:500], len(results), top_score, top_id),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

        return results

    def quality_report(self, days: int = 7) -> dict:
        """Aggregate search_log over recent N days for drift detection.

        Returns avg/min/max top_score, search count, and a sample of the
        weakest recent queries (top_score < 0.5) — useful for spotting
        when search quality has regressed.
        """
        since = (datetime.now(timezone.utc).timestamp() - days * 86400)
        since_iso = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
        conn, _ = self._connect()
        try:
            agg = conn.execute(
                "SELECT COUNT(*) as n, AVG(top_score) as avg_score, "
                "MIN(top_score) as min_score, MAX(top_score) as max_score, "
                "AVG(hit_count) as avg_hits "
                "FROM search_log WHERE timestamp >= ? AND top_score IS NOT NULL",
                (since_iso,),
            ).fetchone()
            empties = conn.execute(
                "SELECT COUNT(*) as n FROM search_log "
                "WHERE timestamp >= ? AND hit_count = 0",
                (since_iso,),
            ).fetchone()
            weak = conn.execute(
                "SELECT query, top_score, timestamp FROM search_log "
                "WHERE timestamp >= ? AND top_score < 0.5 "
                "ORDER BY top_score ASC LIMIT 5",
                (since_iso,),
            ).fetchall()
        finally:
            conn.close()
        return {
            "window_days": days,
            "searches": agg["n"] or 0,
            "empty_searches": empties["n"] or 0,
            "avg_top_score": round(agg["avg_score"] or 0, 4),
            "min_top_score": round(agg["min_score"] or 0, 4),
            "max_top_score": round(agg["max_score"] or 0, 4),
            "avg_hit_count": round(agg["avg_hits"] or 0, 2),
            "weakest_queries": [
                {"query": w["query"], "score": round(w["top_score"], 4),
                 "timestamp": w["timestamp"]}
                for w in weak
            ],
        }

    def list_memories(self, type: str | None = None,
                      agent_id: str | None = None, user_id: str | None = None,
                      project_id: str | None = None) -> list[dict]:
        conn, _ = self._connect()
        try:
            sql = "SELECT id, type, description, created, tags, scope, owner_id, project_id, source, device_id, device_name, agent_id, layer FROM memories m WHERE deleted=0"
            params = []
            if type:
                sql += " AND type=?"
                params.append(type)
            scope_clause = self._build_scope_clause(agent_id, user_id, project_id)
            if scope_clause:
                sql += " " + scope_clause
            sql += " ORDER BY created DESC"
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update(self, id: str, content: str | None = None,
               description: str | None = None) -> bool:
        conn, vec_ok = self._connect()
        try:
            row = conn.execute("SELECT * FROM memories WHERE id=? AND deleted=0", (id,)).fetchone()
            if not row:
                return False

            now = datetime.now(timezone.utc).isoformat()
            new_content = content or row["content"]
            new_desc = description or row["description"]

            conn.execute(
                "UPDATE memories SET content=?, description=?, updated=? WHERE id=?",
                (new_content, new_desc, now, id),
            )

            # Re-embed if content changed
            if content:
                embedding = self._embed_sync(new_content)
                rowid = conn.execute("SELECT rowid FROM memories WHERE id=?", (id,)).fetchone()[0]
                if vec_ok and embedding:
                    conn.execute("DELETE FROM memories_vec WHERE rowid=?", (rowid,))
                    conn.execute(
                        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                        (rowid, _vec_to_blob(embedding)),
                    )
                conn.execute("DELETE FROM memories_fts WHERE rowid=?", (rowid,))
                conn.execute(
                    "INSERT INTO memories_fts(rowid, content, description) VALUES (?, ?, ?)",
                    (rowid, new_content, new_desc),
                )

            conn.commit()
        finally:
            conn.close()

        self._queue_remote_sync(id, new_content, new_desc, row["type"], "update")
        return True

    def delete(self, id: str) -> bool:
        conn, _ = self._connect()
        try:
            row = conn.execute("SELECT * FROM memories WHERE id=? AND deleted=0", (id,)).fetchone()
            if not row:
                return False
            conn.execute("UPDATE memories SET deleted=1, updated=? WHERE id=?",
                         (datetime.now(timezone.utc).isoformat(), id))
            conn.commit()
        finally:
            conn.close()

        self._queue_remote_sync(id, "", row["description"], row["type"], "delete")
        return True

    # --- Agent Messages ---

    def send_message(self, from_agent: str, content: str,
                     message_type: str = "broadcast",
                     to_agent: str | None = None,
                     project_id: str | None = None,
                     priority: str = "normal") -> int:
        """Send a message from one agent to another (or broadcast).

        Args:
            from_agent: Sender agent ID.
            content: Message text.
            message_type: 'request', 'response', 'broadcast', 'alert'.
            to_agent: Recipient agent ID, or None for broadcast.
            project_id: Scope to a project (broadcasts go to project agents).
            priority: 'low', 'normal', 'high', 'urgent'.

        Returns:
            Message ID.
        """
        now = datetime.now(timezone.utc).isoformat()
        conn, _ = self._connect()
        try:
            cur = conn.execute(
                """INSERT INTO agent_messages(from_agent, to_agent, project_id,
                   message_type, content, status, priority, created)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (from_agent, to_agent, project_id, message_type, content,
                 priority, now),
            )
            msg_id = cur.lastrowid
            conn.commit()
            return msg_id
        finally:
            conn.close()

    def get_pending_messages(self, agent_id: str,
                            project_id: str | None = None,
                            include_broadcasts: bool = True,
                            limit: int = 50) -> list[dict]:
        """Get unread messages for an agent.

        Returns direct messages + broadcasts for the agent's project.
        Results are capped so a large mailbox cannot flood an LLM context.
        """
        conn, _ = self._connect()
        try:
            conditions = ["status = 'pending'"]
            params = []

            # Direct messages to this agent
            direct = f"to_agent = ?"
            params.append(agent_id)

            if include_broadcasts and project_id:
                # Broadcasts: to_agent is NULL and project matches
                broadcast = f"(to_agent IS NULL AND project_id = ?)"
                params.append(project_id)
                conditions.append(f"({direct} OR {broadcast})")
            elif include_broadcasts:
                # Broadcasts with no project filter
                broadcast = "to_agent IS NULL"
                conditions.append(f"({direct} OR {broadcast})")
            else:
                conditions.append(direct)

            sql = f"""SELECT id, from_agent, to_agent, project_id, message_type,
                             content, priority, created
                      FROM agent_messages
                      WHERE {' AND '.join(conditions)}
                      ORDER BY
                        CASE priority
                          WHEN 'urgent' THEN 0
                          WHEN 'high' THEN 1
                          WHEN 'normal' THEN 2
                          WHEN 'low' THEN 3
                        END,
                        created ASC
                      LIMIT ?"""
            params.append(max(1, min(int(limit), 500)))
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_message_read(self, message_id: int) -> bool:
        """Mark a message as read."""
        now = datetime.now(timezone.utc).isoformat()
        conn, _ = self._connect()
        try:
            changed = conn.execute(
                "UPDATE agent_messages SET status='read', read_at=? WHERE id=? AND status='pending'",
                (now, message_id),
            ).rowcount
            conn.commit()
            return changed > 0
        finally:
            conn.close()

    def respond_to_message(self, message_id: int, from_agent: str,
                           content: str, priority: str = "normal") -> int | None:
        """Respond to a message — marks original as acted_on and creates response."""
        conn, _ = self._connect()
        try:
            original = conn.execute(
                "SELECT * FROM agent_messages WHERE id=?", (message_id,)
            ).fetchone()
            if not original:
                return None

            now = datetime.now(timezone.utc).isoformat()
            # Mark original as acted on
            conn.execute(
                "UPDATE agent_messages SET status='acted_on', responded_at=? WHERE id=?",
                (now, message_id),
            )
            # Create response
            cur = conn.execute(
                """INSERT INTO agent_messages(from_agent, to_agent, project_id,
                   message_type, content, status, priority, created)
                   VALUES (?, ?, ?, 'response', ?, 'pending', ?, ?)""",
                (from_agent, original["from_agent"], original["project_id"],
                 content, priority, now),
            )
            resp_id = cur.lastrowid
            conn.commit()
            return resp_id
        finally:
            conn.close()

    # --- Agent Registry ---

    def register_agent(self, id: str, name: str, role: str | None = None,
                       capabilities: list[str] | None = None,
                       parent_id: str | None = None,
                       created_by: str | None = None,
                       config: dict | None = None) -> str:
        """Register a new agent. Returns agent ID."""
        now = datetime.now(timezone.utc).isoformat()
        conn, _ = self._connect()
        try:
            conn.execute(
                """INSERT INTO agents(id, name, role, capabilities,
                   parent_id, created_by, status, config, created)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (id, name, role, json.dumps(capabilities or []),
                 parent_id, created_by, json.dumps(config or {}), now),
            )
            conn.commit()
            return id
        finally:
            conn.close()

    def get_agent(self, agent_id: str) -> dict | None:
        conn, _ = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_agents(self, status: str | None = None,
                    parent_id: str | None = None) -> list[dict]:
        conn, _ = self._connect()
        try:
            sql = "SELECT * FROM agents WHERE 1=1"
            params = []
            if status:
                sql += " AND status=?"
                params.append(status)
            if parent_id is not None:
                sql += " AND parent_id=?"
                params.append(parent_id)
            sql += " ORDER BY created"
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_agent_status(self, agent_id: str, status: str) -> bool:
        """Update agent status: active, paused, archived."""
        conn, _ = self._connect()
        try:
            changed = conn.execute(
                "UPDATE agents SET status=? WHERE id=?", (status, agent_id),
            ).rowcount
            conn.commit()
            return changed > 0
        finally:
            conn.close()

    def create_sub_agent(self, parent_id: str, name: str, role: str,
                         capabilities: list[str] | None = None,
                         project_id: str | None = None,
                         config: dict | None = None) -> str:
        """Create a sub-agent under a parent agent.

        Sub-agent inherits:
          - Read access to parent's project memory
          - Own private memory scope
          - NO access to parent's private memory

        The sub-agent is auto-assigned to the project if provided.
        Returns sub-agent ID.
        """
        sub_id = f"{parent_id}-{name.lower().replace(' ', '-')}"

        # Inherit capabilities subset from parent if not specified
        if capabilities is None:
            parent = self.get_agent(parent_id)
            if parent:
                capabilities = json.loads(parent.get("capabilities", "[]"))

        self.register_agent(
            id=sub_id, name=name, role=role,
            capabilities=capabilities,
            parent_id=parent_id, created_by=parent_id,
            config=config,
        )

        # Assign to project
        if project_id:
            self.assign_agent_to_project(sub_id, project_id)

        return sub_id

    def assign_agent_to_project(self, agent_id: str, project_id: str) -> bool:
        """Add an agent to a project's assigned_agents list."""
        conn, _ = self._connect()
        try:
            row = conn.execute(
                "SELECT assigned_agents FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not row:
                return False
            agents = json.loads(row["assigned_agents"] or "[]")
            if agent_id not in agents:
                agents.append(agent_id)
                conn.execute(
                    "UPDATE projects SET assigned_agents=? WHERE id=?",
                    (json.dumps(agents), project_id),
                )
                conn.commit()
            return True
        finally:
            conn.close()

    def unassign_agent_from_project(self, agent_id: str, project_id: str) -> bool:
        """Remove an agent from a project's assigned_agents list."""
        conn, _ = self._connect()
        try:
            row = conn.execute(
                "SELECT assigned_agents FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not row:
                return False
            agents = json.loads(row["assigned_agents"] or "[]")
            if agent_id in agents:
                agents.remove(agent_id)
                conn.execute(
                    "UPDATE projects SET assigned_agents=? WHERE id=?",
                    (json.dumps(agents), project_id),
                )
                conn.commit()
            return True
        finally:
            conn.close()

    def archive_agent(self, agent_id: str) -> bool:
        """Archive an agent and unassign from all projects."""
        if not self.update_agent_status(agent_id, "archived"):
            return False
        # Unassign from all projects
        conn, _ = self._connect()
        try:
            rows = conn.execute("SELECT id, assigned_agents FROM projects").fetchall()
            for row in rows:
                agents = json.loads(row["assigned_agents"] or "[]")
                if agent_id in agents:
                    agents.remove(agent_id)
                    conn.execute(
                        "UPDATE projects SET assigned_agents=? WHERE id=?",
                        (json.dumps(agents), row["id"]),
                    )
            conn.commit()
        finally:
            conn.close()
        return True

    # --- User Registry ---

    def register_user(self, id: str, name: str, role: str = "collaborator",
                      aliases: list[str] | None = None,
                      preferences: dict | None = None) -> str:
        """Register a new user. Returns user ID."""
        now = datetime.now(timezone.utc).isoformat()
        conn, _ = self._connect()
        try:
            conn.execute(
                """INSERT INTO users(id, name, role, aliases, preferences, created)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (id, name, role, json.dumps(aliases or []),
                 json.dumps(preferences or {}), now),
            )
            conn.commit()
            return id
        finally:
            conn.close()

    def get_user(self, user_id: str) -> dict | None:
        conn, _ = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_users(self, role: str | None = None) -> list[dict]:
        conn, _ = self._connect()
        try:
            sql = "SELECT * FROM users WHERE 1=1"
            params = []
            if role:
                sql += " AND role=?"
                params.append(role)
            sql += " ORDER BY created"
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_user_preferences(self, user_id: str,
                                preferences: dict | None = None,
                                role: str | None = None) -> bool:
        """Update user preferences and/or role."""
        conn, _ = self._connect()
        try:
            user = conn.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if not user:
                return False
            if preferences is not None:
                existing = json.loads(user["preferences"] or "{}")
                existing.update(preferences)
                conn.execute(
                    "UPDATE users SET preferences=? WHERE id=?",
                    (json.dumps(existing), user_id),
                )
            if role is not None:
                conn.execute(
                    "UPDATE users SET role=? WHERE id=?", (role, user_id),
                )
            conn.commit()
            return True
        finally:
            conn.close()

    def resolve_user_alias(self, alias: str) -> str | None:
        """Resolve a user alias to a user ID."""
        alias_lower = alias.lower()
        conn, _ = self._connect()
        try:
            # Direct ID match
            row = conn.execute(
                "SELECT id FROM users WHERE id=?", (alias_lower,)
            ).fetchone()
            if row:
                return row["id"]
            # Search aliases
            rows = conn.execute("SELECT id, aliases FROM users").fetchall()
            for row in rows:
                user_aliases = json.loads(row["aliases"] or "[]")
                if alias_lower in [a.lower() for a in user_aliases]:
                    return row["id"]
            return None
        finally:
            conn.close()

    def get_user_projects(self, user_id: str) -> list[dict]:
        """Get projects a user has access to based on role."""
        user = self.get_user(user_id)
        if not user:
            return []
        conn, _ = self._connect()
        try:
            if user["role"] == "owner":
                rows = conn.execute(
                    "SELECT * FROM projects ORDER BY created"
                ).fetchall()
            else:
                # Collaborators/viewers see projects they own or are assigned to
                rows = conn.execute(
                    "SELECT * FROM projects WHERE owner_user=? ORDER BY created",
                    (user_id,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # --- Conversation Turns ---

    # Chunk large turns for better semantic retrieval
    CHUNK_SIZE = 800       # chars per chunk (~150 words)
    CHUNK_OVERLAP = 100    # overlap between chunks for context continuity

    def _chunk_text(self, text: str) -> list[str]:
        """Split long text into overlapping chunks for better embedding coverage."""
        if len(text) <= self.CHUNK_SIZE:
            return [text]
        chunks = []
        start = 0
        while start < len(text):
            end = start + self.CHUNK_SIZE
            chunk = text[start:end]
            # Try to break at sentence boundary
            if end < len(text):
                for sep in (". ", "! ", "? ", "\n", ", "):
                    last = chunk.rfind(sep)
                    if last > self.CHUNK_SIZE // 2:
                        chunk = chunk[:last + len(sep)]
                        end = start + len(chunk)
                        break
            chunks.append(chunk.strip())
            start = end - self.CHUNK_OVERLAP
        return [c for c in chunks if len(c) >= 20]

    async def add_turn(self, session_id, role: str, text: str,
                       session_type: str = "voice"):
        if not text or len(text.strip()) < 5:
            return

        chunks = self._chunk_text(text)

        for chunk in chunks:
            embedding = await self._embed(chunk)

            hrr = _hrr_encode(chunk)

            def _write(ch=chunk, emb=embedding, hrr_vec=hrr):
                conn, vec_ok = self._connect()
                try:
                    cur = conn.execute(
                        "INSERT INTO turns(session_id, session_type, role, text, timestamp, hrr_vector) VALUES (?, ?, ?, ?, ?, ?)",
                        (str(session_id), session_type, role, ch, datetime.now().isoformat(), hrr_vec),
                    )
                    tid = cur.lastrowid
                    if vec_ok and emb:
                        conn.execute(
                            "INSERT INTO turns_vec(rowid, embedding) VALUES (?, ?)",
                            (tid, _vec_to_blob(emb)),
                        )
                    conn.execute(
                        "INSERT INTO turns_fts(rowid, text) VALUES (?, ?)",
                        (tid, ch),
                    )
                    conn.commit()
                finally:
                    conn.close()

            await asyncio.to_thread(_write)

    async def search_turns(self, query: str, exclude_last_n: int = 12,
                           exclude_session: str = None,
                           top_k: int = 8, min_score: float = 0.10) -> list[dict]:
        if not query or len(query.strip()) < 3:
            return []

        # Get IDs to exclude — either by session or by global last N
        exclude_ids = set()
        conn, _ = self._connect()
        try:
            if exclude_session:
                # Exclude all turns from the current session
                rows = conn.execute(
                    "SELECT id FROM turns WHERE session_id=?", (exclude_session,)
                ).fetchall()
                exclude_ids = {row["id"] for row in rows}
            elif exclude_last_n > 0:
                rows = conn.execute(
                    "SELECT id FROM turns ORDER BY id DESC LIMIT ?", (exclude_last_n,)
                ).fetchall()
                exclude_ids = {row["id"] for row in rows}
        finally:
            conn.close()

        results = self._hybrid_search(
            query=query,
            main_table="turns",
            vec_table="turns_vec",
            fts_table="turns_fts",
            text_col="text",
            top_k=top_k,
            min_score=min_score,
            exclude_ids=exclude_ids,
            extra_cols="m.id, m.role, m.session_id",
        )
        return results

    # --- Session Notes ---

    def save_note(self, session_id: str, content: str,
                  note_type: str = "manual", session_type: str = "voice"):
        conn, vec_ok = self._connect()
        try:
            # Dedup: check if exact note already exists for this session
            existing = conn.execute(
                "SELECT id FROM session_notes WHERE session_id=? AND content=? AND note_type=?",
                (session_id, content, note_type),
            ).fetchone()
            if existing:
                return existing["id"]

            hrr = _hrr_encode(content)
            cur = conn.execute(
                "INSERT INTO session_notes(session_id, session_type, note_type, content, created, hrr_vector) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, session_type, note_type, content, datetime.now().isoformat(), hrr),
            )
            nid = cur.lastrowid

            # Index into FTS
            conn.execute(
                "INSERT INTO notes_fts(rowid, content) VALUES (?, ?)",
                (nid, content),
            )

            # Index into vector table (embedding done async by caller if needed)
            if vec_ok:
                emb = self._embed_sync(content)
                if emb:
                    conn.execute(
                        "INSERT INTO notes_vec(rowid, embedding) VALUES (?, ?)",
                        (nid, _vec_to_blob(emb)),
                    )

            conn.commit()
            return nid
        finally:
            conn.close()

    def load_notes(self, session_id: str) -> tuple[list[str], list[str]]:
        conn, _ = self._connect()
        try:
            rows = conn.execute(
                "SELECT content, note_type FROM session_notes WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
            manual = [r["content"] for r in rows if r["note_type"] == "manual"]
            auto = [r["content"] for r in rows if r["note_type"] == "auto"]
            return manual, auto
        finally:
            conn.close()

    def search_notes(self, query: str, top_k: int = 5,
                     min_score: float = 0.10) -> list[dict]:
        """Search session notes across all sessions using hybrid search."""
        if not query or len(query.strip()) < 3:
            return []
        results = self._hybrid_search(
            query=query,
            main_table="session_notes",
            vec_table="notes_vec",
            fts_table="notes_fts",
            text_col="content",
            top_k=top_k,
            min_score=min_score,
            extra_cols="m.id, m.session_id, m.note_type",
        )
        return results

    def get_latest_session_id(self, session_type: str = "voice") -> str | None:
        conn, _ = self._connect()
        try:
            row = conn.execute(
                "SELECT DISTINCT session_id FROM session_notes WHERE session_type=? ORDER BY id DESC LIMIT 1",
                (session_type,),
            ).fetchone()
            return row["session_id"] if row else None
        finally:
            conn.close()

    def promote_note(self, note_id: int, type: str = "reference") -> str | None:
        conn, _ = self._connect()
        try:
            row = conn.execute("SELECT * FROM session_notes WHERE id=?", (note_id,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE session_notes SET promoted=1 WHERE id=?", (note_id,))
            conn.commit()
        finally:
            conn.close()
        return self.store(row["content"], row["content"][:80], type, source="voice")

    # --- Entity Resolution ---

    def resolve_query(self, query: str) -> str:
        """Expand entity aliases in query for better search coverage.
        Uses self._alias_map (hardcoded ENTITY_ALIASES + DB entities table)."""
        words = query.lower().split()
        expanded = set(words)
        alias_map = self._alias_map or _ALIAS_TO_CANONICAL
        for i in range(len(words)):
            # Check 1-word, 2-word, 3-word ngrams
            for n in (3, 2, 1):
                if i + n <= len(words):
                    ngram = " ".join(words[i:i+n])
                    canonical = alias_map.get(ngram)
                    if canonical:
                        expanded.update(canonical.lower().split())
        if expanded != set(words):
            return query + " " + " ".join(expanded - set(words))
        return query

    # --- Trust & Helpful ---

    def mark_helpful(self, id: str) -> bool:
        """Mark a memory as helpful — increases trust score."""
        conn, _ = self._connect()
        try:
            row = conn.execute("SELECT trust_score, helpful_count FROM memories WHERE id=?", (id,)).fetchone()
            if not row:
                return False
            cur_trust = row["trust_score"] if row["trust_score"] is not None else 0.5
            new_trust = min(1.0, cur_trust + 0.05)
            conn.execute(
                "UPDATE memories SET helpful_count=?, trust_score=? WHERE id=?",
                ((row["helpful_count"] or 0) + 1, new_trust, id),
            )
            conn.commit()
        finally:
            conn.close()
        return True

    def mark_canonical(self, id: str, canonical: bool = True) -> bool:
        """Add or remove the `canonical` tag on a memory.
        Canonical memories get a CANONICAL_BOOST during search ranking
        AND are exempt from temporal decay. Tag change is also pushed
        to gitmas.com so other devices see the same curation."""
        conn, _ = self._connect()
        row_meta = None
        try:
            row = conn.execute(
                "SELECT tags, content, description, type FROM memories WHERE id=?",
                (id,),
            ).fetchone()
            if not row:
                return False
            try:
                tags = json.loads(row["tags"] or "[]")
            except Exception:
                tags = []
            if canonical and CANONICAL_TAG not in tags:
                tags.append(CANONICAL_TAG)
            elif not canonical and CANONICAL_TAG in tags:
                tags = [t for t in tags if t != CANONICAL_TAG]
            else:
                return True  # Already in desired state.
            conn.execute(
                "UPDATE memories SET tags=? WHERE id=?",
                (json.dumps(tags), id),
            )
            conn.commit()
            row_meta = (row["content"], row["description"], row["type"])
        finally:
            conn.close()
        # Push the change so canonical curation propagates to all devices.
        if row_meta is not None:
            self._queue_remote_sync(id, row_meta[0], row_meta[1], row_meta[2], "update")
        return True

    def mark_irrelevant(self, id: str) -> bool:
        """Mark a memory as irrelevant — decreases trust score (floor 0.0).
        Inverse of mark_helpful; lets search learn from misses, not just hits."""
        conn, _ = self._connect()
        try:
            row = conn.execute(
                "SELECT trust_score, irrelevant_count FROM memories WHERE id=?", (id,)
            ).fetchone()
            if not row:
                return False
            cur_trust = row["trust_score"] if row["trust_score"] is not None else 0.5
            new_trust = max(0.0, cur_trust - 0.05)
            conn.execute(
                "UPDATE memories SET irrelevant_count=?, trust_score=? WHERE id=?",
                ((row["irrelevant_count"] or 0) + 1, new_trust, id),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    # --- Hybrid Search ---

    def _hybrid_search(self, query: str, main_table: str, vec_table: str,
                       fts_table: str, text_col: str, top_k: int = 5,
                       min_score: float = 0.35, exclude_ids: set | None = None,
                       where_clause: str = "", extra_cols: str = "") -> list[dict]:

        # Entity resolution — expand aliases in query
        expanded_query = self.resolve_query(query)

        # Project routing — match full names AND compound hyphen-split
        # forms ("voice-pipeline" matches "voice pipeline tts").
        matched_projects = self._match_projects_in_query(query)
        # query_word_set still needed for description-match boost.
        query_word_set = set(re.findall(r"\b\w+\b", expanded_query.lower()))

        query_emb = self._embed_sync(expanded_query)
        query_hrr = _hrr_encode(expanded_query)
        has_ollama = query_emb is not None
        exclude_ids = exclude_ids or set()
        conn, vec_ok = self._connect()
        hits: dict[int, dict] = {}

        def _extract_extra(row):
            data = {}
            for col in extra_cols.split(", "):
                col_name = col.split(" as ")[-1].strip() if " as " in col else col.split(".")[-1].strip()
                data[col_name] = row[col_name]
            return data

        try:
            # Vector search (BGE-M3 via Ollama)
            if vec_ok and query_emb:
                try:
                    rows = conn.execute(
                        f"""SELECT v.rowid, v.distance, m.{text_col}, {extra_cols}
                            FROM {vec_table} v
                            JOIN {main_table} m ON m.rowid = v.rowid
                            WHERE v.embedding MATCH ?
                            AND k = {top_k * 3}
                            {where_clause}""",
                        (_vec_to_blob(query_emb),),
                    ).fetchall()
                    rank_idx = 0
                    for row in rows:
                        rid = row["rowid"]
                        if rid in exclude_ids:
                            continue
                        rank_idx += 1
                        score = max(0.0, 1.0 - float(row["distance"]))
                        hit = {"rowid": rid, text_col: row[text_col],
                               "vec": score, "fts": 0.0, "hrr": 0.0,
                               "vec_rank": rank_idx, "fts_rank": None, "hrr_rank": None}
                        hit.update(_extract_extra(row))
                        hits[rid] = hit
                except Exception:
                    pass

            # HRR search — always-on co-ranker. Phase-vector similarity
            # complements vec (semantic) and fts (keyword). When Ollama is
            # down it's the only semantic signal; when up it breaks ties
            # and surfaces hits the other two rankers miss.
            if query_hrr:
                try:
                    # Brute-force HRR sim is O(N) — cap at 1000 rows,
                    # ordered by recency + trust so a 90-day-old high-value
                    # memory still ranks against a fresh trivial one.
                    has_trust_cols = main_table == "memories"
                    order_clause = (
                        "ORDER BY CASE WHEN m.created > datetime('now', '-30 day') "
                        "THEN 0 ELSE 1 END, "
                        "m.trust_score DESC, m.created DESC"
                        if has_trust_cols
                        else "ORDER BY m.rowid DESC"
                    )
                    rows = conn.execute(
                        f"SELECT m.rowid, m.{text_col}, m.hrr_vector, {extra_cols} "
                        f"FROM {main_table} m "
                        f"WHERE m.hrr_vector IS NOT NULL {where_clause} "
                        f"{order_clause} LIMIT 1000",
                    ).fetchall()
                    hrr_scores = []
                    for row in rows:
                        rid = row["rowid"]
                        if rid in exclude_ids:
                            continue
                        sim = _hrr_similarity(query_hrr, row["hrr_vector"])
                        hrr_scores.append((rid, row, max(0.0, sim)))
                    # Keep top candidates
                    hrr_scores.sort(key=lambda x: x[2], reverse=True)
                    for rank_idx, (rid, row, sim) in enumerate(hrr_scores[:top_k * 3], start=1):
                        if rid in hits:
                            hits[rid]["hrr"] = sim
                            hits[rid]["hrr_rank"] = rank_idx
                        else:
                            hit = {"rowid": rid, text_col: row[text_col],
                                   "vec": 0.0, "fts": 0.0, "hrr": sim,
                                   "vec_rank": None, "fts_rank": None, "hrr_rank": rank_idx}
                            hit.update(_extract_extra(row))
                            hits[rid] = hit
                except Exception:
                    pass

            # FTS5 keyword search
            try:
                words = re.findall(r'\b\w{3,}\b', expanded_query.lower())
                # Prefix matching with stem truncation for Lithuanian morphology
                fts_terms = []
                for w in words:
                    if len(w) >= 6:
                        fts_terms.append(w[:5] + "*")  # truncate to stem + wildcard
                    elif len(w) >= 4:
                        fts_terms.append(w + "*")
                    else:
                        fts_terms.append(w)
                fts_query = " OR ".join(fts_terms) if fts_terms else expanded_query
                rows = conn.execute(
                    f"""SELECT f.rowid, bm25({fts_table}) AS rank,
                               m.{text_col}, {extra_cols}
                        FROM {fts_table} f
                        JOIN {main_table} m ON m.rowid = f.rowid
                        WHERE {fts_table} MATCH ?
                        {where_clause}
                        ORDER BY rank LIMIT {top_k * 3}""",
                    (fts_query,),
                ).fetchall()
                rank_idx = 0
                for row in rows:
                    rid = row["rowid"]
                    if rid in exclude_ids:
                        continue
                    rank_idx += 1
                    fts_score = min(1.0, abs(float(row["rank"])) / 10.0)
                    if rid in hits:
                        hits[rid]["fts"] = fts_score
                        hits[rid]["fts_rank"] = rank_idx
                    else:
                        hit = {"rowid": rid, text_col: row[text_col],
                               "vec": 0.0, "fts": fts_score, "hrr": 0.0,
                               "vec_rank": None, "fts_rank": rank_idx, "hrr_rank": None}
                        hit.update(_extract_extra(row))
                        hits[rid] = hit
            except Exception:
                pass

            # Force-include canonical memories matching the query's
            # project(s). When matched_projects is set, fetch every
            # canonical-tagged memory whose project_id (or a tag) matches
            # and inject into hits with a synthetic HRR rank at the
            # top_k boundary. This bypasses the retrieval gap: canonical
            # docs that vec/fts/hrr missed still get scored, and their
            # canonical + project boosts can lift them. Only applies to
            # the memories table; turns/notes don't carry canonical tags.
            if main_table == "memories" and matched_projects:
                try:
                    # extra_cols already contains m.tags and m.project_id,
                    # so don't list them again or SQLite errors on dupes.
                    # Force-include bypasses the scope filter: a canonical
                    # tagged for the queried project may live under a
                    # different project_id (e.g. cac821cf is project=infra
                    # but tagged `hermes`). The project/tag match below is
                    # the access gate.
                    forced_rows = conn.execute(
                        f"""SELECT m.rowid, m.{text_col}, {extra_cols}
                            FROM {main_table} m
                            WHERE m.deleted = 0""",
                    ).fetchall()
                    synthetic_rank = max(1, top_k)
                    for row in forced_rows:
                        rid = row["rowid"]
                        if rid in exclude_ids:
                            continue
                        try:
                            row_tags = json.loads(row["tags"] or "[]")
                        except Exception:
                            row_tags = []
                        if CANONICAL_TAG not in row_tags:
                            continue
                        row_pid = (row["project_id"] or "").lower()
                        row_tags_lower = {t.lower() for t in row_tags}
                        if not (row_pid in matched_projects
                                or row_tags_lower & matched_projects):
                            continue
                        # Synthetic rank across all 3 rankers at the
                        # top_k boundary — gives base RRF ≈ 1.0. If the
                        # canonical was already pulled in by natural
                        # retrieval at a lower rank, UPGRADE its ranks
                        # rather than skip it — the explicit canonical
                        # tag overrides the organic signal.
                        existing = hits.get(rid)
                        if existing:
                            for rf in ("vec_rank", "fts_rank", "hrr_rank"):
                                cur = existing.get(rf)
                                if cur is None or cur > synthetic_rank:
                                    existing[rf] = synthetic_rank
                            existing["_forced"] = True
                        else:
                            hit = {"rowid": rid, text_col: row[text_col],
                                   "vec": 0.0, "fts": 0.0, "hrr": 0.0,
                                   "vec_rank": synthetic_rank,
                                   "fts_rank": synthetic_rank,
                                   "hrr_rank": synthetic_rank,
                                   "_forced": True}
                            hit.update(_extract_extra(row))
                            hits[rid] = hit
                except Exception:
                    pass

            # Fetch trust_score, created, retrieval/helpful counts, tags
            has_trust = main_table == "memories"
            if has_trust:
                for rid, hit in hits.items():
                    try:
                        row = conn.execute(
                            "SELECT trust_score, type, created, retrieval_count, "
                            "helpful_count, irrelevant_count, tags "
                            "FROM memories WHERE rowid=?",
                            (rid,),
                        ).fetchone()
                        if row:
                            hit["_trust"] = row["trust_score"] if row["trust_score"] is not None else 0.5
                            hit["_type"] = row["type"]
                            hit["_created"] = row["created"]
                            hit["_retrieval"] = row["retrieval_count"] or 0
                            hit["_helpful"] = row["helpful_count"] or 0
                            hit["_irrelevant"] = row["irrelevant_count"] or 0
                            try:
                                hit["_tags"] = json.loads(row["tags"] or "[]")
                            except Exception:
                                hit["_tags"] = []
                    except Exception:
                        hit["_trust"] = 0.5

                # Increment retrieval_count for returned results
                for rid in hits:
                    try:
                        conn.execute(
                            "UPDATE memories SET retrieval_count = COALESCE(retrieval_count, 0) + 1 WHERE rowid=?",
                            (rid,),
                        )
                    except Exception:
                        pass
                conn.commit()

        finally:
            conn.close()

        # Reciprocal Rank Fusion base score, normalized to [0,1].
        # Active rankers = those that returned ≥1 hit. With HRR always-on
        # plus vec (Ollama up) + fts, that's typically 3; on Ollama-down
        # it's 2 (hrr + fts). max_rrf normalizes so a hit at rank 1 in
        # every active ranker scores exactly 1.0.
        RRF_K = 60
        active_rankers = sum([
            any(h.get("vec_rank") for h in hits.values()),
            any(h.get("fts_rank") for h in hits.values()),
            any(h.get("hrr_rank") for h in hits.values()),
        ]) or 1
        max_rrf = active_rankers / (RRF_K + 1)
        for h in hits.values():
            rrf = 0.0
            if h.get("vec_rank"):
                rrf += 1.0 / (RRF_K + h["vec_rank"])
            if h.get("fts_rank"):
                rrf += 1.0 / (RRF_K + h["fts_rank"])
            if h.get("hrr_rank"):
                rrf += 1.0 / (RRF_K + h["hrr_rank"])
            h["_base"] = rrf / max_rrf

        # Floor on base (before trust/decay), so a high-trust old project memory
        # isn't double-penalized off the cliff by a weak-but-real semantic match.
        # Force-included canonicals bypass the floor — their whole purpose is
        # to enter the candidate set even when retrieval missed them.
        survivors = [
            h for h in hits.values()
            if h.get("_forced") or h["_base"] >= min_score
        ]

        now = datetime.now(timezone.utc)
        for h in survivors:
            if has_trust:
                # Effective trust = stored trust + boosts − penalties, clamped
                # to [0, 1]. Retrieval is implicit (small log boost). Helpful
                # is explicit positive. Irrelevant counterbalances retrieval —
                # otherwise a repeatedly-but-wrongly returned memory inflates.
                base_trust = h.get("_trust", 0.5)
                retrieval_boost = min(0.3, math.log1p(h.get("_retrieval", 0)) * 0.02)
                helpful_boost = min(0.2, h.get("_helpful", 0) * 0.05)
                irrelevant_penalty = min(0.4, h.get("_irrelevant", 0) * 0.05)
                effective_trust = max(0.0, min(
                    1.0,
                    base_trust + retrieval_boost + helpful_boost - irrelevant_penalty,
                ))
                # Range 0.85–1.0 (was 0.7–1.0, originally 0.5–1.0). Tighter
                # range means trust differences matter less for ranking, but
                # gives back the headroom so a fresh canonical hit can reach
                # ~92.5% of base by default and 100% when trust→1.0.
                trust = 0.85 + 0.15 * effective_trust
            else:
                trust = 1.0
            tags = h.get("_tags", [])
            is_canonical = has_trust and CANONICAL_TAG in tags
            decay = 1.0
            # Canonical memories are exempt from decay — they're curated and
            # don't go stale. Otherwise the standard type-aware exemption.
            if (has_trust and not is_canonical
                    and h.get("_type") not in NO_DECAY_TYPES):
                created_str = h.get("_created", "")
                if created_str:
                    try:
                        created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        age_days = max(0, (now - created_dt).days)
                        decay = 0.5 ** (age_days / HALF_LIFE_DAYS)
                    except Exception:
                        pass
            # Noise/echo penalties: auto-flush prose and session transcripts
            # both ride keyword density. NOISE (auto-flush) is the heavier
            # penalty because flush summaries match nearly any keyword query
            # against the session they captured.
            echo = 1.0
            if has_trust:
                if any(t in NOISE_TAGS for t in tags):
                    echo = NOISE_PENALTY
                elif any(t in ECHO_TAGS for t in tags):
                    echo = ECHO_PENALTY
            # Project routing — boost memories scoped to a project named
            # in the query (matches project_id OR a tag).
            project_boost = 1.0
            if matched_projects:
                pid = (h.get("project_id") or "").lower()
                if pid and pid in matched_projects:
                    project_boost = PROJECT_BOOST
                elif any(t.lower() in matched_projects for t in tags):
                    project_boost = PROJECT_BOOST
            # Canonical boost — curated arch docs Karl has marked. When
            # the query also matches this memory's project, lift further:
            # "official doc for the project named in the query" is the
            # strongest signal the system has.
            if is_canonical:
                canonical_boost = (
                    CANONICAL_PROJECT_BOOST
                    if project_boost > 1.0
                    else CANONICAL_BOOST
                )
            else:
                canonical_boost = 1.0
            # Description match boost — query words present in the
            # memory's description are a stronger signal than the same
            # words buried in content.
            desc_boost = 1.0
            desc_str = (h.get("description") or "").lower()
            if desc_str:
                desc_words = set(re.findall(r"\b\w+\b", desc_str))
                overlap = len(query_word_set & desc_words)
                desc_boost = 1.0 + min(
                    DESC_MATCH_BOOST_MAX,
                    overlap * DESC_MATCH_BOOST_PER_WORD,
                )
            # Final score — cap at 1.0 so percentage display stays readable.
            raw = (h["_base"] * trust * decay * echo
                   * project_boost * canonical_boost * desc_boost)
            h["score"] = min(1.0, raw)
            h["_score_parts"] = {
                "base": round(h["_base"], 4),
                "trust_mult": round(trust, 4),
                "decay": round(decay, 4),
                "echo": round(echo, 4),
                "project": round(project_boost, 4),
                "canonical": round(canonical_boost, 4),
                "desc": round(desc_boost, 4),
                "forced": bool(h.get("_forced")),
                "raw": round(raw, 4),
            }

        return sorted(survivors, key=lambda x: x["score"], reverse=True)[:top_k]

    # --- On Pre-Compress Hook ---

    def extract_key_facts(self, texts: list[str]) -> list[str]:
        """Extract key facts from texts exiting the context window.
        Uses regex heuristics (fast, free). Called by background monitor."""
        from auto_noter import extract_noteworthy
        facts = []
        for text in texts:
            for fact in extract_noteworthy(text, "user"):
                if fact not in facts:
                    facts.append(fact)
        return facts

    # --- Remote Sync ---

    def _is_remote_available(self) -> bool:
        global _remote_available, _last_connectivity_check
        now = time.monotonic()
        if _remote_available:
            return True
        if now - _last_connectivity_check < _CONNECTIVITY_RETRY_INTERVAL:
            return False
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "2",
                 f"{_get_memory_api_url()}/list.php"],
                capture_output=True, text=True, timeout=3,
            )
            _remote_available = result.returncode == 0 and result.stdout.strip() != "000"
        except Exception:
            _remote_available = False
        _last_connectivity_check = now
        return _remote_available

    def _mark_remote_down(self):
        global _remote_available, _last_connectivity_check
        _remote_available = False
        _last_connectivity_check = time.monotonic()

    def _queue_remote_sync(self, mem_id: str, content: str, description: str,
                           mem_type: str, operation: str,
                           created: str | None = None):
        device_id, device_name = self._get_device_info()
        project = None
        owner_id = source = scope = visibility = agent_id = layer = None
        real_session_id = None
        tags_list: list[str] = []
        # Look up local metadata so gitmas.com keeps the real writer
        # (owner_id/source) AND tags (canonical, project tags, etc.)
        # instead of dropping them.
        try:
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                "SELECT created, project_id, owner_id, source, scope, "
                "visibility, agent_id, layer, tags, session_id FROM memories WHERE id=?",
                (mem_id,),
            ).fetchone()
            conn.close()
            if row:
                if created is None and row[0]:
                    created = row[0]
                if row[1]:
                    project = row[1]
                if row[2]:
                    owner_id = row[2]
                if row[3]:
                    source = row[3]
                if row[4]:
                    scope = row[4]
                if row[5]:
                    visibility = row[5]
                if row[6]:
                    agent_id = row[6]
                if row[7]:
                    layer = row[7]
                if row[8]:
                    try:
                        parsed = json.loads(row[8])
                        if isinstance(parsed, list):
                            tags_list = [str(t) for t in parsed]
                    except Exception:
                        pass
                if row[9]:
                    real_session_id = row[9]
        except Exception:
            pass
        payload_obj = {
            # Keep API namespace stable so remote upsert by mcp_id works.
            # The actual scoped project stays in project_id below.
            "project": "mcp-memory",
            "tag": mem_type,
            "title": description,
            "content": content,
            "mcp_id": mem_id,
            "device_id": device_id,
            "device_name": device_name,
            # Real CLI session when the writer resolved one — "unified" was a
            # placeholder that made checkpoint→transcript linking guesswork.
            "session_id": real_session_id or "unified",
            "operation": operation,
        }
        if owner_id:
            payload_obj["owner_id"] = owner_id
        if source:
            payload_obj["source"] = source
        if agent_id:
            payload_obj["agent_id"] = agent_id
        if layer:
            payload_obj["layer"] = layer
        if scope:
            payload_obj["scope"] = scope
        if project:
            payload_obj["project_id"] = project
        if visibility:
            payload_obj["visibility"] = visibility
        if created:
            payload_obj["created"] = created
        if tags_list:
            # Use the plural key — `tag` (singular) is the memory type.
            payload_obj["tags"] = tags_list
        payload = json.dumps(payload_obj)

        # Try immediate push
        api_key = _get_memory_api_key()
        if api_key and self._is_remote_available():
            try:
                subprocess.Popen(
                    ["curl", "-s", "-X", "POST", f"{_get_memory_api_url()}/save.php",
                     "-H", "Content-Type: application/json",
                     "-H", f"X-API-Key: {api_key}",
                     "-d", payload],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                pass

        # Fallback: queue for later
        conn, _ = self._connect()
        try:
            conn.execute(
                "INSERT INTO sync_queue(table_name, record_id, operation, payload, queued_at) VALUES (?, ?, ?, ?, ?)",
                ("memories", mem_id, operation, payload, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    def _normalize_remote_project(self, project: dict) -> dict | None:
        """Normalize a Gitmas API project object for the local registry."""
        if not isinstance(project, dict):
            return None

        project_id = (
            project.get("id")
            or project.get("project_id")
            or project.get("slug")
            or project.get("key")
        )
        if not project_id:
            return None
        project_id = str(project_id).strip()
        if not project_id:
            return None

        assigned_agents = project.get("assigned_agents") or project.get("agents") or []
        if isinstance(assigned_agents, str):
            try:
                assigned_agents = json.loads(assigned_agents)
            except json.JSONDecodeError:
                assigned_agents = [a.strip() for a in assigned_agents.split(",") if a.strip()]
        if not isinstance(assigned_agents, list):
            assigned_agents = []

        return {
            "id": project_id,
            "name": str(project.get("name") or project.get("title") or project_id),
            "description": project.get("description") or "",
            "assigned_agents": json.dumps([str(a) for a in assigned_agents]),
            "owner_user": project.get("owner_user") or project.get("user_id") or project.get("owner"),
            "status": project.get("status") or "active",
            "created": project.get("created") or project.get("created_at") or datetime.now(timezone.utc).isoformat(),
        }

    def sync_projects_from_remote(self) -> int:
        """Pull registered projects from the Gitmas API into local SQLite."""
        api_key = _get_memory_api_key()
        if not api_key or not self._is_remote_available():
            return 0

        try:
            result = subprocess.run(
                ["curl", "-s", "-H", f"X-API-Key: {api_key}", "-m", "3",
                 f"{_get_memory_api_url()}/projects.php"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                self._mark_remote_down()
                return 0
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            # Older Gitmas API deployments do not expose projects.php yet.
            return 0
        except Exception:
            self._mark_remote_down()
            return 0

        if isinstance(payload, dict):
            remote_projects = payload.get("projects") or payload.get("results") or []
        elif isinstance(payload, list):
            remote_projects = payload
        else:
            remote_projects = []

        normalized = [
            row for row in (self._normalize_remote_project(p) for p in remote_projects)
            if row is not None
        ]
        if not normalized:
            return 0

        conn, _ = self._connect()
        try:
            for row in normalized:
                conn.execute(
                    """INSERT INTO projects(id, name, description, assigned_agents,
                       owner_user, status, created)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         name=excluded.name,
                         description=excluded.description,
                         assigned_agents=excluded.assigned_agents,
                         owner_user=excluded.owner_user,
                         status=excluded.status""",
                    (
                        row["id"],
                        row["name"],
                        row["description"],
                        row["assigned_agents"],
                        row["owner_user"],
                        row["status"],
                        row["created"],
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        return len(normalized)

    def sync_from_remote(self):
        """Pull registered projects and new/updated memories from Gitmas."""
        api_key = _get_memory_api_key()
        if not api_key or not self._is_remote_available():
            return

        self.sync_projects_from_remote()

        last_sync = ""
        if os.path.exists(SYNC_STATE_FILE):
            with open(SYNC_STATE_FILE) as f:
                last_sync = f.read().strip()

        url = f"{_get_memory_api_url()}/list.php"
        if last_sync:
            url += f"?since={quote(last_sync)}"

        try:
            result = subprocess.run(
                ["curl", "-s", "-H", f"X-API-Key: {api_key}", "-m", "3", url],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                self._mark_remote_down()
                return
            remote = json.loads(result.stdout)
            if not isinstance(remote, list):
                return
        except Exception:
            self._mark_remote_down()
            return

        remote = [m for m in remote if m.get("project") not in ("bootstrap", "repos", "test")]
        if not remote:
            self._save_sync_time()
            return

        tag_map = {"checkpoint": "project", "config": "reference", "todo": "project", "context": "project"}
        conn, vec_ok = self._connect()
        try:
            existing_ids = {
                r["id"] for r in conn.execute("SELECT id FROM memories").fetchall()
            }

            for mem in remote:
                mcp_id = mem.get("mcp_id")
                if not mcp_id:
                    continue
                try:
                    operation = mem.get("operation", "store")
                    if operation == "delete":
                        if mcp_id in existing_ids:
                            conn.execute("UPDATE memories SET deleted=1 WHERE id=?", (mcp_id,))
                        continue

                    content = mem.get("content", "")
                    if isinstance(content, str) and content.startswith('"') and content.endswith('"'):
                        try:
                            content = json.loads(content)
                        except Exception:
                            pass

                    tag = mem.get("tag", "project")
                    tag = tag_map.get(tag, tag)
                    if tag not in ("user", "feedback", "project", "reference"):
                        tag = "project"

                    title = mem.get("title", "untitled")
                    now = datetime.now(timezone.utc).isoformat()

                    # Map server scope to local scope/project_id. Newer MCP
                    # rows keep the real scoped project in project_id while
                    # project stays as the remote API namespace (mcp-memory).
                    server_project = (mem.get("project") or "").strip()
                    server_project_id = (mem.get("project_id") or "").strip()
                    server_device_id = mem.get("device_id")
                    server_device_name = mem.get("device_name")
                    server_tags_raw = mem.get("tags")
                    server_tags_json: str | None = None
                    if server_tags_raw is not None:
                        try:
                            if isinstance(server_tags_raw, list):
                                server_tags_json = json.dumps(
                                    [str(t) for t in server_tags_raw]
                                )
                            elif isinstance(server_tags_raw, str):
                                parsed = json.loads(server_tags_raw)
                                if isinstance(parsed, list):
                                    server_tags_json = json.dumps(
                                        [str(t) for t in parsed]
                                    )
                        except Exception:
                            server_tags_json = None
                    if server_project_id:
                        local_scope, local_project_id = "project", server_project_id
                    elif server_project and server_project != "mcp-memory":
                        local_scope, local_project_id = "project", server_project
                    else:
                        local_scope, local_project_id = "global", None

                    if mcp_id in existing_ids:
                        # Preserve existing local scope unless the row is still
                        # unscoped and the server now provides project_id.
                        # Tags from remote replace local tags when present —
                        # canonical/project tags need to propagate across
                        # devices to keep ranking behavior consistent.
                        conn.execute(
                            """UPDATE memories
                               SET content=?, description=?, type=?, updated=?,
                                   device_id=COALESCE(?, device_id),
                                   device_name=COALESCE(?, device_name),
                                   scope=CASE
                                     WHEN (project_id IS NULL OR project_id = '') AND ? IS NOT NULL THEN ?
                                     ELSE scope
                                   END,
                                   project_id=COALESCE(project_id, ?),
                                   tags=COALESCE(?, tags)
                               WHERE id=?""",
                            (
                                content, title, tag, now,
                                server_device_id, server_device_name,
                                local_project_id, local_scope,
                                local_project_id, server_tags_json, mcp_id,
                            ),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO memories(id, type, description, content, source, created, scope, project_id, device_id, device_name, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (mcp_id, tag, title, content, "remote", now, local_scope, local_project_id, server_device_id, server_device_name, server_tags_json or "[]"),
                        )
                        # Embed new remote memories
                        embedding = self._embed_sync(content)
                        rowid = conn.execute("SELECT rowid FROM memories WHERE id=?", (mcp_id,)).fetchone()[0]
                        if vec_ok and embedding:
                            conn.execute(
                                "INSERT OR REPLACE INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                                (rowid, _vec_to_blob(embedding)),
                            )
                        conn.execute(
                            "INSERT OR REPLACE INTO memories_fts(rowid, content, description) VALUES (?, ?, ?)",
                            (rowid, content, title),
                        )
                        existing_ids.add(mcp_id)
                except Exception:
                    continue

            conn.commit()
        finally:
            conn.close()

        # Ingest any entity_alias memories that landed via sync. Description
        # convention: "entity:<canonical_name>". Idempotent (entity_add merges).
        try:
            conn, _ = self._connect()
            try:
                ent_rows = conn.execute(
                    "SELECT content FROM memories "
                    "WHERE description LIKE 'entity:%' AND deleted=0"
                ).fetchall()
                proj_rows = conn.execute(
                    "SELECT content FROM memories "
                    "WHERE description LIKE 'project:%' AND deleted=0"
                ).fetchall()
            finally:
                conn.close()
            for r in ent_rows:
                self._ingest_entity_memory(r["content"])
            for r in proj_rows:
                self._ingest_project_memory(r["content"])
        except Exception:
            pass

        self._save_sync_time()

    def drain_sync_queue(self) -> int:
        """Push queued operations to remote. Returns count synced."""
        api_key = _get_memory_api_key()
        if not api_key or not self._is_remote_available():
            return 0

        conn, _ = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, payload FROM sync_queue WHERE synced_at IS NULL ORDER BY id LIMIT 50"
            ).fetchall()
        finally:
            conn.close()

        synced = 0
        for row in rows:
            try:
                result = subprocess.run(
                    ["curl", "-s", "-X", "POST", f"{_get_memory_api_url()}/save.php",
                     "-H", "Content-Type: application/json",
                     "-H", f"X-API-Key: {api_key}",
                     "-d", row["payload"]],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    conn2, _ = self._connect()
                    try:
                        conn2.execute(
                            "UPDATE sync_queue SET synced_at=? WHERE id=?",
                            (datetime.now(timezone.utc).isoformat(), row["id"]),
                        )
                        conn2.commit()
                    finally:
                        conn2.close()
                    synced += 1
            except Exception:
                self._mark_remote_down()
                break

        return synced

    def _save_sync_time(self):
        os.makedirs(os.path.dirname(SYNC_STATE_FILE), exist_ok=True)
        with open(SYNC_STATE_FILE, "w") as f:
            f.write(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

    # --- Stats ---

    def stats(self) -> dict:
        conn, _ = self._connect()
        try:
            memories = conn.execute("SELECT COUNT(*) FROM memories WHERE deleted=0").fetchone()[0]
            turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            notes = conn.execute("SELECT COUNT(*) FROM session_notes").fetchone()[0]
            sessions = conn.execute("SELECT COUNT(DISTINCT session_id) FROM turns").fetchone()[0]
            queued = conn.execute("SELECT COUNT(*) FROM sync_queue WHERE synced_at IS NULL").fetchone()[0]
            return {
                "memories": memories,
                "turns": turns,
                "session_notes": notes,
                "sessions_indexed": sessions,
                "sync_queued": queued,
                "vec_available": self._vec_ok,
                "ollama_available": self._ollama_ok,
            }
        finally:
            conn.close()


# Module-level singleton
_instance: UnifiedMemory | None = None


def get_unified_memory() -> UnifiedMemory:
    global _instance
    if _instance is None:
        _instance = UnifiedMemory()
    return _instance
