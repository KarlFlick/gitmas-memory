#!/usr/bin/env bash
# Gitmas Memory — customer installer.
#
# Installs the Gitmas Memory runtime into ~/.gitmas, registers the MCP server
# with supported agent CLIs (Claude Code, Codex), generates a local encryption
# key for session backups, and enables the session autosave service.
#
# Usage:
#   GITMAS_API_KEY=... ./install.sh
#   ./install.sh --skip-mcp --skip-autosave --skip-venv   (testing/partial)
#
# Re-running is safe — every step is idempotent.

set -euo pipefail

SKIP_MCP=0; SKIP_AUTOSAVE=0; SKIP_VENV=0
for arg in "$@"; do
  case "$arg" in
    --skip-mcp) SKIP_MCP=1 ;;
    --skip-autosave) SKIP_AUTOSAVE=1 ;;
    --skip-venv) SKIP_VENV=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

SRC="$(cd "$(dirname "$0")" && pwd)"
GITMAS_HOME="${GITMAS_HOME:-$HOME/.gitmas}"
SECRETS="$HOME/.secrets"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN\033[0m %s\n' "$*"; }

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }

# --- 1. Runtime files -------------------------------------------------------
say "Installing runtime into $GITMAS_HOME"
mkdir -p "$GITMAS_HOME/bin" "$GITMAS_HOME/memory" "$GITMAS_HOME/memory-server" \
         "$GITMAS_HOME/identity" "$GITMAS_HOME/sessions" "$GITMAS_HOME/logs"
cp "$SRC"/bin/* "$GITMAS_HOME/bin/"
chmod +x "$GITMAS_HOME"/bin/* 2>/dev/null || true
cp "$SRC/memory/unified_memory.py" "$GITMAS_HOME/memory/"
cp "$SRC"/memory-server/*.py "$GITMAS_HOME/memory-server/"
if [ -d "$HOME/.pi/agent" ]; then
  mkdir -p "$HOME/.pi/agent/extensions/gitmas"
  cp "$SRC"/pi/extensions/gitmas/* "$HOME/.pi/agent/extensions/gitmas/"
  say "Pi agent extension installed"
fi

# PATH (guarded, once)
for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
  if [ -f "$rc" ] && ! grep -q "GITMAS PATH" "$rc"; then
    printf '\n# GITMAS PATH\nexport PATH="$HOME/.gitmas/bin:$PATH"\n' >> "$rc"
  fi
done

# --- 2. API key -------------------------------------------------------------
if [ -z "${GITMAS_API_KEY:-}" ] && [ -f "$SECRETS" ]; then
  # shellcheck disable=SC1090
  . "$SECRETS" 2>/dev/null || true
fi
if [ -z "${GITMAS_API_KEY:-}" ] && [ -t 0 ]; then
  printf 'Paste your GITMAS_API_KEY (from your welcome message): '
  stty -echo; read -r GITMAS_API_KEY; stty echo; printf '\n'
fi
if [ -n "${GITMAS_API_KEY:-}" ]; then
  touch "$SECRETS"; chmod 600 "$SECRETS"
  grep -q "GITMAS_API_KEY=" "$SECRETS" 2>/dev/null || {
    printf 'export GITMAS_API_KEY="%s"\nexport MEMORY_API_KEY="%s"\nexport GITMAS_API_URL="https://gitmas.com/memory"\nexport MEMORY_API_URL="https://gitmas.com/memory"\n' \
      "$GITMAS_API_KEY" "$GITMAS_API_KEY" >> "$SECRETS"
    say "API key saved to ~/.secrets (chmod 600)"
  }
else
  warn "No GITMAS_API_KEY — remote sync stays off. Add it to ~/.secrets and re-run."
fi

# --- 3. Encryption key for session backups ----------------------------------
if [ ! -f "$GITMAS_HOME/identity/age.key" ]; then
  if command -v age-keygen >/dev/null 2>&1; then
    age-keygen -o "$GITMAS_HOME/identity/age.key" 2>/dev/null
    chmod 600 "$GITMAS_HOME/identity/age.key"
    grep -o 'age1[a-z0-9]*' "$GITMAS_HOME/identity/age.key" | head -1 > "$GITMAS_HOME/identity/age.pub"
    say "Encryption keypair generated: $GITMAS_HOME/identity/age.key"
    warn "BACK UP age.key somewhere safe — without it your encrypted session backups cannot be read. Gitmas never sees this key."
  else
    warn "age not installed (brew install age / apt install age) — session backups disabled until a key exists."
  fi
fi

# --- 4. Python venv + MCP registration --------------------------------------
# The MCP server needs Python >= 3.10 — pick the newest available.
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && \
     "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  warn "Python 3.10+ not found — MCP server skipped. Install Python (brew install python / apt install python3) and re-run."
  SKIP_VENV=1; SKIP_MCP=1
fi

VENV="$GITMAS_HOME/memory-server/.venv"
if [ "$SKIP_VENV" -eq 0 ]; then
  say "Creating Python venv ($PY) + installing mcp"
  "$PY" -m venv "$VENV" 2>/dev/null || true
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q mcp
fi
if [ "$SKIP_MCP" -eq 0 ]; then
  say "Registering Gitmas Memory MCP server with available agent CLIs"
  GITMAS_MCP_PYTHON="$VENV/bin/python" GITMAS_HOME="$GITMAS_HOME" \
    "$GITMAS_HOME/bin/gitmas-mcp-install" all || warn "MCP registration incomplete — run 'gitmas-mcp-install all' manually"
fi

# --- 5. Session autosave ----------------------------------------------------
if [ "$SKIP_AUTOSAVE" -eq 0 ]; then
  say "Enabling session autosave (60s scan)"
  "$GITMAS_HOME/bin/sessions-autosave" install --interval 60 || warn "autosave install failed — run 'sessions-autosave install' manually"
fi

say "Done. Restart your agent CLI (Claude Code / Codex / Pi), then verify:"
printf '    ask your agent: "store a Gitmas memory that setup works, then search for it"\n'
printf '    or: curl -sS -H "X-API-Key: $GITMAS_API_KEY" "https://gitmas.com/memory/list.php?limit=3"\n'
