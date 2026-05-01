"""Deterministic executor used by orchestration tests."""

import json

from dora_orchestrator.executor_protocol import ExecutorResult, TaskRunContext


class NoopExecutor:
    def run(self, context: TaskRunContext) -> ExecutorResult:
        context.event_path.parent.mkdir(parents=True, exist_ok=True)
        events = [
            {"type": "executor.started", "backend": "noop", "run_id": context.run_id},
            {"type": "executor.completed", "backend": "noop", "outcome": "agent_done"},
        ]
        with context.event_path.open("w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
                f.write("\n")
        return ExecutorResult(
            outcome="agent_done",
            summary="Noop executor completed.",
            touched_files=[],
        )
