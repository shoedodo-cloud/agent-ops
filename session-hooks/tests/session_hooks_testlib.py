"""Helpers shared by the session-hooks tests. Named uniquely so other modules' tests never clash."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parent.parent
BIN = MODULE / "bin"

for _path in (str(MODULE), str(MODULE / "lib")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Every variable the tools read. Cleared so the developer's own shell never leaks in.
TOOL_ENV = [
    "SESSION_NOTES_DIR", "SESSION_PROJECT", "SESSION_HOOKS_CONFIG", "SESSION_HOOKS_TRACKERS",
    "SESSION_LEDGER", "SESSION_HOOKS_HOME", "SESSION_START_TIME", "LINEAR_API_KEY",
    "CLAUDE_PROJECT_DIR", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
]


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            env={**os.environ, **(env or {})}, check=True)
    return result.stdout.strip()


def run_cli(name: str, *args: str, cwd: Path | None = None, stdin: str = "",
            env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(BIN / name), *map(str, args)], cwd=cwd, input=stdin,
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, **(env or {})})


def load_script(name: str):
    """Import a bin/ script (no .py suffix) as a module, to call main() in-process."""
    mod_name = "session_hooks_bin_" + name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def section(text: str, heading: str) -> list:
    import sessionhooks
    return sessionhooks.section(text, heading)
