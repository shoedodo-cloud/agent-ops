"""The Linear adapter, with a fake transport. No request leaves the machine."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

import session_hooks_testlib  # noqa: F401  (puts the module on sys.path)
from session_hooks_testlib import load_script
from trackers import linear
from trackers.base import Context, TrackerError

KEY = "test-key-not-real"


class FakeLinear:
    """Answers the adapter's GraphQL documents and records every request."""

    def __init__(self, issues=None, teams=None, projects=None, errors=None):
        self.calls = []
        self.issues = issues if issues is not None else []
        self.teams = teams if teams is not None else [{"id": "team-uuid", "key": "ENG", "name": "Engineering"}]
        self.projects = projects if projects is not None else [{"id": "project-uuid", "name": "Website"}]
        self.errors = errors

    def __call__(self, url, headers, body, timeout):
        payload = json.loads(body)
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout, **payload})
        if self.errors:
            return json.dumps({"errors": self.errors}).encode()
        q = payload["query"]
        if "issues(filter" in q:
            data = {"issues": {"nodes": self.issues}}
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
    return linear.LinearTracker(s, Context(root, {"LINEAR_API_KEY": KEY} if env is None else env), transport=fake)


ISSUE = {"id": "u1", "identifier": "ENG-12", "title": "Fix the flaky test",
         "url": "https://linear.example.com/issue/ENG-12", "state": {"name": "In Progress", "type": "started"}}


def test_open_items_query_shape_and_auth_header():
    fake = FakeLinear(issues=[ISSUE])
    items = make({"project_name": "Website"}, fake=fake).open_items("website")
    assert items == [{"id": "ENG-12", "title": "Fix the flaky test",
                      "url": "https://linear.example.com/issue/ENG-12", "state": "In Progress"}]
    call = fake.calls[0]
    assert call["url"] == "https://api.linear.app/graphql"
    assert call["headers"]["Authorization"] == KEY          # Linear personal keys: no "Bearer"
    assert call["headers"]["Content-Type"] == "application/json"
    assert "issues(filter: $filter, first: $first)" in call["query"]
    assert "nodes { id identifier title url state { name type } }" in call["query"]
    assert call["variables"] == {
        "filter": {"team": {"key": {"eq": "ENG"}},
                   "state": {"type": {"nin": ["completed", "canceled"]}},
                   "project": {"name": {"eq": "Website"}}},
        "first": 50,
    }
    assert fake.mutations == []


def test_project_map_beats_project_name_and_no_project_means_whole_team():
    fake = FakeLinear(issues=[ISSUE])
    make({"project_name": "Website", "projects": {"api": "Platform"}}, fake=fake).open_items("api")
    assert fake.calls[0]["variables"]["filter"]["project"] == {"name": {"eq": "Platform"}}
    fake = FakeLinear(issues=[ISSUE])
    make(fake=fake).open_items("website")
    assert "project" not in fake.calls[0]["variables"]["filter"]


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


def test_empty_result_with_a_wrong_team_is_an_error():
    fake = FakeLinear(issues=[], teams=[])
    with pytest.raises(TrackerError, match="no Linear team with key ENG"):
        make(fake=fake).open_items("website")


def test_empty_result_with_a_wrong_project_is_an_error():
    fake = FakeLinear(issues=[], projects=[])
    with pytest.raises(TrackerError, match="no Linear project named 'Nope'"):
        make({"project_name": "Nope"}, fake=fake).open_items("website")


def test_empty_result_with_valid_filters_is_an_empty_list():
    fake = FakeLinear(issues=[])
    assert make({"project_name": "Website"}, fake=fake).open_items("website") == []


def test_graphql_errors_raise_without_the_key():
    fake = FakeLinear(errors=[{"message": "Authentication required"}])
    with pytest.raises(TrackerError) as info:
        make(fake=fake).open_items("website")
    assert "Authentication required" in str(info.value)
    assert KEY not in str(info.value)


def test_post_note_comments_on_the_configured_issue():
    fake = FakeLinear()
    url = make({"issue_id": "ENG-42"}, fake=fake).post_note("website", "Wire the login form")
    assert url == "https://linear.example.com/issue/ENG-42#comment-c1"
    assert fake.calls[0]["variables"] == {"id": "ENG-42"}
    comment = fake.mutations[0]
    assert "commentCreate(input: $input)" in comment["query"]
    assert comment["variables"] == {"input": {"issueId": "issue-uuid", "body": "Wire the login form"}}


def test_post_note_creates_an_issue_when_no_issue_id():
    fake = FakeLinear()
    url = make({"project_name": "Website"}, fake=fake).post_note("website", "Wire the login form")
    assert url == "https://linear.example.com/issue/ENG-99"
    create = fake.mutations[0]
    assert "issueCreate(input: $input)" in create["query"]
    assert create["variables"]["input"] == {
        "teamId": "team-uuid", "projectId": "project-uuid",
        "title": "Next session (website): Wire the login form", "description": "Wire the login form"}


def test_plan_post_touches_nothing():
    fake = FakeLinear()
    assert make({"issue_id": "ENG-42"}, fake=fake).plan_post("w") == "add a comment to Linear issue ENG-42"
    assert make(fake=fake).plan_post("w") == "create a Linear issue in team ENG"
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
def linear_repo(repo, write_config, monkeypatch):
    write_config({"tracker": "linear", "trackers": {"linear": {"team_key": "ENG", "issue_id": "ENG-42"}}})
    monkeypatch.setenv("LINEAR_API_KEY", KEY)
    fake = FakeLinear(issues=[ISSUE])
    monkeypatch.setattr(linear, "DEFAULT_TRANSPORT", fake)
    return repo, fake


def test_wrap_without_post_sends_nothing(linear_repo, capsys):
    repo, fake = linear_repo
    code = load_script("session-wrap").main(["--repo", str(repo), "--next", "Wire the login form"],
                                            stdin=io.StringIO(""))
    assert code == 0
    out = capsys.readouterr().out
    assert "would add a comment to Linear issue ENG-42" in out
    assert "nothing sent (dry run)" in out
    assert fake.calls == []


def test_wrap_with_post_sends_one_comment(linear_repo, capsys):
    repo, fake = linear_repo
    code = load_script("session-wrap").main(["--repo", str(repo), "--next", "Wire the login form", "--post"],
                                            stdin=io.StringIO(""))
    assert code == 0
    assert len(fake.mutations) == 1
    body = fake.mutations[0]["variables"]["input"]["body"]
    assert body.startswith("Wire the login form (session note: notes/sessions/")
    assert fake.mutations[0]["headers"]["Authorization"] == KEY
    assert "tracker: sent" in capsys.readouterr().out


def test_session_start_lists_linear_items_and_never_mutates(linear_repo, capsys):
    repo, fake = linear_repo
    assert load_script("session-start").main(["--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "tracker: linear team ENG, all projects (no project_name set)" in out
    assert "- ENG-12 [In Progress] Fix the flaky test  https://linear.example.com/issue/ENG-12" in out
    assert fake.calls and fake.mutations == []


def test_session_start_without_key_says_could_not_determine(linear_repo, monkeypatch, capsys):
    repo, fake = linear_repo
    monkeypatch.delenv("LINEAR_API_KEY")
    assert load_script("session-start").main(["--repo", str(repo)]) == 0
    assert "could not determine: open tracker items (LINEAR_API_KEY is not set)" in capsys.readouterr().out
    assert fake.calls == []
