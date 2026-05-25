import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.project_resolver import (
    BatchSummary,
    ResolvedConfig,
    list_available_batches,
    resolve_project_config,
    write_project_registry,
)


class ResolveProjectConfigTest(unittest.TestCase):
    def test_cli_args_take_priority(self):
        result = resolve_project_config(
            repo="/my/repo",
            project_slug="my-slug",
            project_title="My Title",
        )
        self.assertEqual(result.repo_root, Path("/my/repo").resolve())
        self.assertEqual(result.project_slug, "my-slug")
        self.assertEqual(result.project_title, "My Title")
        self.assertIsNone(result.batch_path)

    def test_batch_id_resolved_to_path(self):
        result = resolve_project_config(
            repo="/my/repo",
            batch_id="20260501A",
        )
        self.assertEqual(
            result.batch_path,
            Path("/my/repo/docs/dora/batches/20260501A").resolve(),
        )

    def test_no_batch_id_returns_none_path(self):
        result = resolve_project_config(repo="/my/repo")
        self.assertIsNone(result.batch_path)

    def test_project_registry_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg_dir = Path(tmp)
            write_project_registry(
                "demo",
                "Demo Project",
                Path("/my/repo"),
                registry_dir=reg_dir,
            )
            result = resolve_project_config(
                project="demo",
                registry_dir=reg_dir,
            )
            self.assertEqual(result.repo_root, Path("/my/repo").resolve())
            self.assertEqual(result.project_slug, "demo")
            self.assertEqual(result.project_title, "Demo Project")

    def test_project_registry_does_not_override_explicit_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg_dir = Path(tmp)
            write_project_registry(
                "demo",
                "Demo Project",
                Path("/my/repo"),
                registry_dir=reg_dir,
            )
            result = resolve_project_config(
                repo="/explicit/repo",
                project_slug="explicit-slug",
                project_title="Explicit Title",
                project="demo",
                registry_dir=reg_dir,
            )
            self.assertEqual(result.repo_root, Path("/explicit/repo").resolve())
            self.assertEqual(result.project_slug, "explicit-slug")
            self.assertEqual(result.project_title, "Explicit Title")

    def test_repo_argument_reads_repos_dora_project_json_not_cwd(self):
        """When --repo is explicit, project_slug must come from
        <repo>/.dora/project.json — NOT from .dora/project.json reachable
        by walking up from cwd. Pre-fix, running `status --repo /path/to/X`
        from inside /path/to/Y silently picked up Y's slug."""
        with tempfile.TemporaryDirectory() as tmp_repo, tempfile.TemporaryDirectory() as tmp_cwd:
            target_repo = Path(tmp_repo)
            (target_repo / ".dora").mkdir()
            (target_repo / ".dora" / "project.json").write_text(
                json.dumps({"project_slug": "target-slug", "title": "Target Title"}),
                encoding="utf-8",
            )

            # cwd has its own .dora/project.json that should be ignored
            cwd_repo = Path(tmp_cwd)
            (cwd_repo / ".dora").mkdir()
            (cwd_repo / ".dora" / "project.json").write_text(
                json.dumps({"project_slug": "cwd-slug", "title": "Cwd Title"}),
                encoding="utf-8",
            )

            import os
            old_cwd = os.getcwd()
            try:
                os.chdir(str(cwd_repo))
                result = resolve_project_config(repo=str(target_repo))
                self.assertEqual(result.project_slug, "target-slug")
                self.assertEqual(result.project_title, "Target Title")
                self.assertEqual(result.repo_root, target_repo.resolve())
            finally:
                os.chdir(old_cwd)

    def test_dora_project_json_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            dora_dir = repo / ".dora"
            dora_dir.mkdir()
            (dora_dir / "project.json").write_text(
                json.dumps(
                    {"project_slug": "discovered", "title": "Discovered Project"}
                ),
                encoding="utf-8",
            )

            import os
            old_cwd = os.getcwd()
            try:
                os.chdir(str(repo))
                result = resolve_project_config()
                self.assertEqual(result.repo_root, repo.resolve())
                self.assertEqual(result.project_slug, "discovered")
                self.assertEqual(result.project_title, "Discovered Project")
            finally:
                os.chdir(old_cwd)

    def test_dora_project_json_discovery_includes_plane_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            dora_dir = repo / ".dora"
            dora_dir.mkdir()
            (dora_dir / "project.json").write_text(
                json.dumps(
                    {
                        "project_slug": "tozoa",
                        "title": "Tozoa",
                        "plane_project_id": "tozoa-plane-id",
                        "plane_workspace_slug": "doraemon",
                    }
                ),
                encoding="utf-8",
            )

            import os
            old_cwd = os.getcwd()
            try:
                os.chdir(str(repo))
                result = resolve_project_config()
                self.assertEqual(result.project_slug, "tozoa")
                self.assertEqual(result.project_title, "Tozoa")
                self.assertEqual(result.plane_project_id, "tozoa-plane-id")
                self.assertEqual(result.plane_workspace_slug, "doraemon")
            finally:
                os.chdir(old_cwd)


class ListAvailableBatchesTest(unittest.TestCase):
    def test_no_batches_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            batches = list_available_batches(Path(tmp))
            self.assertEqual(batches, [])

    def test_lists_batches_with_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = repo / "docs" / "dora" / "batches" / "20260501A"
            batch_dir.mkdir(parents=True)
            tasks_dir = batch_dir / "tasks"
            tasks_dir.mkdir()
            for i in range(3):
                (tasks_dir / f"task_{i:03d}.md").write_text("---\ntask_id: T-1\n---\nbody\n")
            (batch_dir / "batch.md").write_text(
                "---\nbatch_id: 20260501A\ntitle: Initial Scaffold\n---\n",
                encoding="utf-8",
            )

            batches = list_available_batches(repo)
            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0].batch_id, "20260501A")
            self.assertEqual(batches[0].title, "Initial Scaffold")
            self.assertEqual(batches[0].task_count, 3)

    def test_skips_non_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batches_root = repo / "docs" / "dora" / "batches"
            batches_root.mkdir(parents=True)
            (batches_root / "readme.md").write_text("not a batch\n")

            batches = list_available_batches(repo)
            self.assertEqual(batches, [])

    def test_skips_batch_without_batch_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = repo / "docs" / "dora" / "batches" / "no-md"
            batch_dir.mkdir(parents=True)

            batches = list_available_batches(repo)
            self.assertEqual(batches, [])


class WriteProjectRegistryTest(unittest.TestCase):
    def test_writes_registry_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg_dir = Path(tmp)
            entry = write_project_registry(
                "my-project",
                "My Project",
                Path("/home/user/repo"),
                registry_dir=reg_dir,
            )
            self.assertTrue(entry.exists())
            data = json.loads(entry.read_text(encoding="utf-8"))
            self.assertEqual(data["slug"], "my-project")
            self.assertEqual(data["title"], "My Project")
            self.assertIn("repo_root", data)
            self.assertEqual(data["default_executor"], "codex")

    def test_registry_entry_is_valid_project_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg_dir = Path(tmp)
            entry = write_project_registry(
                "demo", "Demo", Path("/tmp/repo"), registry_dir=reg_dir
            )
            data = json.loads(entry.read_text(encoding="utf-8"))
            self.assertIn("slug", data)
            self.assertIn("title", data)
            self.assertIn("repo_root", data)
            self.assertIn("schedule_cron", data)
            self.assertIn("enable_push", data)
