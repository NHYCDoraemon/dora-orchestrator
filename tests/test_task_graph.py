import unittest
from dataclasses import replace

from orchestrator.models import ProjectSpec, TaskSpec
from orchestrator.task_graph import build_task_graph


def task_spec(
    external_id="S1.5-P1-01",
    *,
    depends_on=None,
    acceptance=None,
    project_slug="dora-context",
):
    return TaskSpec(
        external_id=external_id,
        title=f"Task {external_id}",
        project_slug=project_slug,
        cycle="S1.5",
        module="context",
        priority="P1",
        depends_on=list(depends_on or []),
        acceptance=list(acceptance or ["go test ./internal/cognition/..."]),
        verification_level=["L1", "L2"],
        risk="medium",
        agent_hint="codex",
    )


class TaskGraphTest(unittest.TestCase):
    def test_task_spec_defaults_to_claude_agent(self):
        task = TaskSpec(
            external_id="S1.5-P1-01",
            title="Task",
            project_slug="dora-context",
            cycle="S1.5",
            module="context",
            priority="P1",
        )

        self.assertEqual(task.agent_hint, "claude")

    def test_source_hash_is_stable(self):
        task = task_spec()
        spec = ProjectSpec(project_slug="dora-context", title="Dora Context", tasks=[task])
        graph_a = build_task_graph(spec)
        graph_b = build_task_graph(spec)
        self.assertEqual(graph_a.tasks[0].source_hash, graph_b.tasks[0].source_hash)

    def test_source_hash_changes_when_acceptance_changes(self):
        task_a = task_spec()
        task_b = replace(task_a, acceptance=["go test ./..."])
        graph_a = build_task_graph(ProjectSpec("dora-context", "Dora Context", [task_a]))
        graph_b = build_task_graph(ProjectSpec("dora-context", "Dora Context", [task_b]))
        self.assertNotEqual(graph_a.tasks[0].source_hash, graph_b.tasks[0].source_hash)

    def test_duplicate_external_ids_fail(self):
        task = task_spec()
        spec = ProjectSpec(project_slug="dora-context", title="Dora Context", tasks=[task, task])
        with self.assertRaisesRegex(ValueError, "duplicate external_id"):
            build_task_graph(spec)

    def test_missing_dependency_fails(self):
        task = task_spec(depends_on=["S1.5-P1-99"])
        spec = ProjectSpec(project_slug="dora-context", title="Dora Context", tasks=[task])
        with self.assertRaisesRegex(ValueError, "depends on missing task"):
            build_task_graph(spec)

    def test_dependency_cycle_fails(self):
        task_a = task_spec("S1.5-P1-01", depends_on=["S1.5-P1-02"])
        task_b = task_spec("S1.5-P1-02", depends_on=["S1.5-P1-01"])
        spec = ProjectSpec(project_slug="dora-context", title="Dora Context", tasks=[task_a, task_b])
        with self.assertRaisesRegex(ValueError, "dependency cycle detected"):
            build_task_graph(spec)

    def test_task_project_slug_must_match_project(self):
        task = task_spec(project_slug="other-project")
        spec = ProjectSpec(project_slug="dora-context", title="Dora Context", tasks=[task])
        with self.assertRaisesRegex(ValueError, "does not match project"):
            build_task_graph(spec)
