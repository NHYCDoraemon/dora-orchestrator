import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import OrchestratorConfig
from orchestrator.delivery import DeliveryConfig, DeliveryResult
from orchestrator.executor_protocol import ExecutorResult
from orchestrator.executors.claude import _resolve_claude_binary
from orchestrator.in_memory_plane import InMemoryPlaneClient
from orchestrator.run_ready_task import (
    _build_resume_context,
    _format_stream_line,
    _materialize_source_context_files,
    _render_executor_prompt,
    _run_verification_commands,
    run_ready_batch_task,
)


def _packet_metadata() -> dict[str, object]:
    return {
        "execution_packet_version": 1,
        "source_docs": [],
        "source_tables": [],
        "source_queries": [],
    }


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
            **_packet_metadata(),
        },
    )


def _required_read_paths_from_prompt(prompt: str) -> list[str]:
    paths: list[str] = []
    in_required_reads = False
    for line in prompt.splitlines():
        if line.strip() == "Required reads:":
            in_required_reads = True
            continue
        if in_required_reads and not line.strip():
            break
        if in_required_reads and line.startswith("- `") and line.endswith("`"):
            paths.append(line.removeprefix("- `").removesuffix("`"))
    return paths


def _write_read_events_for_prompt(context) -> None:
    context.event_path.parent.mkdir(parents=True, exist_ok=True)
    prompt = context.prompt_path.read_text(encoding="utf-8")
    events = []
    for index, path in enumerate(_required_read_paths_from_prompt(prompt)):
        tool_id = f"read_{index}"
        events.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "Read",
                            "input": {"file_path": path},
                        }
                    ]
                },
            }
        )
        events.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": "read",
                        }
                    ]
                },
            }
        )
    with context.event_path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            f.write("\n")


class _ReadRequiredSourceExecutor:
    def __init__(
        self,
        *,
        summary: str = "Noop executor completed.",
        outcome: str = "agent_done",
        backend_event: str = "",
    ):
        self.summary = summary
        self.outcome = outcome
        self.backend_event = backend_event

    def run(self, context):
        _write_read_events_for_prompt(context)
        if self.backend_event:
            with context.event_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "executor.started", "backend": self.backend_event, "run_id": context.run_id}, sort_keys=True))
                f.write("\n")
        return ExecutorResult(self.outcome, self.summary, [])


class _TransientSourceExecutor:
    def run(self, context):
        _write_read_events_for_prompt(context)
        return ExecutorResult(
            "agent_transient_error",
            "Transient error (rc=1): API Error: The socket connection was closed unexpectedly.",
            [],
        )


def _seed_progress_task(client: InMemoryPlaneClient, *, summary: str = "") -> None:
    client.upsert_project("process-frontend", "Process Frontend")
    client.upsert_issue(
        "process-frontend",
        "PFE-P1FR-20260525C-T01",
        {
            "name": "F97-036 表单模板列表",
            "body": "# Task Summary\n\n接入表单模板列表。\n",
            "issue_type": "task",
            "priority": "P1",
            "depends_on": [],
            "agent_hint": "codex",
            "verification_level": ["L1", "L2", "L3"],
            "verification_commands": ["true"],
            "progress_schema": "pfe-progress/v1",
            "progress_task_id": "B06-F97-036-form-list",
            "row_id": "F97-036",
            "source_scope": "original-97",
            "task_kind": "connect_or_contract",
            "batch_id": "B06",
            "batch_name": "表单引擎",
            "route_path": "/forms/list",
            "backend_contract": "GET /api/v1/forms",
            "frontend_surface": "src/pages/forms/list/FormListPage.tsx | src/api/forms.ts",
            "requirement_signal": "用户能在 /forms/list 看到真实表单模板列表。",
            "development_work": "接入 GET /api/v1/forms 并禁止 mock-only 假成功。",
            "data_lineage": "GET /api/v1/forms -> src/api/forms.ts -> /forms/list -> ledger F97-036",
            "acceptance_signal": "真实接口响应驱动页面；mock-only 不可作为通过。",
            "verification_signal": "L1 focused test; L2 build; L3 ledger; L4 browser; L5 summary",
            "ledger_update_rule": "F97-036 只有证据同步后才允许变更状态。",
            "no_go_signal": "使用 fixture/mock/MSW 写成功即 NO-GO。",
            "test_summary": summary,
            **_packet_metadata(),
        },
    )


