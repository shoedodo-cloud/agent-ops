"""Shared code for session-start, session-wrap and session-hooks-snippet. Standard library only.

Nothing in here writes to disk. Writing is done by the two places that are allowed to:
session-wrap (the note file) and a tracker's post_note/create_item (only with --post).
"""
from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import io
import json
import os
import re
import select
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

HERE = Path(__file__).resolve().parent
MODULE_HOME = HERE.parent  # the session-hooks folder: bin/, lib/, trackers/

DEFAULT_NOTES_DIR = "notes/sessions"
NOTE_HEADINGS = ("What changed", "Decisions", "Next session starts here", "Open questions")
NEXT_HEADING = "Next session starts here"
TRACKER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_(\d{4}))?")


# ---------------------------------------------------------------- config ---------------
class ConfigError(Exception):
    """The config file exists but cannot be used."""


@dataclass
class Config:
    path: Path
    found: bool
    data: Dict[str, Any] = field(default_factory=dict)


def config_path(env: Mapping[str, str]) -> Path:
    explicit = env.get("SESSION_HOOKS_CONFIG")
    if explicit:
        return Path(os.path.expanduser(explicit))
    home = env.get("HOME") or str(Path.home())
    return Path(home) / ".config" / "session-hooks" / "config.json"


def load_config(env: Mapping[str, str]) -> Config:
    path = config_path(env)
    if not path.exists():
        return Config(path, False, {})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: the top level must be a JSON object")
    return Config(path, True, data)


# ---------------------------------------------------------------- git ------------------
def git_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"     # git status must not rewrite the index
    env["GIT_TERMINAL_PROMPT"] = "0"    # never hang on a credential prompt
    env.setdefault("LC_ALL", "C")
    return env


def git(repo: Path, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout, env=git_env(),
    )


