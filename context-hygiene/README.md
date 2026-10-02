# context-hygiene

An agent that starts a session cold acts on a stale summary instead of on files. After a context compaction it trusts its memory of a file over the file. These scripts make a project's state visible at session start and make compactions loud, so the agent re-reads facts from files.

- `project-inventory.sh` lists the project's files, newest first, with ages and a stale block.
- `inventory-snapshot.sh` shows what is new, modified or gone since its last run.
- `compaction-marker.sh` logs each compaction and announces it when the session resumes.
- `context-hygiene-snippet.sh` prints the hook configuration. It never edits a file.
- [PROVENANCE.md](PROVENANCE.md) is a one-page labelling convention (SOURCE, DERIVED, WORKING).

## Setup in 60 seconds

1. From a clone of this repo: `./install.sh context-hygiene` (or copy the `context-hygiene/` folder somewhere and symlink its `bin/*` onto your PATH).
2. Print the hook configuration: `~/.local/bin/context-hygiene-snippet.sh`
3. Merge the Claude Code part by hand into the `hooks` object of `~/.claude/settings.json`.
4. Start a new session. The first thing in context should be `PROJECT INVENTORY`.
5. Optional: copy the rule from `PROVENANCE.md` into your `CLAUDE.md` or `AGENTS.md`.

To link the scripts somewhere else, run `PREFIX=/opt/tools ./install.sh context-hygiene`. Then print
the hooks with `PREFIX=/opt/tools context-hygiene-snippet.sh`, so they point there.

## Usage

```
project-inventory.sh [dir] [max_files]     # default: current project, 100 rows
inventory-snapshot.sh [dir] [--no-store]   # first run stores a baseline
compaction-marker.sh --log | --announce
```

Every script has `--help`, which is the full documentation. Example inventory output (date, size, age, path):

```
PROJECT INVENTORY  /home/alice/work/api
212 files. Newest first. Age is days since modified.
---
2026-10-02   4K   0d    ./docs/api-spec.md
2026-09-28   1K   4d    ./src/handlers.py
---
STALE: 14 file(s) untouched for more than 30 days. Oldest:
2026-05-02   9K   153d  ./notes/old-plan.md
```

Snapshot output lists `NEW`, `MODIFIED` and `GONE` paths. `GONE` means absent now, not deleted by anyone in particular.

## Config

| Variable | Default | Used by |
|---|---|---|
| `CLAUDE_PROJECT_DIR` | the current directory | all three scripts |
| `CONTEXT_HYGIENE_HOME` | `$HOME/.config/context-hygiene` | snapshot, marker |
| `CONTEXT_HYGIENE_STALE_DAYS` | `30` | inventory |
| `CONTEXT_HYGIENE_SKIP` | `.git .pytest_cache __pycache__ node_modules .venv venv dist build .next` | inventory, snapshot |
| `PREFIX` | `$HOME/.local/bin` | snippet |

Snapshots go to `$CONTEXT_HYGIENE_HOME/snapshots/`, one file per project. The compaction log is `$CONTEXT_HYGIENE_HOME/compaction.log`. Nothing is written into the project.

## Harness snippets

Claude Code, in `~/.claude/settings.json` (the snippet script prints this with your paths):

```json
{ "hooks": {
  "SessionStart": [
    { "hooks": [ { "type": "command", "command": "\"/home/alice/.local/bin/project-inventory.sh\"" } ] },
    { "matcher": "compact",
      "hooks": [ { "type": "command", "command": "\"/home/alice/.local/bin/compaction-marker.sh\" --announce" } ] }
  ],
  "PreCompact": [
    { "hooks": [ { "type": "command", "command": "\"/home/alice/.local/bin/compaction-marker.sh\" --log" } ] }
  ]
} }
```

Why two hooks for compaction: PreCompact output is not shown to the model, so it only logs. SessionStart with the `compact` matcher runs when the session resumes, and its output is shown.

Codex, in `~/.codex/hooks.json` (`context-hygiene-snippet.sh --codex`):

```json
{ "hooks": {
  "SessionStart": [
    { "matcher": "startup|resume",
      "hooks": [ { "type": "command", "command": "\"/home/alice/.local/bin/project-inventory.sh\"", "statusMessage": "Project inventory" } ] }
  ],
  "PreCompact": [
    { "hooks": [ { "type": "command", "command": "\"/home/alice/.local/bin/compaction-marker.sh\" --log", "statusMessage": "Log compaction" } ] }
  ]
} }
```

Not run in Codex. The Codex fragment has no announce entry. No hook that runs after a compaction and feeds the model is confirmed there. An entry for an event your build does not know is ignored without an error, so after a compaction check that `compaction.log` gained a line.

Any other harness: run `project-inventory.sh` at session start if it has a start hook, and put the line "run project-inventory.sh before you begin" into its instruction file if it does not.

## How it fails

These never say "all clear" when they could not tell.

- Inventory: a directory it cannot enter, or an empty one, is reported on stdout and exits 0, so a hook does not fail the session. A bad `max_files` or stale-days value exits 2.
- Snapshot: if the comparison itself fails it says "the comparison is unknown", keeps the old snapshot and exits 1. If it cannot create the state directory it says so.
- Marker: `--announce` with no matching log entry still announces the compaction and says the `--log` hook is missing. A log it cannot write is reported on stderr. An unknown mode exits 2.
- Snippet: warns on stderr when a script is missing from `PREFIX`. It refuses a relative `PREFIX`.

## Limitations

- The test suite runs on Ubuntu and macOS in CI. The tool itself was exercised by hand on macOS only.
- Changes are found by modification time. A touched file shows as `MODIFIED`. A content change that keeps its mtime is missed.
- The compaction count covers all sessions in a project, and the log is never rotated. Delete the file to reset it.
- Snapshot files are named after the project path with `/` turned into `-`. Two paths that differ only in `/` versus `-` share one.
- File names that contain a tab or a newline are not handled.
- Whether a harness shows hook output to the model is the harness's behaviour. These tests cover the scripts, not the harnesses.

## Tests

`python3 -m pytest -q tests` runs the hermetic tests. They set `HOME` and `CONTEXT_HYGIENE_HOME` under a temp directory.
