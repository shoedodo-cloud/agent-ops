"""The Linear adapter, with a fake transport. No request leaves the machine."""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import urllib.error

import pytest

import session_hooks_testlib  # noqa: F401  (puts the module on sys.path)
from session_hooks_testlib import load_script
from trackers import linear
from trackers.base import Context, TrackerError, short_title

KEY = "test-key-not-real"
NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)

STATES = [
    {"id": "s-backlog", "name": "Backlog", "type": "backlog"},
    {"id": "s-todo", "name": "Todo", "type": "unstarted"},
    {"id": "s-progress", "name": "In Progress", "type": "started"},
    {"id": "s-done", "name": "Done", "type": "completed"},
]
LABELS = [
    {"id": "l-bug", "name": "bug", "team": {"key": "ENG"}},
    {"id": "l-infra", "name": "infra", "team": None},
    {"id": "l-ui", "name": "ui", "team": {"key": "OPS"}},
]


class FakeLinear:
    """Answers the adapter's GraphQL documents and records every request.

    has_next: True or False fills pageInfo.hasNextPage, None leaves pageInfo out of the answer.
    """

    def __init__(self, issues=None, teams=None, projects=None, errors=None, has_next=False,
                 states=None, labels=None):
        self.calls = []
        self.issues = issues if issues is not None else []
        self.teams = teams if teams is not None else [{"id": "team-uuid", "key": "ENG", "name": "Engineering"}]
        self.projects = projects if projects is not None else [{"id": "project-uuid", "name": "Website"}]
        self.errors = errors
        self.has_next = has_next
        self.states = states if states is not None else STATES
        self.labels = labels if labels is not None else LABELS

    def __call__(self, url, headers, body, timeout):
        payload = json.loads(body)
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout, **payload})
        if self.errors:
            return json.dumps({"errors": self.errors}).encode()
        q = payload["query"]
        if "issues(filter" in q:
            page = {"nodes": self.issues}
            if self.has_next is not None:
                page["pageInfo"] = {"hasNextPage": self.has_next}
            data = {"issues": page}
        elif "issueLabels(" in q:
            data = {"issueLabels": {"nodes": self.labels}}
        elif "states {" in q:
            data = {"teams": {"nodes": [{"id": t["id"], "states": {"nodes": self.states}} for t in self.teams]}}
        elif "teams(" in q:
            data = {"teams": {"nodes": self.teams}}
        elif "projects(" in q:
            data = {"projects": {"nodes": self.projects}}
        elif "issue(id" in q:
            data = {"issue": {"id": "issue-uuid", "identifier": payload["variables"]["id"],
                              "url": "https://linear.example.com/issue/ENG-42"}}
        elif "commentCreate" in q:
            data = {"commentCreate": {"success": True, "comment": {
                "id": "c1", "url": "https://linear.example.com/issue/ENG-42#comment-c1"}}}
        elif "issueCreate" in q:
            data = {"issueCreate": {"success": True, "issue": {
                "id": "i9", "identifier": "ENG-99", "url": "https://linear.example.com/issue/ENG-99"}}}
        else:
            raise AssertionError(f"unexpected query {q}")
        return json.dumps({"data": data}).encode()

    @property
    def mutations(self):
        return [c for c in self.calls if c["query"].lstrip().startswith("mutation")]


def make(settings=None, env=None, fake=None, root="."):
    s = {"team_key": "ENG", **(settings or {})}
    return linear.LinearTracker(s, Context(root, {"LINEAR_API_KEY": KEY} if env is None else env),
                                transport=fake, clock=lambda: NOW)


def issue(ident, state="In Progress", kind="started", updated="2026-10-03T10:00:00.000Z",
          labels=(), due=None, title="Fix the flaky test", priority=0):
    return {"id": "u-" + ident, "identifier": ident, "title": title,
            "url": f"https://linear.example.com/issue/{ident}", "priority": priority, "dueDate": due,
            "updatedAt": updated, "state": {"name": state, "type": kind},
            "labels": {"nodes": [{"name": n} for n in labels]}}


ISSUE = issue("ENG-12", labels=["bug"], due="2026-10-14", priority=2)


