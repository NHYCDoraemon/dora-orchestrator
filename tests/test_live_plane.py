import json
import unittest
from dataclasses import dataclass, field

from orchestrator.plane_live import LivePlaneClient, LivePlaneSettings, _adapt_issue, _issue_markdown, _markdown_to_html
from orchestrator.source_visibility import classify_source_context


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
                "name": "CLI 上下文检查能力",
                "body": "# CLI 上下文检查能力\n\n## 任务概要\n\n实现检查入口。",
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
        self.assertIn("# CLI 上下文检查能力", api.issues[-1]["description_html"])
        self.assertIn("## 系统元数据", api.issues[-1]["description_html"])
        self.assertLess(
            api.issues[-1]["description_html"].index("# CLI 上下文检查能力"),
            api.issues[-1]["description_html"].index("## 系统元数据"),
        )
        self.assertTrue(api.logged_in)
        self.assertIn(("POST", "/api/workspaces/doraemon/projects/project-1/pages/"), api.calls)
        self.assertIn(("POST", "/api/v1/workspaces/doraemon/projects/project-1/issues/"), api.calls)
        self.assertIn(("POST", "/api/v1/workspaces/doraemon/projects/project-1/modules/module-implementation/module-issues/"), api.calls)
        self.assertIn(("POST", "/api/v1/workspaces/doraemon/projects/project-1/cycles/cycle-s15/cycle-issues/"), api.calls)

    def test_adapt_issue_extracts_required_skills_from_frontmatter(self):
        adapted = _adapt_issue({
            "id": "issue-1",
            "sequence_id": 1,
            "external_id": "DORA-BACK-20260501A-T01",
            "description_html": (
                "<pre>---\n"
                "required_skills:\n"
                "  - maritime-java-backend-development\n"
                "depends_on: []\n"
                "---</pre>"
            ),
        })

        self.assertEqual(adapted["required_skills"], ["maritime-java-backend-development"])

    def test_adapt_issue_extracts_suggested_and_forbidden_skills_from_frontmatter(self):
        adapted = _adapt_issue({
            "id": "issue-1",
            "sequence_id": 1,
            "external_id": "DORA-FE-20260501A-T01",
            "description_html": (
                "<pre>---\n"
                "suggested_skills:\n"
                "  - browser:browser\n"
                "forbidden_skills:\n"
                "  - dora-plane\n"
                "depends_on: []\n"
                "---</pre>"
            ),
        })

        self.assertEqual(adapted["suggested_skills"], ["browser:browser"])
        self.assertEqual(adapted["forbidden_skills"], ["dora-plane"])

    def test_live_issue_frontmatter_preserves_progress_metadata(self):
        api = FakePlaneApi()
        client = LivePlaneClient(
            LivePlaneSettings(
                base_url="https://plane.example",
                workspace_slug="doraemon",
                project_id="project-1",
                api_key="token",
            ),
            api=api,
        )

        issue = client.upsert_issue(
            "dora",
            "PFE-P1FR-20260525C-T01",
            {
                "name": "F97-036 表单模板列表",
                "body": "# 表单模板列表\n",
                "priority": "P1",
                "depends_on": [],
                "progress_schema": "pfe-progress/v1",
                "progress_task_id": "B06-F97-036-form-list",
                "row_id": "F97-036",
                "source_scope": "original-97",
                "task_kind": "connect_or_contract",
                "route_path": "/forms/list",
                "backend_contract": "GET /api/v1/forms",
                "frontend_surface": "src/pages/forms/list/FormListPage.tsx",
                "requirement_signal": "用户能在 /forms/list 看到真实列表。",
                "development_work": "接入 GET /api/v1/forms。",
                "data_lineage": "GET /api/v1/forms -> src/api/forms.ts",
                "acceptance_signal": "真实接口响应驱动页面。",
                "verification_signal": "L1-L5 全绿。",
                "ledger_update_rule": "证据同步后才允许变更状态。",
                "no_go_signal": "mock-only 不可通过。",
                "verification_level": ["L1"],
            },
        )

        self.assertEqual(issue["progress_schema"], "pfe-progress/v1")
        self.assertEqual(issue["progress_task_id"], "B06-F97-036-form-list")
        self.assertEqual(issue["row_id"], "F97-036")
        self.assertIn("progress_task_id: B06-F97-036-form-list", api.issues[-1]["description_html"])

    def test_issue_markdown_round_trips_execution_packet_metadata(self):
        payload = {
            "name": "Gateway source",
            "body": "# Gateway source\n",
            "source_hash": "sha256:task",
            "agent_hint": "claude",
            "risk": "medium",
            "depends_on": [],
            "verification_level": ["L1"],
            "verification_commands": ["pytest tests/test_gateway.py -q"],
            "execution_packet_version": 1,
            "execution_packet_hash": "sha256:packet",
            "source_docs": [{"kind": "source_docs", "path": "docs/design.md", "sha256": "sha256:doc", "required": True}],
            "source_tables": [{"id": "progress_ledger", "path": "docs/progress/ledger.tsv", "format": "tsv", "required": True}],
            "source_queries": [{"id": "current_task_row", "table": "progress_ledger", "required": True, "filters": []}],
        }

        markdown = _issue_markdown("DORA-CTX-20260501A-T01", payload)
        adapted = _adapt_issue(
            {
                "id": "plane-1",
                "external_id": "DORA-CTX-20260501A-T01",
                "description_html": _markdown_to_html(markdown),
            }
        )

        self.assertEqual(adapted["execution_packet_version"], 1)
        self.assertEqual(adapted["execution_packet_hash"], payload["execution_packet_hash"])
        self.assertEqual(adapted["source_docs"], payload["source_docs"])
        self.assertEqual(adapted["source_tables"], payload["source_tables"])
        self.assertEqual(adapted["source_queries"], payload["source_queries"])
        self.assertEqual(adapted["verification_commands"], payload["verification_commands"])

    def test_issue_metadata_block_ignores_unknown_keys(self):
        metadata = {
            "name": "Injected name",
            "external_id": "DORA-BAD-20260501A-T99",
            "execution_packet_hash": "sha256:packet",
        }
        markdown = (
            "# Gateway source\n\n"
            "<!-- dora:metadata\n"
            f"{json.dumps(metadata)}\n"
            "dora:metadata -->\n"
        )

        adapted = _adapt_issue(
            {
                "id": "plane-1",
                "name": "Original name",
                "external_id": "DORA-CTX-20260501A-T01",
                "description_html": _markdown_to_html(markdown),
            }
        )

        self.assertEqual(adapted["name"], "Original name")
        self.assertEqual(adapted["external_id"], "DORA-CTX-20260501A-T01")
        self.assertEqual(adapted["execution_packet_hash"], "sha256:packet")

    def test_issue_metadata_block_uses_valid_appended_block_after_malformed_marker(self):
        payload = {
            "name": "Gateway source",
            "body": (
                "# Gateway source\n\n"
                "<!-- dora:metadata\n"
                "{\"execution_packet_hash\": \n\n"
            ),
            "verification_level": ["L1"],
            "execution_packet_version": 1,
            "execution_packet_hash": "sha256:valid",
            "verification_commands": ["pytest tests/test_gateway.py -q"],
        }

        markdown = _issue_markdown("DORA-CTX-20260501A-T01", payload)
        adapted = _adapt_issue(
            {
                "id": "plane-1",
                "external_id": "DORA-CTX-20260501A-T01",
                "description_html": _markdown_to_html(markdown),
            }
        )

        self.assertEqual(adapted["execution_packet_version"], 1)
        self.assertEqual(adapted["execution_packet_hash"], "sha256:valid")
        self.assertEqual(adapted["verification_commands"], payload["verification_commands"])

    def test_query_issues_resolves_label_ids_for_source_context_visibility(self):
        packet_payload = {
            "name": "Source context blocked",
            "body": "# Source context blocked\n",
            "depends_on": [],
            "execution_packet_version": 1,
            "source_docs": [],
            "source_tables": [],
            "source_queries": [],
        }
        api = FakePlaneApi(
            states=[{"id": "s-needs-input", "name": "Needs Input"}],
            labels=[
                {"id": "lbl-source-context-missing", "name": "dora:source-context-missing"},
                {"id": "lbl-source-evidence-missing", "name": "dora:source-evidence-missing"},
            ],
            issues=[
                {
                    "id": "issue-1",
                    "external_id": "DORA-CTX-20260501A-T01",
                    "state": "s-needs-input",
                    "labels": ["lbl-source-context-missing"],
                    "description_html": _markdown_to_html(_issue_markdown("DORA-CTX-20260501A-T01", packet_payload)),
                },
                {
                    "id": "issue-2",
                    "external_id": "DORA-CTX-20260501A-T02",
                    "state": "s-needs-input",
                    "labels": ["lbl-source-evidence-missing"],
                    "description_html": _markdown_to_html(_issue_markdown("DORA-CTX-20260501A-T02", packet_payload)),
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

        issues = client.query_issues("dora")

        visibility = {issue["external_id"]: classify_source_context(issue) for issue in issues}
        self.assertEqual(visibility["DORA-CTX-20260501A-T01"], "source_context_missing")
        self.assertEqual(visibility["DORA-CTX-20260501A-T02"], "source_evidence_missing")
        self.assertEqual(issues[0]["labels"], ["lbl-source-context-missing"])
        self.assertEqual(issues[0]["label_names"], ["dora:source-context-missing"])

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

    def test_next_ready_issue_skips_root_epics_and_orders_generated_tasks_by_sequence(self):
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
        # Generated batch tasks must follow T01..TNN order before priority.
        # Root Epic is excluded entirely.
        self.assertEqual(ready["external_id"], "DORA-AGCORE-20260501C-T01")

    def test_next_ready_issue_skips_orchestrator_invalid_submission_label(self):
        api = FakePlaneApi(
            states=[{"id": "state-todo", "name": "Todo"}],
            labels=[
                {"id": "label-invalid", "name": "dora:orchestrator-invalid-submission"},
            ],
            issues=[
                {
                    "id": "issue-invalid",
                    "external_id": "DORA-AGCORE-20260501C-T01",
                    "state": "state-todo",
                    "priority": "urgent",
                    "labels": ["label-invalid"],
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

        self.assertIsNone(client.next_ready_issue("dora"))

    def test_invalid_submission_does_not_block_next_generated_task(self):
        api = FakePlaneApi(
            states=[{"id": "state-todo", "name": "Todo"}],
            labels=[
                {"id": "label-invalid", "name": "dora:orchestrator-invalid-submission"},
            ],
            issues=[
                {
                    "id": "issue-invalid",
                    "external_id": "DORA-AGCORE-20260501C-T01",
                    "state": "state-todo",
                    "priority": "urgent",
                    "labels": ["label-invalid"],
                },
                {
                    "id": "issue-next",
                    "external_id": "DORA-AGCORE-20260501C-T02",
                    "state": "state-todo",
                    "priority": "urgent",
                    "labels": [],
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
        self.assertEqual(ready["external_id"], "DORA-AGCORE-20260501C-T02")

    def test_next_ready_issue_waits_when_earliest_generated_task_is_in_progress(self):
        api = FakePlaneApi(
            states=[
                {"id": "state-todo", "name": "Todo"},
                {"id": "state-in-progress", "name": "In Progress"},
            ],
            issues=[
                {
                    "id": "issue-running",
                    "external_id": "DORA-AGCORE-20260501C-T01",
                    "state": "state-in-progress",
                    "priority": "low",
                },
                {
                    "id": "issue-ready",
                    "external_id": "DORA-AGCORE-20260501C-T02",
                    "state": "state-todo",
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

        self.assertIsNone(client.next_ready_issue("dora"))

    def test_next_ready_issue_allows_later_dependency_before_blocked_lower_sequence(self):
        api = FakePlaneApi(
            states=[
                {"id": "state-backlog", "name": "Backlog"},
                {"id": "state-todo", "name": "Todo"},
                {"id": "state-done", "name": "Done"},
            ],
            issues=[
                {
                    "id": "issue-done",
                    "external_id": "PFE-P1FE-20260522D-T01",
                    "state": "state-done",
                    "priority": "high",
                },
                {
                    "id": "issue-blocked",
                    "external_id": "PFE-P1FE-20260522D-T03",
                    "state": "state-backlog",
                    "priority": "urgent",
                    "description_html": "<pre>depends_on:\n  - PFE-P1FE-20260522D-T11\n</pre>",
                },
                {
                    "id": "issue-dependency",
                    "external_id": "PFE-P1FE-20260522D-T11",
                    "state": "state-todo",
                    "priority": "medium",
                    "description_html": "<pre>depends_on:\n  - PFE-P1FE-20260522D-T01\n</pre>",
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
        self.assertEqual(ready["external_id"], "PFE-P1FE-20260522D-T11")

    def test_refresh_blocked_preserves_needs_input_state(self):
        api = FakePlaneApi(
            states=[
                {"id": "state-todo", "name": "Todo"},
                {"id": "state-needs-input", "name": "Needs Input"},
            ],
            issues=[
                {
                    "id": "issue-needs-input",
                    "external_id": "DORA-PLN-20260501B-T01",
                    "state": "state-needs-input",
                    "priority": "high",
                    "description_html": "<pre>depends_on: []</pre>",
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

        client._refresh_blocked("dora")

        self.assertNotIn(
            ("PATCH", "/api/v1/workspaces/doraemon/projects/project-1/issues/issue-needs-input/"),
            api.calls,
        )

    def test_release_issue_creates_missing_orchestrator_states_before_patch(self):
        api = FakePlaneApi(
            states=[
                {"id": "state-backlog", "name": "Backlog", "group": "backlog"},
                {"id": "state-todo", "name": "Todo", "group": "unstarted"},
                {"id": "state-in-progress", "name": "In Progress", "group": "started"},
                {"id": "state-done", "name": "Done", "group": "completed"},
                {"id": "state-cancelled", "name": "Cancelled", "group": "cancelled"},
            ],
            issues=[
                {
                    "id": "issue-needs-input",
                    "external_id": "DORA-PLN-20260501B-T01",
                    "state": "state-in-progress",
                    "priority": "high",
                    "description_html": "<pre>depends_on: []</pre>",
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

        client.release_issue("dora", "DORA-PLN-20260501B-T01", "Needs Input")

        created_state_names = {
            payload["name"]
            for method, path, payload in api.payloads
            if method == "POST" and path.endswith("/states/") and payload
        }
        self.assertEqual(created_state_names, {"Blocked", "Partial", "Needs Input"})
        patch_payloads = [
            payload
            for method, path, payload in api.payloads
            if method == "PATCH" and path.endswith("/issues/issue-needs-input/")
        ]
        self.assertEqual(patch_payloads[0]["state"], "state-needs-input")

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
    payloads: list[tuple[str, str, dict | None]] = field(default_factory=list)
    projects: list[dict] = field(default_factory=lambda: [{"id": "project-1", "name": "Dora", "identifier": "DORA"}])
    states: list[dict] = field(default_factory=lambda: [{"id": "state-backlog", "name": "Backlog"}])
    modules: list[dict] = field(default_factory=list)
    cycles: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)
    labels: list[dict] = field(default_factory=list)

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
        if path.endswith("/labels/"):
            return self.labels
        if path.endswith("/issues/"):
            return self.issues
        return []

    def list_pages(self, project_id: str) -> list[dict]:
        return self.pages

    def v1(self, method: str, path: str, payload: dict | None = None, *, ok_statuses=frozenset()):
        self.calls.append((method, path))
        self.payloads.append((method, path, payload))
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
        if method == "POST" and path.endswith("/states/"):
            item = {"id": f"state-{payload['name'].lower().replace(' ', '-')}", **payload}
            self.states.append(item)
            return item
        if method == "POST" and path.endswith("/labels/"):
            item = {"id": f"label-{payload['name']}", **payload}
            self.labels.append(item)
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
