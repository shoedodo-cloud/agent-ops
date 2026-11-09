"""File ledger adapter: a markdown file with one item per line.

Format (items start at the beginning of a line, indented lines below an item are its body):

    - [ ] (website) Fix the flaky login test        open
    - [~] (website, api) Move auth to the new API   in progress
    - [x] (website) Ship the footer                 done, never listed

Ledger path: $SESSION_LEDGER, else trackers.file.path in the config, else notes/ledger.md.
A relative path is relative to the repo root.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional

from trackers.base import Context, Item, Tracker, TrackerError, escape_problem

DEFAULT_LEDGER = "notes/ledger.md"
ITEM_RE = re.compile(r"^[-*] \[([ ~xX])\] \(([^)]*)\)\s*(.*)$")
STATES = {" ": "open", "~": "in progress", "x": "done", "X": "done"}
NEW_LEDGER_HEADER = (
    "# Ledger\n\n"
    "One item per line: `- [ ] (project) text`. `[~]` is in progress, `[x]` is done.\n\n"
)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _clean_tag(project: str) -> str:
    tag = re.sub(r"[(),]", " ", project)
    tag = _one_line(tag)
    if not tag:
        raise TrackerError("project name is empty after removing ( ) and ,")
    return tag


class FileTracker(Tracker):
    name = "file"

    def __init__(self, settings, context: Context) -> None:
        super().__init__(settings, context)
        raw = context.env.get("SESSION_LEDGER") or self.settings.get("path") or DEFAULT_LEDGER
        path = Path(os.path.expanduser(str(raw)))
        self.path = path if path.is_absolute() else Path(context.repo_root) / path

    @property
    def label(self) -> str:
        try:
            return str(self.path.relative_to(self.context.repo_root))
        except ValueError:
            return str(self.path)

    def describe(self, project: str) -> str:
        return f"file ledger {self.label}, tag ({project})"

    def plan_post(self, project: str) -> str:
        return f"append an open item tagged ({project}) to {self.label}"

    def _check_path(self) -> None:
        problem = escape_problem(self.path, self.context.repo_root)
        if problem:
            raise TrackerError(problem)

    def open_items(self, project: str) -> List[Item]:
        self._check_path()
        if not self.path.is_file():
            raise TrackerError(f"no ledger at {self.label}")
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise TrackerError(f"cannot read {self.label}: {exc}") from exc
        wanted = project.strip().lower()
        items: List[Item] = []
        for number, line in enumerate(lines, 1):
            match = ITEM_RE.match(line)
            if not match:
                continue
            mark, tags, text = match.groups()
            state = STATES[mark]
            if state == "done":
                continue
            if wanted not in {t.strip().lower() for t in tags.split(",")}:
                continue
            where = f"{self.label}:{number}"
            items.append({"id": where, "title": text.strip(), "url": where, "state": state})
        return items

    def post_note(self, project: str, text: str) -> str:
        """The first line is the next line. Any later lines (the session note) go in brackets."""
        head, _, rest = str(text).strip().partition("\n")
        detail = _one_line(rest)
        today = dt.date.today().isoformat()
        title = f"Next session: {_one_line(head)}" + (f" ({detail})" if detail else "")
        return self._append(project, f"{title} ({today})", [])

    def create_item(self, project: str, title: str, body: str) -> str:
        return self._append(project, _one_line(title), str(body or "").splitlines())

    def _append(self, project: str, title: str, body_lines: List[str]) -> str:
        tag = _clean_tag(project)
        self._check_path()
        existing: Optional[str] = None
        if self.path.exists():
            try:
                existing = self.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise TrackerError(f"cannot read {self.label}: {exc}") from exc
        text = existing if existing is not None else NEW_LEDGER_HEADER
        if text and not text.endswith("\n"):
            text += "\n"
        line_number = text.count("\n") + 1
        new = [f"- [ ] ({tag}) {title}"] + ["  " + b.rstrip() for b in body_lines if b.strip()]
        text += "\n".join(new) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".ledger-")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            mode = self.path.stat().st_mode & 0o777 if existing is not None else 0o644
            os.chmod(tmp, mode)
            os.replace(tmp, self.path)
        except OSError as exc:
            raise TrackerError(f"cannot write {self.label}: {exc}") from exc
        return f"{self.label}:{line_number}"


ADAPTER = FileTracker
