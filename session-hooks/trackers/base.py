"""The tracker interface. An adapter is one file in trackers/ that subclasses Tracker.

An adapter module must set ADAPTER = <its Tracker subclass>. The loader builds it with
ADAPTER(settings, context), where settings is the adapter's block from the config file
(trackers.<name>) and context carries the repo root and the environment.

Reads (open_items) may run at every session start. Writes (post_note, create_item) only
run when the user passed --post. An adapter raises TrackerError when it cannot answer.
It never returns an empty list to mean "I could not tell".

post_note gets the session's next line, then optionally a second line "Session note: ...".
session-wrap puts no absolute path in that text. An adapter that sends a title uses only
the first line.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, TypedDict

TITLE_LIMIT = 80


class TrackerError(Exception):
    """The tracker could not answer. The message says why, in one line, with no secrets."""


class _ItemRequired(TypedDict):
    id: str     # the tracker's own id, e.g. "ENG-12" or "notes/ledger.md:14"
    title: str
    url: str    # a link, or a path:line for a file ledger
    state: str  # the tracker's own state name, e.g. "In Progress" or "open"


class Item(_ItemRequired, total=False):
    """One open item as every adapter returns it. The keys below are optional extras."""

    priority: int       # the tracker's own number, 0 = none
    labels: List[str]
    due: str            # a date, empty when none
    updated: str        # an ISO 8601 timestamp, empty when unknown


def first_line(text: str) -> str:
    """The first non-empty line of text, whitespace collapsed."""
    for line in str(text).splitlines():
        if line.strip():
            return " ".join(line.split())
    return ""


def short_title(line: str, limit: int = TITLE_LIMIT) -> str:
    """line cut to at most limit characters at a word boundary, with "..." when it was cut."""
    line = " ".join(str(line).split())
    if len(line) <= limit:
        return line
    room = limit - 3
    cut = line[:room]
    if not line[room].isspace():
        space = cut.rfind(" ")
        if space > 0:
            cut = cut[:space]
    return cut.rstrip() + "..."


@dataclass
class Context:
    """What an adapter may know about the session it serves."""

    repo_root: Path
    env: Mapping[str, str] = field(default_factory=dict)


class Tracker:
    """Base class. Subclasses implement open_items and create_item at least."""

    name = "base"

    def __init__(self, settings: Mapping[str, Any], context: Context) -> None:
        self.settings: Dict[str, Any] = dict(settings or {})
        self.context = context
        # One line each for things the adapter skipped or could not do while writing.
        # session-wrap prints them after a post.
        self.warnings: List[str] = []

    def describe(self, project: str) -> str:
        """One line for the baseline: which tracker, which filter."""
        return f"{self.name} (project {project})"

    def scope_label(self, project: str) -> str:
        """What the open-items count is about, e.g. "website" or "team ENG, all projects"."""
        return project

    def more_items_note(self) -> str:
        """After open_items: "" when it returned every open item, else why it stopped, e.g. "max_items=50"."""
        return ""

    def format_item(self, item: Item) -> str:
        """The line session-start prints for one item (it adds the "- " in front)."""
        ident, url = item.get("id", ""), item.get("url", "")
        tail = f"  {url}" if url and url != ident else ""
        return f"{ident} [{item.get('state', '')}] {item.get('title', '')}{tail}"

    def plan_post(self, project: str) -> str:
        """One line saying what post_note would do. Must not touch the network."""
        return f"post a note to {self.describe(project)}"

    def open_items(self, project: str) -> List[Item]:
        raise NotImplementedError

    def post_note(self, project: str, text: str) -> str:
        """Send a session's next line. Default: open a new item titled with the first line
        (cut at 80 characters at a word boundary), the whole text as its body.
        Returns a URL or path."""
        return self.create_item(project, short_title(first_line(text)), text)

    def create_item(self, project: str, title: str, body: str) -> str:
        raise NotImplementedError


# ------------------------------------------------------------ untrusted repo content ----
_CONTROL = __import__("re").compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def clean_text(text: str, limit: int = 200) -> str:
    """One printable line from untrusted text (a note, a ledger line, an issue title).
    Control and bidi characters become '?', so a value cannot forge extra lines or hide text."""
    return _CONTROL.sub("?", " ".join(str(text).split()))[:limit]


def escape_problem(path, root) -> str:
    """Why a path that should sit inside the repo is unsafe to read or write, or "".

    A cloned repo controls its own files. A symlinked notes folder or ledger would let it
    point session-wrap's writes, or session-start's reads, at any file of the user's.
    Only relative paths are checked: an absolute path came from the user's own setting."""
    from pathlib import Path
    root_p = Path(root)
    path_p = Path(path)
    try:
        rel = path_p.relative_to(root_p)
    except ValueError:
        return ""                     # absolute path from the user's own config or env
    cur = root_p
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            return f"{cur.relative_to(root_p)} is a symlink inside the repo, refusing to follow it"
    try:
        path_p.resolve().relative_to(root_p.resolve())
    except ValueError:
        return f"{rel} resolves outside the repo, refusing to follow it"
    return ""
