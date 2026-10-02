# Add a tracker in one file

A tracker adapter is one Python file. It subclasses `Tracker` from `trackers/base.py` and
sets `ADAPTER` to its class. Standard library only.

## The interface

```python
class Tracker:
    name = "base"
    def __init__(self, settings, context): ...          # settings = trackers.<name> from the config
    def describe(self, project) -> str: ...              # one line for the baseline
    def plan_post(self, project) -> str: ...             # what post_note would do, no network
    def open_items(self, project) -> list[Item]: ...     # Item = {id, title, url, state}
    def post_note(self, project, text) -> str: ...       # returns a URL or path:line
    def create_item(self, project, title, body) -> str: ...
```

`context.repo_root` is the repo, `context.env` the environment. Read secrets from
`context.env`, never from the config file. Raise `TrackerError("why")` when you cannot
answer. Never return an empty list to mean "I could not tell": session-start prints an
empty list as "0 open items", and a `TrackerError` as "could not determine".

Only `open_items` runs at session start. `post_note` and `create_item` run only when the
user passed `--post`. `post_note` defaults to opening a new item with `create_item`.

## Skeleton: GitHub Issues with the `gh` CLI

Issues labelled with the project name count as open items. Save as `github.py`.

<!-- skeleton:start -->
```python
"""GitHub Issues adapter: open issues labelled with the project name, through the gh CLI."""
import json
import subprocess

from trackers.base import Tracker, TrackerError


class GitHubTracker(Tracker):
    name = "github"

    def __init__(self, settings, context, runner=subprocess.run):
        super().__init__(settings, context)
        self.repo = self.settings.get("repo", "")  # "owner/name", empty = the repo gh sees
        self.run = runner

    def _gh(self, *args):
        cmd = ["gh", *args] + (["--repo", self.repo] if self.repo else [])
        done = self.run(cmd, capture_output=True, text=True, cwd=str(self.context.repo_root))
        if done.returncode != 0:
            raise TrackerError(f"gh {args[0]} {args[1]}: {done.stderr.strip() or 'failed'}")
        return done.stdout

    def open_items(self, project):
        rows = json.loads(self._gh("issue", "list", "--label", project, "--json", "number,title,url,state"))
        return [{"id": f"#{r['number']}", "title": r["title"], "url": r["url"],
                 "state": r["state"].lower()} for r in rows]

    def create_item(self, project, title, body):
        return self._gh("issue", "create", "--label", project, "--title", title, "--body", body).strip()


ADAPTER = GitHubTracker
```
<!-- skeleton:end -->

The REST API works the same way: build a `urllib.request.Request` to
`https://api.github.com/repos/<owner>/<name>/issues`, with the token from `context.env`,
and keep the transport injectable as `trackers/linear.py` does.

## Register it

Either drop the file into `trackers/`, or keep it outside this repo and point
`SESSION_HOOKS_TRACKERS` at its folder (several folders: separate them with `:`). Those
folders are searched before `trackers/`. Then select it in the config:

```json
{ "tracker": "github", "trackers": { "github": { "repo": "example/website" } } }
```

## Tests

Add `tests/test_<name>_tracker.py`. Tests are hermetic: `tmp_path` for files, a
monkeypatched `HOME`, and no network. Inject a fake runner or transport and assert:

- `open_items` maps the tracker's answer to `id`, `title`, `url`, `state`.
- A failure raises `TrackerError` with a readable message and no secret in it.
- `post_note` and `create_item` send what you expect, and nothing is sent without `--post`.

`tests/test_session_hooks_contributing.py` runs the skeleton above with a fake `gh`, so
keep it working when you edit this page. Run: `python3 -m pytest -q session-hooks/tests`.