class RunReadyBatchTaskTest(unittest.TestCase):
    def test_executor_prompt_includes_required_skills_when_declared(self):
        prompt = _render_executor_prompt(
            {
                "external_id": "DORA-BACK-20260501A-T01",
                "key": "DOR-1",
                "required_skills": ["maritime-java-backend-development"],
                "body": "# 后端任务\n",
            },
            batch_id="20260501A",
            branch="orchestrator/codex/DOR-1",
            worktree_path=Path("/tmp/worktree"),
        )

        self.assertIn("# Required Skills", prompt)
        self.assertIn("REQUIRED SKILL: maritime-java-backend-development", prompt)
        self.assertIn("If this skill is unavailable, stop and report the missing skill.", prompt)

    def test_executor_prompt_omits_required_skills_when_not_declared(self):
        prompt = _render_executor_prompt(
            {
                "external_id": "DORA-FE-20260501A-T01",
                "key": "DOR-2",
                "body": "# 前端任务\n",
            },
            batch_id="20260501A",
            branch="orchestrator/codex/DOR-2",
            worktree_path=Path("/tmp/worktree"),
        )

        self.assertNotIn("# Required Skills", prompt)
        self.assertNotIn("maritime-java-backend-development", prompt)

    def test_executor_prompt_includes_suggested_and_forbidden_skills_when_declared(self):
        prompt = _render_executor_prompt(
            {
                "external_id": "DORA-FE-20260501A-T01",
                "key": "DOR-3",
                "suggested_skills": ["browser:browser"],
                "forbidden_skills": ["dora-plane"],
                "body": "# 前端任务\n",
            },
            batch_id="20260501A",
            branch="orchestrator/codex/DOR-3",
            worktree_path=Path("/tmp/worktree"),
        )

        self.assertIn("# Suggested Skills", prompt)
        self.assertIn("SUGGESTED SKILL: browser:browser", prompt)
        self.assertIn("# Forbidden Skills", prompt)
        self.assertIn("FORBIDDEN SKILL: dora-plane", prompt)
        self.assertNotIn("REQUIRED SKILL: browser:browser", prompt)

    def test_executor_prompt_omits_suggested_and_forbidden_sections_when_not_declared(self):
        prompt = _render_executor_prompt(
            {
                "external_id": "DORA-DOC-20260501A-T01",
                "key": "DOR-4",
                "body": "# 文档任务\n",
            },
            batch_id="20260501A",
            branch="orchestrator/codex/DOR-4",
            worktree_path=Path("/tmp/worktree"),
        )

        self.assertNotIn("# Suggested Skills", prompt)
        self.assertNotIn("# Forbidden Skills", prompt)

    def test_executor_prompt_includes_progress_control_contract(self):
        prompt = _render_executor_prompt(
            {
                "external_id": "PFE-P1FR-20260525C-T01",
                "key": "PFE-1",
                "body": "# 表单模板列表\n",
                "progress_schema": "pfe-progress/v1",
                "progress_task_id": "B06-F97-036-form-list",
                "row_id": "F97-036",
                "acceptance_signal": "真实接口响应驱动页面。",
                "verification_signal": "L1-L5 全绿。",
            },
            batch_id="20260525C",
            branch="orchestrator/codex/PFE-1",
            worktree_path=Path("/tmp/worktree"),
        )

        self.assertIn("# Progress Control Contract", prompt)
        self.assertIn("RESULT: B06-F97-036-form-list <done|partial|needs_input>", prompt)
        self.assertIn("acceptance_signal: 真实接口响应驱动页面。", prompt)

    def test_executor_prompt_includes_resume_context_when_wip_commit_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
            }
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, env=env, check=True)
            (repo / "README.md").write_text("init\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, env=env, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, env=env, check=True)
            prev = repo / ".dora" / "loop-runs" / "prev-run"
            prev.mkdir(parents=True)
            (prev / "summary.md").write_text(
                "Claude exited with 1. API Error: The socket connection was closed unexpectedly.\n",
                encoding="utf-8",
            )
            (prev / "verify.txt").write_text(
                "verification: fail\n  - rc=1 FAIL: mvn -pl platform-gateway-starter test\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=repo, env=env, check=True)
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-q",
                    "-m",
                    "[WIP] MP-GWSTARTER-20260526A-T01: gateway fix",
                    "-m",
                    "dagster-run-id: prev-run",
                ],
                cwd=repo,
                env=env,
                check=True,
            )

            resume_context = _build_resume_context(repo, "current-run")
            prompt = _render_executor_prompt(
                {
                    "external_id": "MP-GWSTARTER-20260526A-T01",
                    "key": "MARITIMEPL-23",
                    "body": "# Gateway task\n",
                },
                batch_id="20260526A",
                branch="orchestrator/20260526A",
                worktree_path=repo,
                resume_context=resume_context,
            )

        self.assertIn("# Resume Context", prompt)
        self.assertIn("Previous run id: `prev-run`", prompt)
        self.assertIn("API Error: The socket connection was closed unexpectedly", prompt)
        self.assertIn("verification: fail", prompt)
        self.assertIn("git show --stat --oneline HEAD", prompt)
        self.assertIn("Continue from the existing WIP commit", prompt)

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

            with patch("orchestrator.run_ready_task.get_executor", return_value=_ReadRequiredSourceExecutor()):
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
                _write_read_events_for_prompt(context)
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

            with patch("orchestrator.run_ready_task.get_executor", return_value=_ReadRequiredSourceExecutor()):
                result = run_ready_batch_task(config, plane_client=client, run_id="run-2")
            task_result = result["runs"][0]

        self.assertEqual(task_result["outcome"], "agent_unverified")
        self.assertEqual(task_result["state"], "Partial")
        self.assertFalse(task_result["verification"]["pass"])
        self.assertEqual(task_result["verification"]["results"][0]["ok"], False)
        self.assertEqual(client.issues[("dora", "DORA-PLN-20260501B-T01")]["state"], "Partial")

    def test_verification_commands_receive_executor_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_verification_commands(
                ['test "$JAVA_HOME" = "/tmp/jdk-17"'],
                Path(tmp),
                extra_env={"JAVA_HOME": "/tmp/jdk-17"},
            )

        self.assertTrue(result["pass"])
        self.assertTrue(result["results"][0]["ok"])

    def test_transient_executor_failure_does_not_consume_task_retry_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            _seed_batch_state(client)
            issue = client.issues[("dora", "DORA-PLN-20260501B-T01")]
            issue["dora_retry_count"] = 3
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="claude",
                project_slug="dora",
                project_title="Dora",
            )

            with patch("orchestrator.run_ready_task.get_executor", return_value=_TransientSourceExecutor()):
                result = run_ready_batch_task(config, plane_client=client, run_id="run-transient")
            task_result = result["runs"][0]

        self.assertEqual(task_result["outcome"], "agent_transient_error")
        self.assertEqual(task_result["state"], "Partial")
        self.assertEqual(issue["dora_retry_count"], 3)
        self.assertEqual(client.issues[("dora", "DORA-PLN-20260501B-T01")]["state"], "Partial")
        self.assertIn("needs:auto-retry", issue.get("labels") or [])

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


