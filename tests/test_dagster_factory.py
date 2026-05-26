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
        self.assertEqual(cfg.default_executor, "claude")
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
                        "codex_home": "/tmp/codex-home",
                        "enable_push": True,
                        "enable_pr": True,
                        "schedule_enabled": True,
                        "max_runtime_seconds": 7200,
                    }
                ),
                encoding="utf-8",
            )

            configs = load_project_configs(d)

        self.assertEqual(configs[0].schedule_cron, "*/15 * * * *")
        self.assertEqual(configs[0].default_executor, "claude")
        self.assertEqual(configs[0].codex_home, Path("/tmp/codex-home"))
        self.assertTrue(configs[0].enable_push)
        self.assertTrue(configs[0].enable_pr)
        self.assertTrue(configs[0].schedule_enabled)
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

    def test_build_project_defs_emits_named_job_and_assets_without_disabled_schedule(self):
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
        self.assertEqual(schedule_names, set())

        asset_keys = {
            ak.to_user_string()
            for ak in defs.resolve_asset_graph().get_all_asset_keys()
        }
        self.assertIn("example_status", asset_keys)
        self.assertIn("example_reset_lease", asset_keys)
        self.assertIn("example_worktrees", asset_keys)

    def test_build_project_defs_can_enable_schedule_by_config(self):
        from dagster import DefaultScheduleStatus
        from orchestrator.dagster_defs import ProjectConfig, build_project_defs

        defs = build_project_defs(
            ProjectConfig(
                slug="frontend",
                title="Frontend",
                repo_root=Path("/tmp/frontend"),
                schedule_enabled=True,
            )
        )

        schedule_defs = defs.get_repository_def().schedule_defs
        self.assertEqual(len(schedule_defs), 1)
        self.assertEqual(schedule_defs[0].default_status, DefaultScheduleStatus.RUNNING)

    def test_run_job_uses_event_log_only_logger(self):
        from orchestrator.dagster_defs import ProjectConfig, build_project_defs

        defs = build_project_defs(
            ProjectConfig(
                slug="quiet",
                title="Quiet",
                repo_root=Path("/tmp/quiet"),
            )
        )

        job = defs.get_repository_def().get_job("quiet_run_ready_batch_task")

        self.assertEqual(set(job.loggers.keys()), {"event_log_only"})

    def test_run_orchestrator_op_has_no_dagster_retry_policy(self):
        from orchestrator.dagster_defs import ProjectConfig, build_project_defs

        defs = build_project_defs(
            ProjectConfig(
                slug="noretry",
                title="No Retry",
                repo_root=Path("/tmp/noretry"),
            )
        )

        job = defs.get_repository_def().get_job("noretry_run_ready_batch_task")
        node = job.graph.node_named("noretry_run_orchestrator")

        self.assertIsNone(node.retry_policy)

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


class ProbeNextReadyTest(unittest.TestCase):
    """The schedule probe must skip empty Plane queues silently and never
    raise on transient Plane outages — both are conditions that should
    NOT spawn an empty Dagster run."""

    def setUp(self):
        if importlib.util.find_spec("dagster") is None:
            self.skipTest("dagster not installed")

    def _cfg(self):
        from orchestrator.dagster_defs import ProjectConfig
        return ProjectConfig(slug="probe_test", title="Probe", repo_root=Path("/tmp/probe"))

    def test_probe_returns_id_when_plane_has_ready(self):
        from unittest.mock import patch
        from orchestrator.dagster_defs import factory

        fake = type("F", (), {"next_ready_issue": lambda self, slug: {"external_id": "DOR-X-20260101A-T01"}})()
        with patch("orchestrator.dagster_defs.plane_helpers.per_project_plane_client", return_value=fake):
            self.assertEqual(factory._probe_next_ready(self._cfg()), "DOR-X-20260101A-T01")

    def test_probe_returns_none_when_plane_empty(self):
        from unittest.mock import patch
        from orchestrator.dagster_defs import factory

        fake = type("F", (), {"next_ready_issue": lambda self, slug: None})()
        with patch("orchestrator.dagster_defs.plane_helpers.per_project_plane_client", return_value=fake):
            self.assertIsNone(factory._probe_next_ready(self._cfg()))

    def test_probe_returns_failed_sentinel_on_exception(self):
        import sys, types as _types
        from orchestrator.dagster_defs import factory

        def boom(settings):
            raise RuntimeError("plane down")

        fake_module = _types.SimpleNamespace(
            LivePlaneClient=boom,
            LivePlaneSettings=_types.SimpleNamespace(from_env=lambda: None),
        )
        stub = sys.modules.get("orchestrator.plane_live")
        sys.modules["orchestrator.plane_live"] = fake_module
        try:
            self.assertEqual(factory._probe_next_ready(self._cfg()), "_probe_failed")
        finally:
            if stub is not None:
                sys.modules["orchestrator.plane_live"] = stub
            else:
                sys.modules.pop("orchestrator.plane_live", None)


if __name__ == "__main__":
    unittest.main()
