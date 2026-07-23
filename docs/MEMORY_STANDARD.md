# Gitmas Memory Standard

This document is the canonical naming and description standard for the shared memory layer used by Karl's agents.

## Canonical source order

1. **This file**: `~/gitmas-config/docs/MEMORY_STANDARD.md` — canonical standard.
2. **Agent bootstrap snippet**: `~/gitmas-config/docs/AGENT_MEMORY_BOOTSTRAP.md` — compact wording injected into agent system/global context layers.
3. **Repository summary**: `~/gitmas-config/README.md` — short product/runtime summary and link to this file.
4. **Runtime reminders**: Gitmas Memory entries may mirror this standard for agent recall, but they are not the source of truth.
5. **Compatibility docs/code**: legacy `~/.claude/*` or `MEMORY_*` names may exist only as backwards-compatible aliases.

## Product naming

| Term | Use |
|---|---|
| **Gitmas Memory** | Product name for the shared durable memory layer. Use this in user-facing explanations, docs, tool descriptions, and UI. |
| **Gitmas** | Product namespace containing memory, sync, identity, and session backup runtime. |
| **UnifiedMemory** / `unified_memory.py` | Internal engine/module name only. Use when discussing implementation details. |
| **Gitmas Memory gateway** | Local HTTP/MCP access layer to Gitmas Memory. |
| **Gitmas Memory MCP server** | Stdio MCP server for Claude Code, Codex, and other MCP clients. |
| **Gitmas Memory HTTP API** | Localhost HTTP API for Pi, Hermes, scripts, and non-MCP clients. |

### Required framing

When an agent describes its memory access, say:

> I have access to **Gitmas Memory** through local memory tools/gateway. UnifiedMemory is the internal engine behind it.

Do **not** lead with “UnifiedMemory” as if it were the product name.

## Ownership model

Gitmas Memory is shared infrastructure. Claude Code, Pi, Codex, Hermes, MAS, Android clients, and future agents are clients/consumers, not owners.

Correct:

- “Pi accesses Gitmas Memory via the local gateway.”
- “Claude Code uses the Gitmas Memory MCP server.”
- “Hermes writes to Gitmas Memory through HTTP.”

Incorrect:

- “Pi memory” when referring to the shared memory product.
- “Claude memory” when referring to cross-agent shared memory.
- “UnifiedMemory memory” in user-facing descriptions.

## Runtime namespace

Canonical runtime paths:

```text
~/.gitmas/
  memory/unified.db
  memory/unified_memory.py
  memory-server/server.py
  memory-server/http_server.py
  identity/user-id
  identity/agent-id
  identity/device-id
  identity/device-name
```

Compatibility paths may remain for existing clients:

```text
~/.claude/memory/*
~/.claude/memory-server/*
```

New integrations must target `~/.gitmas/*` by default.

## Environment variables

Canonical variables use the `GITMAS_*` prefix:

```bash
GITMAS_HOME=~/.gitmas
GITMAS_API_URL=https://gitmas.com/memory
GITMAS_MEMORY_DB=~/.gitmas/memory/unified.db
GITMAS_SYNC_STATE_FILE=~/.gitmas/memory/.last-sync
GITMAS_USER_ID=<user id>
GITMAS_AGENT_ID=<agent id>
GITMAS_SOURCE=<client/source id>
```

Compatibility aliases may remain:

```bash
MEMORY_API_URL=$GITMAS_API_URL
MEMORY_DB_PATH=$GITMAS_MEMORY_DB
MEMORY_USER_ID=$GITMAS_USER_ID
MEMORY_AGENT_ID=$GITMAS_AGENT_ID
MEMORY_SOURCE=$GITMAS_SOURCE
```

New code and docs should prefer `GITMAS_*`; use `MEMORY_*` only for compatibility with existing clients.

## Tool and API descriptions

Tool descriptions should identify the product first and the transport second.

Preferred examples:

- “Search Gitmas Memory through the local gateway.”
- “Store a durable entry in Gitmas Memory.”
- “List recent Gitmas Memory entries.”
- “Gitmas Memory MCP server.”
- “Gitmas Memory HTTP gateway.”

Avoid:

- “UnifiedMemory gateway” unless describing internals.
- “Pi memory gateway” unless specifically describing a Pi adapter wrapper.
- “Claude memory server” unless referring only to a Claude Code compatibility path.

## Memory entry fields

