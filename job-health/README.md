# job-health

A scheduled job (launchd, cron, a systemd timer) can "run" every day and still be broken. A check
that reads a fresh timestamp as success will report a dead backup as healthy for days, because the
timestamp keeps moving. `job-health` gives every job one of three states, never two. **OK**: ran
recently AND reported success. **FAILING**: ran and reported failure. **STALE**: did not run, or ran
and reported nothing readable. A job whose outcome cannot be parsed is STALE, never OK. It prints a
markdown table, worst first, and a one-line headline that names every job that is not OK.

## Setup in 60 seconds

Needs python3 3.9 or newer. Standard library only.

1. From a clone of this repo: `./install.sh job-health` (or copy the `job-health/` folder somewhere and symlink its `bin/*` onto your PATH).
2. `mkdir -p ~/.config/job-health && cp job-health/config.example.json ~/.config/job-health/jobs.json`, then edit the paths
3. `job-health report`

## Usage

```
job-health report          # headline (only if something is not OK), then the table
job-health headline        # one line: "2 job(s) not OK: ..." or "3 job(s) OK"
job-health probe <name>    # one job, verbose: probe file, config keys, verdict, outcome
job-health --help
```

Exit codes: `0` every job OK, `1` at least one FAILING or STALE, `2` config or usage error.

## Config

| Setting | Default |
|---|---|
| `JOB_HEALTH_CONFIG` | `~/.config/job-health/jobs.json` (`--config PATH` overrides it) |
| `JOB_HEALTH_PROBES` | empty. Extra probe folders, colon-separated, searched before the bundled `probes/`. First match wins. |

The config file is a JSON list of jobs. Every job has `name` and `probe`. Optional on every job:
`owner` (default `-`), `max_age_hours` (default 24, git_repo 26), `timeout_seconds` (default 60).
The other keys belong to the probe. See `config.example.json`.

| Probe | Keys | Checks |
|---|---|---|
| `log_file` | `path`, `ok_regex`, `fail_regex`, `timestamp` (`auto`, `iso_z`, `bracketed_iso`, `space_local`, default `auto`) | The last line that carries a timestamp. `fail_regex` is checked first. A line matching neither is STALE. |
| `git_repo` | `path` | Mid-rebase, mid-merge or conflicts: FAILING. Unpushed commits older than `max_age_hours`: FAILING. Behind: reported, not graded. |
| `time_machine` | none | Age of the latest completed Time Machine backup. macOS only. |

## Write your own probe in 15 lines

A probe is a file `<name>.py` with a `probe(job) -> Health` function. This one checks that the
newest file matching a glob is recent and not empty:

```python
# ~/.config/job-health/probes/newest_file.py
import glob, os, time
from job_health import Health, STALE, classify

def probe(job):
    files = glob.glob(os.path.expanduser(job["glob"]))
    if not files:
        return Health(job["name"], "", STALE, None, "no file matches " + job["glob"])
    newest = max(files, key=os.path.getmtime)
    age_h = (time.time() - os.path.getmtime(newest)) / 3600
    size = os.path.getsize(newest)
    ok = size > 0                       # an empty dump is a failure, not a success
    state = classify(age_h, job.get("max_age_hours", 24), ok)
    return Health(job["name"], "", state, age_h, "%s, %d bytes" % (os.path.basename(newest), size))
```

Then `export JOB_HEALTH_PROBES=~/.config/job-health/probes` and add a job:
`{"name": "db-dump", "probe": "newest_file", "glob": "~/dumps/*.sql.gz", "max_age_hours": 26}`.

`classify(age_h, max_age_h, ok)` is the whole decision: pass `ok=None` when you cannot tell, and the
row is STALE. You do not have to catch every error. If your probe raises, the row is STALE with the
error text. Other helpers you can import: `health_from_log`, `last_stamped`, `outcome_from_regex`,
the `ts_*` timestamp readers, `probe_git`.

## Harness snippets

Show the headline at the start of every agent session. `|| true` keeps the exit code at 0, so the
harness still reads the line when a job is not OK.

Claude Code, `~/.claude/settings.json`. Codex takes the same JSON in `~/.codex/hooks.json`.
Not run in Codex.

```json
{ "hooks": { "SessionStart": [ { "hooks": [ { "type": "command", "command": "job-health headline || true" } ] } ] } }
```

OpenCode: no snippet shipped. Run `job-health headline` from your shell profile instead.

A daily report file, from cron (`crontab -e`):

```
15 7 * * * $HOME/.local/bin/job-health report > $HOME/job-health.md 2>&1
```

## How it fails

Every path that cannot decide reports STALE with the reason in the outcome column. Nothing passes silently.

- Log missing, no line with a readable timestamp, last line matches neither pattern: STALE.
- Probe name unknown or invalid, probe file broken, probe raises, probe hangs past
  `timeout_seconds`, probe returns something that is not a `Health` row or an unknown state: STALE,
  with the reason.
- A git repo with no upstream, or output that does not parse: STALE.
- Duplicate job names: the second one is STALE.
- Config missing, not valid JSON, not a list, or an empty list: exit 2, and the reason is printed to
  stdout as well as stderr, so a report file shows it.
- `probe <name>` prints the config keys, never their values, in case a custom probe's config holds a token.

## Limitations

- `time_machine` is macOS only. On other systems it reports STALE. `tmutil` may need Full Disk
  Access for your terminal.
- `git_repo` checks one local checkout. It does not fetch and does not reach other machines.
- `log_file` reads the last 256 KB of the log.
- Probes run one after another. A probe that times out keeps running in the background until
  `job-health` exits.
- Probe files run as your user. Only list folders you control in `JOB_HEALTH_PROBES`.
