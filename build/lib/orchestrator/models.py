"""Core models for the Dora orchestration platform."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TaskSpec:
    external_id: str
    title: str
    project_slug: str
    cycle: str
    module: str
    priority: str
    depends_on: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    verification_level: list[str] = field(default_factory=lambda: ["L1", "L2", "L3"])
    risk: str = "medium"
    agent_hint: str = "codex"


@dataclass(frozen=True)
class ProjectSpec:
    project_slug: str
    title: str
    tasks: list[TaskSpec]
    modules: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompiledTask:
    spec: TaskSpec
    source_hash: str


@dataclass(frozen=True)
class TaskGraph:
    project: ProjectSpec
    tasks: list[CompiledTask]