Use these meanings consistently across clients and sync layers:

| Field | Meaning |
|---|---|
| `content` | Full durable memory body. This is the authoritative statement. |
| `description` | Short human-readable title/summary used in search results and UIs. |
| `type` | Category: `project`, `feedback`, `user`, or `reference`. |
| `scope` | Visibility/namespace: `global`, `project`, `agent`, or `user`. |
| `project` / `project_id` | Project namespace such as `infra`, `voice-pipeline`, `gitmas-config`. |
| `tags` | Search/filter labels, lowercase kebab-case where possible. |
| `customer_id` | Server-side tenant/account id derived from the authenticated Gitmas API key. Clients must not be trusted to assign this. |
| `owner_id` | User or logical owner that wrote/owns the entry inside the tenant. |
| `source` | Client/source that submitted the entry, e.g. `pi-agent`, `claude-code`, `hermes-cli`, `codex`. |
| `agent_id` | Specific agent/persona/process identity when available. |
| `layer` | Optional provenance layer, e.g. `mcp`, `http`, `sync`, `session-index`. |
| `device_id` / `device_name` | Device provenance. |

## Description style

`description` should be concise, specific, and product-neutral unless the product is the subject.

Rules:

- Prefer 6–16 words.
- Start with the subject: project, feature, checkpoint, or standard.
- Include “checkpoint” only for durable project state snapshots.
- Include commit IDs only when useful for retrieval.
- Do not duplicate the entire `content` field.
- Do not infer the agent from text; preserve `owner_id`, `source`, and `agent_id` metadata.

Examples:

```text
Gitmas Memory naming standard — product vs internal UnifiedMemory engine
Cross-agent memory E2E PASS: Hermes HTTP ↔ Claude MCP via Gitmas Memory
Gitmas checkpoints owner/source agent badge fix deployed; commit ff53bda
```

## Scope and type guidance

| Situation | type | scope |
|---|---|---|
| User preference or correction | `feedback` | `global` or `user` |
| User profile/context | `user` | `user` or `global` |
| Project status/checkpoint/decision | `project` | `project` |
| External URL, credential location, command, standard, architecture reference | `reference` | `global` or `project` |
| Cross-agent naming/identity standard | `reference` | `global` |

## Agent system injection standard

Every agent/client bootstrap layer should include the compact standard from `docs/AGENT_MEMORY_BOOTSTRAP.md`, either directly or through a symlink/generated snippet. This includes Claude Code global `CLAUDE.md`, MAS system prompts, Pi global `AGENTS.md`, Codex global/project `AGENTS.md`, Hermes system/bootstrap prompts, and future clients.

Current injection map:

| Client | System/bootstrap injection point | Tool/schema injection point |
|---|---|---|
| Claude Code / MAS | `~/gitmas-config/CLAUDE.md`, `~/gitmas-config/MAS/SYSTEM_PROMPT.md` | `~/gitmas-config/memory-server/server.py` |
| Pi Agent | `~/.pi/agent/AGENTS.md` symlink to `docs/AGENT_MEMORY_BOOTSTRAP.md` | `~/claude-config-pi-agent/pi-agent/extensions/memory-gateway.ts` |
| Codex | `~/.codex/AGENTS.md` symlink to `docs/AGENT_MEMORY_BOOTSTRAP.md` | `~/.codex/config.toml` MCP server `gitmas-memory` |
| Hermes Agent | `agent/prompt_builder.py` `GITMAS_MEMORY_NAMING_GUIDANCE` | `tools/unified_memory_tool.py`, `toolsets.py` |

## Agent response standard

When asked “what memory do you have access to?”, agents should answer in this order:

1. Current conversation context.
2. Project/context files loaded for the session.
3. **Gitmas Memory** durable memory via local tools/gateway.
4. Any other tool-specific or client-specific memory.

Example:

> I can access current chat context, project context files, and durable **Gitmas Memory** through local memory tools. Gitmas Memory is the shared cross-agent memory product; UnifiedMemory is the internal engine behind it.

## Implementation notes

- `unified_memory.py` and `unified.db` may keep their names as internal implementation artifacts.
- Public/client-facing docs, UI labels, and agent explanations should use Gitmas Memory.
- Remote sync endpoints under `gitmas.com/memory` are part of Gitmas Memory.
- Full memory IDs should be preserved where available; avoid truncating IDs in canonical records.
