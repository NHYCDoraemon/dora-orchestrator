import unittest
from dataclasses import replace

from orchestrator.models import ProjectSpec, TaskSpec
from orchestrator.plane_provisioner import provision_project
from orchestrator.task_graph import build_task_graph


class FakePlaneClient:
    def __init__(self):
        self.projects = {}
        self.cycles = {}
        self.modules = {}
        self.issues = {}

    def upsert_project(self, slug, title):
        self.projects[slug] = {"slug": slug, "title": title}
        return self.projects[slug]

    def upsert_cycle(self, project_slug, name):
        self.cycles[(project_slug, name)] = {"project_slug": project_slug, "name": name}
        return self.cycles[(project_slug, name)]

    def upsert_module(self, project_slug, name):
        self.modules[(project_slug, name)] = {"project_slug": project_slug, "name": name}
        return self.modules[(project_slug, name)]

    def upsert_issue(self, project_slug, external_id, payload):
        self.issues[(project_slug, external_id)] = dict(payload)
        return self.issues[(project_slug, external_id)]


def project_spec(task_title="Compile context assembly baseline"):
    task = TaskSpec(
        external_id="S1.5-P1-01",
        title=task_title,
        project_slug="dora-context",
        cycle="S1.5",
        module="context",
        priority="P1",
        depends_on=[],
        acceptance=["go test ./internal/cognition/..."],
        verification_level=["L1", "L2"],
        risk="medium",
        agent_hint="noop",
    )
    return ProjectSpec(project_slug="dora-context", title="Dora Context", tasks=[task])


class PlaneProvisionerTest(unittest.TestCase):
    def test_provision_project_is_idempotent(self):
        client = FakePlaneClient()
        graph = build_task_graph(project_spec())
        result_a = provision_project(client, graph)
        result_b = provision_project(client, graph)

        self.assertEqual(result_a, {"projects": 1, "cycles": 1, "modules": 1, "issues": 1})
        self.assertEqual(result_a, result_b)
        self.assertEqual(len(client.projects), 1)
        self.assertEqual(len(client.cycles), 1)
        self.assertEqual(len(client.modules), 1)
        self.assertEqual(len(client.issues), 1)

    def test_changed_task_updates_issue_payload(self):
        client = FakePlaneClient()
        initial = build_task_graph(project_spec())
        provision_project(client, initial)

        changed_task = replace(project_spec().tasks[0], title="Updated title")
        changed = build_task_graph(ProjectSpec("dora-context", "Dora Context", [changed_task]))
        provision_project(client, changed)

        issue = client.issues[("dora-context", "S1.5-P1-01")]
        self.assertEqual(issue["name"], "Updated title")
        self.assertEqual(len(client.issues), 1)

    def test_issue_payload_contains_source_hash_and_execution_policy(self):
        client = FakePlaneClient()
        graph = build_task_graph(project_spec())
        provision_project(client, graph)

        issue = client.issues[("dora-context", "S1.5-P1-01")]
        self.assertEqual(len(issue["source_hash"]), 64)
        self.assertEqual(issue["agent_hint"], "noop")
        self.assertEqual(issue["verification_level"], ["L1", "L2"])

    def test_provisions_project_modules_even_without_matching_tasks(self):
        client = FakePlaneClient()
        spec = ProjectSpec(
            project_slug="dora-context",
            title="Dora Context",
            tasks=project_spec().tasks,
            modules=["product", "architecture", "planning"],
        )

        provision_project(client, build_task_graph(spec))

        self.assertIn(("dora-context", "product"), client.modules)
        self.assertIn(("dora-context", "architecture"), client.modules)
        self.assertIn(("dora-context", "planning"), client.modules)
        self.assertIn(("dora-context", "context"), client.modules)
