"""In-memory Plane-compatible client for tests and dry-run Dagster assets."""

from dataclasses import dataclass, field
from typing import Any


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
        issue.update(
            {
                "project_slug": project_slug,
                "external_id": external_id,
                "key": existing.get("key", external_id),
                "state": state,
                "assignee": assignee,
            }
        )
        self.issues[(project_slug, external_id)] = issue
        return issue

    def next_ready_issue(self, project_slug: str) -> dict[str, Any] | None:
        done = {
            external_id
            for (slug, external_id), issue in self.issues.items()
            if slug == project_slug and issue.get("state") == "Done"
        }
        candidates = []
        for (slug, external_id), issue in self.issues.items():
            if slug != project_slug or issue.get("state") not in {"Backlog", "Todo", "Partial"}:
                continue
            if issue.get("issue_type") == "root_epic":
                continue
            if all(dep in done for dep in issue.get("depends_on", [])):
                candidates.append((issue.get("priority", ""), external_id, issue))
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2] if candidates else None

    def claim_issue(self, project_slug: str, external_id: str, run_id: str) -> dict[str, Any]:
        issue = self.issues[(project_slug, external_id)]
        if issue.get("state") == "In Progress" and issue.get("assignee") != run_id:
            raise RuntimeError(f"issue already claimed: {external_id}")
        issue["state"] = "In Progress"
        issue["assignee"] = run_id
        issue["dagster_run_id"] = run_id
        return issue

    def release_issue(self, project_slug: str, external_id: str, state: str) -> dict[str, Any]:
        issue = self.issues[(project_slug, external_id)]
        issue["state"] = state
        issue["assignee"] = None
        return issue

    def publish_run_report(self, project_slug: str, external_id: str, report: dict[str, Any]) -> dict[str, Any]:
        payload = {"project_slug": project_slug, "external_id": external_id, **report}
        self.reports.append(payload)
        self.add_comment(project_slug, external_id, str(report), marker="dora-loop:release")
        return payload

    def add_comment(
        self,
        project_slug: str,
        external_id: str,
        body: str,
        *,
        marker: str | None = None,
        raw_html: bool = False,
    ) -> dict[str, Any]:
        entry = {
            "project_slug": project_slug,
            "external_id": external_id,
            "marker": marker,
            "body": body,
            "raw_html": raw_html,
        }
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

    def update_page(
        self,
        project_slug: str,
        slug: str,
        *,
        body: str | None = None,
        title: str | None = None,
        match_substring: str | None = None,
    ) -> dict[str, Any]:
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
