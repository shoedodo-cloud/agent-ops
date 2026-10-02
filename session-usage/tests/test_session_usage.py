"""Tests for bin/session-usage. Run: python3 -m pytest -q session-usage/tests

Hermetic: transcripts are synthetic files under tmp_path, HOME points at tmp_path,
and the environment variables the tool reads are cleared or set per test.
"""
import importlib.machinery
import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "session-usage"
_loader = importlib.machinery.SourceFileLoader("session_usage", str(BIN))
_spec = importlib.util.spec_from_loader("session_usage", _loader)
su = importlib.util.module_from_spec(_spec)
_loader.exec_module(su)

# Fake model names and prices. Order matters: the first match wins.
PRICES = {"as_of": "2026-01-01", "cache_write_multiplier": 2, "path": "test-prices.json",
          "models": [("big-2", 4.0, 20.0, 0.20), ("big", 10.0, 50.0, 0.25),
                     ("small", 1.0, 5.0, 0.10)]}
PRICES_JSON = {"as_of": "2026-01-01", "cache_write_multiplier": 2,
               "models": [{"match": "big-2", "input": 4, "output": 20, "cache_read": 0.20},
                          {"match": "big", "input": 10, "output": 50, "cache_read": 0.25},
                          {"match": "small", "input": 1, "output": 5, "cache_read": 0.10}]}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SESSION_USAGE_HOME", str(tmp_path / "tool-home"))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_CODE_SESSION_ID",
                 "SESSION_USAGE_HISTORY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _asst(req, model, inp, cw, cr, out, ts="2026-09-26T10:00:00Z"):
    return {"type": "assistant", "requestId": req, "timestamp": ts,
            "message": {"id": "msg_" + req, "model": model,
                        "usage": {"input_tokens": inp, "cache_creation_input_tokens": cw,
                                  "cache_read_input_tokens": cr, "output_tokens": out}}}


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _codex_rows(model, inp, cached, out):
    return [{"payload": {"model": model}},
            {"payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}}}]


# ---- reading transcripts, pricing and weekly points ----------------------------------

def test_one_response_split_over_several_lines_counts_once(tmp_path):
    # Claude Code writes one line per content block, each repeating the same usage.
    t = tmp_path / "s.jsonl"
    _write(t, [_asst("r1", "big-2", 2, 100, 1000, 50),
               _asst("r1", "big-2", 2, 100, 1000, 50),
               _asst("r2", "big-2", 1, 0, 2000, 10, ts="2026-09-26T10:30:00Z")])
    per_model, first, last = su.read_claude_transcript(t)
    assert per_model["big-2"] == {"calls": 2, "input": 3, "cache_write": 100,
                                  "cache_read": 3000, "output": 60}
    assert (first, last) == ("2026-09-26T10:00:00Z", "2026-09-26T10:30:00Z")


def test_subagents_are_reported_separately_with_their_description(tmp_path):
    t = tmp_path / "abc.jsonl"
    _write(t, [_asst("r1", "big-2", 1, 1, 1, 1)])
    sub = tmp_path / "abc" / "subagents"
    _write(sub / "agent-x.jsonl", [_asst("s1", "small", 5, 0, 0, 7)])
    (sub / "agent-x.meta.json").write_text(json.dumps({"description": "Scan folder"}))
    rep = su.claude_report(str(t), str(tmp_path))
    assert rep["agents"][0]["label"] == "Scan folder"
    assert rep["agents"][0]["per_model"]["small"]["output"] == 7
    text = su.render(rep, PRICES, None)
    assert "agent: Scan folder" in text and "**total**" in text


def test_codex_takes_last_cumulative_count_and_splits_cached(tmp_path):
    t = tmp_path / "rollout.jsonl"
    tc = lambda i, c, o: {"payload": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": i, "cached_input_tokens": c, "output_tokens": o}}}}
    _write(t, [{"payload": {"model": "codex-example"}}, tc(100, 60, 5), tc(300, 250, 9)])
    m = su.codex_report(str(t), str(tmp_path))["main"]["codex-example"]
    assert (m["input"], m["cache_read"], m["output"]) == (50, 250, 9)


def test_empty_transcript_says_so_instead_of_printing_nothing(tmp_path):
    t = tmp_path / "e.jsonl"
    t.write_text("")
    assert "no usage recorded" in su.render(su.claude_report(str(t), str(tmp_path)), PRICES, None)


def test_list_price_value_uses_model_rates_and_2x_cache_write():
    u = {"calls": 1, "input": 1_000_000, "cache_write": 1_000_000, "cache_read": 1_000_000,
         "output": 1_000_000}
    assert su.usd("big-2", u, PRICES) == pytest.approx(4 + 8 + 0.20 + 20)    # specific entry wins
    assert su.usd("big-1", u, PRICES) == pytest.approx(10 + 20 + 0.25 + 50)  # general entry
    assert su.usd("other-model", u, PRICES) is None             # no price: not guessed


def test_weekly_points_appear_for_claude_with_calibration_but_not_for_codex(tmp_path):
    cal = {"fitted": "2026-01-02", "weekly": 0.04, "buckets": {"big": 0.12}}
    t = tmp_path / "s.jsonl"
    _write(t, [_asst("r1", "big-1", 0, 0, 0, 1_000_000)])        # $50 at the "big" rate
    text = su.render(su.claude_report(str(t), str(tmp_path)), PRICES, cal)
    assert "~weekly pts" in text
    assert "About 2.0 points of the weekly limit, 6.0 of the big limit" in text
    c = tmp_path / "rollout.jsonl"
    _write(c, _codex_rows("codex-example", 10, 0, 1))
    assert "points" not in su.render(su.codex_report(str(c), str(tmp_path)), PRICES, cal)


# ---- price tables, calibration and the command line -----------------------------------

def test_bundled_prices_load_from_json_and_carry_a_date():
    p = su.load_prices(su.bundled_prices_path())
    assert datetime.strptime(p["as_of"], "%Y-%m-%d")
    assert p["models"] and all(len(m) == 4 for m in p["models"])
    assert p["cache_write_multiplier"] > 0


def test_prices_flag_overrides_the_default_table(tmp_path, capsys):
    prices = tmp_path / "mine.json"
    prices.write_text(json.dumps(PRICES_JSON))
    t = tmp_path / "s.jsonl"
    _write(t, [_asst("r1", "small-x", 0, 0, 0, 1_000_000)])
    assert su.main(["--session", str(t), "--prices", str(prices)]) == 0
    out = capsys.readouterr().out
    assert "as of 2026-01-01" in out and "5.00" in out


def test_unusable_explicit_prices_file_stops_with_exit_2(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"models": [{"match": "x", "input": 1, "output": 1, "cache_read": 1}]}))
    t = tmp_path / "s.jsonl"
    _write(t, [_asst("r1", "small", 1, 0, 0, 1)])
    assert su.main(["--session", str(t), "--prices", str(bad)]) == 2
    assert "as_of" in capsys.readouterr().err


def test_unpriced_model_is_named_and_left_out_of_the_total(tmp_path):
    t = tmp_path / "s.jsonl"
    _write(t, [_asst("r1", "mystery-model", 10, 0, 0, 10), _asst("r2", "small", 0, 0, 0, 1_000_000)])
    text = su.render(su.claude_report(str(t), str(tmp_path)), PRICES, None)
    assert "No price for: mystery-model" in text
    assert "n/a" in text and "5.00" in text


def test_no_calibration_file_means_no_points_column(tmp_path, capsys):
    t = tmp_path / "s.jsonl"
    _write(t, [_asst("r1", "big", 0, 0, 0, 1_000_000)])
    assert su.main(["--session", str(t)]) == 0
    out = capsys.readouterr().out
    assert "~list $" in out
    assert "weekly pts" not in out and "points" not in out


def test_calibration_file_adds_the_points_column(env, tmp_path, capsys):
    home = env / "tool-home"
    home.mkdir()
    (home / "calibration.json").write_text(json.dumps({"fitted": "2026-01-02", "weekly": 0.1}))
    t = tmp_path / "s.jsonl"
    _write(t, [_asst("r1", "opus-5-5", 0, 0, 0, 1_000_000)])
    assert su.main(["--session", str(t)]) == 0
    assert "~weekly pts" in capsys.readouterr().out


def test_json_output_is_valid_json_with_rows_and_totals(tmp_path, capsys):
    t = tmp_path / "s.jsonl"
    _write(t, [_asst("r1", "big", 100, 0, 0, 1_000_000)])
    sub = tmp_path / "s" / "subagents"
    _write(sub / "agent-a.jsonl", [_asst("s1", "small", 5, 0, 0, 7)])
    assert su.main(["--session", str(t), "--json", "--prices",
                    str(_prices_file(tmp_path))]) == 0
    data = json.loads(capsys.readouterr().out)
    est = data["estimate"]
    assert data["harness"] == "claude-code" and len(est["rows"]) == 2
    assert est["prices_as_of"] == "2026-01-01"
    assert est["total"]["output"] == 1_000_007 and est["total"]["usd"] > 50


def _prices_file(tmp_path):
    p = tmp_path / "prices.json"
    p.write_text(json.dumps(PRICES_JSON))
    return p


def test_session_id_is_found_under_the_projects_folder(env, monkeypatch, capsys):
    proj = env / "home" / ".claude" / "projects" / "some-project"
    _write(proj / "sess-1.jsonl", [_asst("r1", "small", 3, 0, 0, 4)])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    assert su.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["main"]["small"]["input"] == 3


def test_missing_session_stops_with_exit_2_and_says_where_it_looked(env, capsys):
    assert su.main(["--session", "nope"]) == 2
    assert "no transcript for session nope" in capsys.readouterr().err
    assert su.main([]) == 2
    assert "CLAUDE_CODE_SESSION_ID" in capsys.readouterr().err


def test_unreadable_lines_are_counted_not_hidden(tmp_path):
    t = tmp_path / "s.jsonl"
    t.write_text(json.dumps(_asst("r1", "small", 1, 0, 0, 1)) + "\n{not json\n\n")
    rep = su.claude_report(str(t), str(tmp_path))
    assert rep["bad_lines"] == 1
    assert "1 unreadable transcript line(s) were skipped" in su.render(rep, PRICES, None)


def test_codex_newest_rollout_is_chosen_by_working_directory(env, tmp_path, monkeypatch):
    work = tmp_path / "work dir"
    other = tmp_path / "other"
    work.mkdir()
    other.mkdir()
    day = env / "home" / ".codex" / "sessions" / "2026" / "09" / "26"
    for name, cwd, tokens in (("a", work, 11), ("b", other, 22)):
        rows = [{"type": "session_meta", "payload": {"cwd": str(cwd)}}] + _codex_rows("c", tokens, 0, 1)
        _write(day / f"rollout-{name}.jsonl", rows)
    monkeypatch.chdir(work)
    rep = su.codex_report(None, str(work))
    assert rep["main"]["c"]["input"] == 11
    with pytest.raises(su.UsageError):
        su.codex_report(None, str(tmp_path / "nowhere"))


def test_calibrate_fits_points_per_dollar_and_saves_it(env, tmp_path, capsys):
    now = time.time()
    day = 86400
    t0, t1 = now - 20 * day, now - 10 * day
    snaps = [{"timestamp": t0 - day, "weekly": 20}, {"timestamp": t0, "weekly": 40},
             {"timestamp": t0 + day, "weekly": 2},
             {"timestamp": t1 - day, "weekly": 30, "buckets": {"big": 10}},
             {"timestamp": t1, "weekly": 50, "buckets": {"big": 20}},
             {"timestamp": t1 + day, "weekly": 1}, {"nonsense": True}]
    hist = tmp_path / "history.json"
    hist.write_text(json.dumps({"snapshots": snaps}))
    prices = tmp_path / "tool-home" / "prices.json"
    prices.parent.mkdir(parents=True)
    prices.write_text(json.dumps(PRICES_JSON))
    inside = datetime.fromtimestamp(now - 15 * day, timezone.utc).isoformat().replace("+00:00", "Z")
    outside = datetime.fromtimestamp(now - 2 * day, timezone.utc).isoformat().replace("+00:00", "Z")
    proj = env / "home" / ".claude" / "projects" / "p"
    # $10 of "big" output inside the week, $50 after it (must not count).
    _write(proj / "s.jsonl", [_asst("r1", "big-x", 0, 0, 0, 200_000, ts=inside),
                              _asst("r2", "big-x", 0, 0, 0, 1_000_000, ts=outside)])
    assert su.main(["--calibrate", "--history", str(hist)]) == 0
    out = capsys.readouterr().out
    saved = json.loads((tmp_path / "tool-home" / "calibration.json").read_text())
    assert saved["weekly"] == 5.0 and saved["buckets"] == {"big": 2.0}
    assert "1 history snapshot(s) were unreadable" in out
    assert "Saved" in out


def test_calibrate_without_history_stops_and_explains(capsys):
    assert su.main(["--calibrate"]) == 2
    assert "--history" in capsys.readouterr().err


def test_calibrate_with_too_little_history_refuses_to_guess(tmp_path, capsys):
    hist = tmp_path / "h.json"
    hist.write_text(json.dumps([{"timestamp": time.time(), "weekly": 10}]))
    assert su.main(["--calibrate", "--history", str(hist)]) == 2
    assert "fewer than two weekly resets" in capsys.readouterr().err
    assert not (tmp_path / "tool-home" / "calibration.json").exists()


def test_help_runs_and_names_every_mode(capsys):
    with pytest.raises(SystemExit) as e:
        su.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for word in ("--session", "--codex", "--calibrate", "--json", "--prices", "calibration.json"):
        assert word in out