class RunReadyBatchTaskProgressControlTest(unittest.TestCase):
    def test_strict_progress_task_without_result_signature_stays_partial(self):
        class _FakeExecutor:
            def run(self, context):
                _write_read_events_for_prompt(context)
                return ExecutorResult("agent_done", "完成了页面开发，但没有结果签名。", [])

        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            _seed_progress_task(client)
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="codex",
                project_slug="process-frontend",
                project_title="Process Frontend",
            )

            with patch("orchestrator.run_ready_task.get_executor", return_value=_FakeExecutor()):
                result = run_ready_batch_task(config, plane_client=client, run_id="progress-missing")

        task_result = result["runs"][0]
        self.assertEqual(task_result["outcome"], "agent_unverified")
        self.assertEqual(task_result["state"], "Partial")
        self.assertIn("missing", task_result["result_signal"]["error"])
        self.assertEqual(
            client.issues[("process-frontend", "PFE-P1FR-20260525C-T01")]["state"],
            "Partial",
        )

    def test_strict_progress_task_result_partial_stays_partial(self):
        class _FakeExecutor:
            def run(self, context):
                _write_read_events_for_prompt(context)
                return ExecutorResult(
                    "agent_done",
                    "有进展但仍有 mock-only。\nRESULT: B06-F97-036-form-list partial — mock-only 未清理",
                    [],
                )

        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            _seed_progress_task(client)
            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="codex",
                project_slug="process-frontend",
            )

            with patch("orchestrator.run_ready_task.get_executor", return_value=_FakeExecutor()):
                result = run_ready_batch_task(config, plane_client=client, run_id="progress-partial")

        task_result = result["runs"][0]
        self.assertEqual(task_result["outcome"], "agent_partial")
        self.assertEqual(task_result["state"], "Partial")
        self.assertEqual(task_result["result_signal"]["status"], "partial")
        self.assertIn("mock-only", task_result["result_signal"]["reason"])

    def test_strict_progress_task_done_writes_progress_projection(self):
        class _FakeExecutor:
            def run(self, context):
                _write_read_events_for_prompt(context)
                return ExecutorResult(
                    "agent_done",
                    "表单模板列表已接入。\nRESULT: B06-F97-036-form-list done — L1-L5 全绿",
                    [],
                )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            client = InMemoryPlaneClient()
            _seed_progress_task(client)
            config = OrchestratorConfig(
                spec_path=repo / "unused.json",
                target_repo=repo,
                executor="codex",
                project_slug="process-frontend",
            )

            with patch("orchestrator.run_ready_task.get_executor", return_value=_FakeExecutor()):
                result = run_ready_batch_task(config, plane_client=client, run_id="progress-done")

            progress_path = repo / ".dora" / "progress" / "process-frontend-progress.json"
            progress = json.loads(progress_path.read_text(encoding="utf-8"))

        task_result = result["runs"][0]
        self.assertEqual(task_result["outcome"], "agent_done")
        self.assertEqual(task_result["state"], "Done")
        self.assertEqual(progress["schema_version"], "pfe-progress/v1")
        self.assertEqual(progress["last_result"]["progress_task_id"], "B06-F97-036-form-list")
        self.assertEqual(progress["last_result"]["result"], "done")
        self.assertEqual(progress["tasks"][0]["row_id"], "F97-036")
        self.assertEqual(progress["tasks"][0]["state"], "done")


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

            with patch("orchestrator.run_ready_task.get_executor", return_value=_ReadRequiredSourceExecutor(backend_event="noop")):
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

            with patch("orchestrator.run_ready_task.get_executor", return_value=_ReadRequiredSourceExecutor()):
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

            with patch("orchestrator.run_ready_task.get_executor", return_value=_ReadRequiredSourceExecutor()):
                run_ready_batch_task(config, plane_client=client, run_id="run-emit-2")

        labels = client.issues[("dora", "DORA-PLN-20260501B-T01")].get("labels") or []
        self.assertIn("needs:review", labels)