def test_open_items_query_shape_and_auth_header():
    fake = FakeLinear(issues=[ISSUE])
    items = make({"project_name": "Website"}, fake=fake).open_items("website")
    assert items == [{"id": "ENG-12", "title": "Fix the flaky test",
                      "url": "https://linear.example.com/issue/ENG-12", "state": "In Progress",
                      "priority": 2, "labels": ["bug"], "due": "2026-10-14",
                      "updated": "2026-10-03T10:00:00.000Z"}]
    call = fake.calls[0]
    assert call["url"] == "https://api.linear.app/graphql"
    assert call["headers"]["Authorization"] == KEY          # Linear personal keys: no "Bearer"
    assert call["headers"]["Content-Type"] == "application/json"
    query = " ".join(call["query"].split())
    assert "issues(filter: $filter, first: $first)" in query
    assert "id identifier title url priority dueDate updatedAt" in query
    assert "state { name type }" in query
    assert "labels { nodes { name } }" in query
    assert "pageInfo { hasNextPage }" in query
    assert call["variables"] == {
        "filter": {"team": {"key": {"eq": "ENG"}},
                   "state": {"name": {"in": ["In Progress", "Todo"]}},
                   "project": {"name": {"eq": "Website"}}},
        "first": 50,
    }
    assert fake.mutations == []


# ---- the states setting -------------------------------------------------------------------
def test_states_default_is_in_progress_and_todo():
    fake = FakeLinear(issues=[ISSUE])
    make(fake=fake).open_items("website")
    assert fake.calls[0]["variables"]["filter"]["state"] == {"name": {"in": ["In Progress", "Todo"]}}


def test_states_setting_is_sent_as_a_name_filter():
    fake = FakeLinear(issues=[ISSUE])
    make({"states": ["Backlog", "In Review", "Backlog"]}, fake=fake).open_items("website")
    assert fake.calls[0]["variables"]["filter"]["state"] == {"name": {"in": ["Backlog", "In Review"]}}


def test_an_empty_states_list_means_every_open_state():
    fake = FakeLinear(issues=[ISSUE])
    tracker = make({"states": []}, fake=fake)
    tracker.open_items("website")
    assert fake.calls[0]["variables"]["filter"]["state"] == {"type": {"nin": ["completed", "canceled"]}}
    assert tracker.describe("website") == "linear team ENG, all projects (states: every open state)"


@pytest.mark.parametrize("settings, key", [
    ({"states": "In Progress"}, "states"),
    ({"states": ["Todo", 3]}, "states"),
    ({"labels": "bug"}, "labels"),
    ({"state": ["Todo"]}, "state"),
])
def test_bad_states_labels_or_state_are_a_loud_error(settings, key):
    with pytest.raises(TrackerError, match=f"trackers.linear.{key}"):
        make(settings, fake=FakeLinear())


# ---- the count, the heading, the order, the line ------------------------------------------
def test_more_items_note_comes_from_has_next_page():
    fake = FakeLinear(issues=[ISSUE], has_next=True)
    tracker = make({"max_items": 50}, fake=fake)
    assert tracker.more_items_note() == ""
    tracker.open_items("website")
    assert tracker.more_items_note() == "max_items=50"
    tracker = make(fake=FakeLinear(issues=[ISSUE], has_next=False))
    tracker.open_items("website")
    assert tracker.more_items_note() == ""


def test_a_missing_page_info_is_an_error_not_a_guess():
    with pytest.raises(TrackerError, match="pageInfo.hasNextPage"):
        make(fake=FakeLinear(issues=[ISSUE], has_next=None)).open_items("website")


def test_heading_names_the_team_and_the_project_filter():
    assert make().scope_label("website") == "team ENG, all projects"
    assert make({"project_name": "Website"}).scope_label("website") == "team ENG, project Website"
    mapped = make({"projects": {"api": "Platform"}})
    assert mapped.scope_label("api") == "team ENG, project Platform"
    assert mapped.scope_label("website") == "team ENG, all projects"
    assert make().describe("website") == "linear team ENG, all projects (states: In Progress, Todo)"


def test_items_come_in_progress_first_then_todo_then_the_rest_newest_first():
    fake = FakeLinear(issues=[
        issue("ENG-1", "Backlog", "backlog", "2026-10-03T11:00:00.000Z"),
        issue("ENG-2", "Todo", "unstarted", "2026-10-03T09:00:00.000Z"),
        issue("ENG-3", "In Progress", "started", "2026-10-03T08:00:00.000Z"),
        issue("ENG-4", "In Progress", "started", "2026-10-03T11:30:00.000Z"),
        issue("ENG-5", "Todo", "unstarted", "2026-10-03T10:00:00.000Z"),
        issue("ENG-6", "In Progress", "started", None),
    ])
    items = make({"states": []}, fake=fake).open_items("website")
    assert [i["id"] for i in items] == ["ENG-4", "ENG-3", "ENG-6", "ENG-5", "ENG-2", "ENG-1"]


