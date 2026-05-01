"""Compile and validate project task graphs."""

import hashlib
import json
from dataclasses import asdict

from .models import CompiledTask, ProjectSpec, TaskGraph, TaskSpec


def _source_hash(task: TaskSpec) -> str:
    payload = json.dumps(asdict(task), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_no_cycles(tasks_by_id: dict[str, TaskSpec]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str, path: list[str]) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            cycle = " -> ".join(path + [task_id])
            raise ValueError(f"dependency cycle detected: {cycle}")
        visiting.add(task_id)
        for dep_id in tasks_by_id[task_id].depends_on:
            visit(dep_id, path + [task_id])
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks_by_id:
        visit(task_id, [])


def build_task_graph(project: ProjectSpec) -> TaskGraph:
    seen: set[str] = set()
    tasks_by_id: dict[str, TaskSpec] = {}
    compiled: list[CompiledTask] = []

    for task in project.tasks:
        if not task.external_id:
            raise ValueError("task external_id is required")
        if task.external_id in seen:
            raise ValueError(f"duplicate external_id: {task.external_id}")
        if task.project_slug != project.project_slug:
            raise ValueError(
                f"task {task.external_id} project_slug {task.project_slug!r} "
                f"does not match project {project.project_slug!r}"
            )
        seen.add(task.external_id)
        tasks_by_id[task.external_id] = task

    missing: list[tuple[str, str]] = []
    for task in project.tasks:
        for dep_id in task.depends_on:
            if dep_id not in tasks_by_id:
                missing.append((task.external_id, dep_id))
    if missing:
        owner, dep_id = missing[0]
        raise ValueError(f"task {owner} depends on missing task {dep_id}")

    _validate_no_cycles(tasks_by_id)

    for task in project.tasks:
        compiled.append(CompiledTask(spec=task, source_hash=_source_hash(task)))
    return TaskGraph(project=project, tasks=compiled)

