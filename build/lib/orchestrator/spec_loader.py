"""Load structured Dora orchestration specs."""

import json
from pathlib import Path

from .models import ProjectSpec, TaskSpec


def load_project_spec(path: Path) -> ProjectSpec:
    data = json.loads(path.read_text(encoding="utf-8"))
    project_slug = data["project_slug"]
    tasks: list[TaskSpec] = []
    for raw_task in data.get("tasks", []):
        task_data = dict(raw_task)
        task_project = task_data.pop("project_slug", project_slug)
        if task_project != project_slug:
            raise ValueError(
                f"task {task_data.get('external_id', '<unknown>')} project_slug "
                f"{task_project!r} does not match project {project_slug!r}"
            )
        tasks.append(TaskSpec(project_slug=project_slug, **task_data))
    modules = list(data.get("modules", []))
    return ProjectSpec(project_slug=project_slug, title=data["title"], tasks=tasks, modules=modules)
