# session-hooks

Your coding agent starts every session cold and ends it abruptly. session-hooks gives it a
checked baseline at the start and a written handoff note at the end. `session-start` prints
the branch, ahead/behind, uncommitted files, the last session note and the open tracker items.
`session-wrap` writes a dated note (What changed, Decisions, Next session starts here, Open
questions) seeded from `git log`. Claude Code, Codex, OpenCode or a plain shell all run the same
command and get the same baseline. A markdown ledger is the default tracker, a Linear adapter
ships (not run against the live API), and adding GitHub Issues or Jira is one file
([CONTRIBUTING.md](CONTRIBUTING.md)).

## Setup in 60 seconds

1. From a clone of this repo: `./install.sh session-hooks` (or copy the `session-hooks/` folder somewhere and symlink its `bin/*` onto your PATH, so the tools stay next to `lib/` and `trackers/`).
2. In any git repo, run `session-start`.
3. At the end of the work, run `session-wrap --next "Wire the login form to the API"`.
4. Wire both into your harness: `session-hooks-snippet --harness claude` and merge the JSON.
5. Optional, Linear: `mkdir -p ~/.config/session-hooks && cp session-hooks/config.example.json ~/.config/session-hooks/config.json`,
   set `"tracker": "linear"` and your `team_key`, and put a personal API key in `LINEAR_API_KEY`.

## Usage

```
session-start [--repo DIR] [--fetch] [--project NAME] [--no-tracker] [--max-items N]
session-wrap  [--repo DIR] [--next TEXT] [--since TIME | --since-start] [--skip-if-noted]
              [--decision TEXT]... [--question TEXT]... [--project NAME] [--post]
session-hooks-snippet [--harness claude|codex|generic|all] [--json] [--bin-dir DIR]
```

`session-start` never merges, commits or writes. Only `--fetch` runs `git fetch` (the default
makes no network call to git). `session-wrap` always writes the note. It writes to the
tracker only with `--post`. Without it, it prints what it would send. `--help` on each tool
is the full reference.

The session start for "What changed" comes from `--since`, else `SESSION_START_TIME`, else the
transcript named in a hook payload on stdin (Claude Code sends one), else the time of the last
note, else 24 hours ago. The note says which one it used. `--skip-if-noted` does nothing when a
note was already written after a known session start, so an end-of-session hook does not
duplicate the note the agent wrote itself.

## Config

| Variable | Default | What |
|---|---|---|
| `SESSION_NOTES_DIR` | `notes/sessions` | notes folder, relative to the repo root |
| `SESSION_LEDGER` | `notes/ledger.md` | ledger for the file tracker (beats the config `path`) |
| `SESSION_PROJECT` | repo folder name | project name passed to the tracker |
| `SESSION_START_TIME` | unset | session start, ISO 8601 or Unix seconds |
| `SESSION_HOOKS_CONFIG` | `$HOME/.config/session-hooks/config.json` | config file |
| `SESSION_HOOKS_TRACKERS` | unset | extra adapter folders (`:`-separated), searched first |
| `SESSION_HOOKS_HOME` | the folder above `bin/` | where `lib/` and `trackers/` live |
| `LINEAR_API_KEY` | unset | Linear personal API key. Never put it in the config file |

Config file (see `config.example.json`). No file means `"tracker": "file"` with defaults.
`tracker` picks the adapter. `note_template` (default `""`, the built-in template) points at
your own template, relative to the repo root. Linear settings: `team_key` (required),
`project_name` (default `""`, all team issues), `projects` (repo project name to Linear
project name), `issue_id` (default `""`: `--post` opens a new issue, set it to comment on one
issue instead), `api_key_env` (`LINEAR_API_KEY`), `timeout_seconds` (10), `max_items` (50).
Open means every state except Linear's `completed` and `canceled` types.

## Harness snippets

`session-hooks-snippet` prints these with the resolved install path of the tools. Below, that
path is `/home/alice/.local/share/agent-ops/session-hooks/bin/`. It never edits a settings file.

Claude Code (`~/.claude/settings.json` or `.claude/settings.json`). SessionStart output goes
into the agent's context. The end hook is SessionEnd, because Stop fires after every reply:

```json
{"hooks": {
  "SessionStart": [{"hooks": [{"type": "command", "command": "/home/alice/.local/share/agent-ops/session-hooks/bin/session-start --repo \"$CLAUDE_PROJECT_DIR\""}]}],
  "SessionEnd": [{"hooks": [{"type": "command", "timeout": 30,
    "command": "/home/alice/.local/share/agent-ops/session-hooks/bin/session-wrap --repo \"$CLAUDE_PROJECT_DIR\" --since-start --skip-if-noted"}]}]}}
```

Codex (`~/.codex/hooks.json`): the same SessionStart entry. Codex has no session-end event and
its Stop fires every turn, so run `session-wrap` by hand or from a make target (the snippet
prints one). OpenCode and any harness that reads `AGENTS.md`: add the two lines the snippet
prints, one to run `session-start` first, one to run `session-wrap --next "..."` before finishing.

## How it fails

- `session-start` prints `could not determine: <what> (<why>)` for every line it could not
  establish: no upstream, a failed fetch, no git repo, a bad config, a missing ledger, a
  missing API key, an unknown adapter, an API error. It never drops the line.
- Ahead/behind without `--fetch` is labelled with the age of the last fetch.
- An empty Linear answer is checked: a wrong team key or project name is an error, not "0 open".
- `session-start` exits 0 whatever it finds (2 only for a bad command line).
- `session-wrap` exits 1 when the note cannot be written or `--post` fails, and says so. A
  note with no next line says "Not given", and nothing is sent.
- A script copied away from its folder says it cannot find `lib/` and `trackers/`.

## Limitations

- The Linear adapter's GraphQL shapes come from Linear's public GraphQL docs, not run against
  the live API. Tests run it against a fake transport only.
- The session start is read from a transcript only when the harness sends `transcript_path`
  (Claude Code does). Elsewhere, set `SESSION_START_TIME` or pass `--since`.
- The Codex fragment follows the Claude Code hook shape. It has not been run inside Codex.
- POSIX only (macOS and Linux). Nothing in it is macOS-only. Needs `git` and Python 3.9+.