def test_item_line_format_and_no_url_in_it():
    tracker = make()
    full = issue("ENG-14", labels=["bug", "ui"], due="2026-10-14", title="Wire the form")
    item = make(fake=FakeLinear(issues=[full])).open_items("website")[0]
    line = tracker.format_item(item)
    assert line == "ENG-14 [In Progress] Wire the form (labels: bug, ui; due 2026-10-14; updated 2h ago)"
    assert "https://" not in line
    assert item["url"] == "https://linear.example.com/issue/ENG-14"      # kept for other consumers


@pytest.mark.parametrize("fields, tail", [
    ({"labels": ["bug"], "updated": None}, " (labels: bug)"),
    ({"due": "2026-10-14", "updated": None}, " (due 2026-10-14)"),
    ({"updated": "2026-10-03T11:50:00.000Z"}, " (updated 10m ago)"),
    ({"updated": "2026-10-03T11:59:40.000Z"}, " (updated just now)"),
    ({"updated": "2026-09-30T12:00:00.000Z"}, " (updated 3d ago)"),
    ({"updated": "not a time"}, ""),
    ({"updated": None}, ""),
])
def test_item_line_leaves_out_the_parts_that_are_empty(fields, tail):
    item = {"id": "ENG-7", "title": "Fix it", "url": "u", "state": "Todo", "labels": [], "due": "", **fields}
    assert make().format_item(item) == "ENG-7 [Todo] Fix it" + tail


# ---- empty answers stay honest -------------------------------------------------------------
def test_missing_key_sends_nothing():
    fake = FakeLinear()
    with pytest.raises(TrackerError, match="LINEAR_API_KEY is not set"):
        make(env={}, fake=fake).open_items("website")
    assert fake.calls == []


def test_missing_team_key_is_an_error_not_an_empty_list():
    fake = FakeLinear()
    with pytest.raises(TrackerError, match="team_key"):
        linear.LinearTracker({}, Context(".", {"LINEAR_API_KEY": KEY}), transport=fake).open_items("x")
    assert fake.calls == []


def test_project_map_beats_project_name_and_no_project_means_whole_team():
    fake = FakeLinear(issues=[ISSUE])
    make({"project_name": "Website", "projects": {"api": "Platform"}}, fake=fake).open_items("api")
    assert fake.calls[0]["variables"]["filter"]["project"] == {"name": {"eq": "Platform"}}
    fake = FakeLinear(issues=[ISSUE])
    make(fake=fake).open_items("website")
    assert "project" not in fake.calls[0]["variables"]["filter"]


def test_empty_result_with_a_wrong_team_is_an_error():
    for settings in ({}, {"states": []}):
        with pytest.raises(TrackerError, match="no Linear team with key ENG"):
            make(settings, fake=FakeLinear(issues=[], teams=[])).open_items("website")


def test_empty_result_with_a_wrong_project_is_an_error():
    fake = FakeLinear(issues=[], projects=[])
    with pytest.raises(TrackerError, match="no Linear project named 'Nope'"):
        make({"project_name": "Nope"}, fake=fake).open_items("website")


def test_empty_result_with_a_state_name_the_team_does_not_have_is_an_error():
    fake = FakeLinear(issues=[])
    with pytest.raises(TrackerError) as info:
        make({"states": ["In Progress", "Doing"]}, fake=fake).open_items("website")
    assert "no state named 'Doing' in Linear team ENG" in str(info.value)
    assert "Backlog, Done, In Progress, Todo" in str(info.value)


def test_empty_result_with_valid_filters_is_an_empty_list():
    fake = FakeLinear(issues=[])
    assert make({"project_name": "Website"}, fake=fake).open_items("website") == []
    assert make({"states": []}, fake=FakeLinear(issues=[])).open_items("website") == []


def test_graphql_errors_raise_without_the_key():
    fake = FakeLinear(errors=[{"message": "Authentication required"}])
    with pytest.raises(TrackerError) as info:
        make(fake=fake).open_items("website")
    assert "Authentication required" in str(info.value)
    assert KEY not in str(info.value)


