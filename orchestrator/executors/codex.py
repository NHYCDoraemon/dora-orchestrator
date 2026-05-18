"""Codex executor adapter.

Runs `codex exec` non-interactively with the rendered Issue Packet as the
prompt. Approvals + sandbox are bypassed via
`--dangerously-bypass-approvals-and-sandbox` so the agent can edit / test
inside the worktree without blocking on human confirmation. (The orchestrator
host itself is the sandbox boundary — see CLAUDE.md.) Prompt is fed via
stdin to avoid arg-length limits on long Issue Packets. Output is streamed
line-by-line: every line lands in `event_path` and (if set) invokes
`context.on_line` so a Dagster op can mirror it into the UI logger.
"""

import json
import os

from orchestrator.executor_protocol import ExecutorResult, TaskRunContext

from .claude import _classify_outcome, _stream_subprocess


def _codex_result_text(event_path) -> str:
    """Result-text extractor for codex's --json output.

    Walks the events file backwards looking for the last ``item.completed``
    whose item is an ``agent_message`` and returns its ``text``. Empty on
    any read or parse error. Mirrors the shape of ``_claude_result_text``.
    """
    try:
        if not (event_path and event_path.exists()):
            return ""
        with event_path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") != "item.completed":
            continue
        item = evt.get("item") or {}
        if item.get("type") == "agent_message":
            return str(item.get("text", ""))
    return ""


class CodexExecutor:
    def build_command(self, context: TaskRunContext) -> list[str]:
        return [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
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
        outcome, summary = _classify_outcome(
            rc,
            context.event_path,
            context.idle_timeout_seconds,
            context.hard_timeout_seconds,
            agent_label="Codex",
            result_text_extractor=_codex_result_text,
        )
        return ExecutorResult(
            outcome=outcome,
            summary=summary,
            touched_files=[],
        )
