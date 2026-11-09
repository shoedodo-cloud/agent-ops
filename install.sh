#!/bin/bash
# agent-ops installer. Copies whole modules and links their bin/ tools onto your PATH.
# Never uses sudo, never downloads anything, never edits a config file.
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
PREFIX="${PREFIX:-$HOME/.local/bin}"
AOH="${AGENT_OPS_HOME:-$HOME/.local/share/agent-ops}"
case "$AOH" in /*) ;; *) AOH="$PWD/$AOH" ;; esac
AOH="${AOH%/}"
# Install and uninstall delete $AOH/<module>. Refuse a home that would make that a folder of
# yours: the filesystem root, your home itself, or the repo you are installing from.
HOME_REAL=$(cd "$HOME" 2>/dev/null && pwd -P || echo "$HOME")
AOH_REAL=$(cd "$AOH" 2>/dev/null && pwd -P || echo "$AOH")
case "$AOH_REAL" in
  "" | / | "$HOME_REAL" | "$ROOT")
    echo "AGENT_OPS_HOME=$AOH is not a folder of its own (it is /, your home or this repo). Unset it or pick a new folder." >&2
    exit 1 ;;
esac

usage() {
  cat <<EOF
Usage: install.sh [--force] <module> [<module>...]    install modules
       install.sh --uninstall <module>...   remove the links and the installed copy
       install.sh --list                    show the modules in this repo
       install.sh --help

Each module is copied to \$AGENT_OPS_HOME/<module>/ (default: ~/.local/share/agent-ops).
Each file in its bin/ is linked into \$PREFIX (default: ~/.local/bin), so helper folders
(lib/, probes/, trackers/) stay beside the tool. tests/ is not copied.

The installer refuses to replace a real file in \$PREFIX. It also refuses to replace a
symlink there that points outside \$AGENT_OPS_HOME, because it did not make that link.
--force replaces such a symlink. It never replaces a real file.
EOF
  exit "$1"
}

list_modules() { for d in "$ROOT"/*/; do if [ -d "${d}bin" ]; then basename "$d"; fi; done; }

valid_name() { case "$1" in "" | */* | .*) return 1 ;; esac; }

# The module's setup steps, as the README writes them. Step 1 of every README is the install
# itself, which has just happened, so it is shown as done and its continuation lines are left out.
setup_lines() {
  [ -f "$ROOT/$1/README.md" ] || return 0
  awk '
    /^## Setup/ { f = 1; next }
    f && /^## / { exit }
    f && /^1\. / { for (; b > 0; b--) print ""; print "1. (done: installed by install.sh)"; seen = 1; skip = 1; next }
    f && skip && /^[ \t]+[^ \t]/ { next }
    f && NF { skip = 0; for (; b > 0; b--) print ""; print; seen = 1; next }
    f && seen { b++ }
  ' "$ROOT/$1/README.md"
}

# Remove symlinks in $PREFIX that point into this module's install dir.
unlink_module() {
  [ -d "$PREFIX" ] || return 0
  for l in "$PREFIX"/*; do
    [ -L "$l" ] || continue
    case "$(readlink "$l")" in "$AOH/$1/"*) rm -f "$l" ;; esac
  done
}

# Refuse to replace a real file, or a symlink we did not make (one that points outside
# $AOH), unless --force names the second case. A real file is never replaced.
check_links() {
  for f in "$ROOT/$1/bin"/*; do
    l="$PREFIX/$(basename "$f")"
    if [ -L "$l" ]; then
      target=$(readlink "$l")
      case "$target" in
        "$AOH/"*) ;;
        *)
          if [ "$FORCE" -ne 1 ]; then
            echo "symlink points outside $AOH, refusing to replace it: $l -> $target (run again with --force to replace it)" >&2
            return 1
          fi
          ;;
      esac
    elif [ -e "$l" ]; then
      echo "not a symlink, refusing to replace it: $l (move it away and run again)" >&2
      return 1
    fi
  done
}

install_module() {
  dest="$AOH/$1"
  unlink_module "$1"
  rm -rf "$dest"
  mkdir -p "$dest" "$PREFIX"
  cp -R "$ROOT/$1/." "$dest/"
  rm -rf "$dest/tests"
  find "$dest" \( -name .pytest_cache -o -name __pycache__ \) -prune -exec rm -rf {} +
  for f in "$dest/bin"/*; do
    if [ -f "$f" ]; then
      chmod +x "$f"
      ln -sfn "$f" "$PREFIX/$(basename "$f")"
    fi
  done
  echo "Installed $1 -> $dest"
  next=$(setup_lines "$1")
  if [ -n "$next" ]; then
    printf 'Next:\n%s\n' "$next"
  fi
  echo
}

# --force may sit anywhere in the arguments. Rotate the others so their order is kept
# (an empty array breaks "set -u" on bash 3.2, so no array here).
FORCE=0
n=$#
while [ "$n" -gt 0 ]; do
  a="$1"; shift; n=$((n - 1))
  if [ "$a" = "--force" ]; then FORCE=1; else set -- "$@" "$a"; fi
done

case "${1:-}" in
  -h | --help) usage 0 ;;
  --list) list_modules; exit 0 ;;
  --uninstall)
    shift
    [ $# -gt 0 ] || usage 1
    for m in "$@"; do
      if ! valid_name "$m" || [ ! -d "$AOH/$m" ]; then
        echo "not installed: $m (looked in $AOH)" >&2
        exit 1
      fi
    done
    for m in "$@"; do
      unlink_module "$m"
      rm -rf "$AOH/$m"
      echo "Removed $m"
    done
    exit 0
    ;;
  "" | -*) usage 1 ;;
esac

for m in "$@"; do
  if ! valid_name "$m" || [ ! -d "$ROOT/$m/bin" ]; then
    echo "unknown module: $m (run ./install.sh --list to see the modules)" >&2
    exit 1
  fi
  check_links "$m" || exit 1
done

for m in "$@"; do install_module "$m"; done

case ":$PATH:" in
  *":$PREFIX:"*) ;;
  *) echo "$PREFIX is not on your PATH. Add it to use the installed tools." ;;
esac
