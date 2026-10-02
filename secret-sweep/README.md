# secret-sweep

Agents commit fast. A credential file or a token can land in a repo before anyone reads the diff.
`secret-sweep` scans the tracked files of every git repo under a directory, or with `--history`
every blob reachable from any ref, for credential-shaped file names and token patterns. It never
edits, commits or deletes anything, and never prints any character of a match. A clean sweep means
these patterns were not found. It does not mean a repo is safe to publish.

## Setup in 60 seconds

Needs `python3` 3.9 or newer and `git`. Nothing else to install.

1. From a clone of this repo: `./install.sh secret-sweep` (or copy the `secret-sweep/` folder somewhere and symlink its `bin/*` onto your PATH).
2. Sweep a directory of repos: `secret-sweep ~/code`. Add `--history` to check old commits (slow).
3. Read the exit code: 0 clean, 1 hits, 2 error or no file scanned.

## Usage

```
secret-sweep [--history] [--json] [root]
```

- `root` is the directory to sweep. If it is a git repo, only that repo is swept. Otherwise
  every repo up to 3 levels below it is swept. Symlinked directories are not followed.
- Default mode scans the tracked files as they are on disk now. Files scanned are listed per repo.
- `--history` scans every blob reachable from any ref, including files that were deleted later.
  It reads every blob, so it is slow on big repos.
- `--json` prints one JSON object. Each hit has `repo`, `path`, `pattern` and `commit` (null
  outside `--history`). Errors and the exit code are in the object too.

A hit looks like this. The commit is the first one that contained it.
```
app  (2 hits)
  .env  credential-filename  commit 1a2b3c4d5e6f
  config.txt  anthropic-key  commit 1a2b3c4d5e6f
```

You get one hit per file and pattern. If a hit is real, revoke the credential first, then remove
the file, then decide about the history. Deleting a file does not remove it from any clone.

## Config

There is no config file. The root has no default, so it never points at anyone's home.

| Setting | Default | Meaning |
|---|---|---|
| `root` argument or `SWEEP_ROOT` | none, required | What to sweep. The argument wins over the variable. |

The patterns live at the top of `bin/secret-sweep`, in `SECRET_FILE` and `CONTENT_RULES`.

| Pattern name | What it matches |
|---|---|
| `anthropic-key` | `sk-ant-` then 16 or more key characters |
| `openai-key` | `sk-` (not `sk-ant-`) then 40 or more letters, digits, `_` or `-` |
| `github-token` | `ghp_` `gho_` `ghu_` `ghs_` `ghr_` then 20 or more letters and digits, or `github_pat_` then 40 or more |
| `aws-access-key-id` | `AKIA` then 16 capitals or digits |
| `slack-token` | `xoxb-` `xoxa-` `xoxp-` `xoxr-` `xoxs-` then 10 or more letters, digits or `-` |
| `google-api-key` | `AIza` then 30 or more key characters |
| `private-key-header` | a PEM private key header line (RSA, EC, OpenSSH or plain) |
| `generic-api-key` | `api_key`, `api-key` or `apikey`, then `:` or `=`, then 20 or more key characters that include a digit. Optional quotes. Case-insensitive. |
| `credential-filename` | `.env` and `.env.*`, `id_rsa`, `id_ed25519`, `*.pem`, `*.p12`, `*.pfx`, `*.key`, `*credential*.json`, `*token*.txt`, `*.keychain`, `*.keychain-db` |

The placeholder files `.env.example`, `.env.sample`, `.env.template` and `.env.dist` are not
reported by name. Their content is still scanned. The name patterns are broad on purpose, so
expect some false positives such as a public `.pem` certificate.

Content is read as bytes and never decoded, so no encoding crashes a scan and a token in a binary is found.

## Use it as a pre-push hook

Save this as `.git/hooks/pre-push` and run `chmod +x .git/hooks/pre-push`:

```sh
#!/bin/sh
top=$(git rev-parse --show-toplevel)
exec /path/to/secret-sweep "$top"
```

A hit exits 1 and the push stops. The hook scans the files on disk, not the commits being pushed.
Use `--history` in CI for that.

## Use it in CI

Copy `bin/secret-sweep` into your repo as `tools/secret-sweep`. This GitHub Actions job fails the
build on any hit or error:

```yaml
name: secret-sweep
on: [push, pull_request]
jobs:
  sweep:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: python3 tools/secret-sweep "$GITHUB_WORKSPACE" --history
```

## How it fails

It reports when it cannot decide. It never passes silently.

- No root, a root that is not a directory, a root with no git repos, or no `git`: exit 2.
- Every repo has zero files to scan: `0 files scanned, nothing to conclude`, exit 2, not exit 0.
- A repo that git cannot read (a broken `.git`, a failed `git log`): that repo is listed as an
  error, the others are still scanned, and the exit code is 2. Hits found elsewhere are printed.
- Tracked files that are missing from disk cannot be read. They are counted in `unreadable` and
  checked by name only. This is not an error, because a staged deletion is normal.

## Limitations

- It matches known token shapes and file names. A secret with no recognisable shape is not found.
- The generic key pattern needs a digit in the value and will miss a key that has none.
- Default mode reads the files on disk, not the index. `--history` reads only what is reachable
  from a ref. Unreachable objects and the reflog are not scanned.
- Submodule contents and symlink targets are not scanned. Repos deeper than 3 levels below the
  root are not found. Sweep those as their own root.
- Nothing is macOS-only. Only the shell examples assume a Unix shell.
