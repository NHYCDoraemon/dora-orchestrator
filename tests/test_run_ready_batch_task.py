import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import OrchestratorConfig
from orchestrator.executor_protocol import ExecutorResult
from orchestrator.executors.claude import _resolve_claude_binary
from orchestrator.in_memory_plane import InMemoryPlaneClient
from orchestrator.run_ready_task import _format_stream_line, run_ready_batch_task


def _seed_batch_state(client: InMemoryPlaneClient) -> None:
    client.upsert_project("dora", "Dora")
    client.upsert_issue(
        "dora",
        "DORA-PLN-20260501B-ROOT",
        {
            "name": "[Batch] Smoke",
            "issue_type": "root_epic",
            "priority": "P1",
            "depends_on": [],
        },
    )
    client.upsert_issue(
        "dora",
        "DORA-PLN-20260501B-T01",
        {
            "name": "Drop a marker file",
            "body": "# Task Summary\n\nWrite marker.\n",
            "issue_type": "task",
            "parent_external_id": "DORA-PLN-20260501B-ROOT",
            "priority": "P3",
            "depends_on": [],
            "agent_hint": "noop",
            "verification_level": ["L1"],
            "verification_commands": ["true"],
        },
    )


class RunReadyBatchTaskTest(unittest.TestCase):
    def test_formats_codex_agent_messages(self):
        message = "I will inspect the repository first.\nThen edit."
        line = json.dumps({
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "agent_message",
                "text": message,
            },
        })

        self.assertEqual(
            _format_stream_line(line),
            f"▸ {message}",
        )

    def test_formats_codex_command_execution(self):
        command = "/bin/zsh -lc 'git status --short && git diff --stat -- src/main/java/example/VeryLongFileName.java'"
        output = " M src/App.java\n?? docs/note.md\n"
        started = json.dumps({
            "type": "item.started",
            "item": {
                "id": "item_2",
                "type": "command_execution",
                "command": command,
                "status": "in_progress",
            },
        })
        completed = json.dumps({
            "type": "item.completed",
            "item": {
                "id": "item_2",
                "type": "command_execution",
                "command": command,
                "aggregated_output": output,
                "exit_code": 0,
                "status": "completed",
            },
        })

        self.assertEqual(
            _format_stream_line(started),
            f"▶ command  |  {command}",
        )
        self.assertEqual(
            _format_stream_line(completed),
            f"✓ command rc=0\n{output.rstrip()}",
        )

    def test_formats_codex_plain_stderr(self):
        line = "2026-05-19T09:45:43.080724Z ERROR codex_models_manager::manager: failed"

        self.assertEqual(
            _format_stream_line(line),
            "∙ 2026-05-19T09:45:43.080724Z ERROR codex_models_manager::manager: failed",
        )

    def test_formats_claude_assistant_text_and_thinking(self):
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "text": "I need to inspect the repo first."},
                    {"type": "text", "text": "I will update the executor configuration.\nThen verify."},
                ],
            },
        })

        self.assertEqual(
            _format_stream_line(line),
            "◉  I need to inspect the repo first.\n▸ I will update the executor configuration.",
        )

    def test_formats_claude_result_summary(self):
        line = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "\nDone with verification.\nSecond line.",
        })

        self.assertEqual(
            _format_stream_line(line),
            "★ success  |  Done with verification.",
        )

    def test_resolves_claude_from_extra_search_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            binary = bin_dir / "claude"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")

            self.assertEqual(_resolve_claude_binary("", [bin_dir]), str(binary))

    def test_skips_root_epic_and_runs_first_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            _seed_batch_state(client)
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
                project_title="Dora",
            )

            result = run_ready_batch_task(config, plane_client=client, run_id="run-1")
            task_result = result["runs"][0]

        self.assertEqual(task_result["outcome"], "agent_done")
        self.assertEqual(task_result["state"], "Done")
        self.assertEqual(task_result["issue"], "DORA-PLN-20260501B-T01")
        self.assertTrue(task_result["verification"]["pass"])
        self.assertFalse(task_result["verification"]["skipped"])
        self.assertEqual(client.issues[("dora", "DORA-PLN-20260501B-T01")]["state"], "Done")
        # ROOT rolls up from its sole child: all-Done → Done.
        self.assertEqual(client.issues[("dora", "DORA-PLN-20260501B-ROOT")]["state"], "Done")

    def test_passes_executor_environment_to_executor_context(self):
        seen = {}

        class _FakeExecutor:
            def run(self, context):
                seen["extra_env"] = context.extra_env
                return ExecutorResult("agent_done", "done", [])

        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            _seed_batch_state(client)
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="codex",
                project_slug="dora",
                project_title="Dora",
                executor_env={"CODEX_HOME": "/tmp/codex-home"},
            )

            with patch("orchestrator.run_ready_task.get_executor", return_value=_FakeExecutor()):
                run_ready_batch_task(config, plane_client=client, run_id="run-env")

        self.assertEqual(seen["extra_env"], {"CODEX_HOME": "/tmp/codex-home"})

    def test_failing_verification_command_marks_partial_and_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            _seed_batch_state(client)
            issue = client.issues[("dora", "DORA-PLN-20260501B-T01")]
            issue["verification_commands"] = ["test -f /definitely/not/here/marker"]
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
                project_title="Dora",
            )

            result = run_ready_batch_task(config, plane_client=client, run_id="run-2")
            task_result = result["runs"][0]

        self.assertEqual(task_result["outcome"], "agent_unverified")
        self.assertEqual(task_result["state"], "Partial")
        self.assertFalse(task_result["verification"]["pass"])
        self.assertEqual(task_result["verification"]["results"][0]["ok"], False)
        self.assertEqual(client.issues[("dora", "DORA-PLN-20260501B-T01")]["state"], "Partial")

    def test_returns_no_ready_when_only_root_epic_is_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue(
                "dora",
                "DORA-PLN-20260501B-ROOT",
                {"name": "[Batch] Smoke", "issue_type": "root_epic", "priority": "P1"},
            )
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )

            result = run_ready_batch_task(config, plane_client=client, run_id="run-3")

        self.assertEqual(result["outcome"], "no_ready")
        self.assertEqual(result["run_id"], "run-3")
        self.assertEqual(result["loop_count"], 1)


