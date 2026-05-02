import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import OrchestratorConfig
from orchestrator.plane_backends import create_plane_client
from orchestrator.local_plane import LocalPlaneClient
from orchestrator.plane_live import LivePlaneClient


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
