#!/usr/bin/env bash
# compaction-marker.sh - make context compaction visible instead of silent.
# Run with --help for the full description.
#
# Two modes, because the two hooks behave differently:
#
#   --log       wired to PreCompact. Records the event with a timestamp.
#               PreCompact stdout is not injected into the model's context
#               (per the Claude Code hooks docs), so this mode stays silent
#               on purpose. Printing here would go nowhere.
#
#   --announce  wired to SessionStart with matcher "compact", which fires when
#               a session resumes after compacting. SessionStart stdout is
#               injected into context, so this is the mode the model reads.

set -uo pipefail

usage() {
  cat <<'EOF'
compaction-marker.sh - log context compactions and announce them afterwards.

Usage: compaction-marker.sh --log
       compaction-marker.sh --announce
       compaction-marker.sh --help

  --log        append "timestamp<TAB>project" to the log. Prints nothing.
               Wire it to PreCompact.
  --announce   print a notice for the agent: the context was compacted, how
               many compactions the log holds for this project (all sessions,
               the log is never rotated), when the last one was, and an
               instruction to re-read facts from files instead of trusting
               the summary.
               Wire it to SessionStart with matcher "compact".

Project: $CLAUDE_PROJECT_DIR, else the current directory.
Log:     $CONTEXT_HYGIENE_HOME/compaction.log
         Default for CONTEXT_HYGIENE_HOME: $HOME/.config/context-hygiene

If --announce runs and the log has no entry for this project, it still says the
context was compacted (the hook only fires after a compaction) and that the
--log hook is missing or logging somewhere else. It does not stay silent.

Exit status: 0 for both modes. 2 for an unknown mode.
EOF
}

STATE="${CONTEXT_HYGIENE_HOME:-$HOME/.config/context-hygiene}"
LOG="$STATE/compaction.log"
PROJ="${CLAUDE_PROJECT_DIR:-$PWD}"
export PROJ   # read by awk through ENVIRON, so the path is compared as text, never as a regex
MODE="${1:---log}"
TAB=$(printf '\t')

# grep -c prints "0" AND exits 1 on no match, so `|| echo 0` would emit it twice.
count() {
  _n=$(awk -F'\t' '$2 == ENVIRON["PROJ"] { n++ } END { print n + 0 }' "$LOG" 2>/dev/null) || _n=0
  printf '%s' "${_n:-0}" | tr -dc '0-9'
}

case "$MODE" in
  -h|--help)
    usage
    ;;

  --log)
    if ! mkdir -p "$STATE" 2>/dev/null || ! printf '%s\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$PROJ" >> "$LOG" 2>/dev/null; then
      echo "[compaction-marker] could not write $LOG" >&2
    fi
    ;;

  --announce)
    N=$(count)
    if [ "${N:-0}" -eq 0 ]; then
      echo "CONTEXT WAS COMPACTED. No PreCompact entry for $(basename "$PROJ") is in ${LOG}, so the --log hook is missing or logs elsewhere."
      echo "Everything before the compaction is a summary, not the verbatim conversation. Before asserting"
      echo "any fact from before it (a path, a date, a status, who said what), re-read it from the file."
      echo "If you cannot re-read it, say that you are working from a summary."
      exit 0
    fi
    LAST=$(awk -F'\t' '$2 == ENVIRON["PROJ"] { l = $1 } END { print l }' "$LOG" 2>/dev/null)
    echo "CONTEXT WAS COMPACTED. The log holds ${N} compaction(s) for $(basename "$PROJ"), counted across all sessions."
    echo "Most recent: ${LAST:-unknown}."
    echo "Everything before that point is a summary, not the verbatim conversation. Summaries of"
    echo "your own earlier reasoning are the least reliable thing in your context. Before asserting"
    echo "any fact from before the compaction (a path, a date, a status, who said what), re-read it"
    echo "from the file. If you cannot re-read it, say that you are working from a summary."
    echo "Log: ${LOG}"
    ;;

  *)
    echo "[compaction-marker] unknown mode: $MODE (expected --log or --announce)" >&2
    exit 2
    ;;
esac
exit 0
