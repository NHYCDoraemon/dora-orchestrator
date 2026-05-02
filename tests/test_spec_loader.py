import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.spec_loader import load_project_spec
from orchestrator.task_graph import build_task_graph


class SpecLoaderTest(unittest.TestCase):
    def test_loads_example_spec(self):
        spec = load_project_spec(Path("examples/dora.orchestration.json"))
        self.assertEqual(spec.project_slug, "dora-context-assembly")
        self.assertEqual(len(spec.tasks), 1)
        self.assertEqual(spec.tasks[0].project_slug, spec.project_slug)
        graph = build_task_graph(spec)
        self.assertEqual(len(graph.tasks[0].source_hash), 64)

    def test_rejects_mismatched_task_project_slug(self):
        body = """{
          "project_slug": "dora-context",
          "title": "Dora Context",
          "tasks": [{
            "external_id": "S1.5-P1-01",
            "title": "Bad project",
            "project_slug": "other",
            "cycle": "S1.5",
            "module": "context",
            "priority": "P1"
          }]
        }"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dora.orchestration.json"
            path.write_text(body, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match project"):
                load_project_spec(path)

    def test_loads_root_modules_for_plane_provisioning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dora.orchestration.json"
            path.write_text(
                json.dumps(
                    {
                        "project_slug": "demo-project",
                        "title": "Demo Project",
                        "modules": ["product", "architecture"],
                        "tasks": [],
                    }
                ),
                encoding="utf-8",
            )

            spec = load_project_spec(path)

            self.assertEqual(spec.modules, ["product", "architecture"])