class RunReadyBatchTaskHonorsFrontmatterTest(unittest.TestCase):
    def test_picks_executor_from_agent_hint_when_config_executor_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            _seed_batch_state(client)
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )

            result = run_ready_batch_task(config, plane_client=client, run_id="run-4")

            self.assertEqual(result["runs"][0]["outcome"], "agent_done")
            events = (Path(tmp) / ".dora" / "loop-runs" / "run-4" / "events.ndjson").read_text(encoding="utf-8")
            self.assertIn('"backend": "noop"', events)


class RunReadyBatchTaskEmitsCommentsAndLabelsTest(unittest.TestCase):
    def test_emits_marker_comments_and_no_label_on_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            _seed_batch_state(client)
            client.upsert_page("dora", "batch-20260501B", {"title": "Batch", "body": "stub"})
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )

            result = run_ready_batch_task(config, plane_client=client, run_id="run-emit-1")

        self.assertEqual(result["runs"][0]["outcome"], "agent_done")
        markers = [c["marker"] for c in client.comments]
        self.assertIn("dora-loop:claim", markers)
        self.assertIn("dora-loop:verify", markers)
        self.assertIn("dora-loop:release", markers)
        self.assertTrue(any(m and m.startswith("dora-loop:tool:") for m in markers))
        # happy path: no needs:* label attached
        labels = client.issues[("dora", "DORA-PLN-20260501B-T01")].get("labels") or []
        self.assertNotIn("needs:review", labels)
        self.assertNotIn("needs:input", labels)
        # batch page body was refreshed
        self.assertIn("Progress:", client.pages[("dora", "batch-20260501B")]["body"])

    def test_attaches_needs_review_label_when_verification_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            _seed_batch_state(client)
            client.issues[("dora", "DORA-PLN-20260501B-T01")]["verification_commands"] = ["false"]
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )

            run_ready_batch_task(config, plane_client=client, run_id="run-emit-2")

        labels = client.issues[("dora", "DORA-PLN-20260501B-T01")].get("labels") or []
        self.assertIn("needs:review", labels)


