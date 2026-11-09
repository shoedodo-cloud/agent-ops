# agent-ops

Small, independent tools for engineers who run coding agents (Claude Code, Codex, OpenCode) on
their own machines. Each one deals with one thing that goes wrong when agents run unattended or
several run at once. Each installs alone and ships with hermetic tests. None of them edits your
harness settings: you get the snippet and paste it yourself. Each one exists because the thing it
guards against happened at least once.

## Pick a tool

| Tool | The problem | What it does |
|---|---|---|
| [chrome-lanes](chrome-lanes/) | Two sessions in Claude in Chrome flip each other's browser, and "Browser N" changes at every restart. | Maps browsers to profile names and gives one session the Chrome turn. A Playwright lane runs beside it. |
| [session-hooks](session-hooks/) | Every session starts cold and ends without a handoff. | Prints a checked baseline at the start and writes a dated handoff note at the end, optionally to Linear. |
| [pull-gate](pull-gate/) | Pulled hooks and scripts can run before anyone reads them. | Classifies incoming paths at session start and fast-forwards only what you safe-listed. |
| [job-health](job-health/) | A scheduled job looks healthy because its timestamp moves, while every run fails. | Grades each job OK, FAILING or STALE. An outcome it cannot read is never OK. |
| [secret-sweep](secret-sweep/) | A token or an `.env` file lands in a repo before anyone reads the diff. | Scans tracked files or full history for credential file names and token shapes. Never prints a match. |
| [context-hygiene](context-hygiene/) | An agent trusts a stale summary, or its memory after a compaction, over the files. | Prints a file inventory at session start and makes every compaction visible to the agent. |
| [session-usage](session-usage/) | Nothing shows what one session and its subagents used. | Reads the local transcript and prints tokens per model and per subagent, with a list-price estimate. |

## Install

```sh
git clone https://github.com/shoedodo-cloud/agent-ops.git
cd agent-ops
./install.sh job-health pull-gate
```

For each module you name, the installer copies the whole module folder to
`${AGENT_OPS_HOME:-~/.local/share/agent-ops}/<module>/`, links each file in its `bin/` into
`${PREFIX:-~/.local/bin}/`, and prints that module's setup steps. The links point into the copied
folder, so tools that load `lib/`, `probes/` or `trackers/` from beside themselves still find them.
`./install.sh --list` lists the modules. `./install.sh --uninstall <module>` removes one. The
installer never edits a config file, never downloads anything and never uses sudo.

Then read the setup steps the installer prints.

## Works with

"snippet, unverified" means the README gives a fragment for that harness that was never run in it.

| Module | Claude Code | Codex | OpenCode | Any harness with a shell |
|---|---|---|---|---|
| chrome-lanes, lane 1 | hook, shell | shell | shell | shell |
| chrome-lanes, lane 2 | MCP | snippet, unverified | MCP | n/a, needs MCP |
| session-hooks | hook | snippet, unverified | shell, via `AGENTS.md` | shell |
| pull-gate | hook | snippet, unverified | shell, hook format unchecked | shell |
| job-health | hook | snippet, unverified | shell | shell, cron |
| secret-sweep | git hook | git hook | git hook | shell, CI |
| context-hygiene | hook | snippet, unverified (no compaction announce) | shell | shell |
| session-usage | shell | shell (`--codex`) | n/a | n/a |

## The tools

### chrome-lanes

Claude in Chrome shows each open Chrome profile as "Browser N", numbered by connection order. The
numbers change at every restart, and two sessions at once flip each other's selected browser.
`chrome-lanes` reads each profile's extension storage, read-only, where the device id and label
persist, so an agent picks a browser by profile name. A lease gives one session the Chrome turn,
with wait, force and idle takeover. A hook tells a session when its turn was taken.

Two lanes. Lane 1 is Claude in Chrome, one session at a time, under the lease. Lane 2 is the
Playwright MCP in extension mode, paired by token to one dedicated profile, so it needs no lease.

```
$ chrome-lanes claim work --wait 590
claimed: work <device id> pid 4242
$ chrome-lanes release --all
released: work <device id>
```

Fails by: a storage file it cannot parse is counted, and its row says `(unreadable storage, N
files)`, so it cannot pass for a profile that never connected.

### session-hooks

A coding agent starts every session cold and ends it abruptly. The next session has no checked
baseline and no record of where the last one stopped. `session-start` prints the branch,
ahead/behind, uncommitted files, the last session note and the open tracker items. `session-wrap`
writes a dated note seeded from `git log`, and with `--post` sends its "next session starts here"
line to the tracker. A markdown ledger is the default tracker. The Linear adapter exists and is
untested against the live API. Adding GitHub Issues or Jira is one file
([session-hooks/CONTRIBUTING.md](session-hooks/CONTRIBUTING.md) ships a `gh` skeleton).

```
$ session-start
== session-start: api (repo folder name) at /home/alice/work/api, 2026-10-02 09:14 ==
branch: main
could not determine: ahead/behind upstream (branch 'main' has no upstream branch)
uncommitted: 1 path (git status --porcelain)
last session note: none, notes/sessions does not exist
tracker: file ledger notes/ledger.md, tag (api), from defaults, no config file
could not determine: open tracker items (no ledger at notes/ledger.md)
read-only: nothing merged, committed or written, no git fetch
```

Fails by: every line it could not establish prints as `could not determine: <what> (<why>)`.
An empty Linear answer is checked, so a wrong team key shows as an error.

### pull-gate

Pulled code executes. When several machines or agents push to the same repos, hooks and scripts
can arrive and run before anyone reads them. At session start `pull-gate` fetches every repo
under a root, read-only, and classifies each incoming path as executable, safe-listed or
unclassified. It runs `git merge --ff-only` by itself only when every incoming path is under a
safe prefix you listed, in a repo you named. Both lists are empty by default.

