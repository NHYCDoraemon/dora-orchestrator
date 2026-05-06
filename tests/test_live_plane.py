import unittest
from dataclasses import dataclass, field

from orchestrator.plane_live import LivePlaneClient, LivePlaneSettings


class LivePlaneClientTest(unittest.TestCase):
    def test_live_backend_creates_page_cycle_module_and_issue(self):
        api = FakePlaneApi()
        client = LivePlaneClient(
            LivePlaneSettings(
                base_url="https://plane.example",
                workspace_slug="doraemon",
                project_id="project-1",
                api_key="token",
                user_email="raymond@example.com",
                user_password="secret",
            ),
            api=api,
        )

        project = client.upsert_project("dora", "Dora")
        module = client.upsert_module("dora", "implementation")
        cycle = client.upsert_cycle("dora", "S1.5 Phase 4")
        page = client.upsert_page("dora", "batch-20260501A", {"title": "Batch: 20260501A", "body": "# Batch"})
        issue = client.upsert_issue(
            "dora",
            "DORA-CTX-20260501A-T01",
            {
                "name": "CLI context inspect surface",
                "body": "# Task Summary\n\nImplement it.",
                "cycle": "S1.5 Phase 4",
                "module": "implementation",
                "priority": "P1",
                "depends_on": [],
                "source_hash": "abc",
                "agent_hint": "codex",
                "risk": "medium",
                "acceptance": ["go test ./..."],
                "verification_level": ["L1", "L2"],
            },
        )

        self.assertEqual(project["id"], "project-1")
        self.assertEqual(module["name"], "implementation")
        self.assertEqual(cycle["name"], "S1.5 Phase 4")
        self.assertEqual(page["name"], "Batch: 20260501A")
        self.assertEqual(issue["external_id"], "DORA-CTX-20260501A-T01")
        self.assertTrue(api.logged_in)
        self.assertIn(("POST", "/api/workspaces/doraemon/projects/project-1/pages/"), api.calls)
        self.assertIn(("POST", "/api/v1/workspaces/doraemon/projects/project-1/issues/"), api.calls)
        self.assertIn(("POST", "/api/v1/workspaces/doraemon/projects/project-1/modules/module-implementation/module-issues/"), api.calls)
        self.assertIn(("POST", "/api/v1/workspaces/doraemon/projects/project-1/cycles/cycle-s15/cycle-issues/"), api.calls)

    def test_live_backend_creates_project_when_project_id_is_missing(self):
        api = FakePlaneApi(projects=[])
        client = LivePlaneClient(
            LivePlaneSettings(
                base_url="https://plane.example",
                workspace_slug="doraemon",
                project_id="",
                api_key="token",
            ),
            api=api,
        )

        project = client.upsert_project("new-dora", "New Dora")

        self.assertEqual(project["id"], "project-created")
        self.assertEqual(client.project_id, "project-created")
        self.assertIn(("POST", "/api/v1/workspaces/doraemon/projects/"), api.calls)

    def test_falls_through_to_lookup_when_configured_project_id_is_404(self):
        api = FakePlaneApi(projects=[{"id": "project-new", "name": "Dora", "identifier": "DORA"}])
        client = LivePlaneClient(
            LivePlaneSettings(
                base_url="https://plane.example",
                workspace_slug="doraemon",
                project_id="stale-project-id",
                api_key="token",
            ),
            api=api,
        )

        project = client.upsert_project("dora", "Dora")

        self.assertEqual(project["id"], "project-new")
        self.assertEqual(client.project_id, "project-new")
        # GET on stale id was attempted with ok_statuses={404}, then fell through to identifier lookup.
        self.assertEqual(
            api.calls[0],
            ("GET", "/api/v1/workspaces/doraemon/projects/stale-project-id/"),
        )

    def test_existing_issue_is_reused_without_patch(self):
        api = FakePlaneApi()
        api.issues.append(
            {
                "id": "issue-existing",
                "sequence_id": 217,
                "external_id": "DORA-CTX-20260501A-T01",
                "name": "Existing",
            }
        )
        client = LivePlaneClient(
            LivePlaneSettings(
                base_url="https://plane.example",
                workspace_slug="doraemon",
                project_id="project-1",
                api_key="token",
            ),
            api=api,
        )

        issue = client.upsert_issue("dora", "DORA-CTX-20260501A-T01", {"name": "Changed"})

        self.assertEqual(issue["id"], "issue-existing")
        self.assertNotIn(("PATCH", "/api/v1/workspaces/doraemon/projects/project-1/issues/issue-existing/"), api.calls)

    def test_next_ready_issue_skips_root_epics_and_orders_by_priority(self):
        api = FakePlaneApi(
            states=[{"id": "state-backlog", "name": "Backlog"}],
            issues=[
                {
                    "id": "issue-root",
                    "external_id": "DORA-AGCORE-20260501C-ROOT",
                    "state": "state-backlog",
                    "priority": "high",
                },
                {
                    "id": "issue-low",
                    "external_id": "DORA-AGCORE-20260501C-T05",
                    "state": "state-backlog",
                    "priority": "low",
                },
                {
                    "id": "issue-medium",
                    "external_id": "DORA-AGCORE-20260501C-T01",
                    "state": "state-backlog",
                    "priority": "medium",
                },
                {
                    "id": "issue-urgent",
                    "external_id": "DORA-AGCORE-20260501C-T03",
                    "state": "state-backlog",
                    "priority": "urgent",
                },
            ],
        )
        client = LivePlaneClient(
            LivePlaneSettings(
                base_url="https://plane.example",
                workspace_slug="doraemon",
                project_id="project-1",
                api_key="token",
            ),
            api=api,
        )

        ready = client.next_ready_issue("dora")

        self.assertIsNotNone(ready)
        # urgent < medium < low (priority rank), Root Epic excluded entirely.
        self.assertEqual(ready["external_id"], "DORA-AGCORE-20260501C-T03")

    def test_in_progress_held_by_other_agent_is_not_reclaimed(self):
        """Defensive stale-lock policy: an issue assigned to a different
        agent_uuid (or unassigned) is NEVER reclaimed, no matter the
        heartbeat state. Pre-fix this was the chief cause of the 2026-05-05
        runaway — every tick reclaimed someone else's stuck issue and
        re-fed it to claude."""
        api = FakePlaneApi(
            states=[
                {"id": "state-backlog", "name": "Backlog"},
                {"id": "state-in-progress", "name": "In Progress"},
            ],
            issues=[
                {
                    "id": "issue-stuck",
                    "external_id": "DORA-X-20260505A-T01",
                    "state": "state-in-progress",
                    "priority": "urgent",
                    "assignees": ["someone-else-uuid"],  # not us
                    "description_html": "",  # no heartbeat — pre-fix this triggered reclaim
                },
            ],
        )
        client = LivePlaneClient(
            LivePlaneSettings(
                base_url="https://plane.example",
                workspace_slug="doraemon",
                project_id="project-1",
                api_key="token",
                agent_uuid="our-agent-uuid",
            ),
            api=api,
        )

        ready = client.next_ready_issue("dora")
        self.assertIsNone(ready, "issue held by another agent must NOT be reclaimed")

    def test_in_progress_unassigned_with_no_heartbeat_is_not_reclaimed(self):
        """An issue moved to In Progress by a human (no assignees, no
        heartbeat) is NOT reclaimable. Pre-fix this would have been
        treated as stale and stolen back."""
        api = FakePlaneApi(
            states=[
                {"id": "state-in-progress", "name": "In Progress"},
            ],
            issues=[
                {
                    "id": "issue-human-moved",
                    "external_id": "DORA-X-20260505A-T02",
                    "state": "state-in-progress",
                    "priority": "high",
                    "assignees": [],
                    "description_html": "",
                },
            ],
        )
        client = LivePlaneClient(
            LivePlaneSettings(
                base_url="https://plane.example",
                workspace_slug="doraemon",
                project_id="project-1",
                api_key="token",
                agent_uuid="our-agent-uuid",
            ),
            api=api,
        )

        ready = client.next_ready_issue("dora")
        self.assertIsNone(ready)

    def test_in_progress_own_lock_with_stale_heartbeat_IS_reclaimed(self):
        """The legitimate reclaim case: WE held the lock, our run died,
        heartbeat is older than stale_lock_timeout_seconds. This must
        still work — it's the only way a crashed agent recovers."""
        from datetime import datetime, timezone, timedelta

        ancient = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        api = FakePlaneApi(
            states=[
                {"id": "state-in-progress", "name": "In Progress"},
            ],
            issues=[
                {
                    "id": "issue-our-crash",
                    "external_id": "DORA-X-20260505A-T03",
                    "state": "state-in-progress",
                    "priority": "urgent",
                    "assignees": ["our-agent-uuid"],
                    "description_html": (
                        f"<pre>runtime_lock_heartbeat: {ancient}</pre>"
                    ),
                },
            ],
        )
        client = LivePlaneClient(
            LivePlaneSettings(
                base_url="https://plane.example",
                workspace_slug="doraemon",
                project_id="project-1",
                api_key="token",
                agent_uuid="our-agent-uuid",
                stale_lock_timeout_seconds=600,  # 10 min
            ),
            api=api,
        )

        ready = client.next_ready_issue("dora")
        self.assertIsNotNone(ready, "our own crashed claim with stale heartbeat must reclaim")
        self.assertEqual(ready["external_id"], "DORA-X-20260505A-T03")
        self.assertTrue(ready.get("_stale_reclaim"))

    def test_in_progress_own_lock_with_unparseable_heartbeat_is_not_reclaimed(self):
        """Even our own claim — if heartbeat is corrupted, refuse to
        reclaim. Pre-fix this triggered reclaim on the assumption 'no
        valid heartbeat = stale', which is unsafe (corruption could be
        from concurrent writes, partial render, etc)."""
        api = FakePlaneApi(
            states=[
                {"id": "state-in-progress", "name": "In Progress"},
            ],
            issues=[
                {
                    "id": "issue-corrupted-hb",
                    "external_id": "DORA-X-20260505A-T04",
                    "state": "state-in-progress",
                    "priority": "urgent",
                    "assignees": ["our-agent-uuid"],
                    "description_html": (
                        "<pre>runtime_lock_heartbeat: not-a-real-timestamp</pre>"
                    ),
                },
            ],
        )
        client = LivePlaneClient(
            LivePlaneSettings(
                base_url="https://plane.example",
                workspace_slug="doraemon",
                project_id="project-1",
                api_key="token",
                agent_uuid="our-agent-uuid",
            ),
            api=api,
        )

        ready = client.next_ready_issue("dora")
        self.assertIsNone(ready, "corrupted heartbeat is not evidence of stale lock")

    def test_state_counts_aggregates_every_state(self):
        api = FakePlaneApi(
            states=[
                {"id": "s-todo", "name": "Todo"},
                {"id": "s-prog", "name": "In Progress"},
                {"id": "s-done", "name": "Done"},
                {"id": "s-block", "name": "Blocked"},
            ],
            issues=[
                {"id": "i1", "external_id": "X-T01", "state": "s-todo"},
                {"id": "i2", "external_id": "X-T02", "state": "s-todo"},
                {"id": "i3", "external_id": "X-T03", "state": "s-prog"},
                {"id": "i4", "external_id": "X-T04", "state": "s-done"},
                {"id": "i5", "external_id": "X-T05", "state": "s-block"},
            ],
        )
        client = LivePlaneClient(
            LivePlaneSettings(
                base_url="https://plane.example",
                workspace_slug="doraemon",
                project_id="project-1",
                api_key="token",
            ),
            api=api,
        )

        counts = client.state_counts("dora")

        self.assertEqual(
            counts,
            {"Todo": 2, "In Progress": 1, "Done": 1, "Blocked": 1},
        )

    def test_blocked_issues_exposes_depends_on_from_description(self):
        from orchestrator.plane_live import _adapt_issue

        adapted = _adapt_issue(
            {
                "id": "i1",
                "external_id": "DOR-CHATBUG-20260505B-T06",
                "sequence_id": 999,
                "description_html": (
                    "<pre>depends_on:\n  - DOR-CHATBUG-20260505B-T05\n"
                    "  - DOR-CHATBUG-20260505B-T04\nrisk: high\n</pre>"
                ),
            }
        )

        self.assertEqual(
            adapted["depends_on"],
            ["DOR-CHATBUG-20260505B-T05", "DOR-CHATBUG-20260505B-T04"],
        )
        self.assertEqual(adapted["key"], "DOR-999")

    def test_pages_require_session_credentials(self):
        client = LivePlaneClient(
            LivePlaneSettings(
                base_url="https://plane.example",
                workspace_slug="doraemon",
                project_id="project-1",
                api_key="token",
            ),
            api=FakePlaneApi(),
        )

        with self.assertRaisesRegex(ValueError, "PLANE_USER_EMAIL"):
            client.upsert_page("dora", "program-demo", {"title": "Program", "body": "# Program"})


