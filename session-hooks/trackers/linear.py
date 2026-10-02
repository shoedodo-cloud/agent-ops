"""Linear adapter, over Linear's GraphQL API with urllib (no third-party HTTP library).

The GraphQL shapes come from Linear's public GraphQL docs, not run against the live API.
Every request goes through one transport function, so tests (and you) can swap it for a fake.

Settings (trackers.linear in the config file):
  team_key        required, e.g. "ENG"
  project_name    optional Linear project name used for every repo ("" = whole team)
  projects        optional map of session project -> Linear project name
  issue_id        optional, e.g. "ENG-42": post_note comments on it. Empty = open a new issue
  api_key_env     name of the env var that holds the key (default LINEAR_API_KEY)
  timeout_seconds per request (default 10)
  max_items       open issues to fetch (default 50)

The key is read from the environment only, and sent as "Authorization: <key>".
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from trackers.base import Context, Item, Tracker, TrackerError

API_URL = "https://api.linear.app/graphql"
CLOSED_STATE_TYPES = ["completed", "canceled"]

OPEN_ISSUES_QUERY = """
query SessionHooksOpenIssues($filter: IssueFilter, $first: Int) {
  issues(filter: $filter, first: $first) {
    nodes { id identifier title url state { name type } }
  }
}
""".strip()

TEAM_QUERY = """
query SessionHooksTeam($key: String!) {
  teams(filter: { key: { eq: $key } }) { nodes { id key name } }
}
""".strip()

PROJECT_QUERY = """
query SessionHooksProject($name: String!) {
  projects(filter: { name: { eq: $name } }) { nodes { id name } }
}
""".strip()

ISSUE_QUERY = """
query SessionHooksIssue($id: String!) {
  issue(id: $id) { id identifier url }
}
""".strip()

COMMENT_MUTATION = """
mutation SessionHooksComment($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id url } }
}
""".strip()

ISSUE_CREATE_MUTATION = """
mutation SessionHooksIssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { id identifier url } }
}
""".strip()

# transport(url, headers, body, timeout) -> response body bytes
Transport = Callable[[str, Dict[str, str], bytes, float], bytes]


def _error_text(raw: bytes) -> str:
    try:
        data = json.loads(raw)
        errors = data.get("errors") or []
        messages = [str(e.get("message", "?")) for e in errors if isinstance(e, dict)]
        if messages:
            return " | ".join(messages)
    except (ValueError, AttributeError):
        pass
    return raw[:200].decode("utf-8", "replace").strip() or "no body"


def urllib_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise TrackerError(f"Linear API HTTP {exc.code}: {_error_text(exc.read())}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise TrackerError(f"Linear API unreachable: {exc}") from exc


# Looked up at call time, so a test can replace it with monkeypatch.
DEFAULT_TRANSPORT: Transport = urllib_transport


class LinearTracker(Tracker):
    name = "linear"

    def __init__(self, settings, context: Context, transport: Optional[Transport] = None) -> None:
        super().__init__(settings, context)
        s = self.settings
        self.team_key = str(s.get("team_key") or "").strip()
        self.project_name = str(s.get("project_name") or "").strip()
        self.projects = s.get("projects") if isinstance(s.get("projects"), dict) else {}
        self.issue_id = str(s.get("issue_id") or "").strip()
        self.key_env = str(s.get("api_key_env") or "LINEAR_API_KEY")
        self.timeout = float(s.get("timeout_seconds", 10))
        self.max_items = int(s.get("max_items", 50))
        self._transport = transport

    # ---- what the hooks print -------------------------------------------------------
    def linear_project(self, project: str) -> str:
        return str(self.projects.get(project) or self.project_name or "").strip()

    def describe(self, project: str) -> str:
        team = self.team_key or "(team_key not set)"
        lp = self.linear_project(project)
        scope = f"project {lp}" if lp else "all projects (no project_name set)"
        return f"linear team {team}, {scope}"

    def plan_post(self, project: str) -> str:
        if self.issue_id:
            return f"add a comment to Linear issue {self.issue_id}"
        lp = self.linear_project(project)
        where = f", project {lp}" if lp else ""
        return f"create a Linear issue in team {self.team_key or '(team_key not set)'}{where}"

    # ---- transport ------------------------------------------------------------------
    def _call(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        key = str(self.context.env.get(self.key_env) or "").strip()
        if not key:
            raise TrackerError(f"{self.key_env} is not set")
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        headers = {
            "Authorization": key,
            "Content-Type": "application/json",
            "User-Agent": "session-hooks",
        }
        transport = self._transport or DEFAULT_TRANSPORT
        raw = transport(API_URL, headers, body, self.timeout)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise TrackerError("Linear API returned a body that is not JSON") from exc
        if not isinstance(data, dict):
            raise TrackerError("Linear API returned an unexpected body")
        if data.get("errors"):
            raise TrackerError(f"Linear API: {_error_text(json.dumps(data).encode())}")
        return data.get("data") or {}

    def _need_team(self) -> None:
        if not self.team_key:
            raise TrackerError("set trackers.linear.team_key in the config")

    def _resolve_team(self) -> str:
        self._need_team()
        nodes = ((self._call(TEAM_QUERY, {"key": self.team_key}).get("teams") or {}).get("nodes")) or []
        if not nodes:
            raise TrackerError(f"no Linear team with key {self.team_key}")
        return str(nodes[0]["id"])

    def _resolve_project(self, name: str) -> str:
        nodes = ((self._call(PROJECT_QUERY, {"name": name}).get("projects") or {}).get("nodes")) or []
        if not nodes:
            raise TrackerError(f"no Linear project named {name!r}")
        return str(nodes[0]["id"])

    def _resolve_issue(self, ident: str) -> Dict[str, Any]:
        issue = self._call(ISSUE_QUERY, {"id": ident}).get("issue")
        if not issue or not issue.get("id"):
            raise TrackerError(f"no Linear issue {ident}")
        return issue

    # ---- the interface --------------------------------------------------------------
    def open_items(self, project: str) -> List[Item]:
        self._need_team()
        flt: Dict[str, Any] = {
            "team": {"key": {"eq": self.team_key}},
            "state": {"type": {"nin": CLOSED_STATE_TYPES}},
        }
        lp = self.linear_project(project)
        if lp:
            flt["project"] = {"name": {"eq": lp}}
        data = self._call(OPEN_ISSUES_QUERY, {"filter": flt, "first": self.max_items})
        nodes = (data.get("issues") or {}).get("nodes")
        if nodes is None:
            raise TrackerError("Linear API response has no issues.nodes")
        if not nodes:
            # An empty list must mean "nothing open", not "the filter matched nothing".
            self._resolve_team()
            if lp:
                self._resolve_project(lp)
        items: List[Item] = []
        for n in nodes:
            items.append({
                "id": str(n.get("identifier") or n.get("id") or ""),
                "title": str(n.get("title") or ""),
                "url": str(n.get("url") or ""),
                "state": str((n.get("state") or {}).get("name") or ""),
            })
        return items

    def post_note(self, project: str, text: str) -> str:
        if not self.issue_id:
            return super().post_note(project, text)
        issue = self._resolve_issue(self.issue_id)
        data = self._call(COMMENT_MUTATION, {"input": {"issueId": issue["id"], "body": text}})
        result = data.get("commentCreate") or {}
        if not result.get("success"):
            raise TrackerError("Linear API: commentCreate did not report success")
        return str((result.get("comment") or {}).get("url") or issue.get("url") or "")

    def create_item(self, project: str, title: str, body: str) -> str:
        payload: Dict[str, Any] = {"teamId": self._resolve_team(), "title": title, "description": body}
        lp = self.linear_project(project)
        if lp:
            payload["projectId"] = self._resolve_project(lp)
        data = self._call(ISSUE_CREATE_MUTATION, {"input": payload})
        result = data.get("issueCreate") or {}
        if not result.get("success"):
            raise TrackerError("Linear API: issueCreate did not report success")
        return str((result.get("issue") or {}).get("url") or "")


ADAPTER = LinearTracker
