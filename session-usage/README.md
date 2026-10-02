# session-usage

A coding-agent session writes every model call to a transcript file, with token counts, and each
subagent gets its own file. Nothing shows you those numbers for one session. `session-usage` reads
the local transcript and prints tokens per model and per subagent, plus an estimated cost at API
list prices from a dated price table. It reads files only. It sends nothing anywhere.

Token counts are read from the transcript ("checked"). The dollar column is tokens times list
prices, a yardstick. Your plan may be flat-rate, so it is not a bill.

## Setup in 60 seconds

1. From a clone of this repo: `./install.sh session-usage` (or copy the `session-usage/` folder somewhere and symlink its `bin/*` onto your PATH).
2. Check it runs: `session-usage --help`
3. Inside a Claude Code session, ask the agent to run `session-usage`. You get one markdown table:
   a row per model for the main thread and for each subagent, then a total. Columns are Calls,
   Fresh in, Cache write, Cache read, Output and `~list $`.

Python 3.9 or newer, standard library only.

## Usage

```
session-usage                       current Claude Code session ($CLAUDE_CODE_SESSION_ID)
session-usage --session ID          another Claude Code session (an id, or a path to its .jsonl)
session-usage --codex [PATH]        Codex: that rollout file, or the newest one for this directory
session-usage --json                the same data as JSON (rows, totals, price date)
session-usage --prices PATH         use your own price table
session-usage --calibrate --history PATH    optional weekly-points column, see below
```

`--help` is the full reference. The table includes each subagent's task description, copied from
its `.meta.json`. That is your own data and can hold project or file names. Read it before you
paste a table anywhere.

## Config

| What | Default |
|---|---|
| `SESSION_USAGE_HOME` | `~/.config/session-usage`. Holds `calibration.json` and an optional `prices.json`. |
| `CLAUDE_CONFIG_DIR` | `~/.claude`. Transcripts are read from its `projects/` folder. |
| `CODEX_HOME` | `~/.codex`. Rollouts are read from its `sessions/` folder. |
| `SESSION_USAGE_HISTORY` | unset. Same as `--history`. |
| `--prices PATH` | `$SESSION_USAGE_HOME/prices.json` if present, else the `prices.json` shipped here. |

**Price table.** `prices.json` lists model-name fragments with dollars per million tokens. The first
`match` that appears in the model name wins, so put specific names first. `as_of` is the date the
prices were read. It is printed under every table, with a warning when it is over 120 days old.
List prices change: check your copy against your provider's page. A model with no entry shows `n/a`,
is named in the footer, and is left out of the total. Cache writes cost `cache_write_multiplier`
times input. The shipped 2 is the one-hour cache rate (assumed, not read from transcripts). Use 1.25
for the five-minute cache.

**Weekly points (optional).** Some plans show usage as a percentage of a weekly limit. To estimate
what one session cost in that unit, the tool needs a factor from list-price dollars to percentage
points. It ships none. You fit your own:

1. Record your weekly percentage now and then (daily is enough) in a JSON file:
   `{"snapshots": [{"timestamp": 1790000000, "weekly": 42.5}]}`. `timestamp` is unix seconds or
   ISO-8601. Add `"buckets": {"name": 12.0}` for a separate limit that counts only models whose
   name contains `name`.
2. Run `session-usage --calibrate --history history.json`. It needs two weekly resets (a fall of
   more than 5 points) in the last 35 days. `--days N` changes the window.
3. It prints one row per week and saves the pooled fit to `$SESSION_USAGE_HOME/calibration.json`.
   `--no-save` only prints.

After that the table gains a `~weekly pts` column. Without `calibration.json` the column is not
there. Codex rows never get points. `--calibrate` scans every transcript under the Claude Code
projects folder (read-only), which can be many files, and it sees only this machine. Usage from
other machines or web chat counts toward your percentage but is not in these files, so the fitted
value reads high.

## Where transcripts live

Claude Code, one JSON-lines file per session, subagents in a folder beside it:

```
~/.claude/projects/<project-folder>/<session-id>.jsonl
~/.claude/projects/<project-folder>/<session-id>/subagents/agent-<id>.jsonl
~/.claude/projects/<project-folder>/<session-id>/subagents/agent-<id>.meta.json   (task description)
```

`<project-folder>` is the working directory with every non-alphanumeric character turned into `-`.
One API response appears on several lines with the same usage, so lines are counted once per
request id. This layout was checked against a real install. Claude Code can change it.

Codex, one rollout per session, split by date:

```
~/.codex/sessions/<year>/<month>/<day>/rollout-<timestamp>-<id>.jsonl
```

Its first line records the working directory, which is how `--codex` finds the newest rollout for
the directory you run it in. Codex reports running totals, not per-call usage, so the last total in
the file is used and Calls shows `-` on Codex rows. Cached input is shown apart from fresh input.

## Harness snippets

Nothing to add to a harness. Claude Code exposes `CLAUDE_CODE_SESSION_ID` to commands the agent
runs, so "run session-usage" works as is. For Codex, run `session-usage --codex` in the project directory.

## How it fails

It reports and exits with code 2 when it cannot decide. It never passes silently.

- No session id, or no transcript for that id: the message says where it looked.
- A transcript with no usage prints "no usage recorded". Unreadable lines are counted in the footer.
- A broken `--prices` file stops the run. A broken default price file warns and costs show `n/a`.
- A broken `calibration.json` warns and the points column is left out.
- `--calibrate` stops on fewer than two weekly resets, no priced usage, or an unreadable history.

## Limitations

- Estimates only. List prices are not what your plan charges.
- Only the Claude Code and Codex layouts above are read, as they are today.
- The shipped table has no Codex models, so those rows show tokens and `n/a`. Add your own entries.
- The test suite runs on Ubuntu and macOS in CI. The tool itself was exercised by hand on macOS
  only. Nothing in it is macOS-specific. Windows was not tried.
- Tests: `python3 -m pytest -q session-usage/tests`, from the repo root.
