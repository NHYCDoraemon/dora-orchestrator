import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.executor_protocol import TaskRunContext
from orchestrator.executors import get_executor
from orchestrator.executors.claude import (
    RC_HARD_TIMEOUT,
    RC_IDLE_TIMEOUT,
    ClaudeExecutor,
    _classify_outcome,
    _kill_process_group,
    _stream_subprocess,
)
from orchestrator.executors.codex import CodexExecutor
from orchestrator.executors.noop import NoopExecutor
from orchestrator.local_artifacts import create_run_artifacts


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
            self.assertEqual(command[:2], ["codex", "exec"])
            # Non-interactive: must be self-driving without human approval prompts.
            # Codex CLI 0.128 dropped --full-auto; the bypass flag is now the
            # only way to skip both approvals and sandboxing in headless mode.
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
            # Worktrees may not be valid git repos at the moment of invocation;
            # skip the inner check so the executor doesn't refuse to run.
            self.assertIn("--skip-git-repo-check", command)
            # Working directory points at the configured repo root.
            self.assertIn("--cd", command)
            self.assertIn(str(Path(tmp)), command)
            # Prompt is fed via stdin (avoids arg-length limits).
            self.assertEqual(command[-1], "-")
            self.assertNotIn("--ask-for-approval", command)

    def test_codex_executor_passes_extra_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp))
            ctx = TaskRunContext(
                **{**ctx.__dict__, "extra_env": {"CODEX_HOME": "/tmp/codex-home"}}
            )
            captured = {}

            def _fake_stream(*args, **kwargs):
                captured["env"] = kwargs["env"]
                return 0

            with patch("orchestrator.executors.codex._stream_subprocess", side_effect=_fake_stream):
                result = CodexExecutor().run(ctx)

            self.assertEqual(result.outcome, "agent_done")
            self.assertEqual(captured["env"]["CODEX_HOME"], "/tmp/codex-home")

    # ── idle / hard timeout and process-group tests ──────────────

    def test_stream_subprocess_clean_exit(self):
        """Process that prints a line and exits 0."""
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.ndjson"
            rc = _stream_subprocess(
                ["echo", "hello"],
                cwd=tmp,
                env={**os.environ},
                event_path=event_path,
                on_line=None,
                idle_timeout_seconds=5,
            )
            self.assertEqual(rc, 0)
            self.assertIn("hello", event_path.read_text())

    def test_stream_subprocess_idle_timeout_kills_hung_process(self):
        """A process that sleeps without output is killed after idle_timeout."""
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.ndjson"
            rc = _stream_subprocess(
                ["sleep", "60"],
                cwd=tmp,
                env={**os.environ},
                event_path=event_path,
                on_line=None,
                idle_timeout_seconds=1,
                hard_timeout_seconds=None,
            )
            self.assertEqual(rc, RC_IDLE_TIMEOUT)

    def test_stream_subprocess_hard_timeout(self):
        """A chatty process that runs past hard_timeout is killed."""
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.ndjson"
            # Print once then sleep forever — idle timeout won't trigger
            # because the first line arrives, but hard timeout will.
            rc = _stream_subprocess(
                [
                    "sh",
                    "-c",
                    "echo start; sleep 60",
                ],
                cwd=tmp,
                env={**os.environ},
                event_path=event_path,
                on_line=None,
                idle_timeout_seconds=10,
                hard_timeout_seconds=1,
            )
            self.assertEqual(rc, RC_HARD_TIMEOUT)

    def test_stream_subprocess_orphan_grandchild_detected(self):
        """Grandchild holding stdout after parent exits is cleaned up."""
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.ndjson"
            # Parent prints a line, then exits immediately.  A backgrounded
            # grandchild inherits stdout and sleeps, keeping the pipe open
            # after the parent is gone.
            script = "echo start; sleep 60 & exit 0"
            rc = _stream_subprocess(
                ["sh", "-c", script],
                cwd=tmp,
                env={**os.environ},
                event_path=event_path,
                on_line=None,
                idle_timeout_seconds=30,
                hard_timeout_seconds=None,
                orphan_grace_seconds=1,
            )
            # Parent exited 0; after 1 s grace the orphan is killed.
            # The pipe closes and we get the parent's exit code.
            self.assertEqual(rc, 0)
            content = event_path.read_text()
            self.assertIn("start", content)

    def test_classify_timeout_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.ndjson"
            event_path.write_text("", encoding="utf-8")

            outcome, summary = _classify_outcome(RC_IDLE_TIMEOUT, event_path, 30, 600)
            self.assertEqual(outcome, "agent_idle_timeout")
            self.assertIn("30s", summary)

            outcome, summary = _classify_outcome(RC_HARD_TIMEOUT, event_path, 600, 120)
            self.assertEqual(outcome, "agent_hard_timeout")
            self.assertIn("120s", summary)

    def test_successful_claude_run_uses_result_text_as_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.ndjson"
            event_path.write_text(
                json.dumps({
                    "type": "result",
                    "subtype": "success",
                    "result": "Implemented changes.\nRESULT: item-R020-implementation done - verified",
                }) + "\n",
                encoding="utf-8",
            )

            outcome, summary = _classify_outcome(0, event_path)

        self.assertEqual(outcome, "agent_done")
        self.assertIn("RESULT: item-R020-implementation done", summary)

    def test_classify_socket_closed_as_transient(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.ndjson"
            event_path.write_text(
                json.dumps({
                    "type": "result",
                    "result": (
                        "API Error: The socket connection was closed unexpectedly. "
                        "For more information, pass `verbose: true` in the second argument to fetch()"
                    ),
                }) + "\n",
                encoding="utf-8",
            )

            outcome, summary = _classify_outcome(1, event_path)

            self.assertEqual(outcome, "agent_transient_error")
            self.assertIn("socket connection was closed", summary)

    def test_kill_process_group_terminates_tree(self):
        """Spawn a process tree, kill the group, verify nothing survives."""
        with tempfile.TemporaryDirectory() as tmp:
            # Spawn a parent that spawns a child; both sleep.
            proc = subprocess.Popen(
                ["sh", "-c", "sleep 60 & sleep 60; wait"],
                cwd=tmp,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                env={**os.environ},
            )
            # Give it a moment to fork.
            time.sleep(0.5)
            self.assertIsNone(proc.poll(), "process should still be alive")

            _kill_process_group(proc)

            # After _kill_process_group the direct child is reaped.
            self.assertIsNotNone(proc.poll(), "process should be dead")
            # SIGTERM or SIGKILL → negative return code.
            self.assertNotEqual(proc.returncode, 0)

    def test_kill_process_group_noop_when_already_dead(self):
        """Calling _kill_process_group on an exited process is a no-op."""
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                ["true"],
                cwd=tmp,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env={**os.environ},
            )
            # Create a mock-like Popen to pass to the function.
            # We just verify it doesn't raise.
            proc2 = subprocess.Popen(
                ["true"],
                cwd=tmp,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env={**os.environ},
            )
            proc2.wait()
            _kill_process_group(proc2)  # no-op, must not raise

    def test_claude_command_uses_stream_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(Path(tmp))
            command = ClaudeExecutor().build_command(ctx)
            self.assertTrue(command[0].endswith("/claude") or command[0] == "claude")
            self.assertIn("--print", command)
            self.assertIn("--dangerously-skip-permissions", command)
            self.assertIn("--output-format", command)
            self.assertIn("stream-json", command)
            self.assertIn("--verbose", command)
            # No --append-system-prompt: directive framing belongs in the
            # rendered prompt body, not the system prompt (matches dev_loop.sh).
            self.assertNotIn("--append-system-prompt", command)
            # Prompt is the last positional arg (claude has no --cd; cwd is set
            # on subprocess.run, prompt comes from the rendered Issue Packet body).
            self.assertEqual(command[-1], "do the smallest useful thing")
