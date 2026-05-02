"""Tests for the per-project Dagster Definitions factory.

The factory itself imports `dagster`; everything that doesn't (config loading,
project_id derivation, batch_id parsing) is covered separately so it runs even
in environments without the dagster extra installed.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


class LoaderTest(unittest.TestCase):
    """Loader tests don't need dagster — they only touch JSON parsing."""

    def test_load_skips_missing_dir(self):
        from orchestrator.dagster_defs.loader import load_project_configs

        configs = load_project_configs(Path("/definitely/does/not/exist"))
        self.assertEqual(configs, [])

    def test_load_parses_minimal_config(self):
        from orchestrator.dagster_defs.loader import load_project_configs

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "dora.json").write_text(
                json.dumps(
                    {
                        "slug": "dora",
                        "title": "Dora",
                        "repo_root": "/tmp/repo",
                    }
                ),
                encoding="utf-8",
            )

            configs = load_project_configs(d)

        self.assertEqual(len(configs), 1)
        cfg = configs[0]
        self.assertEqual(cfg.slug, "dora")
        self.assertEqual(cfg.title, "Dora")
        self.assertEqual(cfg.repo_root, Path("/tmp/repo"))
        self.assertEqual(cfg.default_executor, "noop")
        self.assertEqual(cfg.schedule_cron, "*/2 * * * *")
        self.assertFalse(cfg.enable_push)
        self.assertFalse(cfg.enable_pr)

    def test_load_honors_overrides(self):
        from orchestrator.dagster_defs.loader import load_project_configs

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "x.json").write_text(
                json.dumps(
                    {
                        "slug": "foo",
                        "title": "Foo",
                        "repo_root": "/tmp/foo",
                        "schedule_cron": "*/15 * * * *",
                        "default_executor": "claude",
                        "enable_push": True,
                        "enable_pr": True,
                        "max_runtime_seconds": 7200,
                    }
                ),
                encoding="utf-8",
            )

            configs = load_project_configs(d)

        self.assertEqual(configs[0].schedule_cron, "*/15 * * * *")
        self.assertEqual(configs[0].default_executor, "claude")
        self.assertTrue(configs[0].enable_push)
        self.assertTrue(configs[0].enable_pr)
        self.assertEqual(configs[0].max_runtime_seconds, 7200)

    def test_load_skips_invalid_files_without_failing_others(self):
        from orchestrator.dagster_defs.loader import load_project_configs

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "bad.json").write_text("not valid json", encoding="utf-8")
            (d / "missing-fields.json").write_text(json.dumps({"slug": "x"}), encoding="utf-8")
            (d / "good.json").write_text(
                json.dumps({"slug": "good", "title": "Good", "repo_root": "/tmp/r"}),
                encoding="utf-8",
            )

            configs = load_project_configs(d)

        self.assertEqual([c.slug for c in configs], ["good"])


class FactoryDefinitionsTest(unittest.TestCase):
    """Factory tests need dagster installed."""

    def setUp(self):
        if importlib.util.find_spec("dagster") is None:
            self.skipTest("dagster not installed")

    def test_build_project_defs_emits_named_job_schedule_and_assets(self):
        from orchestrator.dagster_defs import ProjectConfig, build_project_defs

        defs = build_project_defs(
            ProjectConfig(
                slug="example",
                title="Example",
                repo_root=Path("/tmp/example"),
            )
        )

        job_names = {j.name for j in defs.get_repository_def().get_all_jobs()}
        self.assertIn("example_run_ready_batch_task", job_names)

        schedule_names = {
            s.name for s in defs.get_repository_def().schedule_defs
        }
        self.assertEqual(len(schedule_names), 1)
        # Cron-derived label
        for sn in schedule_names:
            self.assertTrue(sn.startswith("example_every_"))

        asset_keys = {
            ak.to_user_string()
            for ak in defs.resolve_asset_graph().get_all_asset_keys()
        }
        self.assertIn("example_status", asset_keys)
        self.assertIn("example_reset_lease", asset_keys)
        self.assertIn("example_worktrees", asset_keys)

    def test_build_orchestrated_projects_defs_merges_multiple_projects(self):
        from orchestrator.dagster_defs import build_orchestrated_projects_defs

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "dora.json").write_text(
                json.dumps({"slug": "dora", "title": "Dora", "repo_root": "/tmp/dora"}),
                encoding="utf-8",
            )
            (d / "foo.json").write_text(
                json.dumps({"slug": "foo", "title": "Foo", "repo_root": "/tmp/foo"}),
                encoding="utf-8",
            )

            defs = build_orchestrated_projects_defs(d)

        job_names = {j.name for j in defs.get_repository_def().get_all_jobs()}
        self.assertIn("dora_run_ready_batch_task", job_names)
        self.assertIn("foo_run_ready_batch_task", job_names)


class HelpersTest(unittest.TestCase):
    """Tests for utility helpers in ops.py that don't need dagster."""

    def test_extract_batch_id_strips_program_and_task_segments(self):
        from orchestrator.run_ready_task import _extract_batch_id

        self.assertEqual(_extract_batch_id("DORA-AGCORE-20260501C-T01"), "20260501C")
        self.assertEqual(_extract_batch_id("DORA-MATH-20260501D-T16"), "20260501D")
        self.assertEqual(_extract_batch_id(""), "")
        # Defensive default: any 3rd dash-segment is returned even if it doesn't
        # look like a real batch id.
        self.assertEqual(_extract_batch_id("a-b-c-d"), "c")


if __name__ == "__main__":
    unittest.main()
