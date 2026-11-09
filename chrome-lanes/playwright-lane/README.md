# Playwright lane: a second browser lane in its own Chrome profile

The Playwright MCP server (`@playwright/mcp`) in extension mode drives a real Chrome window
through the Playwright extension. Install that extension in one dedicated Chrome profile and
this lane works only there. It does not use the Claude in Chrome extension, so it needs no
`chrome-lanes claim` and runs beside the session that holds the Chrome turn without changing
its selected browser. Any harness that speaks MCP over stdio can use it: Claude Code, Codex,
OpenCode and others.

The pairing is by token, not by a profile flag. The server has no option to name a profile.
It connects to the Playwright extension, and the token in your token file lets it skip the
approval prompt. So the lane can reach only a profile where the Playwright extension is
installed. Install it in exactly one profile and the lane never reaches your other profiles.
That profile is signed out of everything unless you sign it in.

## Setup

1. **Create a dedicated Chrome profile.** Click the profile icon in Chrome's toolbar, then
   Add. Name it for the job (for example `agent-lane`). Sign in to nothing you do not want
   an agent to use.
2. **Install the Playwright extension in that profile only.** In that profile's window,
   install the Playwright MCP extension from the link in the `@playwright/mcp` README.
3. **Save the pairing token, mode 0600.** The extension shows a token for
   `PLAYWRIGHT_MCP_EXTENSION_TOKEN`. Copy it, then paste it at this hidden prompt so it never
   lands in your shell history (works in bash and zsh):
   ```sh
   mkdir -p ~/.config/playwright-mcp && chmod 700 ~/.config/playwright-mcp
   ( umask 077; printf 'token: '; stty -echo; IFS= read -r T; stty echo; echo
     printf '%s' "$T" > ~/.config/playwright-mcp/token )
   chmod 600 ~/.config/playwright-mcp/token
   ```
4. **Install the wrapper and register it.** Copy the template, then use the snippet for your
   harness below:
   ```sh
   mkdir -p ~/.local/bin && install -m 700 playwright-ext.sh.example ~/.local/bin/playwright-ext.sh
   ```
   Edit the `PATH=` line in the copy if your `npx` is not in `/opt/homebrew/bin`,
   `/usr/local/bin` or `/usr/bin`.

## What the wrapper does

`playwright-ext.sh.example` (bash, `set -eu`):
- Sets a fixed `PATH`, so the harness's `PATH` cannot choose a different `node` or `npx`.
- Reads the token from `${PLAYWRIGHT_MCP_TOKEN_FILE:-$HOME/.config/playwright-mcp/token}`.
- Refuses to start (exit 1, message on stderr) if that file is missing, empty, or not mode 600.
- Exports it as `PLAYWRIGHT_MCP_EXTENSION_TOKEN`, never as a command-line argument.
- Pins the server version in `PLAYWRIGHT_MCP_VERSION` and runs
  `exec npx -y @playwright/mcp@$PLAYWRIGHT_MCP_VERSION --extension`. The version is pinned,
  its dependencies are not, and npx may fetch them at a start. This server drives a real
  Chrome, so for a stricter setup install it once with a lockfile (`npm ci` in a folder of
  its own) and point the `exec` line at that folder's `node_modules/.bin/mcp-server-playwright`.

## Register it

Replace `/path/to/playwright-ext.sh` with the absolute path of your copy.

Claude Code (user scope, so every project gets it):
```sh
claude mcp add --scope user playwright -- /path/to/playwright-ext.sh
```

OpenCode (`~/.config/opencode/opencode.jsonc`):
```jsonc
{
  "mcp": {
    "playwright": {
      "type": "local",
      "command": ["/path/to/playwright-ext.sh"],
      "enabled": true
    }
  }
}
```

Codex (`~/.codex/config.toml`):
```toml
[mcp_servers.playwright]
command = "/path/to/playwright-ext.sh"
args = []
```

Any other harness: it is a stdio MCP server. Point your harness at the wrapper.

## How it fails

The wrapper exits 1 with one line on stderr when the token file is missing, empty or not
mode 600. It never prints the token. If the extension is not installed or the profile's
window is closed, the error comes from `@playwright/mcp` itself.

## What was tested here, and what was not

- Tested, macOS bash 3.2: syntax, the three refusal paths, and the success path up to the
  `exec` line (with `npx` replaced by `echo`).
- Not run here: the `npx` start itself, the Linux `stat -c` branch, and the extension's
  install link and token page. That the token skips the extension's approval prompt comes
  from the `@playwright/mcp` docs, not from a test here.
- The Claude Code and OpenCode snippets match a working setup of version 0.0.80. The Codex
  snippet follows Codex's `mcp_servers` config shape and was never run.
- Check `npx @playwright/mcp@latest --help` before you bump the version.