@dataclass
class FakePlaneApi:
    logged_in: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)
    projects: list[dict] = field(default_factory=lambda: [{"id": "project-1", "name": "Dora", "identifier": "DORA"}])
    states: list[dict] = field(default_factory=lambda: [{"id": "state-backlog", "name": "Backlog"}])
    modules: list[dict] = field(default_factory=list)
    cycles: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)

    def login(self) -> None:
        self.logged_in = True

    def paginate_v1(self, path: str, query: dict | None = None) -> list[dict]:
        if path.endswith("/projects/"):
            return self.projects
        if path.endswith("/states/"):
            return self.states
        if path.endswith("/modules/"):
            return self.modules
        if path.endswith("/cycles/"):
            return self.cycles
        if path.endswith("/issues/"):
            return self.issues
        return []

    def list_pages(self, project_id: str) -> list[dict]:
        return self.pages

    def v1(self, method: str, path: str, payload: dict | None = None, *, ok_statuses=frozenset()):
        self.calls.append((method, path))
        if method == "GET" and path.endswith("/projects/project-1/"):
            return self.projects[0]
        if method == "GET" and "/projects/stale-project-id/" in path and 404 in ok_statuses:
            return None
        if method == "POST" and path.endswith("/projects/"):
            item = {"id": "project-created", **payload}
            self.projects.append(item)
            return item
        if method == "POST" and path.endswith("/modules/"):
            item = {"id": f"module-{payload['name']}", **payload}
            self.modules.append(item)
            return item
        if method == "POST" and path.endswith("/cycles/"):
            item = {"id": "cycle-s15", **payload}
            self.cycles.append(item)
            return item
        if method == "POST" and path.endswith("/issues/"):
            item = {"id": "issue-1", "sequence_id": 1, **payload}
            self.issues.append(item)
            return item
        if method == "POST" and path.endswith("/module-issues/"):
            return {"ok": True}
        if method == "POST" and path.endswith("/cycle-issues/"):
            return {"ok": True}
        return {"ok": True}

    def internal(self, method: str, path: str, payload: dict | None = None):
        self.calls.append((method, path))
        if method == "POST" and path.endswith("/pages/"):
            item = {"id": "page-1", **payload}
            self.pages.append(item)
            return item
        return {"ok": True}
