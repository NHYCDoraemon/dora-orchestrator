"""Provision compiled Dora task graphs into Plane-like task systems."""

from .models import TaskGraph


def provision_project(client, graph: TaskGraph) -> dict[str, int]:
    project = graph.project
    client.upsert_project(project.project_slug, project.title)

    cycles: set[str] = set()
    modules: set[str] = set()
    for compiled in graph.tasks:
        task = compiled.spec
        cycles.add(task.cycle)
        modules.add(task.module)

    for cycle in sorted(cycles):
        client.upsert_cycle(project.project_slug, cycle)
    for module in sorted(modules):
        client.upsert_module(project.project_slug, module)

    for compiled in graph.tasks:
        task = compiled.spec
        client.upsert_issue(
            project.project_slug,
            task.external_id,
            {
                "name": task.title,
                "cycle": task.cycle,
                "module": task.module,
                "priority": task.priority,
                "depends_on": list(task.depends_on),
                "source_hash": compiled.source_hash,
                "agent_hint": task.agent_hint,
                "risk": task.risk,
                "acceptance": list(task.acceptance),
                "verification_level": list(task.verification_level),
            },
        )

    return {
        "projects": 1,
        "cycles": len(cycles),
        "modules": len(modules),
        "issues": len(graph.tasks),
    }

