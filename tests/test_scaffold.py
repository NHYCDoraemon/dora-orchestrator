import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.project_resolver import write_project_registry
from orchestrator.scaffold import STANDARD_MODULES, scaffold_project


class ScaffoldTest(unittest.TestCase):
    def test_scaffold_creates_standard_project_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            result = scaffold_project(repo, project_slug="demo-project", title="Demo Project")

            root = repo.resolve()
            self.assertIn(root / ".dora" / "project.json", result.created)
            self.assertIn(root / "docs" / "dora" / "index.md", result.created)
            self.assertIn(root / "docs" / "dora" / "modules.md", result.created)
            self.assertIn(root / "docs" / "dora" / "product" / "vision.md", result.created)
            self.assertIn(root / "docs" / "dora" / "architecture" / "overview.md", result.created)
            self.assertIn(root / "docs" / "dora" / "planning" / "implementation.md", result.created)
            self.assertIn(root / "docs" / "dora" / "quality" / "risk-register.md", result.created)

            project = json.loads((repo / ".dora" / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(project["project_slug"], "demo-project")
            self.assertEqual(project["title"], "Demo Project")
            self.assertEqual(project["module_taxonomy"], [module["id"] for module in STANDARD_MODULES])

    def test_scaffold_does_not_overwrite_existing_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            existing = repo / "docs" / "dora" / "product" / "vision.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("existing vision\n", encoding="utf-8")

            result = scaffold_project(repo, project_slug="demo-project", title="Demo Project")

            self.assertIn(existing.resolve(), result.skipped)
            self.assertEqual(existing.read_text(encoding="utf-8"), "existing vision\n")

    def test_standard_modules_are_fixed_cross_project_taxonomy(self):
        module_ids = [module["id"] for module in STANDARD_MODULES]

        self.assertEqual(
            module_ids,
            [
                "product",
                "architecture",
                "planning",
                "implementation",
                "verification",
                "operations",
                "governance",
            ],
        )

    def test_scaffold_writes_project_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            reg_dir = Path(tmp) / "registry"
            write_project_registry(
                "demo-project",
                "Demo Project",
                repo.resolve(),
                registry_dir=reg_dir,
            )
            entry_path = reg_dir / "demo-project.json"
            self.assertTrue(entry_path.exists())
            data = json.loads(entry_path.read_text(encoding="utf-8"))
            self.assertEqual(data["slug"], "demo-project")
            self.assertEqual(data["title"], "Demo Project")
            self.assertEqual(data["repo_root"], str(repo.resolve()))
