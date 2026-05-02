"""Codex executor adapter.

Runs `codex exec` non-interactively with the rendered Issue Packet as the
prompt. Approvals are bypassed via `--full-auto` so the agent can edit / test
inside the worktree without blocking on human confirmation. Prompt is fed via
stdin to avoid arg-length limits on long Issue Packets. Output is streamed
line-by-line: every line lands in `event_path` and (if set) invokes
`context.on_line` so a Dagster op can mirror it into the UI logger.
"""

import os
import subprocess

from orchestrator.executor_protocol import ExecutorResult, TaskRunContext

from .claude import RC_IDLE_TIMEOUT, _stream_subprocess


class CodexExecutor:
    def build_command(self, context: TaskRunContext) -> list[str]:
        return [
            "codex",
            "exec",
            "--full-auto",
            "--skip-git-repo-check",
            "--json",
            "--cd",
            str(context.repo_root),
            "-",  # read prompt from stdin
        ]

    def run(self, context: TaskRunContext) -> ExecutorResult:
        prompt = (
            context.prompt_path.read_text(encoding="utf-8")
            if context.prompt_path.exists()
            else ""
        )
        env = {
            **os.environ,
            "CI": "1",
            "TERM": "dumb",
            "DEBIAN_FRONTEND": "noninteractive",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONUNBUFFERED": "1",
        }
        rc = _stream_subprocess(
            self.build_command(context),
            cwd=str(context.repo_root),
            env=env,
            event_path=context.event_path,
            on_line=context.on_line,
            stdin_text=prompt,
            idle_timeout_seconds=context.idle_timeout_seconds,
            hard_timeout_seconds=context.hard_timeout_seconds,
        )
        if rc == RC_IDLE_TIMEOUT:
            outcome = "agent_idle_timeout"
            summary = f"No output for {context.idle_timeout_seconds}s — killed process group."
        elif rc == 0:
            outcome = "agent_done"
            summary = "Codex exited with 0."
        else:
            outcome = f"agent_err:{rc}"
            summary = f"Codex exited with {rc}."
        return ExecutorResult(
            outcome=outcome,
            summary=summary,
            touched_files=[],
        )
