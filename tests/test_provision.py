import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import OrchestratorConfig
from orchestrator.provision import provision_project_from_config


class ProvisionTest(unittest.TestCase):
    def test_provision_project_from_config_updates_local_plane_state_without_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = root / "spec.json"
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
                                "title": "First task",
                                "cycle": "S1",
                                "module": "planning",
                                "priority": "P1",
                                "depends_on": [],
                                "acceptance": ["first acceptance"],
                                "verification_level": ["L1"],
                                "risk": "low",
                                "agent_hint": "noop",
                            },
                            {
                                "external_id": "DEMO-2",
                                "title": "Second task",
                                "cycle": "S1",
                                "module": "implementation",
                                "priority": "P2",
                                "depends_on": ["DEMO-1"],
                                "acceptance": ["second acceptance"],
                                "verification_level": ["L1", "L2"],
                                "risk": "medium",
                                "agent_hint": "codex",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = OrchestratorConfig(
                spec_path=spec_path,
                target_repo=target_repo,
                executor="noop",
                plane_backend="local",
            )

            result = provision_project_from_config(config)

            self.assertEqual(result, {"projects": 1, "cycles": 1, "modules": 2, "issues": 2})
            state_path = target_repo / ".dora" / "orchestrator-plane-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(state["reports"]), 0)
            self.assertEqual(len(state["issues"]), 2)
            self.assertEqual(
                state["issues"][0]["value"]["state"],
                "Backlog",
            )