# ---- writes --------------------------------------------------------------------------------
def test_post_note_comments_on_the_configured_issue():
    fake = FakeLinear()
    text = "Wire the login form\nSession note: notes/sessions/a.md, main at abc1234"
    url = make({"issue_id": "ENG-42"}, fake=fake).post_note("website", text)
    assert url == "https://linear.example.com/issue/ENG-42#comment-c1"
    assert fake.calls[0]["variables"] == {"id": "ENG-42"}
    comment = fake.mutations[0]
    assert "commentCreate(input: $input)" in comment["query"]
    assert comment["variables"] == {"input": {"issueId": "issue-uuid", "body": text}}
    assert comment["variables"]["input"]["body"].splitlines()[0] == "Wire the login form"


def test_post_note_creates_an_issue_titled_with_the_first_line_only():
    fake = FakeLinear()
    text = "Wire the login form\nSession note: notes/sessions/a.md, main at abc1234"
    url = make({"project_name": "Website"}, fake=fake).post_note("website", text)
    assert url == "https://linear.example.com/issue/ENG-99"
    create = fake.mutations[0]
    assert "issueCreate(input: $input)" in create["query"]
    assert create["variables"]["input"] == {
        "teamId": "team-uuid", "projectId": "project-uuid", "title": "Wire the login form", "description": text}


def test_a_long_first_line_is_cut_at_80_characters_at_a_word_boundary():
    line = "Wire the login form to the new session API and then delete the old cookie handling code paths"
    text = line + "\nSession note: notes/sessions/a.md, main at abc1234"
    fake = FakeLinear()
    make(fake=fake).post_note("website", text)
    sent = fake.mutations[0]["variables"]["input"]
    assert len(sent["title"]) <= 80 and sent["title"].endswith("...")
    stem = sent["title"][:-3]
    assert line.startswith(stem) and line[len(stem)] == " "       # a whole word ends the title
    assert sent["description"] == text                              # the full line is in the body


def test_short_title_edges():
    assert short_title("x" * 80) == "x" * 80
    assert short_title("  a   b  ") == "a b"
    one_word = short_title("x" * 100)
    assert one_word == "x" * 77 + "..."
    exact = "a" * 10 + " " + "b" * 66 + " " + "c" * 10             # a space sits right after the 77th character
    assert short_title(exact) == "a" * 10 + " " + "b" * 66 + "..."
    assert len(short_title(exact)) == 80


def test_create_sends_no_label_or_state_ids_and_makes_no_extra_request_by_default():
    fake = FakeLinear()
    make(fake=fake).create_item("website", "Title", "Body")
    assert set(fake.mutations[0]["variables"]["input"]) == {"teamId", "title", "description"}
    assert len(fake.calls) == 2                                    # the team lookup, then the mutation


def test_create_resolves_labels_and_state_and_sends_their_ids():
    fake = FakeLinear()
    tracker = make({"labels": ["bug", "infra"], "state": "In Progress"}, fake=fake)
    tracker.create_item("website", "Title", "Body")
    labels_call = next(c for c in fake.calls if "issueLabels(" in c["query"])
    assert "issueLabels(filter: { name: { in: $names } })" in labels_call["query"]
    assert labels_call["variables"] == {"names": ["bug", "infra"]}
    sent = fake.mutations[0]["variables"]["input"]
    assert sent["labelIds"] == ["l-bug", "l-infra"]
    assert sent["stateId"] == "s-progress"
    assert sent["teamId"] == "team-uuid"
    assert tracker.warnings == []


def test_unknown_labels_and_a_state_the_team_lacks_are_reported_and_skipped():
    fake = FakeLinear()
    tracker = make({"labels": ["bug", "nope", "ui"], "state": "Doing"}, fake=fake)
    tracker.create_item("website", "Title", "Body")
    sent = fake.mutations[0]["variables"]["input"]
    assert sent["labelIds"] == ["l-bug"]                           # "ui" belongs to another team
    assert "stateId" not in sent
    assert len(tracker.warnings) == 3
    assert "label 'nope' not found" in tracker.warnings[0]
    assert "label 'ui' not found" in tracker.warnings[1]
    assert "state 'Doing' not found in team ENG" in tracker.warnings[2]
    assert "In Progress" in tracker.warnings[2]


