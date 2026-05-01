import json
import tempfile
import unittest
from pathlib import Path

from dora_orchestrator.executor_protocol import TaskRunContext
from dora_orchestrator.executors import get_executor
from dora_orchestrator.executors.claude import ClaudeExecutor
from dora_orchestrator.executors.codex import CodexExecutor
from dora_orchestrator.executors.noop import NoopExecutor
from dora_orchestrator.local_artifacts import create_run_artifacts


def context(repo_root: Path) -> TaskRunContext:
    artifacts = create_run_artifacts(repo_root, "run-1")
    artifacts.prompt_path.write_text("do the smallest useful thing", encoding="utf-8")
    return TaskRunContext(
        run_id="run-1",
        issue_key="DOR-217",
        external_id="S1.5-P1-01",
        project_slug="dora-context",
        repo_root=repo_root,
        branch="dora-agent/noop/DOR-217",
        agent="noop",
        prompt_path=artifacts.prompt_path,
        event_path=artifacts.event_path,
        verification_level=["L1"],
    )


class ExecutorNoopTest(unittest.TestCase):
    def test_noop_executor_writes_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp))
            result = NoopExecutor().run(ctx)

            self.assertEqual(result.outcome, "agent_done")
            lines = ctx.event_path.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in lines]
            self.assertEqual(events[0]["type"], "executor.started")
            self.assertEqual(events[1]["outcome"], "agent_done")

    def test_executor_registry(self):
        self.assertIsInstance(get_executor("noop"), NoopExecutor)
        with self.assertRaisesRegex(ValueError, "unsupported executor"):
            get_executor("missing")

    def test_codex_command_uses_supported_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp))
            command = CodexExecutor().build_command(ctx)
            self.assertEqual(command[:5], ["codex", "exec", "--json", "-C", str(Path(tmp))])
            self.assertNotIn("--ask-for-approval", command)

    def test_claude_command_uses_stream_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp))
            command = ClaudeExecutor().build_command(ctx)
            self.assertIn("--output-format", command)
            self.assertIn("stream-json", command)
