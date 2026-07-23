# Gitmas Memory

Smart shared memory for your AI agents — decisions, checkpoints, and encrypted
session history, recallable from any supported workflow, on any of your devices.

## Install

**macOS installer**: download `GitmasMemory.pkg` from the
[releases page](https://github.com/KarlFlick/gitmas-memory/releases),
right-click → Open (the package is not yet notarized), and after install run
in Terminal:

```bash
gitmas-connect <your key>
```

**One line** (macOS / Linux):

```bash
GITMAS_API_KEY=<your key> bash -c "$(curl -fsSL https://www.gitmas.com/install.sh)"
```

Or download a versioned package from the
[releases page](https://github.com/KarlFlick/gitmas-memory/releases)
(checksums in `SHA256SUMS`), extract, and run:

```bash
GITMAS_API_KEY=<your key> ./install.sh
```

You received your `GITMAS_API_KEY` in your welcome message. The installer:

1. installs the runtime into `~/.gitmas`
2. saves your API key to `~/.secrets` (chmod 600)
3. generates a **local** encryption keypair for session backups
   (`~/.gitmas/identity/age.key`) — back it up; Gitmas never sees it
4. registers the `gitmas-memory` MCP server with Claude Code and Codex
   (Pi extension installs automatically if Pi is present)
5. enables session autosave — closing a terminal is never the moment that
   decides whether your work was saved

Then restart your agent CLI.

## Verify

Ask your agent:

> store a Gitmas memory that setup works, then search for it

Or directly:

```bash
curl -sS -H "X-API-Key: $GITMAS_API_KEY" "https://gitmas.com/memory/list.php?limit=3"
```

## What your agents get

19 memory tools over MCP — `memory_store`, `memory_search`, `memory_list`,
`memory_update`, `memory_delete`, session read/list, project routing, agent
messaging and more. Full reference: `docs/MEMORY_STANDARD.md`.

## Hermes and other HTTP agents

Agents that speak HTTP instead of MCP use the local gateway:

```bash
gitmas-gateway install     # starts on login, http://127.0.0.1:8765
gitmas-gateway token       # bearer token for Authorization header
```

The installer enables this automatically when `~/.hermes` exists. Hermes
session files are also picked up by session autosave.

## Privacy

- Your data is scoped to your account server-side; the tenant is derived from
  your API key, never from client-supplied fields.
- Session transcripts are compressed and encrypted **on your machine**
  (`age`, your key) before upload. Gitmas stores ciphertext it cannot read.

## New device

Run the same one-liner, paste the same API key, and copy
`~/.gitmas/identity/age.key` from your first device (so both encrypt to the
same key you hold).

Support: support@gitmas.com