```
PULL-GATE: incoming changes, classified. Nothing has been run.
alpha: AUTO-FAST-FORWARDED, 1 file(s), all under safe prefixes, no exec pattern matched.
beta: 3 incoming commit(s), 3 file(s) changed: 1 executable, 1 safe-listed, 1 unclassified.
  EXECUTABLE, review before merge:
    bin/tool.sh
```

Fails by: when a repo it entered reached no verdict, the output starts with `THE CHECK ITSELF
FAILED on N of M repo(s)` and exits 1. A failed fetch is reported as "may be stale".

### job-health

A scheduled job (launchd, cron, a systemd timer) can run every day and still be broken. A check
that reads a fresh timestamp as success reports a dead backup as healthy for days. `job-health`
gives every job one of three states: OK (ran recently and reported success), FAILING (ran and
reported failure) or STALE (did not run, or reported nothing readable). It ships probes for log
files, git repos and Time Machine, and a new probe is one Python file.

```
$ job-health headline
**2 job(s) not OK: nightly-backup FAILING, db-dump STALE**
```

Fails by: a missing log, an unparseable outcome, or a probe that raises or hangs is STALE, with
the reason in the outcome column. A broken config exits 2 and prints the reason to stdout too.

### secret-sweep

Agents commit fast. A credential file or a token can land in a repo before anyone reads the diff.
`secret-sweep` scans the tracked files of every git repo under a directory, or every reachable
blob with `--history`, for credential-shaped file names and token patterns. It exits 1 on a hit
and never prints any character of a match. Run it as a git pre-push hook or in CI. A clean sweep
means these patterns were not found. It does not say a repo is safe to publish.

```
$ secret-sweep --history ~/code
app  (2 hits)
  .env  credential-filename  commit 1a2b3c4d5e6f
  config.txt  anthropic-key  commit 1a2b3c4d5e6f
```

Fails by: a repo that git cannot read is listed as an error, the others are still scanned, and
the exit code is 2. A run that read no file at all says `0 files scanned, nothing to conclude`
and also exits 2.

### context-hygiene

An agent that starts cold acts on a stale summary instead of on files. After a context compaction
it trusts its memory of a file over the file. `project-inventory.sh` lists the project's files
newest first, with a stale block, at session start. `inventory-snapshot.sh` shows what is new,
modified or gone since its last run. `compaction-marker.sh` logs each compaction and announces it
when the session resumes. [PROVENANCE.md](context-hygiene/PROVENANCE.md) is a one-page convention
for labelling files SOURCE, DERIVED or WORKING.

```
PROJECT INVENTORY  /home/alice/work/api
212 files. Newest first. Age is days since modified.
---
2026-10-02   4K   0d    ./docs/api-spec.md
---
STALE: 14 file(s) untouched for more than 30 days. Oldest:
2026-05-02   9K   153d  ./notes/old-plan.md
```

Fails by: when the snapshot comparison fails it says "the comparison is unknown", keeps the old
snapshot and exits 1. An announce with no log entry says the log hook is missing.

### session-usage

A coding-agent session writes every model call to a local transcript with token counts, and each
subagent gets its own file. Nothing shows you those numbers for one session. `session-usage` reads
the Claude Code or Codex transcript and prints tokens per model and per subagent, plus an estimate
at API list prices from a dated price table. It reads files only and sends nothing. The dollar
column is tokens times list prices, and your plan may charge differently.

```
$ session-usage
Thread usage (claude-code, 1 h 12 min)

| Who | Model | Calls | Fresh in | Cache write | Cache read | Output | ~list $ |
|---|---|---|---|---|---|---|---|
| this thread | <model> | 212 | 41k | 380k | 9.6M | 88k | 9.12 |
| agent: <task description> | <model> | 31 | 12k | 64k | 1.1M | 9k | 0.84 |
| **total** |  | 243 | 53k | 444k | 10.7M | 97k | **9.96** |

~list $ = tokens at API list prices from <prices.json> (as of 2026-09-25). A yardstick, not a bill.
```

Fails by: no session id or no transcript exits 2 and says where it looked. A model with no price
shows `n/a` and is left out of the total.

## How every tool behaves

- Policy is config with empty or safe defaults. pull-gate merges nothing until you list safe
  prefixes and repos. secret-sweep has no default root. chrome-lanes uses ssh only to hosts you list.
- Snippets are printed, settings are never edited. Where a tool needs a hook, it prints the
  fragment (`chrome-lanes snippet`, `session-hooks-snippet`, `context-hygiene-snippet.sh`) or its
  README shows it. You merge it yourself.
- A check that cannot decide fails closed and says why. Silence never means "could not tell".
  Every module README has a "How it fails" section.
- Python 3.9+ standard library or bash 3.2+. No package manager, nothing downloaded. The one
  exception is the optional Playwright lane, whose wrapper starts `@playwright/mcp` through `npx`.
- Tests are hermetic: pytest with `tmp_path`, a fake `HOME`, no network. Run them all with
  `python3 -m pytest -q` from the repo root. CI runs them on Ubuntu and macOS.
- macOS-only parts are named. The job-health `time_machine` probe is macOS only. chrome-lanes ran
  on macOS only, and its Linux and Windows paths are written but never run.

## Contributing

Read [AGENTS.md](AGENTS.md) and [CONTRIBUTING.md](CONTRIBUTING.md) first. Every module follows the
same contract: a short README, an executable in `bin/` with `--help`, hermetic tests in `tests/`,
and a `config.example.*` when it takes a config file. One tool per pull request.

## License

MIT. See [LICENSE](LICENSE).
