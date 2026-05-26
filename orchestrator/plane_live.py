"""Live Plane backend using v1 API plus internal Pages session API."""

from __future__ import annotations

import http.cookiejar
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .batch_models import PROGRESS_METADATA_FIELDS
from .issue_order import batch_sort_key, batch_task_order_key


_DORA_METADATA_START = "<!-- dora:metadata"
_DORA_METADATA_END = "dora:metadata -->"
_DORA_METADATA_KEYS = [
    "execution_packet_version",
    "execution_packet_hash",
    "source_docs",
    "source_tables",
    "source_queries",
    "verification_commands",
]
_ORCHESTRATOR_STATE_PAYLOADS = {
    "Blocked": {
        "name": "Blocked",
        "group": "backlog",
        "color": "#FF6B00",
        "description": "Blocked by unfinished dependencies",
    },
    "Partial": {
        "name": "Partial",
        "group": "started",
        "color": "#f1cc36",
        "description": "Executor made progress but did not finish verification",
    },
    "Needs Input": {
        "name": "Needs Input",
        "group": "unstarted",
        "color": "#ed6cb1",
        "description": "Waiting for operator input before execution can continue",
    },
}


@dataclass(frozen=True)
class LivePlaneSettings:
    base_url: str
    workspace_slug: str
    project_id: str
    api_key: str
    user_email: str = ""
    user_password: str = ""
    agent_uuid: str = ""
    throttle: float = 0.6
    stale_lock_timeout_seconds: int = 3600

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "LivePlaneSettings":
        env = _load_plane_env(environ)
        missing = [
            key
            for key in ("PLANE_BASE_URL", "PLANE_WORKSPACE_SLUG", "PLANE_API_KEY")
            if not env.get(key)
        ]
        if missing:
            raise ValueError(f"missing Plane env var(s): {', '.join(missing)}")
        return cls(
            base_url=env["PLANE_BASE_URL"].rstrip("/"),
            workspace_slug=env["PLANE_WORKSPACE_SLUG"],
            project_id=env["PLANE_PROJECT_ID"],
            api_key=env["PLANE_API_KEY"],
            user_email=env.get("PLANE_USER_EMAIL", ""),
            user_password=env.get("PLANE_USER_PASSWORD", ""),
            agent_uuid=env.get("PLANE_AGENT_UUID", ""),
            throttle=float(env.get("PLANE_THROTTLE_SECONDS", "0.6")),
            stale_lock_timeout_seconds=int(env.get("PLANE_LOCK_LEASE_SECONDS", "3600")),
        )