def test_a_team_label_beats_a_workspace_label_of_the_same_name():
    labels = [{"id": "l-shared", "name": "bug", "team": None}, {"id": "l-own", "name": "bug", "team": {"key": "ENG"}}]
    fake = FakeLinear(labels=labels)
    make({"labels": ["bug"]}, fake=fake).create_item("website", "T", "B")
    assert fake.mutations[0]["variables"]["input"]["labelIds"] == ["l-own"]


def test_no_label_ids_are_sent_when_none_resolve():
    fake = FakeLinear(labels=[])
    make({"labels": ["nope"]}, fake=fake).create_item("website", "T", "B")
    assert "labelIds" not in fake.mutations[0]["variables"]["input"]


def test_labels_and_state_do_not_touch_a_comment():
    fake = FakeLinear()
    make({"issue_id": "ENG-42", "labels": ["bug"], "state": "Todo"}, fake=fake).post_note("w", "x")
    assert not any("issueLabels(" in c["query"] or "states {" in c["query"] for c in fake.calls)


def test_plan_post_touches_nothing():
    fake = FakeLinear()
    assert make({"issue_id": "ENG-42"}, fake=fake).plan_post("w") == "add a comment to Linear issue ENG-42"
    assert make(fake=fake).plan_post("w") == "create a Linear issue in team ENG"
    assert make({"state": "Todo", "labels": ["bug", "ui"]}, fake=fake).plan_post("w") == \
        "create a Linear issue in team ENG, state Todo, labels bug, ui"
    assert fake.calls == []


def test_default_transport_builds_a_post_request(monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, method=request.get_method(), timeout=timeout,
                    auth=request.get_header("Authorization"), body=request.data)
        return Response(b'{"data": {"issues": {"nodes": []}}}')

    monkeypatch.setattr(linear.urllib.request, "urlopen", fake_urlopen)
    body = linear.urllib_transport(linear.API_URL, {"Authorization": KEY}, b"{}", 7)
    assert body == b'{"data": {"issues": {"nodes": []}}}'
    assert seen == {"url": linear.API_URL, "method": "POST", "timeout": 7, "auth": KEY, "body": b"{}"}

    def failing(request, timeout):
        raise urllib.error.HTTPError(linear.API_URL, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"errors": [{"message": "bad key"}]}'))

    monkeypatch.setattr(linear.urllib.request, "urlopen", failing)
    with pytest.raises(TrackerError, match="HTTP 401: bad key"):
        linear.urllib_transport(linear.API_URL, {}, b"{}", 7)




# ---- through the CLIs, in-process, with the module's default transport replaced -----------
@pytest.fixture
def linear_cli(repo, write_config, monkeypatch):
    """Configure the Linear tracker for the CLIs. Returns setup(settings, **fake_kwargs) -> (fake, repo)."""
    def setup(settings=None, **fake_kwargs):
        write_config({"tracker": "linear", "trackers": {"linear": {"team_key": "ENG", **(settings or {})}}})
        monkeypatch.setenv("LINEAR_API_KEY", KEY)
        monkeypatch.setattr(linear, "DEFAULT_CLOCK", lambda: NOW)
        fake = FakeLinear(**fake_kwargs)
        monkeypatch.setattr(linear, "DEFAULT_TRANSPORT", fake)
        return fake, repo
    return setup


@pytest.fixture
def linear_repo(linear_cli):
    fake, repo = linear_cli({"issue_id": "ENG-42"}, issues=[ISSUE])
    return repo, fake


def wrap(repo, *args):
    return load_script("session-wrap").main(["--repo", str(repo), *args], stdin=io.StringIO(""))


NOTE_LINE = re.compile(r"Session note: notes/sessions/\d{4}-\d{2}-\d{2}_\d{4}_session\.md, main at [0-9a-f]{7,}")
LONG_LINE = "Wire the login form to the new session API and then delete the old cookie handling code paths"


def test_wrap_without_post_sends_nothing(linear_repo, capsys):
    repo, fake = linear_repo
    assert wrap(repo, "--next", "Wire the login form") == 0
    out = capsys.readouterr().out
    assert "would add a comment to Linear issue ENG-42" in out
    assert "  Wire the login form\n  Session note: notes/sessions/" in out
    assert "nothing sent (dry run)" in out
    assert fake.calls == []


