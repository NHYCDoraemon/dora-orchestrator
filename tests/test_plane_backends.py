import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import OrchestratorConfig
from orchestrator.plane_backends import create_plane_client
from orchestrator.local_plane import LocalPlaneClient
from orchestrator.plane_live import LivePlaneClient
from orchestrator.project_resolver import resolve_project_config


class PlaneBackendTest(unittest.TestCase):
    def test_config_creates_memory_backend_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "spec.json",
                target_repo=Path(tmp),
                executor="noop",
            )
            client = create_plane_client(config)

            self.assertEqual(client.__class__.__name__, "InMemoryPlaneClient")

    def test_config_creates_local_backend_with_persistent_state_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "spec.json",
                target_repo=Path(tmp),
                executor="noop",
                plane_backend="local",
            )
            client = create_plane_client(config)

            self.assertIsInstance(client, LocalPlaneClient)
            self.assertEqual(client.state_path, Path(tmp) / ".dora" / "orchestrator-plane-state.json")

    def test_config_creates_live_backend_from_plane_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "spec.json",
                target_repo=Path(tmp),
                executor="noop",
                plane_backend="live",
            )
            with patch.dict(
                "os.environ",
                {
                    "PLANE_BASE_URL": "https://plane.example",
                    "PLANE_WORKSPACE_SLUG": "doraemon",
                    "PLANE_PROJECT_ID": "project-1",
                    "PLANE_API_KEY": "token",
                },
                clear=False,
            ):
                client = create_plane_client(config)

            self.assertIsInstance(client, LivePlaneClient)
            self.assertEqual(client.settings.project_id, "project-1")

    def test_live_backend_prefers_project_registry_plane_id_over_env_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            registry = Path(tmp) / "registry"
            registry.mkdir()
            (registry / "process-engine.json").write_text(
                """
{
  "slug": "process-engine",
  "title": "Process Engine",
  "repo_root": "%s",
  "plane_project_id": "process-plane-id",
  "plane_workspace_slug": "doraemon"
}
"""
                % repo,
                encoding="utf-8",
            )
            resolved = resolve_project_config(
                project="process-engine",
                registry_dir=registry,
            )
            config = OrchestratorConfig(
                spec_path=repo / "spec.json",
                target_repo=repo,
                executor="noop",
                plane_backend="live",
            )

            with patch.dict(
                "os.environ",
                {
                    "PLANE_BASE_URL": "https://plane.example",
                    "PLANE_WORKSPACE_SLUG": "doraemon",
                    "PLANE_PROJECT_ID": "dora-plane-id",
                    "PLANE_API_KEY": "token",
                },
                clear=False,
            ):
                client = create_plane_client(config, resolved_project=resolved)

            self.assertIsInstance(client, LivePlaneClient)
            self.assertEqual(client.settings.project_id, "process-plane-id")
            self.assertEqual(client.settings.workspace_slug, "doraemon")

    def test_local_backend_persists_issue_state_between_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "plane-state.json"
            client = LocalPlaneClient(state_path)
            client.upsert_project("demo", "Demo")
            client.upsert_cycle("demo", "S1")
            client.upsert_module("demo", "core")
            client.upsert_page("demo", "program-demo", {"title": "Program", "body": "# Program"})
            client.upsert_issue(
                "demo",
                "DEMO-1",
                {
                    "name": "Task 1",
                    "cycle": "S1",
                    "module": "core",
                    "priority": "P1",
                    "depends_on": [],
                    "source_hash": "abc",
                    "agent_hint": "noop",
                    "risk": "low",
                    "acceptance": [],
                    "verification_level": ["L1"],
                },
            )
            client.claim_issue("demo", "DEMO-1", "run-1")
            client.release_issue("demo", "DEMO-1", "Done")

            reloaded = LocalPlaneClient(state_path)

            self.assertEqual(reloaded.issues[("demo", "DEMO-1")]["state"], "Done")
            self.assertEqual(reloaded.pages[("demo", "program-demo")]["title"], "Program")
            self.assertIsNone(reloaded.next_ready_issue("demo"))

    def test_root_epic_state_rolls_up_from_children(self):
        from orchestrator.in_memory_plane import InMemoryPlaneClient

        client = InMemoryPlaneClient()
        client.upsert_project("demo", "Demo")
        # ROOT first, then 3 children parented to it.
        client.upsert_issue("demo", "DEMO-ROOT", {"name": "Root", "issue_type": "root_epic"})
        for ext in ("DEMO-T01", "DEMO-T02", "DEMO-T03"):
            client.upsert_issue(
                "demo",
                ext,
                {
                    "name": ext,
                    "issue_type": "task",
                    "parent_external_id": "DEMO-ROOT",
                    "depends_on": [],
                },
            )

        # All children Backlog/Todo → ROOT stays Backlog (not started).
        client.release_issue("demo", "DEMO-T01", "Todo")
        self.assertEqual(client.issues[("demo", "DEMO-ROOT")]["state"], "Backlog")

        # First child claimed → ROOT becomes In Progress.
        client.claim_issue("demo", "DEMO-T01", "run-1")
        self.assertEqual(client.issues[("demo", "DEMO-ROOT")]["state"], "In Progress")

        # First child Done, others not yet → ROOT stays In Progress.
        client.release_issue("demo", "DEMO-T01", "Done")
        self.assertEqual(client.issues[("demo", "DEMO-ROOT")]["state"], "In Progress")

        # All children Done → ROOT becomes Done.
        client.release_issue("demo", "DEMO-T02", "Done")
        client.release_issue("demo", "DEMO-T03", "Done")
        self.assertEqual(client.issues[("demo", "DEMO-ROOT")]["state"], "Done")

    def test_state_counts_groups_in_memory_issues_by_state(self):
        from orchestrator.in_memory_plane import InMemoryPlaneClient

        client = InMemoryPlaneClient()
        client.upsert_project("demo", "Demo")
        for ext in ("DEMO-T01", "DEMO-T02", "DEMO-T03"):
            client.upsert_issue("demo", ext, {"name": ext, "depends_on": []})
        client.claim_issue("demo", "DEMO-T01", "run-1")
        client.release_issue("demo", "DEMO-T02", "Done")
        client.upsert_issue("demo", "OTHER-T01", {"name": "OTHER-T01", "depends_on": []})
        client.upsert_project("other", "Other")
        client.upsert_issue("other", "OTHER-T01", {"name": "Other project", "depends_on": []})

        counts = client.state_counts("demo")

        self.assertEqual(counts.get("In Progress"), 1)
        self.assertEqual(counts.get("Done"), 1)
        self.assertEqual(sum(counts.values()), 4)
        self.assertNotIn("Other project", counts)

    def test_local_backend_keeps_done_issue_done_after_upsert(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "plane-state.json"
            client = LocalPlaneClient(state_path)
            payload = {
                "name": "Task 1",
                "cycle": "S1",
                "module": "core",
                "priority": "P1",
                "depends_on": [],
                "source_hash": "abc",
                "agent_hint": "noop",
                "risk": "low",
                "acceptance": [],
                "verification_level": ["L1"],
            }
            client.upsert_issue("demo", "DEMO-1", payload)
            client.release_issue("demo", "DEMO-1", "Done")
            client.upsert_issue("demo", "DEMO-1", {**payload, "name": "Task 1 updated"})

            reloaded = LocalPlaneClient(state_path)

            self.assertEqual(reloaded.issues[("demo", "DEMO-1")]["name"], "Task 1 updated")
            self.assertEqual(reloaded.issues[("demo", "DEMO-1")]["state"], "Done")
