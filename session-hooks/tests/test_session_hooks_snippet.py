"""session-hooks-snippet: valid JSON fragments, no Stop wiring, prints only."""
from __future__ import annotations

import json

from session_hooks_testlib import run_cli


def json_blocks(text):
    """Each top-level JSON object in the commented output: from a '{' line to a '}' line."""
    blocks, current = [], None
    for line in text.splitlines():
        if line == "{":
            current = [line]
        elif current is not None:
            current.append(line)
            if line == "}":
                blocks.append(json.loads("\n".join(current)))
                current = None
    return blocks


def test_claude_json_is_valid_and_uses_session_end_not_stop(tmp_path):
    result = run_cli("session-hooks-snippet", "--harness", "claude", "--json", "--bin-dir", tmp_path)
    assert result.returncode == 0
    hooks = json.loads(result.stdout)["hooks"]
    assert set(hooks) == {"SessionStart", "SessionEnd"}
    start = hooks["SessionStart"][0]["hooks"][0]
    end = hooks["SessionEnd"][0]["hooks"][0]
    assert start["type"] == end["type"] == "command"
    assert start["command"].startswith(str(tmp_path / "session-start"))
    assert "--since-start" in end["command"] and "--skip-if-noted" in end["command"]
    assert "--post" not in end["command"]
    assert end["timeout"] == 30


def test_codex_json_is_valid_and_has_no_end_hook(tmp_path):
    result = run_cli("session-hooks-snippet", "--harness", "codex", "--json", "--bin-dir", tmp_path)
    assert result.returncode == 0
    hooks = json.loads(result.stdout)["hooks"]
    assert set(hooks) == {"SessionStart"}


def test_default_output_has_every_harness_and_valid_blocks(tmp_path):
    out = run_cli("session-hooks-snippet", "--bin-dir", tmp_path).stdout
    blocks = json_blocks(out)
    assert len(blocks) == 2
    assert "SessionEnd" in blocks[0]["hooks"]
    assert "SessionEnd" not in blocks[1]["hooks"]
    assert "Codex has no session-end event" in out
    assert "Stop event fires after every turn" in out
    assert "\twrap" not in out and "\n\t" in out          # make recipe line starts with a tab
    assert "At the start of a session, run `session-start`" in out
    assert "session-wrap --next" in out


def test_json_needs_one_json_harness():
    result = run_cli("session-hooks-snippet", "--harness", "generic", "--json")
    assert result.returncode == 2


def test_paths_with_spaces_are_quoted(tmp_path):
    spaced = tmp_path / "my tools"
    out = run_cli("session-hooks-snippet", "--harness", "claude", "--json", "--bin-dir", spaced).stdout
    command = json.loads(out)["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command.startswith(f"'{spaced / 'session-start'}'")