class RunReadyBatchTaskSourceContextTest(unittest.TestCase):
    def test_next_ready_skips_orchestrator_invalid_submission_label(self):
        client = InMemoryPlaneClient()
        client.upsert_project("dora", "Dora")
        client.upsert_issue(
            "dora",
            "DORA-PLN-20260501B-T01",
            {
                "name": "Invalid source packet",
                "issue_type": "task",
                "priority": "P1",
                "depends_on": [],
                "labels": ["dora:orchestrator-invalid-submission"],
            },
        )

        self.assertIsNone(client.next_ready_issue("dora"))

    def test_invalid_submission_does_not_block_next_generated_task(self):
        client = InMemoryPlaneClient()
        client.upsert_project("dora", "Dora")
        client.upsert_issue(
            "dora",
            "DORA-PLN-20260501B-T01",
            {
                "name": "Invalid source packet",
                "issue_type": "task",
                "priority": "P1",
                "depends_on": [],
                "labels": ["dora:orchestrator-invalid-submission"],
            },
        )
        client.upsert_issue(
            "dora",
            "DORA-PLN-20260501B-T02",
            {
                "name": "Next valid task",
                "issue_type": "task",
                "priority": "P1",
                "depends_on": [],
            },
        )

        ready = client.next_ready_issue("dora")

        self.assertIsNotNone(ready)
        self.assertEqual(ready["external_id"], "DORA-PLN-20260501B-T02")

    def test_noop_executor_without_required_reads_blocks_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            client = InMemoryPlaneClient()
            _seed_batch_state(client)
            config = OrchestratorConfig(
                spec_path=repo / "unused.json",
                target_repo=repo,
                executor="",
                project_slug="dora",
            )
            events: list[tuple[str, dict]] = []

            result = run_ready_batch_task(
                config,
                plane_client=client,
                run_id="source-evidence-missing",
                on_progress=lambda event, data: events.append((event, data)),
            )

        task_result = result["runs"][0]
        issue = client.issues[("dora", "DORA-PLN-20260501B-T01")]
        self.assertEqual(task_result["outcome"], "source_evidence_missing")
        self.assertEqual(task_result["state"], "Needs Input")
        self.assertTrue(task_result["verification"]["pass"])
        self.assertFalse(task_result["source_evidence"]["pass"])
        self.assertTrue(task_result["source_evidence"]["missing_paths"])
        self.assertEqual(issue["state"], "Needs Input")
        self.assertIn("dora:source-evidence-missing", issue.get("labels") or [])
        markers = [comment["marker"] for comment in client.comments]
        self.assertIn("dora-loop:source-evidence", markers)
        self.assertIn(("source_evidence", {"external_id": "DORA-PLN-20260501B-T01", "pass": False}), events)
        report = client.reports[-1]
        self.assertEqual(report["outcome"], "source_evidence_missing")
        self.assertFalse(report["source_evidence"]["pass"])

    def test_executor_reading_all_required_paths_allows_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            client = InMemoryPlaneClient()
            _seed_batch_state(client)
            config = OrchestratorConfig(
                spec_path=repo / "unused.json",
                target_repo=repo,
                executor="codex",
                project_slug="dora",
            )

            with patch("orchestrator.run_ready_task.get_executor", return_value=_ReadRequiredSourceExecutor()):
                result = run_ready_batch_task(config, plane_client=client, run_id="source-evidence-ok")

        task_result = result["runs"][0]
        self.assertEqual(task_result["outcome"], "agent_done")
        self.assertEqual(task_result["state"], "Done")
        self.assertTrue(task_result["source_evidence"]["pass"])
        self.assertEqual(task_result["source_evidence"]["missing_paths"], [])

    def test_delivery_is_skipped_when_source_evidence_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            client = InMemoryPlaneClient()
            _seed_batch_state(client)
            config = OrchestratorConfig(
                spec_path=repo / "unused.json",
                target_repo=repo,
                executor="",
                project_slug="dora",
            )
            delivery = DeliveryConfig(repo_root=repo, project_slug="dora")

            with patch("orchestrator.delivery.run_delivery") as run_delivery:
                result = run_ready_batch_task(config, plane_client=client, run_id="source-evidence-delivery", delivery=delivery)

        task_result = result["runs"][0]
        self.assertEqual(task_result["outcome"], "source_evidence_missing")
        self.assertTrue(task_result["verification"]["pass"])
        self.assertFalse(task_result["source_evidence"]["pass"])
        self.assertNotIn("delivery", task_result)
        run_delivery.assert_not_called()

    def test_missing_required_source_doc_blocks_before_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-PLN-20260501B-T01", {
                "name": "Needs source doc",
                "issue_type": "task",
                "priority": "P3",
                "depends_on": [],
                "agent_hint": "noop",
                "execution_packet_version": 1,
                "source_docs": [{"kind": "source_docs", "path": "docs/missing.md", "required": True}],
                "source_tables": [],
                "source_queries": [],
            })
            config = OrchestratorConfig(
                spec_path=repo / "unused.json",
                target_repo=repo,
                executor="",
                project_slug="dora",
            )

            with patch("orchestrator.run_ready_task.get_executor") as get_executor:
                result = run_ready_batch_task(config, plane_client=client, run_id="source-missing")

        task_result = result["runs"][0]
        issue = client.issues[("dora", "DORA-PLN-20260501B-T01")]
        self.assertEqual(task_result["outcome"], "orchestrator_invalid_submission")
        self.assertEqual(task_result["state"], "Todo")
        self.assertEqual(issue["state"], "Todo")
        self.assertIsNone(issue["assignee"])
        self.assertIn("dora:orchestrator-invalid-submission", issue.get("labels") or [])
        self.assertNotIn("dora:source-context-missing", issue.get("labels") or [])
        self.assertIn("dora-loop:orchestrator-invalid-submission", [comment["marker"] for comment in client.comments])
        get_executor.assert_not_called()

    def test_legacy_issue_without_execution_packet_blocks_before_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-PLN-20260501B-T01", {
                "name": "Legacy task",
                "issue_type": "task",
                "priority": "P3",
                "depends_on": [],
                "agent_hint": "noop",
            })
            config = OrchestratorConfig(
                spec_path=repo / "unused.json",
                target_repo=repo,
                executor="",
                project_slug="dora",
            )

            with patch("orchestrator.run_ready_task.get_executor") as get_executor:
                result = run_ready_batch_task(config, plane_client=client, run_id="legacy")

        task_result = result["runs"][0]
        issue = client.issues[("dora", "DORA-PLN-20260501B-T01")]
        self.assertEqual(task_result["outcome"], "orchestrator_invalid_submission")
        self.assertEqual(task_result["state"], "Todo")
        self.assertEqual(issue["state"], "Todo")
        self.assertIsNone(issue["assignee"])
        self.assertIn("dora:orchestrator-invalid-submission", issue.get("labels") or [])
        self.assertNotIn("dora:source-context-missing", issue.get("labels") or [])
        marker_comments = [comment for comment in client.comments if comment["marker"] == "dora-loop:orchestrator-invalid-submission"]
        self.assertEqual(len(marker_comments), 1)
        self.assertIn("Execution Packet v1 is missing", marker_comments[0]["body"])
        get_executor.assert_not_called()

    def test_packet_v1_without_source_metadata_blocks_before_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-PLN-20260501B-T01", {
                "name": "Missing source fields",
                "issue_type": "task",
                "priority": "P3",
                "depends_on": [],
                "agent_hint": "noop",
                "execution_packet_version": 1,
            })
            config = OrchestratorConfig(
                spec_path=repo / "unused.json",
                target_repo=repo,
                executor="",
                project_slug="dora",
            )

            with patch("orchestrator.run_ready_task.get_executor") as get_executor:
                result = run_ready_batch_task(config, plane_client=client, run_id="source-fields-missing")

        task_result = result["runs"][0]
        issue = client.issues[("dora", "DORA-PLN-20260501B-T01")]
        self.assertEqual(task_result["outcome"], "orchestrator_invalid_submission")
        self.assertEqual(task_result["state"], "Todo")
        self.assertEqual(issue["state"], "Todo")
        self.assertIsNone(issue.get("assignee"))
        self.assertIsNone(issue.get("dagster_run_id"))
        self.assertIn("dora:orchestrator-invalid-submission", issue.get("labels") or [])
        self.assertNotIn("dora:source-context-missing", issue.get("labels") or [])
        marker_comments = [comment for comment in client.comments if comment["marker"] == "dora-loop:orchestrator-invalid-submission"]
        self.assertEqual(len(marker_comments), 1)
        self.assertIn("source_docs", marker_comments[0]["body"])
        self.assertIn("source_tables", marker_comments[0]["body"])
        self.assertIn("source_queries", marker_comments[0]["body"])
        get_executor.assert_not_called()

    def test_prompt_includes_source_context_contract_paths_and_slice(self):
        captured = {}

        class _FakeExecutor:
            def run(self, context):
                captured["prompt"] = context.prompt_path.read_text(encoding="utf-8")
                _write_read_events_for_prompt(context)
                return ExecutorResult("agent_done", "done", [])

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            (repo / "docs").mkdir()
            (repo / "docs" / "design.md").write_text("# Design\n", encoding="utf-8")
            (repo / "ledger.tsv").write_text(
                "row_id\tfrontend_surface\nF97-036\tsrc/pages/forms/list/FormListPage.tsx\n",
                encoding="utf-8",
            )
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-PLN-20260501B-T01", {
                "name": "Use source packet",
                "body": "# Task Summary\n\nUse the source context.\n",
                "issue_type": "task",
                "priority": "P3",
                "depends_on": [],
                "agent_hint": "codex",
                "verification_commands": ["true"],
                "execution_packet_version": 1,
                "batch_id": "B06",
                "row_id": "F97-036",
                "source_docs": [{"kind": "source_docs", "path": "docs/design.md", "required": True}],
                "source_tables": [
                    {
                        "id": "progress_ledger",
                        "path": "ledger.tsv",
                        "format": "tsv",
                        "key_columns": ["row_id"],
                        "required": True,
                    }
                ],
                "source_queries": [
                    {
                        "id": "current_task_row",
                        "table": "progress_ledger",
                        "required": True,
                        "filters": [{"column": "row_id", "op": "equals", "value_from": "task.row_id"}],
                        "columns": ["row_id", "frontend_surface"],
                        "max_rows": 10,
                    }
                ],
            })
            config = OrchestratorConfig(
                spec_path=repo / "unused.json",
                target_repo=repo,
                executor="codex",
                project_slug="dora",
            )

            with patch("orchestrator.run_ready_task.get_executor", return_value=_FakeExecutor()):
                result = run_ready_batch_task(config, plane_client=client, run_id="source-prompt")

        self.assertEqual(result["runs"][0]["outcome"], "agent_done")
        prompt = captured["prompt"]
        self.assertIn("## Source Context Contract", prompt)
        self.assertIn(str(repo / ".dora" / "source-bundles" / "B06" / "DORA-PLN-20260501B-T01" / "source-bundle.md"), prompt)
        self.assertIn(str(repo / "docs" / "design.md"), prompt)
        self.assertIn(str(repo / ".dora" / "source-bundles" / "B06" / "DORA-PLN-20260501B-T01" / "slices" / "current_task_row.tsv"), prompt)

    def test_delivery_executor_prompt_uses_actual_batch_worktree_path(self):
        captured = {}

        class _FakeExecutor:
            def run(self, context):
                captured["prompt"] = context.prompt_path.read_text(encoding="utf-8")
                _write_read_events_for_prompt(context)
                return ExecutorResult("agent_done", "done", [])

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            (repo / "docs").mkdir()
            (repo / "docs" / "design.md").write_text("# Design\n", encoding="utf-8")
            worktree_root = repo / "worktrees"
            actual_worktree = worktree_root / "dora" / "20260501B"
            wrong_configured_worktree = worktree_root / "dora" / "WRONG"
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-PLN-20260501B-T01", {
                "name": "Use delivery worktree",
                "body": "# Task Summary\n\nUse the source context.\n",
                "issue_type": "task",
                "priority": "P3",
                "depends_on": [],
                "agent_hint": "codex",
                "verification_commands": ["true"],
                "execution_packet_version": 1,
                "source_docs": [{"kind": "source_docs", "path": "docs/design.md", "required": True}],
                "source_tables": [],
                "source_queries": [],
            })
            config = OrchestratorConfig(
                spec_path=repo / "unused.json",
                target_repo=repo,
                executor="codex",
                project_slug="dora",
            )
            delivery = DeliveryConfig(
                repo_root=repo,
                project_slug="dora",
                batch_id="WRONG",
                worktree_root=worktree_root,
                enable_commit=True,
            )

            def _fake_ensure_worktree(_repo_root, wt_path, _branch, _base_branch, auto_clean=False):
                wt_path.mkdir(parents=True)
                (wt_path / "docs").mkdir()
                (wt_path / "docs" / "design.md").write_text("# Design\n", encoding="utf-8")
                return True, False

            with (
                patch("orchestrator.delivery.ensure_worktree", side_effect=_fake_ensure_worktree),
                patch("orchestrator.delivery.run_delivery", return_value=DeliveryResult()),
                patch("orchestrator.run_ready_task.get_executor", return_value=_FakeExecutor()),
            ):
                result = run_ready_batch_task(config, plane_client=client, run_id="delivery-prompt", delivery=delivery)

        self.assertEqual(result["runs"][0]["outcome"], "agent_no_changes")
        self.assertIn(f"worktree at `{actual_worktree}`", captured["prompt"])
        self.assertNotIn(f"worktree at `{wrong_configured_worktree}`", captured["prompt"])

    def test_delivery_materializes_source_context_before_bundle_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            (repo / "docs" / "dora" / "batches" / "20260526D").mkdir(parents=True)
            (repo / "docs" / "dora" / "batches" / "20260526D" / "program-page.md").write_text(
                "# Program page\n",
                encoding="utf-8",
            )
            worktree_root = repo / "worktrees"
            actual_worktree = worktree_root / "dora" / "20260526D"
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-PLN-20260526D-T01", {
                "name": "Use generated batch source docs",
                "body": "# Task Summary\n\nUse the source context.\n",
                "issue_type": "task",
                "priority": "P3",
                "depends_on": [],
                "agent_hint": "codex",
                "verification_commands": ["true"],
                "execution_packet_version": 1,
                "source_pages": [
                    {"path": "docs/dora/batches/20260526D/program-page.md", "required": True},
                ],
                "source_docs": [],
                "source_tables": [],
                "source_queries": [],
            })
            config = OrchestratorConfig(
                spec_path=repo / "unused.json",
                target_repo=repo,
                executor="codex",
                project_slug="dora",
            )
            delivery = DeliveryConfig(
                repo_root=repo,
                project_slug="dora",
                worktree_root=worktree_root,
                enable_commit=True,
            )
            events: list[tuple[str, dict]] = []

            def _fake_ensure_worktree(_repo_root, wt_path, _branch, _base_branch, auto_clean=False):
                wt_path.mkdir(parents=True)
                return True, False

            with (
                patch("orchestrator.delivery.ensure_worktree", side_effect=_fake_ensure_worktree),
                patch("orchestrator.delivery.run_delivery", return_value=DeliveryResult()),
                patch("orchestrator.run_ready_task.get_executor", return_value=_ReadRequiredSourceExecutor()),
            ):
                result = run_ready_batch_task(
                    config,
                    plane_client=client,
                    run_id="delivery-source-materialized",
                    delivery=delivery,
                    on_progress=lambda event, data: events.append((event, data)),
                )
            materialized_exists = (actual_worktree / "docs" / "dora" / "batches" / "20260526D" / "program-page.md").is_file()

        self.assertEqual(result["runs"][0]["outcome"], "agent_no_changes")
        self.assertEqual(result["runs"][0]["state"], "Done")
        self.assertTrue(materialized_exists)
        self.assertIn(
            ("source_context_materialized", {"external_id": "DORA-PLN-20260526D-T01", "file_count": 1}),
            events,
        )

    def test_materializes_source_context_files_into_delivery_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            worktree_root = root / "worktree"
            (source_root / "docs" / "dora" / "batches" / "20260526D").mkdir(parents=True)
            (source_root / "docs" / "dora" / "contracts").mkdir(parents=True)
            (source_root / "docs" / "dora" / "batches" / "20260526D" / "program-page.md").write_text(
                "# Program page\n",
                encoding="utf-8",
            )
            (source_root / "docs" / "dora" / "batches" / "20260526D" / "summary.md").write_text(
                "# Summary\n",
                encoding="utf-8",
            )
            (source_root / "docs" / "dora" / "contracts" / "ledger.tsv").write_text(
                "row_id\tname\nF97-036\tForm list\n",
                encoding="utf-8",
            )
            issue = {
                "source_pages": [
                    {"path": "docs/dora/batches/20260526D/program-page.md", "required": True},
                ],
                "source_summaries": [
                    {"path": "docs/dora/batches/20260526D/summary.md", "required": True},
                ],
                "source_docs": [
                    {"path": "docs/dora/contracts/ledger.tsv", "required": True},
                ],
                "source_tables": [
                    {"id": "ledger", "path": "docs/dora/contracts/ledger.tsv", "format": "tsv", "required": True},
                ],
            }

            copied = _materialize_source_context_files(issue, source_root=source_root, worktree_root=worktree_root)

            self.assertEqual(
                sorted(str(path.relative_to(worktree_root.resolve())) for path in copied),
                [
                    "docs/dora/batches/20260526D/program-page.md",
                    "docs/dora/batches/20260526D/summary.md",
                    "docs/dora/contracts/ledger.tsv",
                ],
            )
            self.assertEqual(
                (worktree_root / "docs" / "dora" / "batches" / "20260526D" / "program-page.md").read_text(encoding="utf-8"),
                "# Program page\n",
            )


