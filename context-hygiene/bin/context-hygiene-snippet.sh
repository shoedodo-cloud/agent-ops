#!/usr/bin/env bash
# context-hygiene-snippet.sh - print the hook wiring for the context-hygiene
# scripts. It only prints. It never reads or edits any settings file.
# Bash 3.2 compatible. Run with --help for the full description.

set -uo pipefail

usage() {
  cat <<'EOF'
context-hygiene-snippet.sh - print hook configuration for the context-hygiene scripts.

Usage: context-hygiene-snippet.sh [--claude | --codex]
       context-hygiene-snippet.sh --help

With no option it prints both fragments with a plain-text heading above each.
--claude prints only the Claude Code fragment, --codex only the Codex fragment.
Each of those is valid JSON on its own, ready to merge by hand.

Claude Code fragment (merge into the "hooks" object of ~/.claude/settings.json):
  SessionStart                   -> project-inventory.sh
  SessionStart, matcher compact  -> compaction-marker.sh --announce
  PreCompact                     -> compaction-marker.sh --log

Codex fragment (merge into the "hooks" object of ~/.codex/hooks.json):
  SessionStart, startup|resume   -> project-inventory.sh
  PreCompact                     -> compaction-marker.sh --log
No "resume after compaction" hook is confirmed for Codex, so no announce entry
is printed for it. The script says so on lines starting with "Note:".

Environment:
  PREFIX   directory the scripts were copied to. Must be an absolute path.
           Default: $HOME/.local/bin
           If a script is missing there, a warning goes to stderr. The
           fragment is still printed.

This script never edits any file.

PREFIX must not contain a double quote, dollar sign, backtick or backslash,
because the printed command quotes the path for the shell.

Exit status: 0 on success. 2 for a bad option or an unusable PREFIX.
EOF
}

WHICH=both
case "${1:-}" in
  "")          WHICH=both ;;
  --claude)    WHICH=claude ;;
  --codex)     WHICH=codex ;;
  -h|--help)   usage; exit 0 ;;
  *) echo "[snippet] unknown option: $1 (see --help)" >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { echo "[snippet] too many arguments (see --help)" >&2; exit 2; }

PREFIX="${PREFIX:-$HOME/.local/bin}"
case "$PREFIX" in
  /*) ;;
  *) echo "[snippet] PREFIX must be an absolute path, got: $PREFIX" >&2; exit 2 ;;
esac
PREFIX="${PREFIX%/}"
case "$PREFIX" in
  *[\"\$\`\\]*) echo "[snippet] PREFIX must not contain a double quote, dollar sign, backtick or backslash, got: $PREFIX" >&2; exit 2 ;;
esac

for s in project-inventory.sh compaction-marker.sh; do
  [ -x "$PREFIX/$s" ] || echo "[snippet] warning: $PREFIX/$s is not an executable file. Copy the scripts there first." >&2
done

# Escape a string for use inside a JSON string: backslash first, then quote.
jesc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# A shell command that runs "$PREFIX/<script> <args>", with the script path
# quoted for the shell, then escaped for JSON.
cmd() { # script [args]
  _q="\"$PREFIX/$1\""
  [ -n "${2:-}" ] && _q="$_q $2"
  jesc "$_q"
}

claude_fragment() {
  cat <<EOF
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "$(cmd project-inventory.sh)" }
        ]
      },
      {
        "matcher": "compact",
        "hooks": [
          { "type": "command", "command": "$(cmd compaction-marker.sh --announce)" }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          { "type": "command", "command": "$(cmd compaction-marker.sh --log)" }
        ]
      }
    ]
  }
}
EOF
}

codex_fragment() {
  cat <<EOF
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          { "type": "command", "command": "$(cmd project-inventory.sh)", "statusMessage": "Project inventory" }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          { "type": "command", "command": "$(cmd compaction-marker.sh --log)", "statusMessage": "Log compaction" }
        ]
      }
    ]
  }
}
EOF
}

codex_note() {
  echo "Note: no announce entry is printed for Codex, because no hook that runs after a compaction and feeds the model is confirmed."
  echo "Note: an entry for an event your Codex build does not know is ignored without an error. After a compaction, check that compaction.log gained a line."
}

case "$WHICH" in
  claude) claude_fragment ;;
  codex)  codex_fragment; codex_note >&2 ;;
  both)
    echo "# Claude Code: merge into the \"hooks\" object of ~/.claude/settings.json"
    claude_fragment
    echo
    echo "# Codex: merge into the \"hooks\" object of ~/.codex/hooks.json"
    codex_fragment
    echo
    codex_note | sed 's/^/# /'
    ;;
esac