def test_wrap_with_post_sends_one_comment_with_the_next_line_first(linear_repo, capsys):
    repo, fake = linear_repo
    assert wrap(repo, "--next", "Wire the login form", "--post") == 0
    assert len(fake.mutations) == 1
    first, second = fake.mutations[0]["variables"]["input"]["body"].split("\n")
    assert first == "Wire the login form"
    assert NOTE_LINE.fullmatch(second)
    assert fake.mutations[0]["headers"]["Authorization"] == KEY
    assert "tracker: sent" in capsys.readouterr().out


def test_wrap_with_post_opens_an_issue_titled_with_the_next_line(linear_cli):
    fake, repo = linear_cli()
    assert wrap(repo, "--next", LONG_LINE, "--post") == 0
    sent = fake.mutations[0]["variables"]["input"]
    assert len(sent["title"]) <= 80 and "session note" not in sent["title"].lower()
    stem = sent["title"][:-3]
    assert LONG_LINE.startswith(stem) and LONG_LINE[len(stem)] == " "
    first, second = sent["description"].split("\n")
    assert first == LONG_LINE                                         # the full line is in the body
    assert NOTE_LINE.fullmatch(second)


def test_wrap_sends_no_absolute_path_to_the_tracker(linear_cli, tmp_path, monkeypatch):
    fake, repo = linear_cli({"issue_id": "ENG-42"})
    assert wrap(repo, "--next", "Wire the login form", "--post") == 0
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("SESSION_NOTES_DIR", str(elsewhere))           # a notes folder outside the repo
    assert wrap(repo, "--next", "Ship the footer", "--post") == 0
    assert len(fake.mutations) == 2
    outside = fake.mutations[1]["variables"]["input"]["body"].split("\n")[1]
    assert re.fullmatch(r"Session note: \d{4}-\d{2}-\d{2}_\d{4}_session\.md, main at [0-9a-f]{7,}", outside)
    sent = json.dumps([c["variables"] for c in fake.mutations])
    for forbidden in (str(tmp_path), str(repo), str(elsewhere), "/private", "/var/folders"):
        assert forbidden not in sent


def test_wrap_sends_no_absolute_path_in_a_title_or_description_either(linear_cli, tmp_path, monkeypatch):
    fake, repo = linear_cli()
    monkeypatch.setenv("SESSION_NOTES_DIR", str(tmp_path / "elsewhere"))
    assert wrap(repo, "--next", LONG_LINE, "--post") == 0
    sent = fake.mutations[0]["variables"]["input"]
    assert str(tmp_path) not in json.dumps(sent)
    assert sent["title"] == short_title(LONG_LINE)


@pytest.mark.parametrize("line", [
    "/home/alice/run.log shows three failures",
    "~/work/todo.md",
    "./gradlew test",
    "../other/file.txt",
    "# Session note: website",
    "PASS PASS FAIL PASS",
    "PASS: 12 FAIL: 3",
])
def test_wrap_refuses_to_post_an_unreadable_first_line(linear_repo, capsys, line):
    repo, fake = linear_repo
    assert wrap(repo, "--next", line, "--post") == 1
    err = capsys.readouterr().err
    assert "NOT sent" in err and "--allow-raw" in err
    assert fake.calls == []
    assert len(list((repo / "notes" / "sessions").iterdir())) == 1    # the note is still written


@pytest.mark.parametrize("line", [
    "Wire the login form",
    "Fix FAIL and PASS handling in the test report",
    "Skip the flaky test",
    "Check ./config handling",
])
def test_wrap_posts_a_readable_first_line(linear_repo, line):
    repo, fake = linear_repo
    assert wrap(repo, "--next", line, "--post") == 0
    assert len(fake.mutations) == 1


def test_wrap_allow_raw_posts_it_anyway(linear_repo):
    repo, fake = linear_repo
    assert wrap(repo, "--next", "# Session note: website", "--post", "--allow-raw") == 0
    assert fake.mutations[0]["variables"]["input"]["body"].startswith("# Session note: website\n")


def test_wrap_dry_run_says_post_would_refuse(linear_repo, capsys):
    repo, fake = linear_repo
    assert wrap(repo, "--next", "PASS PASS FAIL") == 0
    out = capsys.readouterr().out
    assert "--post would refuse" in out and "PASS/FAIL" in out
    assert "nothing sent (dry run)" in out
    assert fake.calls == []


