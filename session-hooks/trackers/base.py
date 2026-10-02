"""The tracker interface. An adapter is one file in trackers/ that subclasses Tracker.

An adapter module must set ADAPTER = <its Tracker subclass>. The loader builds it with
ADAPTER(settings, context), where settings is the adapter's block from the config file
(trackers.<name>) and context carries the repo root and the environment.

Reads (open_items) may run at every session start. Writes (post_note, create_item) only
run when the user passed --post. An adapter raises TrackerError when it cannot answer.
It never returns an empty list to mean "I could not tell".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, TypedDict


class TrackerError(Exception):
    """The tracker could not answer. The message says why, in one line, with no secrets."""


class Item(TypedDict):
    """One open item as every adapter returns it."""

    id: str     # the tracker's own id, e.g. "ENG-12" or "notes/ledger.md:14"
    title: str
    url: str    # a link, or a path:line for a file ledger
    state: str  # the tracker's own state name, e.g. "In Progress" or "open"


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

    def describe(self, project: str) -> str:
        """One line for the baseline: which tracker, which filter."""
        return f"{self.name} (project {project})"

    def plan_post(self, project: str) -> str:
        """One line saying what post_note would do. Must not touch the network."""
        return f"post a note to {self.describe(project)}"

    def open_items(self, project: str) -> List[Item]:
        raise NotImplementedError

    def post_note(self, project: str, text: str) -> str:
        """Send a session's next line. Default: open a new item. Returns a URL or path."""
        short = " ".join(text.split())
        if len(short) > 70:
            short = short[:67] + "..."
        return self.create_item(project, f"Next session ({project}): {short}", text)

    def create_item(self, project: str, title: str, body: str) -> str:
        raise NotImplementedError
