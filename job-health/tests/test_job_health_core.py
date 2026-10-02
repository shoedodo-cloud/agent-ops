"""Tests for the job-health core: the three-state decision and its readers.

What is defended here: the tool never reports OK for a job it has not seen
succeed. Every "unknown" path asserts STALE.

The classifier is pure, so most of this runs with no schedule, no drive and
no clock. TestTheGitProbeItself runs real git in a temp folder.
"""

import datetime
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
import time

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "bin", "job-health")


def _load():
    if "job_health" in sys.modules and getattr(sys.modules["job_health"], "__file__", "") == BIN:
        return sys.modules["job_health"]
    loader = importlib.machinery.SourceFileLoader("job_health", BIN)
    spec = importlib.util.spec_from_loader("job_health", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["job_health"] = mod
    loader.exec_module(mod)
    return mod


jh = _load()

NOW = datetime.datetime(2030, 1, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("JOB_HEALTH_CONFIG", raising=False)
    monkeypatch.delenv("JOB_HEALTH_PROBES", raising=False)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(k, raising=False)


def regex_reader():
    return jh.outcome_from_regex(r"\bDONE\b|failed=0\b", r"\bFAIL\b|failed=[1-9]")


class TestClassify:
    """The three-state table, exhaustively. This is the whole decision."""

    def test_recent_and_succeeded_is_ok(self):
        assert jh.classify(1.0, 24.0, True) == jh.OK

    def test_recent_and_failed_is_failing(self):
        assert jh.classify(1.0, 24.0, False) == jh.FAILING

    def test_recent_but_unreadable_outcome_is_stale_not_ok(self):
        """The core promise. A job that ran and said nothing legible has NOT
        been shown to work."""
        assert jh.classify(1.0, 24.0, None) == jh.STALE

    def test_old_but_successful_is_stale(self):
        """A success from last week is not evidence about today."""
        assert jh.classify(200.0, 24.0, True) == jh.STALE

    def test_old_and_failed_is_stale(self):
        assert jh.classify(200.0, 24.0, False) == jh.STALE

    def test_never_ran_is_stale(self):
        assert jh.classify(None, 24.0, True) == jh.STALE

    def test_exactly_at_threshold_is_still_ok(self):
        assert jh.classify(24.0, 24.0, True) == jh.OK

    def test_future_timestamp_is_stale_not_ok(self):
        """Clock skew or a bad parse must fail closed, not produce a negative
        age that sails through the age check."""
        assert jh.classify(-5.0, 24.0, True) == jh.STALE


class TestLastStamped:
    """Many jobs write a block per run, so the final line is not the stamped one."""

    def test_skips_trailing_unstamped_lines(self):
        text = ("[2030-01-15T11:00:00] backup: run=done\n"
                "- detail line one\n"
                "- detail line two\n")
        assert jh.last_stamped(text, jh.ts_bracketed_iso).startswith("[2030-01-15T11:00:00]")

    def test_picks_the_most_recent_stamped_line(self):
        text = ("2030-01-14T11:30:04Z copied=0 failed=3\n"
                "2030-01-15T07:30:04Z copied=4 failed=0\n")
        assert "copied=4" in jh.last_stamped(text, jh.ts_iso_z)

    def test_no_stamped_line_at_all(self):
        assert jh.last_stamped("some error text\n", jh.ts_space_local) == ""

    def test_empty_log(self):
        assert jh.last_stamped("", jh.ts_iso_z) == ""


class TestTimestampReaders:

    def test_iso_z(self):
        assert jh.ts_iso_z("2030-01-15T07:30:04Z copied=4").hour == 7

    def test_bracketed_iso_with_utc_offset(self):
        """A reader that required `]` straight after the seconds would match
        nothing here and report a job that just ran as 'never'."""
        ts = jh.ts_bracketed_iso("[2030-01-15T07:05:05+02:00] backup: run=done")
        assert ts is not None
        assert ts.utcoffset() == datetime.timedelta(hours=2)

    def test_bracketed_iso_without_offset_still_parses(self):
        assert jh.ts_bracketed_iso("[2030-01-15T07:05:05] backup: run=done") is not None

    def test_bracketed_iso_with_z(self):
        ts = jh.ts_bracketed_iso("[2030-01-15T07:05:05Z] backup: run=done")
        assert ts.utcoffset() == datetime.timedelta(0)

    def test_time_machine_path(self):
        ts = jh.ts_tm_path("/Volumes/Backups/X/2030-01-14-232710.backup")
        assert (ts.year, ts.month, ts.day, ts.hour) == (2030, 1, 14, 23)

    def test_unparseable_returns_none_everywhere(self):
        for reader in (jh.ts_iso_z, jh.ts_bracketed_iso, jh.ts_space_local,
                       jh.ts_tm_path, jh.ts_auto):
            assert reader("nonsense") is None, reader.__name__
            assert reader("") is None, reader.__name__


class TestHealthFromLog:

    def test_missing_log_is_stale_never_ok(self):
        h = jh.health_from_log("x", "work", "", 24.0, jh.ts_iso_z, regex_reader(), NOW)
        assert h.state == jh.STALE
        assert h.outcome == "no log"

    def test_a_fresh_timestamp_with_a_failure_outcome_is_failing(self):
        """The case this tool exists for: the job runs every day, the timestamp
        is fresh, and every run copied nothing. A freshness check calls it fine."""
        text = ("2030-01-13T11:30:05Z copied=0 failed=3\n"
                "2030-01-14T11:30:04Z copied=0 failed=3\n")
        h = jh.health_from_log("copy-job", "work", text, 48.0, jh.ts_iso_z, regex_reader(),
                               datetime.datetime(2030, 1, 14, 13, 0, 0,
                                                 tzinfo=datetime.timezone.utc))
        assert h.state == jh.FAILING

    def test_unreadable_outcome_annotates_itself(self):
        h = jh.health_from_log("copy-job", "work", "2030-01-15 07:05:10 START /a -> /b\n",
                               30.0, jh.ts_space_local, regex_reader(), NOW)
        assert h.state == jh.STALE
        assert "no outcome could be read" in h.outcome


class TestRenderAndHeadline:

    def _rows(self):
        return [jh.Health("good", "work", jh.OK, 1.0, "fine"),
                jh.Health("broken", "work", jh.FAILING, 1.0, "exit=23"),
                jh.Health("quiet", "work", jh.STALE, None, "no log")]

    def test_worst_first(self):
        body = "\n".join(jh.render(self._rows()))
        assert body.index("broken") < body.index("quiet") < body.index("good")

    def test_bad_states_are_bold(self):
        body = "\n".join(jh.render(self._rows()))
        assert "**FAILING**" in body
        assert "**STALE**" in body
        assert "**OK**" not in body

    def test_table_is_written_even_when_everything_is_broken(self):
        """The report must not go quiet exactly when it matters."""
        rows = [jh.Health("a", "work", jh.FAILING, 1.0, "x"),
                jh.Health("b", "work", jh.STALE, None, "y")]
        assert "## Scheduled jobs" in "\n".join(jh.render(rows))

    def test_pipes_in_outcome_do_not_break_the_table(self):
        """A literal `|` in an outcome would split one cell into two and shift
        every column after it."""
        rows = [jh.Health("a", "work", jh.OK, 1.0, "copied=4 | errors=none")]
        row = jh.render(rows)[-1]
        assert r"\|" in row
        cells = re.split(r"(?<!\\)\|", row)[1:-1]
        assert len(cells) == 5, cells

    def test_headline_silent_when_all_ok(self):
        assert jh.headline([jh.Health("a", "work", jh.OK, 1.0, "fine")]) == ""

    def test_headline_names_the_bad_jobs(self):
        head = jh.headline(self._rows())
        assert "broken FAILING" in head
        assert "quiet STALE" in head
        assert head.startswith("**")


class TestGitState:
    """Pure: a probe line in, a verdict out. No repo needed."""

    NOW = 1900000000.0  # fixed epoch so the unpushed-age tests cannot drift

    def test_clean_is_ok(self):
        state, _ = jh.parse_git_probe(
            "rebase=0 merge=0 conflicts=0 ahead=0 behind=0 oldest=0", self.NOW)
        assert state == jh.OK

    def test_stuck_mid_rebase_is_failing(self):
        """A stopped rebase cannot push, so it cannot report itself."""
        state, msg = jh.parse_git_probe(
            "rebase=1 merge=0 conflicts=1 ahead=3 behind=0 oldest=0", self.NOW)
        assert state == jh.FAILING
        assert "STUCK" in msg

    def test_mid_merge_is_failing(self):
        state, _ = jh.parse_git_probe(
            "rebase=0 merge=1 conflicts=2 ahead=0 behind=0 oldest=0", self.NOW)
        assert state == jh.FAILING

    def test_conflicts_without_rebase_or_merge_still_failing(self):
        state, _ = jh.parse_git_probe(
            "rebase=0 merge=0 conflicts=4 ahead=0 behind=0 oldest=0", self.NOW)
        assert state == jh.FAILING

    def test_behind_alone_is_NOT_a_fault(self):
        """A machine that pulls by hand rests behind. Grading that as a fault
        would make the row cry wolf daily."""
        state, _ = jh.parse_git_probe(
            "rebase=0 merge=0 conflicts=0 ahead=0 behind=47 oldest=0", self.NOW)
        assert state == jh.OK

    def test_fresh_unpushed_work_is_ok(self):
        state, _ = jh.parse_git_probe(
            "rebase=0 merge=0 conflicts=0 ahead=2 behind=0 oldest=%d"
            % int(self.NOW - 3600), self.NOW)
        assert state == jh.OK

    def test_stranded_unpushed_work_is_failing(self):
        """Commits that exist but nobody else can see are silence by another route."""
        state, msg = jh.parse_git_probe(
            "rebase=0 merge=0 conflicts=0 ahead=2 behind=0 oldest=%d"
            % int(self.NOW - 60 * 3600), self.NOW)
        assert state == jh.FAILING
        assert "stranded" in msg

    def test_no_answer_is_stale_never_ok(self):
        """A probe that gave no answer has NOT shown the repo is healthy."""
        assert jh.parse_git_probe("", self.NOW)[0] == jh.STALE

    def test_garbage_is_stale(self):
        assert jh.parse_git_probe("fatal: not a git repository", self.NOW)[0] == jh.STALE

    def test_no_upstream_is_stale(self):
        state, _ = jh.parse_git_probe(
            "rebase=0 merge=0 conflicts=0 ahead=-1 behind=-1 oldest=0", self.NOW)
        assert state == jh.STALE

    def test_unknown_conflict_count_is_stale_not_clean(self):
        """git_probe_line() writes `?` when git could not list conflicts. That
        must not read as zero conflicts."""
        state, msg = jh.parse_git_probe(
            "rebase=0 merge=0 conflicts=? ahead=0 behind=0 oldest=0", self.NOW)
        assert state == jh.STALE
        assert "could not be parsed" in msg


class TestTheGitProbeItself:
    """git_probe_line() runs real git, so a pure test cannot reach it.

    The classic defect lives in the command, not the classifier: reading the
    NEWEST unpushed commit as `oldest` resets the age on every commit, and the
    stranded-work alarm never fires on a repo that commits daily. A pure
    classifier fed by a command needs at least one test that runs the command.
    """

    @pytest.fixture(autouse=True)
    def repo(self, tmp_path):
        self.tmp = str(tmp_path)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.work = os.path.join(self.tmp, "work")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin],
                       check=True, capture_output=True)
        subprocess.run(["git", "clone", "-q", self.origin, self.work],
                       check=True, capture_output=True)
        self._config(self.work)
        self.commit("seed", 0)
        self._git("push", "-q", "origin", "main")

    @staticmethod
    def _config(path):
        for k, v in (("user.email", "alice"), ("user.name", "Alice"),
                     ("commit.gpgsign", "false")):
            subprocess.run(["git", "-C", path, "config", k, v],
                           check=True, capture_output=True)

    def _git(self, *args):
        return subprocess.run(["git", "-C", self.work] + list(args),
                              check=True, capture_output=True, text=True)

    def commit(self, name, hours_ago):
        """A commit dated `hours_ago` before now, so age is controllable."""
        when = int(time.time()) - int(hours_ago * 3600)
        with open(os.path.join(self.work, name), "w") as fh:
            fh.write(name)
        self._git("add", name)
        stamp = "%d +0000" % when
        env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        subprocess.run(["git", "-C", self.work, "commit", "-qm", name],
                       check=True, capture_output=True, env=env)

    def probe(self):
        out = jh.git_probe_line(self.work)
        return dict(kv.split("=", 1) for kv in out.split() if "=" in kv)

    def test_probe_reports_the_OLDEST_unpushed_commit_not_the_newest(self):
        """Old work, then a fresh commit on top."""
        self.commit("old", hours_ago=89)
        self.commit("fresh", hours_ago=0)
        f = self.probe()
        assert f["ahead"] == "2"
        age_h = (time.time() - float(f["oldest"])) / 3600.0
        assert age_h > 80, "read the NEWEST commit"

    def test_the_stranded_alarm_actually_fires_end_to_end(self):
        """Probe and classifier together."""
        self.commit("old", hours_ago=89)
        self.commit("fresh", hours_ago=0)
        state, msg = jh.parse_git_probe(jh.git_probe_line(self.work))
        assert state == jh.FAILING, msg
        assert "stranded" in msg

    def test_fresh_unpushed_work_stays_ok(self):
        """The fix must not turn every unpushed commit into an alarm."""
        self.commit("recent", hours_ago=1)
        state, msg = jh.parse_git_probe(jh.git_probe_line(self.work))
        assert state == jh.OK, msg

    def test_clean_repo_reports_zero_and_zero(self):
        f = self.probe()
        assert (f["ahead"], f["behind"]) == ("0", "0")
        state, _ = jh.parse_git_probe(jh.git_probe_line(self.work))
        assert state == jh.OK

    def test_behind_is_reported_but_still_not_a_fault(self):
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "clone", "-q", self.origin, other],
                       check=True, capture_output=True)
        self._config(other)
        with open(os.path.join(other, "theirs"), "w") as fh:
            fh.write("x")
        for args in (["add", "-A"], ["commit", "-qm", "theirs"], ["push", "-q", "origin", "main"]):
            subprocess.run(["git", "-C", other] + args, check=True, capture_output=True)
        self._git("fetch", "-q", "origin", "main")
        f = self.probe()
        assert f["behind"] == "1"
        state, msg = jh.parse_git_probe(jh.git_probe_line(self.work))
        assert state == jh.OK, msg