class RunReadyBatchTaskLoopTest(unittest.TestCase):
    def test_generated_batches_run_by_batch_then_task_sequence_before_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-PLN-20260502A-T01", {
                "name": "Later batch urgent", "issue_type": "task", "priority": "P1",
                "depends_on": [], "agent_hint": "noop",
            })
            client.upsert_issue("dora", "DORA-PLN-20260501B-T02", {
                "name": "Earlier batch task 2", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
            })
            client.upsert_issue("dora", "DORA-PLN-20260501B-T01", {
                "name": "Earlier batch task 1", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
            })

            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )
            result = run_ready_batch_task(
                config,
                plane_client=client,
                run_id="strict-1",
                max_loops=2,
            )

            self.assertEqual(
                [run["external_id"] for run in result["runs"]],
                ["DORA-PLN-20260501B-T01", "DORA-PLN-20260501B-T02"],
            )
            self.assertEqual(client.issues[("dora", "DORA-PLN-20260502A-T01")]["state"], "Todo")

    def test_generated_batch_waits_when_earliest_task_is_already_in_progress(self):
        client = InMemoryPlaneClient()
        client.upsert_project("dora", "Dora")
        client.upsert_issue("dora", "DORA-PLN-20260501B-T01", {
            "name": "Earlier running", "issue_type": "task", "priority": "P3",
            "depends_on": [], "agent_hint": "noop",
        })
        client.upsert_issue("dora", "DORA-PLN-20260501B-T02", {
            "name": "Later ready", "issue_type": "task", "priority": "P1",
            "depends_on": [], "agent_hint": "noop",
        })
        client.issues[("dora", "DORA-PLN-20260501B-T01")]["state"] = "In Progress"

        self.assertIsNone(client.next_ready_issue("dora"))

    def test_chains_through_multiple_tasks_in_one_call(self):
        """After T01 completes, T02 should be picked up automatically."""
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-ROOT", {
                "name": "Root", "issue_type": "root_epic", "priority": "P1",
            })
            client.upsert_issue("dora", "DORA-T01", {
                "name": "Task 1", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
            })
            client.upsert_issue("dora", "DORA-T02", {
                "name": "Task 2", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
            })

            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )
            result = run_ready_batch_task(config, plane_client=client, run_id="chain-1")

            self.assertEqual(result["outcome"], "agent_done")
            self.assertEqual(result["loop_count"], 3)
            self.assertEqual(len(result["runs"]), 3)
            self.assertEqual(result["runs"][0]["external_id"], "DORA-T01")
            self.assertEqual(result["runs"][1]["external_id"], "DORA-T02")
            self.assertEqual(result["runs"][2]["outcome"], "no_ready")
            self.assertEqual(client.issues[("dora", "DORA-T01")]["state"], "Done")
            self.assertEqual(client.issues[("dora", "DORA-T02")]["state"], "Done")

    def test_stops_when_first_task_depends_on_unfinished(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-T01", {
                "name": "Task 1", "issue_type": "task", "priority": "P3",
                "depends_on": ["DORA-T02"],
            })
            client.upsert_issue("dora", "DORA-T02", {
                "name": "Task 2", "issue_type": "task", "priority": "P3",
                "depends_on": ["DORA-T01"],
            })

            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )
            result = run_ready_batch_task(config, plane_client=client, run_id="deadlock-1")

            self.assertEqual(result["outcome"], "no_ready")
            self.assertEqual(result["loop_count"], 1)
            self.assertEqual(client.issues[("dora", "DORA-T01")]["state"], "Blocked")
            self.assertEqual(client.issues[("dora", "DORA-T02")]["state"], "Blocked")


class RunReadyBatchTaskCircuitBreakerTest(unittest.TestCase):
    """The 2026-05-05 runaway needs a last line of defence: even if the
    schedule probe (Fix #1) and stale-lock policy (Fix #2) both miss a
    pathological task, a single Dagster run must not feed claude up to
    max_loops=10 times on the same broken issue. Cap zero-progress streak."""

    def test_circuit_breaker_opens_after_two_consecutive_no_progress(self):
        """Two iterations in a row with no agent_done and no commit →
        run aborts at loop 2 instead of running to max_loops. Uses two
        distinct failing tasks because the in-process exclude list would
        otherwise short-circuit iter 2 with no_ready before the breaker
        had a chance to count it."""
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-T01", {
                "name": "Fails-1", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
                "verification_commands": ["test -f /definitely/not/here/marker"],
            })
            client.upsert_issue("dora", "DORA-T02", {
                "name": "Fails-2", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
                "verification_commands": ["test -f /definitely/not/there/either"],
            })
            client.upsert_issue("dora", "DORA-T03", {
                "name": "Should-never-run", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
            })

            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )
            events: list[tuple[str, dict]] = []
            result = run_ready_batch_task(
                config, plane_client=client, run_id="cb-1",
                on_progress=lambda e, d: events.append((e, d)),
            )

            # Without the breaker, T03 would also be picked up after the
            # two failing ones. With the breaker (default 2) the run aborts
            # at loop 2 and T03 is never touched.
            self.assertTrue(result.get("circuit_breaker_open"), "breaker must trip")
            self.assertEqual(result["loop_count"], 2)
            self.assertEqual(len(result["runs"]), 2)
            # T03 must have been left alone — proves the breaker stopped
            # claude from being launched for further work.
            self.assertNotEqual(client.issues[("dora", "DORA-T03")]["state"], "Done")
            # An on_progress event was emitted so the operator can spot it.
            kinds = [e for e, _ in events]
            self.assertIn("circuit_breaker_open", kinds)

    def test_circuit_breaker_does_not_open_on_happy_path(self):
        """Two agent_done iterations followed by no_ready must NOT trip
        the breaker — happy path stays clean."""
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-T01", {
                "name": "T1", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
            })
            client.upsert_issue("dora", "DORA-T02", {
                "name": "T2", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
            })

            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )
            result = run_ready_batch_task(config, plane_client=client, run_id="cb-2")

            self.assertNotIn("circuit_breaker_open", result)
            self.assertEqual(result["loop_count"], 3)  # T01, T02, no_ready
            self.assertEqual(result["outcome"], "agent_done")

    def test_circuit_breaker_threshold_is_configurable(self):
        """max_no_progress_streak=1 means the breaker trips on the
        first failure — useful for super-defensive ops modes."""
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-T01", {
                "name": "Fail-1", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
                "verification_commands": ["test -f /definitely/not/here"],
            })

            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )
            result = run_ready_batch_task(
                config, plane_client=client, run_id="cb-3",
                max_no_progress_streak=1,
            )

            self.assertTrue(result["circuit_breaker_open"])
            self.assertEqual(result["loop_count"], 1)


if __name__ == "__main__":
    unittest.main()
