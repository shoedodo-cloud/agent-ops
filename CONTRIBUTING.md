# Contributing

Read AGENTS.md first. It describes the module contract and design rules every tool follows.

## Pull request checklist

- One tool per pull request.
- Tests are required. Run `python -m pytest -q <module>` and ensure all pass.
- Tests must be hermetic: no network, no real HOME directory, no personal paths or names.
- Use fake names in fixtures: `alice`, `bob`, `example.com`, `work`, `personal`.
- Before opening a PR, run the leak check and fix any hits:

  ```bash
  grep -rnE '/Users/|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}' <module> | grep -v 'example\.'
  ```

- No downloads or installations during tests.