class RunReadyBatchTaskLoopTest(unittest.TestCase):
    def test_generated_batches_run_by_batch_then_task_sequence_before_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = InMemoryPlaneClient()
            client.upsert_project("dora", "Dora")
            client.upsert_issue("dora", "DORA-PLN-20260502A-T01", {
                "name": "Later batch urgent", "issue_type": "task", "priority": "P1",
                "depends_on": [], "agent_hint": "noop",
                **_packet_metadata(),
            })
            client.upsert_issue("dora", "DORA-PLN-20260501B-T02", {
                "name": "Earlier batch task 2", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
                **_packet_metadata(),
            })
            client.upsert_issue("dora", "DORA-PLN-20260501B-T01", {
                "name": "Earlier batch task 1", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
                **_packet_metadata(),
            })

            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )
            with patch("orchestrator.run_ready_task.get_executor", return_value=_ReadRequiredSourceExecutor()):
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
            **_packet_metadata(),
        })
        client.upsert_issue("dora", "DORA-PLN-20260501B-T02", {
            "name": "Later ready", "issue_type": "task", "priority": "P1",
            "depends_on": [], "agent_hint": "noop",
            **_packet_metadata(),
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
                **_packet_metadata(),
            })
            client.upsert_issue("dora", "DORA-T02", {
                "name": "Task 2", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
                **_packet_metadata(),
            })

            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )
            with patch("orchestrator.run_ready_task.get_executor", return_value=_ReadRequiredSourceExecutor()):
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
                **_packet_metadata(),
            })
            client.upsert_issue("dora", "DORA-T02", {
                "name": "Task 2", "issue_type": "task", "priority": "P3",
                "depends_on": ["DORA-T01"],
                **_packet_metadata(),
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
                **_packet_metadata(),
            })
            client.upsert_issue("dora", "DORA-T02", {
                "name": "Fails-2", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
                "verification_commands": ["test -f /definitely/not/there/either"],
                **_packet_metadata(),
            })
            client.upsert_issue("dora", "DORA-T03", {
                "name": "Should-never-run", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
                **_packet_metadata(),
            })

            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )
            events: list[tuple[str, dict]] = []
            with patch("orchestrator.run_ready_task.get_executor", return_value=_ReadRequiredSourceExecutor()):
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
                **_packet_metadata(),
            })
            client.upsert_issue("dora", "DORA-T02", {
                "name": "T2", "issue_type": "task", "priority": "P3",
                "depends_on": [], "agent_hint": "noop",
                **_packet_metadata(),
            })

            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )
            with patch("orchestrator.run_ready_task.get_executor", return_value=_ReadRequiredSourceExecutor()):
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
                **_packet_metadata(),
            })

            config = OrchestratorConfig(
                spec_path=Path(tmp) / "unused.json",
                target_repo=Path(tmp).resolve(),
                executor="",
                project_slug="dora",
            )
            with patch("orchestrator.run_ready_task.get_executor", return_value=_ReadRequiredSourceExecutor()):
                result = run_ready_batch_task(
                    config, plane_client=client, run_id="cb-3",
                    max_no_progress_streak=1,
                )

            self.assertTrue(result["circuit_breaker_open"])
            self.assertEqual(result["loop_count"], 1)


if __name__ == "__main__":
    unittest.main()
