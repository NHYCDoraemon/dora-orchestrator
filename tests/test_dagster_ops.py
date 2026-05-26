import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.dagster_defs.ops import _executor_env_for, _format_progress_log
from orchestrator.dagster_defs.project_config import ProjectConfig


class DagsterOpsProgressLogTest(unittest.TestCase):
    def test_executor_output_logs_summary_only(self):
        self.assertEqual(
            _format_progress_log(
                "executor_output",
                {
                    "external_id": "PE-BREVIEW-20260519A-T11",
                    "summary": "command rc=0  |  2L  |  done",
                },
            ),
            "command rc=0  |  2L  |  done",
        )

    def test_non_executor_output_keeps_structured_event_json(self):
        line = _format_progress_log(
            "executor_started",
            {"external_id": "PE-BREVIEW-20260519A-T11", "agent": "codex"},
        )

        self.assertEqual(
            json.loads(line),
            {
                "event": "executor_started",
                "external_id": "PE-BREVIEW-20260519A-T11",
                "agent": "codex",
            },
        )


class DagsterOpsExecutorEnvTest(unittest.TestCase):
    def test_claude_executor_receives_java17_environment(self):
        cfg = ProjectConfig(
            slug="process-frontend",
            title="ProcessEngineFrontend",
            repo_root=Path("/tmp/process-frontend"),
            default_executor="claude",
            codex_home=Path("/tmp/codex-home"),
        )

        with patch.dict(os.environ, {"ORCHESTRATOR_JAVA_HOME": "/tmp/jdk-17", "PATH": "/usr/bin"}, clear=False):
            env = _executor_env_for("claude", cfg)

        self.assertEqual(env["JAVA_HOME"], "/tmp/jdk-17")
        self.assertEqual(env["PATH"], "/tmp/jdk-17/bin:/usr/bin")
        self.assertNotIn("CODEX_HOME", env)

    def test_codex_executor_receives_codex_home_and_java17_environment(self):
        cfg = ProjectConfig(
            slug="process-frontend",
            title="ProcessEngineFrontend",
            repo_root=Path("/tmp/process-frontend"),
            default_executor="codex",
            codex_home=Path("/tmp/codex-home"),
        )

        with patch.dict(os.environ, {"ORCHESTRATOR_JAVA_HOME": "/tmp/jdk-17", "PATH": "/usr/bin"}, clear=False):
            env = _executor_env_for("codex", cfg)

        self.assertEqual(env["CODEX_HOME"], "/tmp/codex-home")
        self.assertEqual(env["JAVA_HOME"], "/tmp/jdk-17")
        self.assertEqual(env["PATH"], "/tmp/jdk-17/bin:/usr/bin")
