"""Executor protocol shared by Claude, Codex, noop, and future workers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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


@dataclass(frozen=True)
class ExecutorResult:
    outcome: str
    summary: str
    touched_files: list[Path]


class Executor(Protocol):
    def run(self, context: TaskRunContext) -> ExecutorResult:
        ...