def test_wrap_prints_the_adapter_warnings_after_a_post(linear_cli, capsys):
    fake, repo = linear_cli({"labels": ["bug", "nope"], "state": "Doing"})
    assert wrap(repo, "--next", "Wire the login form", "--post") == 0
    out = capsys.readouterr().out
    assert "tracker: warning, Linear label 'nope' not found" in out
    assert "tracker: warning, Linear state 'Doing' not found in team ENG" in out
    sent = fake.mutations[0]["variables"]["input"]
    assert sent["labelIds"] == ["l-bug"] and "stateId" not in sent


def test_wrap_creates_the_issue_with_label_and_state_ids(linear_cli, capsys):
    fake, repo = linear_cli({"labels": ["bug"], "state": "Todo"})
    assert wrap(repo, "--next", "Wire the login form") == 0
    assert "would create a Linear issue in team ENG, state Todo, labels bug" in capsys.readouterr().out
    assert wrap(repo, "--next", "Wire the login form", "--post") == 0
    sent = fake.mutations[0]["variables"]["input"]
    assert sent["labelIds"] == ["l-bug"] and sent["stateId"] == "s-todo"
    assert "warning" not in capsys.readouterr().out


def test_session_start_lists_linear_items_and_never_mutates(linear_repo, capsys):
    repo, fake = linear_repo
    assert load_script("session-start").main(["--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "tracker: linear team ENG, all projects (states: In Progress, Todo)" in out
    assert "open items: 1 for team ENG, all projects\n" in out
    assert ("  - ENG-12 [In Progress] Fix the flaky test (labels: bug; due 2026-10-14; updated 2h ago)\n") in out
    assert "https://linear.example.com" not in out                    # the link stays out of the text line
    assert fake.calls and fake.mutations == []


def test_session_start_names_the_project_filter(linear_cli, capsys):
    fake, repo = linear_cli({"project_name": "Website"}, issues=[ISSUE])
    assert load_script("session-start").main(["--repo", str(repo)]) == 0
    assert "open items: 1 for team ENG, project Website\n" in capsys.readouterr().out


def test_session_start_says_of_more_when_linear_has_more(linear_cli, capsys):
    fake, repo = linear_cli({"max_items": 50}, issues=[ISSUE], has_next=True)
    assert load_script("session-start").main(["--repo", str(repo)]) == 0
    assert "open items: 1 of more (max_items=50) for team ENG, all projects" in capsys.readouterr().out


def test_session_start_prints_the_true_count_when_there_is_no_more(linear_cli, capsys):
    issues = [issue(f"ENG-{n}") for n in range(1, 4)]
    fake, repo = linear_cli(issues=issues, has_next=False)
    assert load_script("session-start").main(["--repo", str(repo)]) == 0
    assert "open items: 3 for team ENG, all projects" in capsys.readouterr().out


def test_session_start_prints_in_progress_first(linear_cli, capsys):
    fake, repo = linear_cli({"states": []}, issues=[
        issue("ENG-1", "Backlog", "backlog", "2026-10-03T11:00:00.000Z", title="Idea"),
        issue("ENG-2", "Todo", "unstarted", "2026-10-03T09:00:00.000Z", title="Next up"),
        issue("ENG-3", "In Progress", "started", "2026-10-03T08:00:00.000Z", title="Doing it"),
    ])
    assert load_script("session-start").main(["--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    order = [out.index(f"- ENG-{n} ") for n in (3, 2, 1)]
    assert order == sorted(order)
    assert fake.calls[0]["variables"]["filter"]["state"] == {"type": {"nin": ["completed", "canceled"]}}


def test_session_start_without_key_says_could_not_determine(linear_repo, monkeypatch, capsys):
    repo, fake = linear_repo
    monkeypatch.delenv("LINEAR_API_KEY")
    assert load_script("session-start").main(["--repo", str(repo)]) == 0
    assert "could not determine: open tracker items (LINEAR_API_KEY is not set)" in capsys.readouterr().out
    assert fake.calls == []


def test_session_start_reports_a_bad_states_setting(linear_cli, capsys):
    fake, repo = linear_cli({"states": "Todo"})
    assert load_script("session-start").main(["--repo", str(repo)]) == 0
    assert "could not determine: open tracker items (trackers.linear.states must be a list" in capsys.readouterr().out
    assert fake.calls == []
