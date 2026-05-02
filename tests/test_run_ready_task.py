import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.in_memory_plane import InMemoryPlaneClient
from orchestrator.run_ready_task import run_ready_task_from_paths


class RunReadyTaskTest(unittest.TestCase):
    def test_noop_run_claims_executes_reports_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = root / "dora.orchestration.json"
            target_repo = root / "repo"
            target_repo.mkdir()
            spec_path.write_text(
                json.dumps(
                    {
                        "project_slug": "demo-project",
                        "title": "Demo Project",
                        "tasks": [
                            {
                                "external_id": "DEMO-1",
                                "title": "Run noop task",
                                "cycle": "S1",
                                "module": "demo",
                                "priority": "P1",
                                "depends_on": [],
                                "acceptance": ["noop acceptance"],
                                "verification_level": ["L1"],
                                "risk": "low",
                                "agent_hint": "noop",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            plane = InMemoryPlaneClient()

            result = run_ready_task_from_paths(
                spec_path,
                target_repo,
                plane_client=plane,
                run_id="run-123",
                executor="noop",
            )

            self.assertEqual(result["outcome"], "agent_done")
            self.assertEqual(result["state"], "Done")
            issue = plane.issues[("demo-project", "DEMO-1")]
            self.assertEqual(issue["state"], "Done")
            self.assertIsNone(issue["assignee"])
            self.assertEqual(len(plane.reports), 1)
            event_path = Path(result["event_path"])
            self.assertTrue(event_path.exists())
            self.assertIn("executor.completed", event_path.read_text(encoding="utf-8"))

    def test_returns_no_ready_when_dependencies_are_not_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = root / "dora.orchestration.json"
            target_repo = root / "repo"
            target_repo.mkdir()
            spec_path.write_text(
                json.dumps(
                    {
                        "project_slug": "demo-project",
                        "title": "Demo Project",
                        "tasks": [
                            {
                                "external_id": "DEMO-1",
                                "title": "Blocked task",
                                "cycle": "S1",
                                "module": "demo",
                                "priority": "P1",
                                "depends_on": ["DEMO-0"],
                                "acceptance": [],
                                "verification_level": ["L1"],
                                "risk": "low",
                                "agent_hint": "noop",
                            },
                            {
                                "external_id": "DEMO-0",
                                "title": "Dependency",
                                "cycle": "S1",
                                "module": "demo",
                                "priority": "P1",
                                "depends_on": [],
                                "acceptance": [],
                                "verification_level": ["L1"],
                                "risk": "low",
                                "agent_hint": "noop",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            plane = InMemoryPlaneClient()

            result = run_ready_task_from_paths(
                spec_path,
                target_repo,
                plane_client=plane,
                run_id="run-123",
                executor="noop",
            )

            self.assertEqual(result["outcome"], "agent_done")
            self.assertEqual(plane.issues[("demo-project", "DEMO-0")]["state"], "Done")
            self.assertEqual(plane.issues[("demo-project", "DEMO-1")]["state"], "Backlog")
