#!/usr/bin/env bash
# project-inventory.sh - compact, agent-readable listing of a project's files.
# Portable: macOS (BSD stat) and Linux (GNU stat). No GNU-only find flags.
# Run it from a SessionStart hook, or by hand, or from an instruction in
# CLAUDE.md / AGENTS.md. Run with --help for the full description.

set -uo pipefail

usage() {
  cat <<'EOF'
project-inventory.sh - list a project's files, newest first, with ages.

Usage: project-inventory.sh [dir] [max_files]
       project-inventory.sh --help

  dir         project directory. Default: $CLAUDE_PROJECT_DIR, else the
              current directory.
  max_files   how many rows to print. Default: 100.

Prints one row per file: modified date, size, age in days, path. Newest
first. Skips the directories named in CONTEXT_HYGIENE_SKIP, and .DS_Store.

After the list it prints a STALE block when files are older than the stale
limit (the 8 oldest of them), the last-modified date of CLAUDE.md, AGENTS.md
and README.md if present, and a one-line reminder to check provenance headers.

Environment:
  CLAUDE_PROJECT_DIR           default for dir (set by Claude Code)
  CONTEXT_HYGIENE_STALE_DAYS   age in days that counts as stale. Default: 30.
                               Must be a whole number.
  CONTEXT_HYGIENE_SKIP         directory names to leave out, separated by spaces.
                               A name is matched at any depth and may be a find
                               pattern such as '*.egg-info'. Default:
                               .git .pytest_cache __pycache__ node_modules .venv
                               venv dist build .next
                               Set it to an empty string to skip nothing.

Exit status: 0 when it ran or when it reported a problem on stdout (a hook
must not fail the session). 2 for a bad max_files or stale-days value.
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
esac

DIR="${1:-${CLAUDE_PROJECT_DIR:-$PWD}}"
MAX="${2:-100}"
STALE_DAYS="${CONTEXT_HYGIENE_STALE_DAYS:-30}"
SKIP="${CONTEXT_HYGIENE_SKIP-.git .pytest_cache __pycache__ node_modules .venv venv dist build .next}"

case "$MAX" in
  ''|*[!0-9]*) echo "[inventory] max_files must be a whole number, got: $MAX" >&2; exit 2 ;;
esac
case "$STALE_DAYS" in
  ''|*[!0-9]*) echo "[inventory] CONTEXT_HYGIENE_STALE_DAYS must be a whole number, got: $STALE_DAYS" >&2; exit 2 ;;
esac

cd "$DIR" 2>/dev/null || { echo "[inventory] cannot enter $DIR"; exit 0; }

TAB=$(printf '\t')
if stat -f '%m' . >/dev/null 2>&1; then
  STAT_FLAG=-f; STAT_FMT="%m${TAB}%z${TAB}%N"      # BSD: mtime, size, name
else
  STAT_FLAG=-c; STAT_FMT="%Y${TAB}%s${TAB}%n"      # GNU: mtime, size, name
fi
now=$(date +%s)

# Epoch seconds -> YYYY-MM-DD. BSD date takes -r SECONDS, GNU date takes -d @SECONDS.
rowdate() { date -r "$1" '+%Y-%m-%d' 2>/dev/null || date -d "@$1" '+%Y-%m-%d'; }

# Build find's prune expression from the skip list. Globbing is off so a pattern
# like '*.egg-info' reaches find as written.
PRUNE=()
set -f
for name in $SKIP; do
  [ "${#PRUNE[@]}" -gt 0 ] && PRUNE+=(-o)
  PRUNE+=(-name "$name")
done
set +f

list_files() {
  if [ "${#PRUNE[@]}" -gt 0 ]; then
    find . -type d \( "${PRUNE[@]}" \) -prune -o \
      -type f ! -name '.DS_Store' ! -name '*[[:cntrl:]]*' -exec stat "$STAT_FLAG" "$STAT_FMT" {} + 2>/dev/null
  else
    find . -type f ! -name '.DS_Store' ! -name '*[[:cntrl:]]*' -exec stat "$STAT_FLAG" "$STAT_FMT" {} + 2>/dev/null
  fi
}

# Files whose names hold a newline or another control character are left out of the listing,
# because such a name could print as extra lines of this report. They are counted instead.
odd_names() {
  if [ "${#PRUNE[@]}" -gt 0 ]; then
    find . -type d \( "${PRUNE[@]}" \) -prune -o -type f -name '*[[:cntrl:]]*' -print0 2>/dev/null
  else
    find . -type f -name '*[[:cntrl:]]*' -print0 2>/dev/null
  fi | tr -dc '\000' | wc -c | tr -d ' '
}

# One stat process per batch of files, not one per file.
# Rows: mtime<TAB>size<TAB>age-in-days<TAB>path, newest first.
ROWS=$(list_files |
  awk -F'\t' -v now="$now" '{
    mt = $1; sz = $2; age = int((now - mt) / 86400)
    if      (sz >= 1048576) h = int(sz / 1048576) "M"
    else if (sz >= 1024)    h = int(sz / 1024) "K"
    else                    h = sz "B"
    p = $3; for (i = 4; i <= NF; i++) p = p "\t" $i
    printf "%s\t%s\t%sd\t%s\n", mt, h, age, p
  }' | sort -rn)

TOTAL=$(printf '%s\n' "$ROWS" | grep -c . || true)
[ "$TOTAL" -eq 0 ] && { echo "[inventory] no files under $DIR"; exit 0; }

# Turn mtime into a date for the rows that are printed. Only those rows, so the
# cost of date conversion does not grow with the size of the tree.
show_rows() {
  while IFS="$TAB" read -r mt h age path; do
    printf '%s\t%s\t%s\t%s\n' "$(rowdate "$mt")" "$h" "$age" "$path"
  done
}

echo "PROJECT INVENTORY  $DIR"
echo "$TOTAL files. Newest first. Age is days since modified."
ODD=$(odd_names)
[ "${ODD:-0}" -gt 0 ] && echo "NOT LISTED: $ODD file(s) whose names contain a newline or control character."
echo "---"
printf '%s\n' "$ROWS" | head -n "$MAX" | show_rows
[ "$TOTAL" -gt "$MAX" ] && echo "... $(( TOTAL - MAX )) more not shown. Re-run with a larger limit if needed."

STALE=$(printf '%s\n' "$ROWS" | awk -F'\t' -v d="$STALE_DAYS" '{a=$3; sub(/d$/,"",a); if (a+0 > d+0) print}')
STALE_N=$(printf '%s\n' "$STALE" | grep -c . || true)
if [ "${STALE_N:-0}" -gt 0 ]; then
  echo "---"
  echo "STALE: $STALE_N file(s) untouched for more than $STALE_DAYS days. Oldest:"
  printf '%s\n' "$STALE" | tail -n 8 | show_rows
fi

for f in CLAUDE.md AGENTS.md README.md; do
  if [ -f "$f" ]; then
    row=$(printf '%s\n' "$ROWS" | grep -m1 "${TAB}\./$f\$" || true)
    mt=$(printf '%s' "$row" | cut -f1)
    if [ -n "$mt" ]; then echo "--- $f last modified $(rowdate "$mt")"; else echo "--- $f last modified unknown"; fi
  fi
done
echo "---"
echo "Reminder: files marked DERIVED never outrank their SOURCE. Check provenance headers before quoting."
