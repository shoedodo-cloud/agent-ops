#!/usr/bin/env bash
# pull-gate-session-start.sh - run `pull-gate check` at session start and print the banner.
#
# Point a harness's session-start hook at this script. Whatever it prints on stdout is the
# banner. It always exits 0, so it never blocks a session from opening.
#
# The check fetches read-only, classifies what is incoming, and fast-forwards only what
# the config allows (see `pull-gate --help`). This wrapper adds one more layer of
# fail-closed behaviour: if python3 is missing, pull-gate is missing, it crashes, or it
# prints nothing, the banner says the check failed. A silent hook is never an all-clear.
#
# Arguments are passed to `pull-gate check`, for example --root DIR or --no-merge.
# Env: PULL_GATE_BIN overrides the path of the pull-gate script. The other settings are
# listed in `pull-gate --help`.

set -uo pipefail

# Find this script's directory with builtins only, following a symlink if there is one.
src="${BASH_SOURCE[0]:-$0}"
while [ -L "$src" ]; do
  link=$(readlink "$src") || break
  case "$link" in
    /*) src="$link" ;;
    *)  src="${src%/*}/$link" ;;
  esac
done
case "$src" in
  */*) here=$(cd "${src%/*}" 2>/dev/null && pwd) || here="" ;;
  *)   here=$(pwd) ;;
esac

gate="${PULL_GATE_BIN:-$here/pull-gate}"

fail() {
  echo "PULL-GATE: THE CHECK ITSELF FAILED: $1."
  echo "This is NOT an all-clear. Audit by hand before starting work:"
  echo "  git -C <repo> fetch origin && git -C <repo> rev-list --left-right --count HEAD...origin/<branch>"
}

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 was not found on PATH"
  exit 0
fi
if [ ! -f "$gate" ]; then
  fail "pull-gate was not found at ${gate:-<unknown>}"
  exit 0
fi

out=$(python3 "$gate" check "$@" 2>&1)
rc=$?

if [ "$rc" -ne 0 ]; then
  case "$out" in
    *"THE CHECK ITSELF FAILED"*) ;;
    *) fail "pull-gate exited with status $rc" ;;
  esac
  [ -n "$out" ] && printf '%s\n' "$out"
  exit 0
fi

if [ -z "$out" ]; then
  fail "pull-gate printed nothing"
  exit 0
fi

printf '%s\n' "$out"
exit 0
