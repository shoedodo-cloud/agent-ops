# pull-gate

Pulled code executes. Hooks, scripts and agent settings all run on the machine that pulled them.
When several machines or agents push to the same repos, a session should see what is incoming
before it runs anything.

pull-gate fetches at session start (read-only), classifies every incoming path as executable or not,
and prints a banner. It runs `git merge --ff-only` by itself only when every incoming path matches a
safe list you wrote, in a repo you named. Anything else waits for a person.

## Setup in 60 seconds

Needs `python3` (3.9 or newer) and `git`. Nothing else.

1. From a clone of this repo: `./install.sh pull-gate` (or copy the `pull-gate/` folder somewhere and symlink its `bin/*` onto your PATH).
2. Check it runs: `pull-gate --help`
3. Copy the config: `mkdir -p ~/.config/pull-gate && cp pull-gate/config.example.json ~/.config/pull-gate/config.json`
4. Try a sweep that cannot merge: `pull-gate check --root ~/code --no-merge`
5. Add the session-start hook for your harness (snippets below).
6. Once the output looks right, fill in `safe_prefixes` and `safe_repos`. Until then nothing auto-merges.

## Usage
```
pull-gate check [--root DIR] [--no-merge]   sweep every git repo directly under DIR
pull-gate decide REPO PATH...               verdict for a list of paths, no git, no network
pull-gate-session-start.sh [check args]     the hook wrapper, prints the banner
```

The root is `--root`, else `$PULL_GATE_ROOT`, else the parent of the current directory. `check`
sweeps the root (if it is a repo) and every directory directly under it that is one. Per repo it
fetches `origin/<current branch>` and compares. Example output:

```
PULL-GATE: incoming changes, classified. Nothing has been run.
alpha: AUTO-FAST-FORWARDED, 1 file(s), all under safe prefixes, no exec pattern matched.
beta: 3 incoming commit(s), 3 file(s) changed: 1 executable, 1 safe-listed, 1 unclassified.
  EXECUTABLE, review before merge:
    bin/tool.sh
  NO VERDICT, matches no exec pattern and no safe prefix:
    docs/guide.txt
```

Each path gets a class: **exec** (matches an `exec_patterns` regex, never auto-merged, even
under a safe prefix), **safe** (under a safe prefix, no exec match), **unclassified** (matches
neither, so nothing says whether it executes and there is no verdict) or **suspicious** (absolute,
contains `..` or control characters, refused). The batch gets one verdict, because a fast-forward
is all or nothing. `decide` exits 0 for `AUTO-FF`, 1 for `MANUAL`, 2 for `NO-VERDICT`, 3 for an
unusable config, 64 for a usage error. Auto-merge also needs no local unpushed commits and no
rebase or merge in progress. `--ff-only` fast-forwards or refuses. It cannot merge or drop a commit.

## Config
File: `${PULL_GATE_CONFIG:-$HOME/.config/pull-gate/config.json}`, a JSON object. Keys that start
with an underscore are ignored, so use them as comments. `config.example.json` is a template.

| Key | Default | Meaning |
|---|---|---|
| `safe_prefixes` | `[]` | Path prefixes that may fast-forward unattended. Each must end with `/` |
| `safe_repos` | `[]` | Repo directory names where auto-fast-forward is allowed at all |
| `exec_patterns` | `[]` | Case-insensitive regexes, matched anywhere in the path. A match means "executes" |

All lists are empty by default, so nothing is classified and nothing merges until you configure
it. The example ships exec patterns and leaves both safe lists empty. A missing file means an
empty policy and the banner says so. Invalid JSON, an unknown key, a prefix without a trailing
slash or a bad regex is reported loudly and also means an empty policy.

| Env var | Default | Meaning |
|---|---|---|
| `PULL_GATE_CONFIG` | see above | config file path |
| `PULL_GATE_ROOT` | parent of cwd | directory to sweep |
| `PULL_GATE_THROTTLE` | `900` | seconds. Skip a repo's fetch if its last one is newer |
| `PULL_GATE_FETCH_TIMEOUT` | `20` | seconds allowed per fetch |
| `PULL_GATE_BUDGET` | `60` | seconds allowed for all fetches together |
| `PULL_GATE_BIN` | next to the wrapper | path of the `pull-gate` script the wrapper runs |

## Harness snippets

The hook's stdout is the banner. The wrapper always exits 0, so it never blocks a session.
pull-gate never edits your settings. Copy the snippet yourself, with the real path.

Claude Code (`~/.claude/settings.json` or `.claude/settings.json`). Codex
(`~/.codex/hooks.json`) is assumed to take the same shape. That is not tested against a live
Codex session, so check your version's hook docs and approve the hook if the app asks.

```json
{ "hooks": { "SessionStart": [ { "hooks": [
  { "type": "command", "command": "/path/to/agent-ops/pull-gate/bin/pull-gate-session-start.sh" }
] } ] } }
```

OpenCode, and anything else: any harness that can run a shell command at session start can call
`pull-gate-session-start.sh`. OpenCode's hook format was not checked for this README.

## How it fails
It reports. It does not pass silently.
- Every repo the sweep enters is counted (SEEN). Every repo that comes out with an explicit
  result is counted (VERDICTS). If they differ, the output starts with `THE CHECK ITSELF FAILED
  on N of M repo(s)`, says it is not an all-clear, and the exit code is 1.
- A git error, an unreadable count or any exception inside one repo is a missing verdict. The
  other repos are still reported.
- A failed fetch, or a spent time budget, is reported as "may be stale". No all-clear is printed.
- A path that matches no pattern gives no verdict and is listed. A detached HEAD is reported.
  A repo with no origin is listed under "Not compared".
- Git cannot say if a rebase or merge is in progress: no auto-merge, `could not determine: repo state`.
- If `python3` or the script is missing, or it crashes or prints nothing, the wrapper prints the
  same failure banner. The all-clear line appears only when every repo was compared.

## Limitations

- Classification reads the path, never the content. A file under a safe prefix is trusted by
  where it is. List only files that are generated and never run.
- `exec_patterns` lists known-bad shapes. A script with an unusual name is "unclassified", not
  "executable". It still does not auto-merge, but read the unclassified list.
- Compares only with `origin/<current branch>`. Submodules and symlink targets are not looked at.
- File names are data. Control characters are escaped, but a name can still contain words.
- A fetch can use your credential helper or SSH agent. Prompts are off and fetches are time-boxed.
- POSIX only. The test suite runs on Ubuntu and macOS in CI. The tool itself was exercised by
  hand on macOS only.

Tests: `python3 -m pytest -q pull-gate/tests` (real git in temp dirs, no network, fake HOME).
