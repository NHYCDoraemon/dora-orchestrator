import unittest
from pathlib import Path

from orchestrator.config import load_config


class ConfigTest(unittest.TestCase):
    def test_loads_env_paths_and_executor(self):
        config = load_config(
            {
                "ORCHESTRATOR_SPEC": "/tmp/spec.json",
                "DORA_TARGET_REPO": "/tmp/repo",
                "ORCHESTRATOR_EXECUTOR": "noop",
                "ORCHESTRATOR_PLANE_BACKEND": "local",
                "ORCHESTRATOR_BATCH_PATH": "/tmp/repo/docs/dora/batches/20260501A",
                "ORCHESTRATOR_PROJECT_SLUG": "dora",
                "ORCHESTRATOR_PROJECT_TITLE": "Dora",
            }
        )

        self.assertEqual(config.spec_path, Path("/tmp/spec.json").resolve())
        self.assertEqual(config.target_repo, Path("/tmp/repo").resolve())
        self.assertEqual(config.executor, "noop")
        self.assertEqual(config.plane_backend, "local")
        self.assertEqual(config.batch_path, Path("/tmp/repo/docs/dora/batches/20260501A").resolve())
        self.assertEqual(config.project_slug, "dora")
        self.assertEqual(config.project_title, "Dora")
