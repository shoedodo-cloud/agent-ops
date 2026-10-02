# chrome-lanes

**Lane 1: Claude in Chrome, one session at a time.** Agents see each open Chrome profile as
"Browser N" by connection order, so the numbers change at every restart, a new session inherits
the last selection, and two sessions at once flip each other's target. `chrome-lanes` reads each
profile's extension storage (read-only), where the device id and connect label persist, so an
agent picks a browser by profile name. A lease gives one session the Chrome turn, with wait,
force and idle takeover. A hook tells a session when its turn was taken.

**Lane 2: Playwright, in its own profile.** The Playwright MCP in extension mode, paired by
token to the Playwright extension in one dedicated profile. It needs no lease and never
touches lane 1. See [playwright-lane/README.md](playwright-lane/README.md).

## Setup in 60 seconds

1. From a clone of this repo: `./install.sh chrome-lanes` (or copy the `chrome-lanes/` folder somewhere and symlink its `bin/*` onto your PATH).
2. Write the first snapshot: `chrome-lanes snapshot`
3. Check it found your profiles: `chrome-lanes list --cached`
4. Print the Claude Code settings fragment: `chrome-lanes snippet`. Merge it into
   `~/.claude/settings.json` yourself. The tool never edits that file.
5. Optional: set up lane 2 with [playwright-lane/README.md](playwright-lane/README.md).

## Usage

```sh
chrome-lanes lookup work --cached     # device id, machine, dir, name, account, label
chrome-lanes claim work --wait 600    # take the Chrome turn, prints the device id
#   then in the agent: select_browser <device id>, and work in its own tabs
chrome-lanes release --all            # give the turn back when the browser part is done
chrome-lanes status                   # who holds what, clears dead leases
chrome-lanes open work                # a closed profile cannot be selected: open it first
```

`NAME` matches profile name, account or label, any case. `MACHINE:NAME` or a full device id
picks one row when a name is ambiguous. `HELD` (exit 4) names the holder, its project and how
long it has been idle. `chrome-lanes --help` is the full reference.

A paragraph you can paste into `CLAUDE.md` or `AGENTS.md`:

> Before using Claude in Chrome, run `chrome-lanes claim <profile> --wait 600` and select the
> device id it prints with `select_browser`. Never call `switch_browser`. HELD means another
> session has the turn: wait, do other work, or ask the user. Use `--force` only when the user
> says so. Run `chrome-lanes release --all` as soon as the browser part is done.

## Config

Environment variables only. No config file.

| Variable | Default | What |
|---|---|---|
| `CHROME_LANES_HOME` | `~/.config/chrome-lanes` | snapshot, `leases/`, `leases/taken/`, `peers/`. Files mode 0600 |
| `CHROME_LANES_MACHINE` | short hostname | this machine's name in leases and rows |
| `CHROME_LANES_PROFILE_ROOT` | per OS, below | Chrome's user data dir |
| `CHROME_LANES_STALE_MINUTES` | `15` | idle minutes before a holder is taken over |
| `CHROME_LANES_EXTENSION_ID` | Claude in Chrome's id | extension storage to read |
| `CHROME_LANES_CHROME_BIN` | `google-chrome` | Chrome command for `open` on Linux |
| `CHROME_LANES_HOSTS` | unset | several machines, `name=ssh-host,name=ssh-host` |
| `CLAUDE_PID` | set by Claude Code | identifies the session. Without it: first non-shell parent |

Profile root defaults: macOS `~/Library/Application Support/Google/Chrome`, Linux
`~/.config/google-chrome`, Windows `%LOCALAPPDATA%\Google\Chrome\User Data`.

**Several machines.** Unset `CHROME_LANES_HOSTS` means nothing ever goes over ssh. When set,
`snapshot --peers` stores each other machine's profile list under `peers/`, and a claim on a
profile that lives elsewhere places a lease copy there through `ssh HOST python3 -` (this
script's own source on stdin, nothing installed remotely). Names must match each machine's
`CHROME_LANES_MACHINE`. Remote copies are trusted until `--ttl` runs out (default 4 h).

## Harness snippets

Claude Code: `chrome-lanes snippet` prints this, with your path filled in (JSON on stdout,
a note per entry on stderr):

```json
{"permissions": {"deny": ["mcp__claude-in-chrome__switch_browser"]},
 "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "/path/to/chrome-lanes snapshot || true"}]}],
           "PreToolUse": [{"matcher": "mcp__claude-in-chrome__.*",
                           "hooks": [{"type": "command", "command": "/path/to/chrome-lanes guard", "timeout": 15}]}]}}
```

The deny stops `switch_browser`, which asks every Chrome window to reconnect and moves the
selection under every other session. The guard and the deny concern Claude in Chrome tools,
so they exist for Claude Code only. Codex and OpenCode sessions can still run `claim` and
`release` from their shell. For lane 2 in all three harnesses, see the Playwright README.

## How it fails

- `--cached` with no snapshot: exit 3 and the command to fix it.
- Chrome's profile list unreadable: exit 1, nothing written.
- A name that matches nothing or several rows: exit 2, `NO MATCH` or `AMBIGUOUS`.
- A storage file that cannot be parsed is skipped and counted. A row without an id then says
  `(unreadable storage, N files)`, never `(never connected)`.
- A lease whose timestamp cannot be read counts as stuck, so it cannot hold the turn forever.
- A remote lease copy that fails warns on stderr and asks the agent to tell the user. The
  claim itself still succeeds.
- A bad `CHROME_LANES_STALE_MINUTES` or `CHROME_LANES_HOSTS` entry: a note on stderr, the
  default is used or the entry ignored.
- The guard never blocks on anything except delivering a takeover note once.

## Limitations

- The lease is a convention, not a lock. Only sessions that run `claim` respect it.
- The test suite runs on Ubuntu and macOS in CI. The tool itself was exercised by hand on macOS
  only. The Linux and Windows profile paths, the `open` commands and the Windows pid check were
  never run by hand.
- It depends on the extension's storage keys `bridgeDeviceId` and `bridgeDisplayName`. If a
  future version renames them, every row shows `(never connected)`.
- Several machines need key-based ssh (`BatchMode=yes`) and python3 3.9 or newer on each one.