def git_out(repo: Path, *args: str, timeout: float = 20) -> Optional[str]:
    """stdout of a git command, or None when it failed or git is missing."""
    try:
        result = git(repo, *args, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def repo_root(start: Path) -> Optional[Path]:
    out = git_out(start, "rev-parse", "--show-toplevel")
    return Path(out) if out else None


def current_branch(root: Path) -> Tuple[Optional[str], str]:
    """(branch name or None, a human description)."""
    name = git_out(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    head = git_out(root, "rev-parse", "--short", "HEAD")
    if name:
        return name, name if head else f"{name} (no commits yet)"
    if head:
        return None, f"detached HEAD at {head}"
    return None, ""


def head_short(root: Path) -> str:
    return git_out(root, "rev-parse", "--short", "HEAD") or "no commit"


# ---------------------------------------------------------------- project and paths ----
def resolve_project(flag: Optional[str], env: Mapping[str, str], base: Path) -> Tuple[str, str]:
    """(project name, where it came from)."""
    if flag:
        return flag, "--project"
    if env.get("SESSION_PROJECT"):
        return env["SESSION_PROJECT"], "SESSION_PROJECT"
    return base.name, "repo folder name"


def notes_dir(env: Mapping[str, str], base: Path) -> Path:
    raw = env.get("SESSION_NOTES_DIR") or DEFAULT_NOTES_DIR
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else base / path


def rel(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def rel_or_name(path: Path, base: Path) -> str:
    """path relative to base when it is inside base, else only its file name.
    For text that leaves the machine: it never carries an absolute path."""
    try:
        return str(path.relative_to(base))
    except ValueError:
        return path.name


def newest_note(folder: Path) -> Optional[Path]:
    """Newest file under folder. A dated name (YYYY-MM-DD[_HHMM]...) beats mtime."""
    best: Optional[Path] = None
    best_key: Optional[tuple] = None
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            match = DATE_PREFIX_RE.match(name)
            stamp = (match.group(1) + (match.group(2) or "")) if match else ""
            key = (stamp, mtime, name)
            if best_key is None or key > best_key:
                best, best_key = path, key
    return best


def notes_since(folder: Path, timestamp: float) -> List[Path]:
    """Files under folder modified at or after timestamp, newest first."""
    found = []
    if not folder.is_dir():
        return []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= timestamp:
                found.append((mtime, path))
    return [p for _, p in sorted(found, reverse=True)]


def section(text: str, heading: str) -> List[str]:
    """Non-empty lines under '## heading' up to the next '## '."""
    out: List[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip().lower() == heading.lower()
            continue
        if inside and line.strip():
            out.append(line.rstrip())
    return out


# ---------------------------------------------------------------- time -----------------
def parse_time(raw: str) -> Optional[dt.datetime]:
    """ISO 8601 (with or without offset, 'Z' allowed) or Unix seconds. Aware result."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{9,11}(\.\d+)?", raw):
        return dt.datetime.fromtimestamp(float(raw)).astimezone()
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)  # Python 3.9 takes at most 6 fraction digits
    try:
        value = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return value.astimezone() if value.tzinfo else value.replace(tzinfo=None).astimezone()


def transcript_start(path: str) -> Optional[dt.datetime]:
    """Earliest 'timestamp' in the first lines of a JSONL transcript (Claude Code writes one)."""
    found: List[dt.datetime] = []
    try:
        with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle):
                if number >= 200:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and isinstance(record.get("timestamp"), str):
                    when = parse_time(record["timestamp"])
                    if when:
                        found.append(when)
    except OSError:
        return None
    return min(found) if found else None


def fmt_time(when: dt.datetime) -> str:
    return when.astimezone().strftime("%Y-%m-%d %H:%M")


@dataclass
class Since:
    git_since: str               # passed to git log --since
    label: str                   # what a human reads
    start: Optional[dt.datetime]  # the session start, only when it came from a real source


def resolve_since(explicit: Optional[str], env: Mapping[str, str], hook: Mapping[str, Any],
                  notes: Path, now: dt.datetime) -> Since:
    if explicit:
        when = parse_time(explicit)
        label = f"since {fmt_time(when) if when else explicit} (--since)"
        return Since(when.isoformat() if when else explicit, label, when)
    raw = env.get("SESSION_START_TIME", "")
    when = parse_time(raw)
    if when:
        return Since(when.isoformat(), f"since {fmt_time(when)} (session start, SESSION_START_TIME)", when)
    transcript = hook.get("transcript_path") if isinstance(hook, Mapping) else None
    if isinstance(transcript, str) and transcript:
        when = transcript_start(transcript)
        if when:
            return Since(when.isoformat(), f"since {fmt_time(when)} (session start, from the harness transcript)", when)
    last = newest_note(notes) if notes.is_dir() else None
    if last is not None:
        when = dt.datetime.fromtimestamp(last.stat().st_mtime).astimezone()
        return Since(when.isoformat(),
                     f"since {fmt_time(when)} (time of the last session note {last.name}, "
                     "session start time unknown)", None)
    when = now - dt.timedelta(hours=24)
    return Since(when.isoformat(),
                 f"since {fmt_time(when)} (last 24 hours, session start time unknown and no earlier note)",
                 None)


# ---------------------------------------------------------------- the next line --------
PASS_FAIL_WORDS = {"pass", "passed", "fail", "failed"}


def first_line_problem(line: str) -> str:
    """Why a next line would be a bad first line for a tracker item, or "" when it reads fine.
    It catches a pasted path, a markdown heading and a dump of test results."""
    text = line.strip()
    if text.startswith(("/", "~", "./", "../")):
        return f"it starts with a path ({text.split()[0][:40]})"
    if text.startswith("#"):
        return "it starts with # (a markdown heading, not a sentence)"
    words = [w.lower() for w in re.findall(r"[A-Za-z]+", text)]
    hits = [w for w in words if w in PASS_FAIL_WORDS]
    if hits and (len(words) == 1 or (len(hits) >= 2 and len(hits) * 2 >= len(words))):
        return "it looks like a list of PASS/FAIL results"
    return ""


# ---------------------------------------------------------------- stdin ----------------
def read_stdin(stream: Any, wait_seconds: float = 2.0) -> str:
    """Read stdin without hanging: nothing from a terminal, stop after a quiet pause."""
    if stream is None:
        return ""
    try:
        if stream.isatty():
            return ""
    except (AttributeError, ValueError):
        pass
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return stream.read()
    chunks: List[bytes] = []
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], wait_seconds)
        except (OSError, ValueError):
            break
        if not ready:
            break
        data = os.read(fd, 65536)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks).decode("utf-8", "replace")


def split_stdin(text: str) -> Tuple[Dict[str, Any], str]:
    """(hook payload, plain text). A JSON object on stdin is a harness hook payload."""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                return payload, ""
        except ValueError:
            pass
    return {}, stripped


# ---------------------------------------------------------------- trackers -------------
def tracker_dirs(env: Mapping[str, str]) -> List[Path]:
    dirs: List[Path] = []
    extra = env.get("SESSION_HOOKS_TRACKERS", "")
    for part in extra.split(os.pathsep):
        if part.strip():
            dirs.append(Path(os.path.expanduser(part.strip())))
    dirs.append(MODULE_HOME / "trackers")
    return dirs


def tracker_name(config: Config) -> str:
    return str(config.data.get("tracker") or "file")


def load_tracker(config: Config, env: Mapping[str, str], root: Path):
    """Build the configured adapter. Raises TrackerError when it cannot."""
    if str(MODULE_HOME) not in sys.path:
        sys.path.insert(0, str(MODULE_HOME))
    from trackers.base import Context, TrackerError

    name = tracker_name(config)
    if not TRACKER_NAME_RE.match(name) or name == "base":
        raise TrackerError(f"invalid tracker name {name!r} (lowercase letters, digits, _)")
    all_settings = config.data.get("trackers") or {}
    settings = all_settings.get(name) if isinstance(all_settings, dict) else None
    shipped = MODULE_HOME / "trackers"
    dirs = tracker_dirs(env)
    for folder in dirs:
        source = folder / f"{name}.py"
        if not source.is_file():
            continue
        try:
            if folder.resolve() == shipped.resolve():
                module = importlib.import_module(f"trackers.{name}")
            else:
                mod_name = f"session_hooks_tracker_{name}"
                spec = importlib.util.spec_from_file_location(mod_name, source)
                if spec is None or spec.loader is None:
                    raise TrackerError(f"cannot load {source}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
            adapter = getattr(module, "ADAPTER", None)
            if adapter is None:
                raise TrackerError(f"{source} does not set ADAPTER")
            return adapter(settings if isinstance(settings, dict) else {}, Context(Path(root), dict(env)))
        except TrackerError:
            raise
        except Exception as exc:  # a broken adapter must not break a session start
            raise TrackerError(f"adapter {source} failed to load: {type(exc).__name__}: {exc}") from exc
    raise TrackerError(f"no tracker named {name!r} (looked in {', '.join(str(d) for d in dirs)})")
