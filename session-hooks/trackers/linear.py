"""Linear adapter, over Linear's GraphQL API with urllib (no third-party HTTP library).

Run against the live API on 2026-10-02: listing issues, adding a comment, creating an issue.
Other shapes (labels, states) follow Linear's public GraphQL docs.
Every request goes through one transport function, so tests (and you) can swap it for a fake.

Settings (trackers.linear in the config file):
  team_key        required, e.g. "ENG"
  project_name    optional Linear project name used for every repo ("" = whole team)
  projects        optional map of session project -> Linear project name
  issue_id        optional, e.g. "ENG-42": post_note comments on it. Empty = open a new issue
  states          state names to list (default ["In Progress", "Todo"]). [] = every state
                  that is not completed or canceled
  labels          label names for a new issue (default none). Unknown names are reported and skipped
  state           state name for a new issue (default unset: Linear's default state)
  api_key_env     name of the env var that holds the key (default LINEAR_API_KEY)
  timeout_seconds per request (default 10)
  max_items       open issues to fetch (default 50)

The key is read from the environment only, and sent as "Authorization: <key>".
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from trackers.base import Context, Item, Tracker, TrackerError

API_URL = "https://api.linear.app/graphql"
CLOSED_STATE_TYPES = ["completed", "canceled"]
DEFAULT_STATES = ["In Progress", "Todo"]

OPEN_ISSUES_QUERY = """
query SessionHooksOpenIssues($filter: IssueFilter, $first: Int) {
  issues(filter: $filter, first: $first) {
    nodes {
      id identifier title url priority dueDate updatedAt
      state { name type }
      labels { nodes { name } }
    }
    pageInfo { hasNextPage }
  }
}
""".strip()

TEAM_QUERY = """
query SessionHooksTeam($key: String!) {
  teams(filter: { key: { eq: $key } }) { nodes { id key name } }
}
""".strip()

TEAM_STATES_QUERY = """
query SessionHooksTeamStates($key: String!) {
  teams(filter: { key: { eq: $key } }) { nodes { id states { nodes { id name type } } } }
}
""".strip()

LABELS_QUERY = """
query SessionHooksLabels($names: [String!]) {
  issueLabels(filter: { name: { in: $names } }) { nodes { id name team { key } } }
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


# Looked up at call time, so a test can replace them with monkeypatch.
DEFAULT_TRANSPORT: Transport = urllib_transport


def DEFAULT_CLOCK() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_time(raw: Any) -> Optional[dt.datetime]:
    """An ISO 8601 timestamp as an aware datetime, or None when it is empty or unreadable."""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        value = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _ago(then: dt.datetime, now: dt.datetime) -> str:
    seconds = (now - then).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _rank(state: Dict[str, Any]) -> int:
    """0 = In Progress, 1 = Todo, 2 = anything else. Linear's state type counts as well as the name."""
    name = str(state.get("name") or "").strip().lower()
    kind = str(state.get("type") or "").strip().lower()
    if name == "in progress" or kind == "started":
        return 0
    if name == "todo" or kind == "unstarted":
        return 1
    return 2


def _number(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class LinearTracker(Tracker):
    name = "linear"

    def __init__(self, settings, context: Context, transport: Optional[Transport] = None,
                 clock: Optional[Callable[[], dt.datetime]] = None) -> None:
        super().__init__(settings, context)
        s = self.settings
        self.team_key = str(s.get("team_key") or "").strip()
        self.project_name = str(s.get("project_name") or "").strip()
        self.projects = s.get("projects") if isinstance(s.get("projects"), dict) else {}
        self.issue_id = str(s.get("issue_id") or "").strip()
        self.key_env = str(s.get("api_key_env") or "LINEAR_API_KEY")
        self.timeout = float(s.get("timeout_seconds", 10))
        self.max_items = int(s.get("max_items", 50))
        self.states = self._names("states", DEFAULT_STATES)
        self.labels = self._names("labels", [])
        state = s.get("state")
        if state is not None and not isinstance(state, str):
            raise TrackerError('trackers.linear.state must be one state name, e.g. "Todo"')
        self.state = (state or "").strip()
        self._transport = transport
        self._clock = clock
        self._more = False

    def _names(self, key: str, default: List[str]) -> List[str]:
        raw = self.settings.get(key)
        if raw is None:
            return list(default)
        if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
            raise TrackerError(f'trackers.linear.{key} must be a list of names, e.g. ["A", "B"]')
        names: List[str] = []
        for name in raw:
            name = name.strip()
            if name and name not in names:
                names.append(name)
        return names

    # ---- what the hooks print -------------------------------------------------------
    def linear_project(self, project: str) -> str:
        return str(self.projects.get(project) or self.project_name or "").strip()

    def scope_label(self, project: str) -> str:
        team = self.team_key or "(team_key not set)"
        lp = self.linear_project(project)
        return f"team {team}, project {lp}" if lp else f"team {team}, all projects"

    def describe(self, project: str) -> str:
        states = ", ".join(self.states) if self.states else "every open state"
        return f"linear {self.scope_label(project)} (states: {states})"

    def plan_post(self, project: str) -> str:
        if self.issue_id:
            return f"add a comment to Linear issue {self.issue_id}"
        lp = self.linear_project(project)
        parts = [f"in team {self.team_key or '(team_key not set)'}"]
        if lp:
            parts.append(f"project {lp}")
        if self.state:
            parts.append(f"state {self.state}")
        if self.labels:
            parts.append("labels " + ", ".join(self.labels))
        return "create a Linear issue " + ", ".join(parts)

    def more_items_note(self) -> str:
        return f"max_items={self.max_items}" if self._more else ""

    def format_item(self, item: Item) -> str:
        title = " ".join(str(item.get("title", "")).split())
        line = f"{item.get('id', '')} [{item.get('state', '')}] {title}"
        parts: List[str] = []
        labels = item.get("labels") or []
        if labels:
            parts.append("labels: " + ", ".join(labels))
        if item.get("due"):
            parts.append(f"due {item['due']}")
        when = _parse_time(item.get("updated"))
        if when is not None:
            parts.append("updated " + _ago(when, (self._clock or DEFAULT_CLOCK)()))
        return f"{line} ({'; '.join(parts)})" if parts else line

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

    def _team_states(self) -> Tuple[str, List[Dict[str, Any]]]:
        """(team id, the team's workflow states)."""
        self._need_team()
        data = self._call(TEAM_STATES_QUERY, {"key": self.team_key})
        nodes = ((data.get("teams") or {}).get("nodes")) or []
        if not nodes:
            raise TrackerError(f"no Linear team with key {self.team_key}")
        states = [x for x in ((nodes[0].get("states") or {}).get("nodes") or [])
                  if isinstance(x, dict) and x.get("name")]
        return str(nodes[0]["id"]), states

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

    def _resolve_labels(self) -> List[str]:
        """Label ids for the configured names. A name that is not found is reported and skipped."""
        data = self._call(LABELS_QUERY, {"names": list(self.labels)})
        nodes = [n for n in ((data.get("issueLabels") or {}).get("nodes") or []) if isinstance(n, dict)]
        ids: List[str] = []
        for name in self.labels:
            found = [n for n in nodes if str(n.get("name") or "").lower() == name.lower()]
            # A label of this team beats a workspace label. A label of another team is no match.
            own = [n for n in found if (n.get("team") or {}).get("key") == self.team_key]
            shared = [n for n in found if not n.get("team")]
            pick = (own or shared or [None])[0]
            if pick is None or not pick.get("id"):
                self.warnings.append(
                    f"Linear label {name!r} not found for team {self.team_key} or the workspace, left off the issue")
            else:
                ids.append(str(pick["id"]))
        return ids

    def _resolve_state(self, states: List[Dict[str, Any]]) -> str:
        """The state id for the configured name, or "" (reported) when the team has no such state."""
        wanted = self.state.lower()
        for exact in (True, False):
            for st in states:
                name = str(st.get("name"))
                if (name == self.state) if exact else (name.lower() == wanted):
                    return str(st.get("id") or "")
        self.warnings.append(
            f"Linear state {self.state!r} not found in team {self.team_key} "
            f"(it has: {', '.join(str(x.get('name')) for x in states) or 'no states'}), "
            "the issue gets Linear's default state")
        return ""

    def _check_empty(self, linear_project: str) -> None:
        """An empty list must mean "nothing open", not "the filter matched nothing"."""
        if self.states:
            _, states = self._team_states()
            known = {str(x.get("name")) for x in states}
            missing = [n for n in self.states if n not in known]
            if missing:
                raise TrackerError(
                    f"no state named {', '.join(repr(n) for n in missing)} in Linear team {self.team_key} "
                    f"(it has: {', '.join(sorted(known)) or 'no states'})")
        else:
            self._resolve_team()
        if linear_project:
            self._resolve_project(linear_project)

    # ---- the interface --------------------------------------------------------------
    def open_items(self, project: str) -> List[Item]:
        self._need_team()
        flt: Dict[str, Any] = {"team": {"key": {"eq": self.team_key}}}
        if self.states:
            flt["state"] = {"name": {"in": list(self.states)}}
        else:
            flt["state"] = {"type": {"nin": CLOSED_STATE_TYPES}}
        lp = self.linear_project(project)
        if lp:
            flt["project"] = {"name": {"eq": lp}}
        data = self._call(OPEN_ISSUES_QUERY, {"filter": flt, "first": self.max_items})
        issues = data.get("issues") or {}
        nodes = issues.get("nodes")
        if nodes is None:
            raise TrackerError("Linear API response has no issues.nodes")
        more = (issues.get("pageInfo") or {}).get("hasNextPage")
        if not isinstance(more, bool):
            raise TrackerError("Linear API response has no issues.pageInfo.hasNextPage")
        self._more = more
        if not nodes:
            self._check_empty(lp)

        def order(node: Dict[str, Any]) -> Tuple[int, float]:
            when = _parse_time(node.get("updatedAt"))
            return _rank(node.get("state") or {}), -(when.timestamp() if when else 0.0)

        items: List[Item] = []
        for n in sorted(nodes, key=order):
            label_nodes = (n.get("labels") or {}).get("nodes") or []
            items.append({
                "id": str(n.get("identifier") or n.get("id") or ""),
                "title": str(n.get("title") or ""),
                "url": str(n.get("url") or ""),
                "state": str((n.get("state") or {}).get("name") or ""),
                "priority": _number(n.get("priority")),
                "labels": [str(x["name"]) for x in label_nodes if isinstance(x, dict) and x.get("name")],
                "due": str(n.get("dueDate") or ""),
                "updated": str(n.get("updatedAt") or ""),
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
        states: List[Dict[str, Any]] = []
        if self.state:
            team_id, states = self._team_states()
        else:
            team_id = self._resolve_team()
        payload: Dict[str, Any] = {"teamId": team_id, "title": title, "description": body}
        lp = self.linear_project(project)
        if lp:
            payload["projectId"] = self._resolve_project(lp)
        if self.labels:
            label_ids = self._resolve_labels()
            if label_ids:
                payload["labelIds"] = label_ids
        if self.state:
            state_id = self._resolve_state(states)
            if state_id:
                payload["stateId"] = state_id
        data = self._call(ISSUE_CREATE_MUTATION, {"input": payload})
        result = data.get("issueCreate") or {}
        if not result.get("success"):
            raise TrackerError("Linear API: issueCreate did not report success")
        return str((result.get("issue") or {}).get("url") or "")


ADAPTER = LinearTracker