class LivePlaneClient:
    """Plane-compatible client used by batch submission and future runners."""

    def __init__(self, settings: LivePlaneSettings, *, api: PlaneApi | None = None):
        self.settings = settings
        self.api = api or PlaneApi(settings)
        self._project_id = settings.project_id
        self._state_by_name: dict[str, str] | None = None
        self._module_by_name: dict[str, dict[str, Any]] | None = None
        self._cycle_by_name: dict[str, dict[str, Any]] | None = None
        self._issue_by_external_id: dict[str, dict[str, Any]] | None = None
        self._label_by_name: dict[str, dict[str, Any]] | None = None

    @property
    def project_id(self) -> str:
        if not self._project_id:
            raise ValueError("Plane project is not initialized; call upsert_project first")
        return self._project_id

    @property
    def proj_v1(self) -> str:
        return f"/api/v1/workspaces/{self.settings.workspace_slug}/projects/{self.project_id}"

    def upsert_project(self, slug: str, title: str) -> dict[str, Any]:
        if self._project_id:
            project = self.api.v1(
                "GET",
                f"/api/v1/workspaces/{self.settings.workspace_slug}/projects/{self._project_id}/",
                ok_statuses={404},
            )
            if isinstance(project, dict):
                return project
            self._project_id = ""
        for project in self.api.paginate_v1(f"/api/v1/workspaces/{self.settings.workspace_slug}/projects/"):
            if project.get("identifier") == _project_identifier(slug) or project.get("name") == title:
                self._project_id = str(project["id"])
                return project
        created = self.api.v1(
            "POST",
            f"/api/v1/workspaces/{self.settings.workspace_slug}/projects/",
            {
                "name": title,
                "identifier": _project_identifier(slug),
                "module_view": True,
                "cycle_view": True,
                "issue_views_view": True,
                "page_view": True,
            },
        )
        if not isinstance(created, dict) or not created.get("id"):
            raise RuntimeError(f"Plane project create returned invalid response: {title}")
        self._project_id = str(created["id"])
        return created

    def upsert_cycle(self, project_slug: str, name: str) -> dict[str, Any]:
        cycles = self._cycles()
        if name in cycles:
            return cycles[name]
        cycle = self.api.v1(
            "POST",
            f"{self.proj_v1}/cycles/",
            {"name": name, "project_id": self.project_id},
        )
        if not isinstance(cycle, dict):
            raise RuntimeError(f"Plane cycle create returned invalid response: {name}")
        cycles[name] = cycle
        return cycle

    def upsert_module(self, project_slug: str, name: str) -> dict[str, Any]:
        modules = self._modules()
        if name in modules:
            return modules[name]
        module = self.api.v1("POST", f"{self.proj_v1}/modules/", {"name": name})
        if not isinstance(module, dict):
            raise RuntimeError(f"Plane module create returned invalid response: {name}")
        modules[name] = module
        return module

    def upsert_page(self, project_slug: str, slug: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not (self.settings.user_email and self.settings.user_password):
            raise ValueError("PLANE_USER_EMAIL and PLANE_USER_PASSWORD are required for Plane Pages")
        self.api.login()
        page_name = str(payload.get("title") or payload.get("name") or slug)
        for page in self.api.list_pages(self.project_id):
            if page.get("name") == page_name:
                return page
        page_payload = {
            "name": page_name,
            "description_html": payload.get("description_html") or _markdown_to_html(str(payload.get("body", ""))),
            "access": payload.get("access", 0),
            "view_props": payload.get("view_props", {"full_width": True}),
        }
        page = self.api.internal("POST", f"/api/workspaces/{self.settings.workspace_slug}/projects/{self.project_id}/pages/", page_payload)
        if not isinstance(page, dict):
            raise RuntimeError(f"Plane page create returned invalid response: {page_name}")
        return page

    def upsert_issue(self, project_slug: str, external_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        issues = self._issues()
        if external_id in issues:
            return _adapt_issue(issues[external_id])
        issue_payload = self._issue_payload(external_id, payload)
        issue = self.api.v1("POST", f"{self.proj_v1}/issues/", issue_payload)
        if not isinstance(issue, dict):
            raise RuntimeError(f"Plane issue create returned invalid response: {external_id}")
        issues[external_id] = issue
        self._attach_module(issue, payload.get("module"))
        self._attach_cycle(issue, payload.get("cycle"))
        return _adapt_issue(issue)

    def next_ready_issue(self, project_slug: str, *, exclude: set[str] | None = None) -> dict[str, Any] | None:
        states = {item["id"]: item["name"] for item in self.api.paginate_v1(f"{self.proj_v1}/states/")}
        issues = list(self.api.paginate_v1(f"{self.proj_v1}/issues/"))
        done = {
            issue.get("external_id")
            for issue in issues
            if states.get(issue.get("state")) in {"Done", "Cancelled"}
        }
        strict_head = _strict_ready_head_key(issues, states, done)
        now_utc = datetime.now(timezone.utc)
        stale_timeout = self.settings.stale_lock_timeout_seconds
        candidates = []
        reclaimed = 0
        for issue in issues:
            state_name = states.get(issue.get("state"))
            if state_name is None:
                continue
            external_id = issue.get("external_id") or ""
            if not external_id:
                continue
            # Root Epics are batch anchors, never directly executable.
            if external_id.endswith("-ROOT"):
                continue
            order_key = batch_task_order_key(external_id)
            if strict_head is not None and order_key != strict_head:
                continue
            if exclude and external_id in exclude:
                continue

            # Stale lock detection (defensive — see docs/audit/orchestrator-runaway):
            # ONLY reclaim when ALL of these hold:
            #   1. State is "In Progress"
            #   2. The assignee set contains our own agent_uuid (i.e. the
            #      stuck claim is one we made and crashed mid-run)
            #   3. A parseable heartbeat exists AND its age exceeds
            #      stale_lock_timeout_seconds
            #
            # The aggressive "no heartbeat = stale" / "unparseable = stale"
            # / "different agent = stale" rules were the root cause of the
            # 2026-05-05 runaway: any issue moved to In Progress by a human
            # operator, a different agent, or a pre-heartbeat legacy claim
            # got reclaimed every 2 minutes and re-fed to claude forever
            # even when all batches were Done. Reclaim must be evidence-
            # based, not assumption-based.
            is_stale = False
            if state_name == "In Progress":
                assignees = issue.get("assignees") or []
                own_lock = bool(
                    self.settings.agent_uuid
                    and self.settings.agent_uuid in assignees
                )
                if own_lock:
                    heartbeat = _extract_frontmatter_value(
                        issue.get("description_html") or "",
                        "runtime_lock_heartbeat",
                    )
                    if heartbeat:
                        try:
                            heartbeat_str = heartbeat.strip().strip("'\"")
                            heartbeat_dt = datetime.fromisoformat(
                                heartbeat_str.replace("Z", "+00:00")
                            )
                            age = (now_utc - heartbeat_dt).total_seconds()
                            if age > stale_timeout:
                                is_stale = True
                        except (ValueError, TypeError):
                            # Unparseable heartbeat — log nothing here
                            # (next_ready_issue is hot path), refuse to
                            # reclaim. Operator must repair metadata
                            # manually if the issue is genuinely stuck.
                            is_stale = False

            if state_name not in {"Backlog", "Todo", "Blocked", "Partial"} and not is_stale:
                continue

            deps = _extract_frontmatter_list(issue.get("description_html") or "", "depends_on")
            if all(dep in done for dep in deps):
                adapted = _adapt_issue(issue)
                if is_stale:
                    adapted["_stale_reclaim"] = True
                    reclaimed += 1
                batch_key = batch_sort_key(external_id)
                candidates.append((_priority_sort_key(adapted.get("priority", "")), batch_key, external_id, adapted))
        if reclaimed:
            candidates.sort(key=lambda item: (item[0], item[1], item[2]))
            # Prefer reclaimed stale locks so they get unstuck first.
            stale_candidates = [c for c in candidates if c[3].get("_stale_reclaim")]
            fresh_candidates = [c for c in candidates if not c[3].get("_stale_reclaim")]
            candidates = stale_candidates + fresh_candidates
        else:
            candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        return candidates[0][3] if candidates else None

    def claim_issue(self, project_slug: str, external_id: str, run_id: str) -> dict[str, Any]:
        if not self.settings.agent_uuid:
            raise ValueError("PLANE_AGENT_UUID is required to claim live Plane issues")
        issue = self._resolve_issue(external_id)
        payload = {
            "state": self._states()["In Progress"],
            "assignees": [self.settings.agent_uuid],
        }
        updated = self.api.v1("PATCH", f"{self.proj_v1}/issues/{issue['id']}/", payload)
        # Tag with run label so we can recover if this run crashes.
        self.add_label(project_slug, external_id, _run_label(run_id))
        self._refresh_root_epics(project_slug)
        return _adapt_issue(updated if isinstance(updated, dict) else issue)

    def release_issue(self, project_slug: str, external_id: str, state: str) -> dict[str, Any]:
        issue = self._resolve_issue(external_id)
        payload = {
            "state": self._states()[state],
            "assignees": [],
        }
        # Retry on transient Plane API failures so a single 502/503 or
        # connection blip does not leave the task stuck "In Progress" forever.
        max_tries = 4
        last_exc: Exception | None = None
        for attempt in range(max_tries):
            try:
                updated = self.api.v1("PATCH", f"{self.proj_v1}/issues/{issue['id']}/", payload)
                result = _adapt_issue(updated if isinstance(updated, dict) else issue)
                self._refresh_blocked(project_slug)
                self._refresh_root_epics(project_slug)
                return result
            except RuntimeError as exc:
                last_exc = exc
                if attempt < max_tries - 1:
                    time.sleep(min(30, 2 ** (attempt + 1)))
        raise RuntimeError(
            f"release_issue failed after {max_tries} attempts for {external_id}"
        ) from last_exc

    def _refresh_root_epics(self, project_slug: str) -> None:
        """Roll up every ROOT epic's state from its direct children.

        - all children Done/Cancelled               → ROOT Done
        - any child In Progress/Partial             → ROOT In Progress
        - any child Done/Cancelled (some not yet)   → ROOT In Progress
        - otherwise (all Backlog/Todo/Blocked)      → ROOT Backlog
        """
        states = self._states()
        issues = list(self.api.paginate_v1(f"{self.proj_v1}/issues/"))
        by_id: dict[str, dict[str, Any]] = {}
        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        for i in issues:
            iid = i.get("id")
            if iid:
                by_id[iid] = i
            parent = i.get("parent")
            if parent:
                children_by_parent.setdefault(parent, []).append(i)
        for parent_id, children in children_by_parent.items():
            parent = by_id.get(parent_id)
            if not parent:
                continue
            ext = parent.get("external_id") or ""
            if not ext.endswith("-ROOT"):
                continue
            statuses = [self._state_name(c.get("state")) for c in children]
            if all(s in {"Done", "Cancelled"} for s in statuses):
                target_id = states.get("Done")
            elif any(s in {"In Progress", "Partial", "Done", "Cancelled"} for s in statuses):
                target_id = states.get("In Progress")
            else:
                target_id = states.get("Backlog")
            if not target_id or parent.get("state") == target_id:
                continue
            try:
                self.api.v1("PATCH", f"{self.proj_v1}/issues/{parent['id']}/", {"state": target_id})
            except RuntimeError:
                continue
            self._issue_by_external_id = None

    def _refresh_blocked(self, project_slug: str) -> None:
        """Re-evaluate all non-terminal issues: Todo if deps met, Blocked otherwise."""
        states = self._states()
        done = {
            issue.get("external_id")
            for issue in self.api.paginate_v1(f"{self.proj_v1}/issues/")
            if self._state_name(issue.get("state")) in {"Done", "Cancelled"}
        }
        for issue in self.api.paginate_v1(f"{self.proj_v1}/issues/"):
            external_id = issue.get("external_id") or ""
            if not external_id or external_id.endswith("-ROOT"):
                continue
            current = self._state_name(issue.get("state"))
            if current in {"In Progress", "Done", "Cancelled", "Partial", "Needs Input"}:
                continue
            deps = _extract_frontmatter_list(issue.get("description_html") or "", "depends_on")
            if all(dep in done for dep in deps):
                if current != "Todo":
                    self._transition_issue(issue, states.get("Todo"))
            else:
                if current != "Blocked":
                    self._transition_issue(issue, states.get("Blocked"))

    def _state_name(self, state_id: object) -> str:
        """Map a Plane state ID back to its name."""
        if self._state_by_name is None:
            self._states()
        for name, sid in (self._state_by_name or {}).items():
            if sid == state_id:
                return name
        return ""

    def _transition_issue(self, issue: dict[str, Any], target_state_id: object) -> dict[str, Any]:
        """Move an issue to a new state via PATCH."""
        if target_state_id is None:
            return issue
        try:
            updated = self.api.v1("PATCH", f"{self.proj_v1}/issues/{issue['id']}/", {"state": target_state_id})
            self._issues().pop(issue.get("external_id"), None)
            return updated if isinstance(updated, dict) else issue
        except RuntimeError:
            return issue

    def blocked_issues(self, project_slug: str) -> list[dict[str, Any]]:
        """Return all issues currently in the Blocked state."""
        self._refresh_blocked(project_slug)
        result = []
        label_names_by_id = self._label_names_by_id()
        for issue in self.api.paginate_v1(f"{self.proj_v1}/issues/"):
            if self._state_name(issue.get("state")) == "Blocked":
                result.append(_adapt_issue(issue, label_names_by_id=label_names_by_id))
        return sorted(result, key=lambda i: i.get("external_id", ""))

    def state_counts(self, project_slug: str) -> dict[str, int]:
        """Return {state_name: count} for all issues in this project."""
        states = {item["id"]: item["name"] for item in self.api.paginate_v1(f"{self.proj_v1}/states/")}
        counts: dict[str, int] = {}
        for issue in self.api.paginate_v1(f"{self.proj_v1}/issues/"):
            name = states.get(issue.get("state")) or "Unknown"
            counts[name] = counts.get(name, 0) + 1
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

        See ``InMemoryPlaneClient.query_issues`` for filter semantics. The
        ``modules`` filter is resolved against Plane's module-issues join.
        """
        states_set = set(states) if states else None
        state_id_to_name = {item["id"]: item["name"] for item in self.api.paginate_v1(f"{self.proj_v1}/states/")}

        module_member_ids: set[str] | None = None
        if modules:
            module_member_ids = set()
            modules_by_name = self._modules()
            for name in modules:
                module = modules_by_name.get(name)
                if not module:
                    continue
                for entry in self.api.paginate_v1(f"{self.proj_v1}/modules/{module['id']}/module-issues/"):
                    issue_id = entry.get("issue") or entry.get("id")
                    if issue_id:
                        module_member_ids.add(str(issue_id))

        results: list[dict[str, Any]] = []
        label_names_by_id = self._label_names_by_id()
        for issue in self.api.paginate_v1(f"{self.proj_v1}/issues/"):
            external_id = issue.get("external_id") or ""
            if not external_id:
                continue
            if not include_root_epic and external_id.endswith("-ROOT"):
                continue
            state_name = state_id_to_name.get(issue.get("state")) or ""
            if states_set is not None and state_name not in states_set:
                continue
            if batch is not None and batch_sort_key(external_id) != batch:
                continue
            if module_member_ids is not None and str(issue.get("id") or "") not in module_member_ids:
                continue
            adapted = _adapt_issue(issue, label_names_by_id=label_names_by_id)
            adapted["state"] = state_name or adapted.get("state")
            results.append(adapted)
        results.sort(key=lambda i: i.get("external_id", ""))
        return results

    def publish_run_report(self, project_slug: str, external_id: str, report: dict[str, Any]) -> dict[str, Any]:
        return self.add_comment(
            project_slug,
            external_id,
            _render_run_report(report),
            marker="dora-loop:release",
        )

    def add_comment(
        self,
        project_slug: str,
        external_id: str,
        body: str,
        *,
        marker: str | None = None,
        raw_html: bool = False,
    ) -> dict[str, Any]:
        issue = self._resolve_issue(external_id)
        comment_html = _comment_html(body, marker=marker, raw_html=raw_html)
        return self.api.v1(
            "POST",
            f"{self.proj_v1}/issues/{issue['id']}/comments/",
            {"comment_html": comment_html},
        ) or {"ok": True}

    def heartbeat_issue(self, project_slug: str, external_id: str, run_id: str) -> dict[str, Any]:
        if not self.settings.agent_uuid:
            raise ValueError("PLANE_AGENT_UUID is required to heartbeat live Plane issues")
        issue = self._resolve_issue(external_id)
        payload = {"assignees": [self.settings.agent_uuid]}
        updated = self.api.v1("PATCH", f"{self.proj_v1}/issues/{issue['id']}/", payload)
        return _adapt_issue(updated if isinstance(updated, dict) else issue)

    def add_label(self, project_slug: str, external_id: str, label_name: str) -> dict[str, Any]:
        label = self._ensure_label(label_name)
        issue = self._resolve_issue_full(external_id)
        current = list(issue.get("labels") or [])
        if label["id"] in current:
            return _adapt_issue(issue)
        current.append(label["id"])
        updated = self.api.v1("PATCH", f"{self.proj_v1}/issues/{issue['id']}/", {"labels": current})
        if isinstance(updated, dict):
            issue.update(updated)
        issue["labels"] = current
        return _adapt_issue(issue)

    def remove_label(self, project_slug: str, external_id: str, label_name: str) -> dict[str, Any]:
        label = self._labels().get(label_name)
        if not label:
            return {}
        issue = self._resolve_issue_full(external_id)
        current = [lid for lid in (issue.get("labels") or []) if lid != label["id"]]
        if list(current) == list(issue.get("labels") or []):
            return _adapt_issue(issue)
        updated = self.api.v1("PATCH", f"{self.proj_v1}/issues/{issue['id']}/", {"labels": current})
        if isinstance(updated, dict):
            issue.update(updated)
        issue["labels"] = current
        return _adapt_issue(issue)

    def set_retry_count(self, project_slug: str, external_id: str, count: int) -> None:
        """Store `dora_retry_count` in the issue's frontmatter."""
        issue = self._resolve_issue(external_id)
        desc = str(issue.get("description_html") or "")
        import re as _re
        if "dora_retry_count:" in desc:
            desc = _re.sub(r"dora_retry_count:\s*\d+", f"dora_retry_count: {count}", desc)
        else:
            desc = desc.replace("---\n", f"---\ndora_retry_count: {count}\n", 1)
        self.api.v1("PATCH", f"{self.proj_v1}/issues/{issue['id']}/", {"description_html": desc})
        self._issue_by_external_id = None

    def _recover_run_claims(self, project_slug: str, run_id: str) -> int:
        """Release any issues stuck In Progress from a previous attempt of *run_id*.

        Looks for the ``orch-run-<short>`` label that claim_issue attaches.
        Returns the number of recovered issues.
        """
        label_name = _run_label(run_id)
        states = self._states()
        in_progress_id = states.get("In Progress")
        backlog_id = states.get("Backlog")
        recovered = 0
        label_ids = self._labels()
        target_id = label_ids.get(label_name, {}).get("id") if isinstance(label_ids, dict) else None
        if not target_id:
            return 0
        for issue in self.api.paginate_v1(f"{self.proj_v1}/issues/"):
            if issue.get("state") != in_progress_id:
                continue
            issue_labels = list(issue.get("labels") or [])
            if target_id in issue_labels:
                eid = issue.get("external_id", "")
                try:
                    self.api.v1(
                        "PATCH",
                        f"{self.proj_v1}/issues/{issue['id']}/",
                        {"state": backlog_id, "assignees": []},
                    )
                    if eid:
                        self.remove_label(project_slug, eid, label_name)
                    recovered += 1
                except Exception:
                    pass
        return recovered

    def _resolve_issue_full(self, external_id: str) -> dict[str, Any]:
        """Like _resolve_issue but ensures fields like `labels` are populated.

        The /issues/ list endpoint omits some fields; a single-issue GET is
        required to read the full label list before mutating it. We cache the
        enriched result so repeat calls on the same issue stay cheap.
        """
        issue = self._resolve_issue(external_id)
        if not issue.get("_full_loaded"):
            fresh = self.api.v1("GET", f"{self.proj_v1}/issues/{issue['id']}/")
            if isinstance(fresh, dict):
                issue.update(fresh)
            issue["_full_loaded"] = True
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
        """PATCH a Plane Page by exact name OR substring match.

        Plane Pages are looked up by `name` (no external_id field). When
        `slug` was assigned by `upsert_page` it doesn't appear in the Plane
        page name, so callers can pass `match_substring` (e.g. the batch_id)
        to find the right page by partial name containment.
        """
        if not (self.settings.user_email and self.settings.user_password):
            raise ValueError("PLANE_USER_EMAIL and PLANE_USER_PASSWORD are required for Plane Pages")
        self.api.login()
        target = None
        for page in self.api.list_pages(self.project_id):
            name = page.get("name", "")
            if name == slug or (title and name == title):
                target = page
                break
            if match_substring and match_substring in name:
                target = page
                break
        if not target:
            raise KeyError(f"page not found by slug/title/substring: {slug!r}/{title!r}/{match_substring!r}")
        payload: dict[str, Any] = {}
        if title is not None:
            payload["name"] = title
        if body is not None:
            payload["description_html"] = _markdown_to_html(body)
        if not payload:
            return target
        path = f"/api/workspaces/{self.settings.workspace_slug}/projects/{self.project_id}/pages/{target['id']}/"
        updated = self.api.internal("PATCH", path, payload)
        return updated if isinstance(updated, dict) else target

    def _ensure_label(self, name: str) -> dict[str, Any]:
        labels = self._labels()
        if name in labels:
            return labels[name]
        created = self.api.v1("POST", f"{self.proj_v1}/labels/", {"name": name})
        if not isinstance(created, dict) or not created.get("id"):
            raise RuntimeError(f"Plane label create failed: {name}")
        labels[name] = created
        return created

    def _labels(self) -> dict[str, dict[str, Any]]:
        if self._label_by_name is None:
            self._label_by_name = {
                item["name"]: item
                for item in self.api.paginate_v1(f"{self.proj_v1}/labels/")
                if item.get("name")
            }
        return self._label_by_name

    def _label_names_by_id(self) -> dict[str, str]:
        return {
            str(label["id"]): name
            for name, label in self._labels().items()
            if label.get("id")
        }

    def _issue_payload(self, external_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = _issue_markdown(external_id, payload)
        state_id = self._states().get("Backlog")
        issue_type = str(payload.get("issue_type") or "")
        if issue_type == "root_epic":
            state_id = self._states().get("Backlog")
        else:
            deps = list(payload.get("depends_on") or [])
            if deps:
                done = {
                    issue.get("external_id")
                    for issue in self._issues().values()
                    if self._state_name(issue.get("state")) in {"Done", "Cancelled"}
                }
                if all(dep in done for dep in deps):
                    state_id = self._states().get("Todo")
                else:
                    state_id = self._states().get("Blocked")
            else:
                state_id = self._states().get("Todo")
        out = {
            "name": payload.get("name") or payload.get("title") or external_id,
            "description_html": _markdown_to_html(body),
            "external_id": external_id,
            "external_source": "dora-orchestrator",
            "state": state_id,
            "priority": _map_priority(str(payload.get("priority", ""))),
        }
        parent_external_id = payload.get("parent_external_id")
        if parent_external_id:
            parent = self._issues().get(str(parent_external_id))
            if parent:
                out["parent"] = parent["id"]
        return out

    def _attach_module(self, issue: dict[str, Any], module_name: object) -> None:
        if not module_name:
            return
        module = self._modules().get(str(module_name))
        if module:
            self.api.v1("POST", f"{self.proj_v1}/modules/{module['id']}/module-issues/", {"issues": [issue["id"]]})

    def _attach_cycle(self, issue: dict[str, Any], cycle_name: object) -> None:
        if not cycle_name:
            return
        cycle = self._cycles().get(str(cycle_name))
        if cycle:
            self.api.v1("POST", f"{self.proj_v1}/cycles/{cycle['id']}/cycle-issues/", {"issues": [issue["id"]]})

    def _resolve_issue(self, external_id: str) -> dict[str, Any]:
        issue = self._issues().get(external_id)
        if issue:
            return issue
        self._issue_by_external_id = None
        issue = self._issues().get(external_id)
        if not issue:
            raise KeyError(f"Plane issue not found by external_id: {external_id}")
        return issue

    def _states(self) -> dict[str, str]:
        if self._state_by_name is None:
            self._state_by_name = {
                item["name"]: item["id"]
                for item in self.api.paginate_v1(f"{self.proj_v1}/states/")
                if item.get("name") and item.get("id")
            }
            self._ensure_orchestrator_states(self._state_by_name)
        return self._state_by_name

    def _ensure_orchestrator_states(self, state_by_name: dict[str, str]) -> None:
        for name, payload in _ORCHESTRATOR_STATE_PAYLOADS.items():
            if name in state_by_name:
                continue
            created = self.api.v1("POST", f"{self.proj_v1}/states/", payload)
            if not isinstance(created, dict) or not created.get("id"):
                raise RuntimeError(f"Plane state create failed: {name}")
            state_by_name[name] = str(created["id"])

    def _modules(self) -> dict[str, dict[str, Any]]:
        if self._module_by_name is None:
            self._module_by_name = {
                item["name"]: item
                for item in self.api.paginate_v1(f"{self.proj_v1}/modules/")
                if item.get("name")
            }
        return self._module_by_name

    def _cycles(self) -> dict[str, dict[str, Any]]:
        if self._cycle_by_name is None:
            self._cycle_by_name = {
                item["name"]: item
                for item in self.api.paginate_v1(f"{self.proj_v1}/cycles/")
                if item.get("name")
            }
        return self._cycle_by_name

    def _issues(self) -> dict[str, dict[str, Any]]:
        if self._issue_by_external_id is None:
            self._issue_by_external_id = {
                item["external_id"]: item
                for item in self.api.paginate_v1(f"{self.proj_v1}/issues/")
                if item.get("external_id")
            }
        return self._issue_by_external_id


class PlaneApi:
    """Thin urllib wrapper around Plane v1 and internal APIs."""

    def __init__(self, settings: LivePlaneSettings):
        self.settings = settings
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.csrf: str | None = None
        self._last_call = 0.0
        self._logged_in = False

    def login(self) -> None:
        if self._logged_in:
            return
        if not (self.settings.user_email and self.settings.user_password):
            raise ValueError("PLANE_USER_EMAIL and PLANE_USER_PASSWORD are required for Plane session login")
        token = self._open(
            urllib.request.Request(f"{self.settings.base_url}/auth/get-csrf-token/"),
            use_opener=True,
        )
        if not isinstance(token, dict) or not token.get("csrf_token"):
            raise RuntimeError("Plane did not return csrf_token")
        self.csrf = str(token["csrf_token"])
        data = urllib.parse.urlencode(
            {
                "email": self.settings.user_email,
                "password": self.settings.user_password,
                "csrfmiddlewaretoken": self.csrf,
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.settings.base_url}/auth/sign-in/",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRFToken": self.csrf,
                "Referer": self.settings.base_url + "/sign-in",
            },
            method="POST",
        )
        self._open(req, use_opener=True)
        for cookie in self.cookie_jar:
            if cookie.name == "csrftoken":
                self.csrf = cookie.value
        if "session-id" not in {cookie.name for cookie in self.cookie_jar}:
            raise RuntimeError("Plane login did not return session-id cookie")
        self._logged_in = True

    def v1(self, method: str, path: str, payload: dict[str, Any] | None = None, *, ok_statuses: set[int] = frozenset()):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.settings.base_url}{path}",
            data=data,
            headers={
                "X-API-Key": self.settings.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        return self._open(req, ok_statuses=ok_statuses)

    def internal(self, method: str, path: str, payload: dict[str, Any] | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": self.settings.base_url + "/",
        }
        if self.csrf:
            headers["X-CSRFToken"] = self.csrf
        req = urllib.request.Request(f"{self.settings.base_url}{path}", data=data, headers=headers, method=method)
        return self._open(req, use_opener=True)

    def paginate_v1(self, path: str, query: dict[str, str] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        q = dict(query or {})
        q["per_page"] = "100"
        while True:
            data = self.v1("GET", f"{path}?{urllib.parse.urlencode(q)}")
            if isinstance(data, dict):
                out.extend(data.get("results", []) or data.get("items", []))
                cursor = data.get("next_cursor")
                if not data.get("next_page_results") or not cursor:
                    break
                q["cursor"] = cursor
                continue
            if isinstance(data, list):
                out.extend(data)
            break
        return out

    def list_pages(self, project_id: str) -> list[dict[str, Any]]:
        data = self.internal("GET", f"/api/workspaces/{self.settings.workspace_slug}/projects/{project_id}/pages/")
        return data if isinstance(data, list) else []

    def _open(self, req: urllib.request.Request, *, use_opener: bool = False, ok_statuses: set[int] = frozenset()):
        max_tries = 8
        for tries in range(max_tries):
            self._wait()
            try:
                opener = self.opener.open if use_opener else urllib.request.urlopen
                with opener(req, timeout=20) as response:
                    body = response.read()
                    if not body:
                        return None
                    try:
                        return json.loads(body)
                    except json.JSONDecodeError:
                        # Python 3.13+ rejects control characters in JSON.
                        # Plane's description_html may contain unescaped raw
                        # control chars that are safe to strip.
                        text = body.decode("utf-8", errors="replace")
                        cleaned = _strip_json_control_chars(text)
                        try:
                            return json.loads(cleaned)
                        except json.JSONDecodeError:
                            return None
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and tries < max_tries - 1:
                    time.sleep(min(60, 2 ** (tries + 1)))
                    continue
                if exc.code in ok_statuses:
                    return None
                body = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"Plane HTTP {exc.code} on {req.method} {req.full_url}: {body}") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Plane unreachable: {exc.reason}") from exc
        raise RuntimeError("Plane request retry exhausted")

    def _wait(self) -> None:
        gap = self.settings.throttle - (time.time() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        self._last_call = time.time()


def _load_plane_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(environ or os.environ)
    merged: dict[str, str] = {}
    candidates = [
        Path.home() / ".dora" / "plane.env",
        Path.home() / "dagster" / ".env",
    ]
    if env.get("DORA_HOME"):
        candidates.append(Path(env["DORA_HOME"]) / "plane.env")
    for path in reversed(candidates):
        merged.update(_parse_env_file(path))
    merged.update({key: value for key, value in env.items() if key.startswith("PLANE_") and value})
    return merged


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _markdown_to_html(markdown: str) -> str:
    return "<pre>" + html.escape(markdown) + "</pre>"


def _metadata_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in _DORA_METADATA_KEYS if key in payload}


def _append_metadata_block(markdown: str, metadata: dict[str, Any]) -> str:
    if not metadata:
        return markdown
    encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    return f"{markdown.rstrip()}\n\n{_DORA_METADATA_START}\n{encoded}\n{_DORA_METADATA_END}\n"


def _extract_metadata_block(description_html: str) -> dict[str, Any]:
    text = html.unescape(description_html)
    if "<pre>" in text and "</pre>" in text:
        text = text.split("<pre>", 1)[1].split("</pre>", 1)[0]
    search_before = len(text)
    while True:
        end = text.rfind(_DORA_METADATA_END, 0, search_before)
        if end == -1:
            return {}
        start = text.rfind(_DORA_METADATA_START, 0, end)
        if start == -1:
            return {}
        raw = text[start + len(_DORA_METADATA_START) : end].strip()
        search_before = start
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return {key: parsed[key] for key in _DORA_METADATA_KEYS if key in parsed}


def _comment_html(body: str, *, marker: str | None = None, raw_html: bool = False) -> str:
    """Format a Plane comment matching dora_plane.py's marker convention.

    Marker is wrapped as ``<!-- {marker} -->`` so it stays out of the rendered
    body but remains greppable in the raw HTML, matching dev_loop's `dora-loop:*`
    convention.
    """
    rendered = body if raw_html else "<pre>" + html.escape(body) + "</pre>"
    if not marker:
        return rendered
    marker = marker.strip()
    if not (marker.startswith("<!--") and marker.endswith("-->")):
        marker = f"<!-- {marker} -->"
    return f"{marker}\n{rendered}"


def _issue_markdown(external_id: str, payload: dict[str, Any]) -> str:
    body = str(payload.get("body", "")).strip()
    lines = [
        body,
        "",
        "## 系统元数据",
        "",
        "---",
        f"external_id: {external_id}",
        f"external_source: dora-orchestrator",
        f"source_hash: {payload.get('source_hash', '')}",
        f"agent_hint: {payload.get('agent_hint', '')}",
        f"risk: {payload.get('risk', '')}",
        "depends_on: " + ("[]" if not payload.get("depends_on") else ""),
    ]
    for dep in payload.get("depends_on") or []:
        lines.append(f"  - {dep}")
    required_skills = payload.get("required_skills") or []
    if required_skills:
        lines.append("required_skills:")
        for skill in required_skills:
            lines.append(f"  - {skill}")
    suggested_skills = payload.get("suggested_skills") or []
    if suggested_skills:
        lines.append("suggested_skills:")
        for skill in suggested_skills:
            lines.append(f"  - {skill}")
    forbidden_skills = payload.get("forbidden_skills") or []
    if forbidden_skills:
        lines.append("forbidden_skills:")
        for skill in forbidden_skills:
            lines.append(f"  - {skill}")
    for key in PROGRESS_METADATA_FIELDS:
        value = payload.get(key)
        if value is not None and str(value).strip():
            lines.append(f"{key}: {value}")
    lines.append("verification_level: " + ("[]" if not payload.get("verification_level") else ""))
    for level in payload.get("verification_level") or []:
        lines.append(f"  - {level}")
    lines.append("---")
    markdown = "\n".join(lines).strip() + "\n"
    return _append_metadata_block(markdown, _metadata_payload(payload))


def _extract_frontmatter_list(description_html: str, key: str) -> list[str]:
    text = html.unescape(description_html)
    if "<pre>" in text and "</pre>" in text:
        text = text.split("<pre>", 1)[1].split("</pre>", 1)[0]
    lines = text.splitlines()
    out: list[str] = []
    in_key = False
    for line in lines:
        if line.startswith(f"{key}:"):
            in_key = True
            continue
        if in_key and line.startswith("  - "):
            out.append(line[4:].strip())
            continue
        if in_key and line and not line.startswith(" "):
            break
    return out


def _extract_frontmatter_value(description_html: str, key: str) -> str | None:
    """Extract a single scalar value from YAML front-matter embedded in Plane HTML."""
    text = html.unescape(description_html)
    if "<pre>" in text and "</pre>" in text:
        text = text.split("<pre>", 1)[1].split("</pre>", 1)[0]
    # Match key: value patterns common in front-matter (handles quoted and unquoted)
    m = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
    if not m:
        return None
    return m.group(1).strip()


def _map_priority(priority: str) -> str:
    return {
        "P0": "urgent",
        "P1": "high",
        "P2": "medium",
        "P3": "low",
    }.get(priority.upper(), priority.lower() if priority else "medium")


_PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "none": 4, "": 5}


def _priority_sort_key(priority: str) -> int:
    """Numeric rank for Plane priority strings so urgent < high < medium < low.

    Naive string sort makes 'high' < 'medium' (alphabetical), which puts
    higher-priority issues first only by accident. This explicit rank
    preserves the intended ordering and falls through gracefully for
    unknown values.
    """
    return _PRIORITY_RANK.get(str(priority).lower(), len(_PRIORITY_RANK))


def _strict_ready_head_key(
    issues: list[dict[str, Any]],
    states: dict[str, str],
    done: set[str],
) -> tuple[str, int, str] | None:
    keys: list[tuple[str, int, str]] = []
    for issue in issues:
        external_id = issue.get("external_id") or ""
        if not external_id or external_id.endswith("-ROOT"):
            continue
        if states.get(issue.get("state")) in {"Done", "Cancelled"}:
            continue
        order_key = batch_task_order_key(external_id)
        if order_key is not None:
            deps = _extract_frontmatter_list(issue.get("description_html") or "", "depends_on")
            if all(dep in done for dep in deps):
                keys.append(order_key)
    return min(keys) if keys else None


def _project_identifier(slug: str) -> str:
    identifier = "".join(ch for ch in slug.upper() if ch.isalnum())
    return (identifier or "DORA")[:5]


def _strip_json_control_chars(text: str) -> str:
    """Remove control characters that Python 3.13+ json.loads rejects.

    Plane's API occasionally emits raw control characters (U+0000–U+001F)
    inside ``description_html`` fields. The JSON spec requires these to be
    escaped, but Python < 3.13 accepted them anyway. We strip them so
    ``json.loads`` succeeds on Python 3.13+ without changing semantics
    (they are almost always formatting noise, not meaningful data).
    """
    # Keep \t (0x09), \n (0x0a), \r (0x0d) — they are valid JSON whitespace.
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def _adapt_issue(issue: dict[str, Any], *, label_names_by_id: dict[str, str] | None = None) -> dict[str, Any]:
    adapted = dict(issue)
    description_html = issue.get("description_html") or ""
    sequence_id = adapted.get("sequence_id")
    adapted.setdefault("key", f"DOR-{sequence_id}" if sequence_id is not None else adapted.get("external_id", ""))
    adapted.setdefault(
        "depends_on",
        _extract_frontmatter_list(description_html, "depends_on"),
    )
    adapted.setdefault(
        "required_skills",
        _extract_frontmatter_list(description_html, "required_skills"),
    )
    adapted.setdefault(
        "suggested_skills",
        _extract_frontmatter_list(description_html, "suggested_skills"),
    )
    adapted.setdefault(
        "forbidden_skills",
        _extract_frontmatter_list(description_html, "forbidden_skills"),
    )
    for key in PROGRESS_METADATA_FIELDS:
        if not str(adapted.get(key) or "").strip():
            value = _extract_frontmatter_value(description_html, key)
            if value is not None:
                adapted[key] = value
    if label_names_by_id:
        label_names = _issue_label_names(adapted.get("labels"), label_names_by_id)
        if label_names:
            adapted["label_names"] = label_names
    adapted.update(_extract_metadata_block(description_html))
    return adapted


def _issue_label_names(labels: object, label_names_by_id: dict[str, str]) -> list[str]:
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for item in labels:
        if isinstance(item, str):
            name = label_names_by_id.get(item)
            if name and name not in names:
                names.append(name)
        elif isinstance(item, dict):
            name_value = item.get("name")
            if isinstance(name_value, str) and name_value not in names:
                names.append(name_value)
            id_value = item.get("id")
            if id_value is not None:
                mapped = label_names_by_id.get(str(id_value))
                if mapped and mapped not in names:
                    names.append(mapped)
    return names


def _render_run_report(report: dict[str, Any]) -> str:
    lines = ["# Dora Orchestrator Run Report", ""]
    for key in sorted(report):
        lines.append(f"- {key}: {report[key]}")
    return "\n".join(lines) + "\n"


def _run_label(run_id: str) -> str:
    """Return a stable Plane label name for *run_id*.

    Uses the first 8 characters to keep the label short and readable.
    """
    short = run_id.split("-")[0] if "-" in run_id else run_id[:8]
    return f"orch-run-{short}"
