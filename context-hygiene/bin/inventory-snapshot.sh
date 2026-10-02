#!/usr/bin/env bash
# inventory-snapshot.sh - what changed in this project since the last snapshot.
# Companion to project-inventory.sh. That one answers "what exists". This one
# answers "what moved since I last looked".
#
# Snapshots live OUTSIDE the project, so nothing is added to the repo and no
# diff churn lands in git. Run with --help for the full description.
#
# Portable: macOS (BSD stat/date) and Linux (GNU stat/date).

set -uo pipefail
export LC_ALL=C   # join and sort must agree on collation

usage() {
  cat <<'EOF'
inventory-snapshot.sh - show which files are new, modified or gone since the
last snapshot, then store a new snapshot.

Usage: inventory-snapshot.sh [dir] [--no-store]
       inventory-snapshot.sh --help

  dir         project directory. Default: $CLAUDE_PROJECT_DIR, else the
              current directory.
  --no-store  print the diff but leave the stored snapshot as it was.

The first run in a directory stores a baseline and reports no diff. Later runs
compare file modification times against the stored snapshot. Output lists NEW,
MODIFIED and GONE paths (up to 40 each). GONE means absent now, not deleted by
anyone in particular.

Snapshots are tab-separated files (path, mtime), one per project, in:
  $CONTEXT_HYGIENE_HOME/snapshots/
Default for CONTEXT_HYGIENE_HOME: $HOME/.config/context-hygiene

Skips the directories named in CONTEXT_HYGIENE_SKIP, and .DS_Store. It reads the
same variable, with the same default, as project-inventory.sh, so the inventory
and the snapshot agree about which files a project has:
  CONTEXT_HYGIENE_SKIP   directory names to leave out, separated by spaces. A name
                         is matched at any depth and may be a find pattern such as
                         '*.egg-info'. Default: .git .pytest_cache __pycache__
                         node_modules .venv venv dist build .next
                         Set it to an empty string to skip nothing.

Exit status: 0 when it ran or when it reported a problem on stdout. 1 when the
comparison itself failed (it says so rather than reporting "no changes").
EOF
}

DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
STORE=1
SKIP="${CONTEXT_HYGIENE_SKIP-.git .pytest_cache __pycache__ node_modules .venv venv dist build .next}"
for a in "$@"; do
  case "$a" in
    -h|--help) usage; exit 0 ;;
    --no-store) STORE=0 ;;
    *) DIR="$a" ;;
  esac
done

cd "$DIR" 2>/dev/null || { echo "[snapshot] cannot enter $DIR"; exit 0; }
DIR="$PWD"

SLUG=$(printf '%s' "$DIR" | sed 's|/|-|g; s|^-||')
STATE_DIR="${CONTEXT_HYGIENE_HOME:-$HOME/.config/context-hygiene}/snapshots"
PREV="$STATE_DIR/${SLUG}.tsv"
mkdir -p "$STATE_DIR" 2>/dev/null || echo "[snapshot] cannot create $STATE_DIR, nothing will be stored"

TAB=$(printf '\t')
if stat -f '%m' . >/dev/null 2>&1; then
  FMT=bsd; STAT_FLAG=-f; STAT_FMT="%N${TAB}%m"     # BSD: name, mtime
else
  FMT=gnu; STAT_FLAG=-c; STAT_FMT="%n${TAB}%Y"     # GNU: name, mtime
fi
mtime() { if [ "$FMT" = bsd ]; then stat -f '%m' "$1" 2>/dev/null; else stat -c '%Y' "$1" 2>/dev/null; fi; }
fmtdate() { date -r "$1" '+%Y-%m-%d %H:%M' 2>/dev/null || date -d "@$1" '+%Y-%m-%d %H:%M'; }

CUR=$(mktemp) || { echo "[snapshot] cannot create a temporary file"; exit 1; }
trap 'rm -f "$CUR" "$CUR.j" 2>/dev/null' EXIT

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
      -type f ! -name '.DS_Store' -exec stat "$STAT_FLAG" "$STAT_FMT" {} + 2>/dev/null
  else
    find . -type f ! -name '.DS_Store' -exec stat "$STAT_FLAG" "$STAT_FMT" {} + 2>/dev/null
  fi
}

# One stat process per batch of files, not one per file. Rows: path<TAB>mtime.
list_files | sort -t"$TAB" -k1,1 > "$CUR"

TOTAL=$(grep -c . "$CUR" || true)

echo "INVENTORY DIFF  $DIR"

if [ ! -f "$PREV" ]; then
  echo "No previous snapshot. Baseline stored: $TOTAL files."
  echo "The next run in this project will report changes against this point."
  [ "$STORE" -eq 1 ] && cp "$CUR" "$PREV"
  exit 0
fi

PREV_TS=$(mtime "$PREV")
echo "Since last snapshot ($(fmtdate "$PREV_TS")). $TOTAL files now."
echo "---"

# join on path: 1=path 2=old-mtime 3=new-mtime ; unpaired lines flagged
if ! join -t"$TAB" -a1 -a2 -e '-' -o '0,1.2,2.2' "$PREV" "$CUR" > "$CUR.j" 2>/dev/null; then
  echo "[snapshot] join failed, so the comparison is unknown. The stored snapshot was left as it was."
  exit 1
fi

ADDED=$(awk -F'\t' '$2=="-"{print $1}' "$CUR.j")
REMOVED=$(awk -F'\t' '$3=="-"{print $1}' "$CUR.j")
CHANGED=$(awk -F'\t' '$2!="-" && $3!="-" && $2!=$3 {print $1}' "$CUR.j")

show() { # label, list
  n=$(printf '%s\n' "$2" | grep -c . || true)
  [ "$n" -eq 0 ] && return
  echo "$1 ($n):"
  printf '%s\n' "$2" | head -n 40 | sed 's/^/  /'
  [ "$n" -gt 40 ] && echo "  ... $(( n - 40 )) more"
}

show "NEW" "$ADDED"
show "MODIFIED" "$CHANGED"
show "GONE" "$REMOVED"

if [ -z "$ADDED$REMOVED$CHANGED" ]; then
  echo "No file changes since the last snapshot."
fi

echo "---"
echo "GONE means absent now, not deleted by anyone in particular. Check before concluding."

[ "$STORE" -eq 1 ] && cp "$CUR" "$PREV"
exit 0
