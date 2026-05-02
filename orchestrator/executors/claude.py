"""Claude executor adapter."""

import subprocess

from dora_orchestrator.executor_protocol import ExecutorResult, TaskRunContext


class ClaudeExecutor:
    def build_command(self, context: TaskRunContext) -> list[str]:
        return [
            "claude",
            "--output-format",
            "stream-json",
            "--verbose",
            "-p",
            context.prompt_path.read_text(encoding="utf-8") if context.prompt_path.exists() else "",
        ]

    def run(self, context: TaskRunContext) -> ExecutorResult:
        with context.event_path.open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                self.build_command(context),
                cwd=context.repo_root,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        outcome = "agent_done" if result.returncode == 0 else f"agent_err:{result.returncode}"
        return ExecutorResult(outcome=outcome, summary=f"Claude exited with {result.returncode}.", touched_files=[])
