"""In-memory Plane-compatible client for tests and dry-run Dagster assets."""

from dataclasses import dataclass, field
from typing import Any

READY_STATES = {"Backlog", "Todo", "Blocked", "Partial"}


def _deps_satisfied(issue: dict[str, Any], done: set[str]) -> bool:
    return all(dep in done for dep in issue.get("depends_on", []))


def _batch_sort_key(external_id: str) -> str:
    """Extract the batch-date segment (third dash-group) for chronological sort."""
    parts = external_id.split("-")
    if len(parts) >= 3:
        return parts[2]
    return external_id


@dataclass
class InMemoryPlaneClient:
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    cycles: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    modules: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    pages: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    issues: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    reports: list[dict[str, Any]] = field(default_factory=list)
    labels: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    comments: list[dict[str, Any]] = field(default_factory=list)
    heartbeats: list[dict[str, Any]] = field(default_factory=list)

    def upsert_project(self, slug: str, title: str) -> dict[str, Any]:
        self.projects[slug] = {"slug": slug, "title": title}
        return self.projects[slug]

    def upsert_cycle(self, project_slug: str, name: str) -> dict[str, Any]:
        self.cycles[(project_slug, name)] = {"project_slug": project_slug, "name": name}
        return self.cycles[(project_slug, name)]

    def upsert_module(self, project_slug: str, name: str) -> dict[str, Any]:
        self.modules[(project_slug, name)] = {"project_slug": project_slug, "name": name}
        return self.modules[(project_slug, name)]

    def upsert_page(self, project_slug: str, slug: str, payload: dict[str, Any]) -> dict[str, Any]:
        page = dict(payload)
        page.update({"project_slug": project_slug, "slug": slug})
        self.pages[(project_slug, slug)] = page
        return page

    def upsert_issue(self, project_slug: str, external_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self.issues.get((project_slug, external_id), {})
        state = existing.get("state", "Backlog")
        assignee = existing.get("assignee")
        issue = dict(payload)
        issue.update({
            "project_slug": project_slug,
            "external_id": external_id,
            "key": existing.get("key", external_id),
            "state": state,
            "assignee": assignee,
        })
        # For new issues (no existing state), evaluate deps to set Blocked/Todo
        if not existing:
            issue["state"] = self._initial_state(project_slug, issue)
        self.issues[(project_slug, external_id)] = issue
        return issue

    def _initial_state(self, project_slug: str, issue: dict[str, Any]) -> str:
        """Determine initial state for a new issue based on its dependencies."""
        if issue.get("issue_type") == "root_epic":
            return "Backlog"
        done = self._done_set(project_slug)
        if _deps_satisfied(issue, done):
            return "Todo"
        return "Blocked"

    def next_ready_issue(self, project_slug: str, *, exclude: set[str] | None = None) -> dict[str, Any] | None:
        self._refresh_blocked(project_slug)
        done = self._done_set(project_slug)
        candidates = []
        for (slug, external_id), issue in self.issues.items():
            if slug != project_slug or issue.get("state") not in READY_STATES:
                continue
            if issue.get("issue_type") == "root_epic":
                continue
            if exclude and external_id in exclude:
                continue
            if _deps_satisfied(issue, done):
                batch_key = _batch_sort_key(external_id)
                candidates.append((issue.get("priority", ""), batch_key, external_id, issue))
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        return candidates[0][3] if candidates else None

    def claim_issue(self, project_slug: str, external_id: str, run_id: str) -> dict[str, Any]:
        issue = self.issues[(project_slug, external_id)]
        if issue.get("state") == "In Progress" and issue.get("assignee") != run_id:
            raise RuntimeError(f"issue already claimed: {external_id}")
        issue["state"] = "In Progress"
        issue["assignee"] = run_id
        issue["dagster_run_id"] = run_id
        self._refresh_root_epics(project_slug)
        return issue

    def release_issue(self, project_slug: str, external_id: str, state: str) -> dict[str, Any]:
        issue = self.issues[(project_slug, external_id)]
        issue["state"] = state
        issue["assignee"] = None
        # Re-evaluate all issues in the project — a newly Done task
        # may unblock dependents.
        self._refresh_blocked(project_slug)
        self._refresh_root_epics(project_slug)
        return issue

    def publish_run_report(self, project_slug: str, external_id: str, report: dict[str, Any]) -> dict[str, Any]:
        payload = {"project_slug": project_slug, "external_id": external_id, **report}
        self.reports.append(payload)
        self.add_comment(project_slug, external_id, str(report), marker="dora-loop:release")
        return payload

    def add_comment(self, project_slug: str, external_id: str, body: str, *, marker: str | None = None, raw_html: bool = False) -> dict[str, Any]:
        entry = {"project_slug": project_slug, "external_id": external_id, "marker": marker, "body": body, "raw_html": raw_html}
        self.comments.append(entry)
        return entry

    def heartbeat_issue(self, project_slug: str, external_id: str, run_id: str) -> dict[str, Any]:
        issue = self.issues[(project_slug, external_id)]
        issue["assignee"] = run_id
        issue["dagster_run_id"] = run_id
        entry = {"project_slug": project_slug, "external_id": external_id, "run_id": run_id}
        self.heartbeats.append(entry)
        return issue

    def add_label(self, project_slug: str, external_id: str, label_name: str) -> dict[str, Any]:
        self.labels.setdefault((project_slug, label_name), {"project_slug": project_slug, "name": label_name})
        issue = self.issues[(project_slug, external_id)]
        labels = list(issue.get("labels") or [])
        if label_name not in labels:
            labels.append(label_name)
        issue["labels"] = labels
        return issue

    def remove_label(self, project_slug: str, external_id: str, label_name: str) -> dict[str, Any]:
        issue = self.issues[(project_slug, external_id)]
        labels = [item for item in (issue.get("labels") or []) if item != label_name]
        issue["labels"] = labels
        return issue

    def update_page(self, project_slug: str, slug: str, *, body: str | None = None, title: str | None = None, match_substring: str | None = None) -> dict[str, Any]:
        page = self.pages.get((project_slug, slug))
        if page is None and match_substring:
            for (proj, _slug), candidate in self.pages.items():
                if proj != project_slug:
                    continue
                if match_substring in str(candidate.get("title", "")) or match_substring in _slug:
                    page = candidate
                    break
        if page is None:
            raise KeyError(f"page not found: {slug}")
        if body is not None:
            page["body"] = body
        if title is not None:
            page["title"] = title
        return page

    def blocked_issues(self, project_slug: str) -> list[dict[str, Any]]:
        """Return all currently blocked issues sorted by external_id."""
        return sorted(
            [i for (s, _), i in self.issues.items()
             if s == project_slug and i.get("state") == "Blocked"],
            key=lambda i: i.get("external_id", ""),
        )

    def state_counts(self, project_slug: str) -> dict[str, int]:
        """Return {state_name: count} for all issues in *project_slug*."""
        counts: dict[str, int] = {}
        for (slug, _ext), issue in self.issues.items():
            if slug != project_slug:
                continue
            state = str(issue.get("state") or "Unknown")
            counts[state] = counts.get(state, 0) + 1
        return counts

    def query_issues(
        self,
        project_slug: str,
        *,
        states: list[str] | None = None,
        modules: list[str] | None = None,
        batch: str | None = None,
        include_root_epic: bool = False,
    ) -> list[dict[str, Any]]:
        """Return issues for *project_slug* that match every supplied filter.

        Filters are AND-combined; an unset filter matches everything.

        - ``states``: keep only issues whose state name is in this list.
        - ``modules``: keep only issues whose ``module`` field is in this list.
        - ``batch``: keep only issues whose external_id batch segment matches.
          The batch segment is the third dash-group of the external_id
          (the ``<YYYYMMDDA>`` from ``<PROJECT>-<PROGRAM>-<YYYYMMDDA>-T<NN>``).
        - ``include_root_epic``: include ``issue_type == "root_epic"`` rows
          (default False — operators usually want task issues only).
        """
        states_set = set(states) if states else None
        modules_set = set(modules) if modules else None
        results: list[dict[str, Any]] = []
        for (slug, external_id), issue in self.issues.items():
            if slug != project_slug:
                continue
            if not include_root_epic and issue.get("issue_type") == "root_epic":
                continue
            if states_set is not None and str(issue.get("state") or "") not in states_set:
                continue
            if modules_set is not None and str(issue.get("module") or "") not in modules_set:
                continue
            if batch is not None and _batch_sort_key(external_id) != batch:
                continue
            results.append(issue)
        results.sort(key=lambda i: i.get("external_id", ""))
        return results

    # ── internal helpers ──────────────────────────────────────────

    def _done_set(self, project_slug: str) -> set[str]:
        return {
            external_id
            for (slug, external_id), issue in self.issues.items()
            if slug == project_slug and issue.get("state") == "Done"
        }

    def _refresh_blocked(self, project_slug: str) -> None:
        """Re-evaluate every issue: Blocked↔Todo based on current Done set."""
        done = self._done_set(project_slug)
        for (slug, external_id), issue in self.issues.items():
            if slug != project_slug:
                continue
            if issue.get("issue_type") == "root_epic":
                continue
            current = issue.get("state", "Backlog")
            if current in {"In Progress", "Done", "Partial"}:
                continue
            if _deps_satisfied(issue, done):
                if current != "Todo":
                    issue["state"] = "Todo"
            else:
                if current != "Blocked":
                    issue["state"] = "Blocked"

    def _refresh_root_epics(self, project_slug: str) -> None:
        """Roll up every root_epic state from its direct children.

        - all children Done/Cancelled               → ROOT Done
        - any child In Progress/Partial             → ROOT In Progress
        - any child Done/Cancelled (some not yet)   → ROOT In Progress
        - otherwise (all Backlog/Todo/Blocked)      → ROOT Backlog
        """
        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        for (slug, _ext), issue in self.issues.items():
            if slug != project_slug:
                continue
            parent_ext = issue.get("parent_external_id")
            if parent_ext:
                children_by_parent.setdefault(parent_ext, []).append(issue)
        for parent_ext, children in children_by_parent.items():
            parent = self.issues.get((project_slug, parent_ext))
            if not parent or parent.get("issue_type") != "root_epic":
                continue
            statuses = [c.get("state", "Backlog") for c in children]
            if all(s in {"Done", "Cancelled"} for s in statuses):
                target = "Done"
            elif any(s in {"In Progress", "Partial", "Done", "Cancelled"} for s in statuses):
                target = "In Progress"
            else:
                target = "Backlog"
            if parent.get("state") != target:
                parent["state"] = target

    def set_retry_count(self, project_slug: str, external_id: str, count: int) -> None:
        issue = self.issues.get((project_slug, external_id))
        if issue is not None:
            issue["dora_retry_count"] = count
