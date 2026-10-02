# agent-ops for agents

This repo holds modular tools for coding agents (Claude Code, Codex, OpenCode). Each top-level folder that has a `bin/` folder is one independent tool. Tools work alone and follow a common contract so you can audit, test, and install only what you need.

## The module contract

Each tool lives in its own folder with this layout:

- `README.md`: what the tool does, how to set it up, usage examples, how it fails when it cannot decide (it reports, never stays silent)
- `bin/<name>`: the executable (Python 3.9+ or bash 3.2+). It includes `--help` documentation
- `tests/test_*.py`: pytest tests that are hermetic: they use tmp_path for files, monkeypatch HOME, never touch the network or your real home directory
- `config.example.*`: if the tool needs a config file

A module may also hold helper folders and data files beside `bin/` (for example `lib/`, `probes/`, `trackers/`, `playwright-lane/`, `prices.json`). `install.sh` copies the whole module, `tests/` aside, to `$AGENT_OPS_HOME/<module>/` and links each file in `bin/` into `$PREFIX`, so the helpers stay together with the tool. A tool must find its helpers from its own real path (resolve symlinks), not from the current directory.

## How to run tests

From the repo root:
```
python -m pytest -q
```

Or for one module:
```
python -m pytest -q <module>/tests
```

The installer has its own tests in `tests/test_install.py` at the repo root.

Tests are hermetic. They create temporary files in `tmp_path` and set a fake HOME via monkeypatch so they never touch your real home directory or the network.

## Design rules

- Tools ship with safe defaults. No risky behavior until explicitly configured.
- Tools never edit your settings file. They print the code snippet you would add, and you decide whether to copy it.
- A tool that cannot decide fails closed and loud. Silence means "nothing to say", never "I could not tell".
- Every parameter comes from an environment variable or a config file, and every default is stated in the README.

## Contributing

Add a tool: create a new top-level folder following the module contract, write a line in the root README.md, and open a pull request. `./install.sh --list` must show the new module (it does automatically once the module has a `bin/` folder).

Add support for another harness or tracker: write one adapter file in that tool's adapter folder (for example `trackers/` or `probes/`) and add a snippet to the tool's README.

Keep module READMEs under 120 lines (the root README is exempt). Use plain language and short sentences. Never use marketing words (seamless, robust, leverage, powerful).

Tools must pass tests (`python -m pytest -q <module>/tests`), never download anything or need network access during tests, and never use personal paths or names in test fixtures (use `alice`, `bob`, `example.com`, `work`, `personal`).
