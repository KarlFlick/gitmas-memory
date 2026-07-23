#!/usr/bin/env bash
# Shared environment helpers for Gitmas session backup scripts.
# Source this file from bash scripts; it intentionally has no side effects
# beyond defining functions.

_gitmas_trim_file() {
  local f="$1"
  [[ -f "$f" ]] && tr -d '\r\n\t ' < "$f"
}

gitmas_source_secrets() {
  # Prefer ~/.secrets so key rotations are picked up by hook-launched scripts.
  # shellcheck disable=SC1090
  [[ -f "$HOME/.secrets" ]] && source "$HOME/.secrets" 2>/dev/null || true
}

gitmas_api_base() {
  local base="${GITMAS_API_URL:-${MEMORY_API_URL:-https://gitmas.com/memory}}"
  printf '%s\n' "${base%/}"
}

gitmas_api_url() {
  local path="$1"
  printf '%s/%s\n' "$(gitmas_api_base)" "${path#/}"
}

gitmas_memory_api_key() {
  gitmas_source_secrets
  printf '%s\n' "${GITMAS_API_KEY:-${MEMORY_API_KEY:-}}"
}

gitmas_export_api_key_aliases() {
  local k
  k="$(gitmas_memory_api_key)"
  [[ -n "$k" ]] || return 1
  export GITMAS_API_KEY="$k"
  export MEMORY_API_KEY="$k"
}

gitmas_device_id() {
  local v="${SESSION_ARCHIVE_DEVICE_ID:-${MEMORY_DEVICE_ID:-}}"
  [[ -z "$v" ]] && v="$(_gitmas_trim_file "$HOME/.gitmas/identity/device-id" || true)"
  [[ -z "$v" ]] && v="$(_gitmas_trim_file "$HOME/.claude/memory-device-id" || true)"
  [[ -z "$v" ]] && v="$(_gitmas_trim_file "$HOME/.claude/device-id" || true)"
  [[ -z "$v" ]] && v="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown)"
  printf '%s\n' "$v"
}

gitmas_device_name() {
  local v="${SESSION_ARCHIVE_DEVICE_NAME:-${MEMORY_DEVICE_NAME:-}}"
  [[ -z "$v" ]] && v="$(_gitmas_trim_file "$HOME/.gitmas/identity/device-name" || true)"
  [[ -z "$v" ]] && v="$(_gitmas_trim_file "$HOME/.claude/memory-device-name" || true)"
  [[ -z "$v" ]] && v="$(_gitmas_trim_file "$HOME/.claude/device-name" || true)"
  if [[ -z "$v" && "$(uname -s)" == "Darwin" ]] && command -v scutil >/dev/null 2>&1; then
    v="$(scutil --get ComputerName 2>/dev/null || true)"
  fi
  [[ -z "$v" ]] && v="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown)"
  printf '%s\n' "$v"
}

gitmas_python() {
  local p="${GITMAS_PYTHON:-${MEMORY_PYTHON:-}}"
  [[ -n "$p" && -x "$p" ]] && { printf '%s\n' "$p"; return; }
  for p in \
    "$HOME/.gitmas/memory-server/.venv/bin/python3" \
    "$HOME/gitmas-config/.venv/bin/python3"; do
    [[ -x "$p" ]] && { printf '%s\n' "$p"; return; }
  done
  command -v python3
}
