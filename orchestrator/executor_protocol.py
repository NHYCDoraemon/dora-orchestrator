"""Executor protocol shared by Claude, Codex, noop, and future workers."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol


@dataclass(frozen=True)
class TaskRunContext:
    run_id: str
    issue_key: str
    external_id: str
    project_slug: str
    repo_root: Path
    branch: str
    agent: str
    prompt_path: Path
    event_path: Path
    verification_level: list[str]
    permission_mode: str = "default"
    # Subprocess watchdog: if no stdout line appears for this many seconds the
    # process group is killed and the outcome is `agent_idle_timeout`.
    idle_timeout_seconds: int = 1800
    # Hard wall clock: if the subprocess hasn't exited after this many seconds
    # the process group is killed and the outcome is `agent_hard_timeout`.
    hard_timeout_seconds: int = 3600
    extra_env: dict[str, str] = field(default_factory=dict)
    # Optional per-line streaming callback. Subprocess executors invoke this
    # for every stdout/stderr line so the caller (typically a Dagster op) can
    # mirror progress into a UI logger while the full stream is captured to
    # `event_path` for replay.
    on_line: Callable[[str], None] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ExecutorResult:
    outcome: str
    summary: str
    touched_files: list[Path]


class Executor(Protocol):
    def run(self, context: TaskRunContext) -> ExecutorResult:
        ...
