"""Dagster assets for the initial Dora orchestration graph."""

from pathlib import Path

from dagster import asset

from dora_orchestrator.plane_provisioner import provision_project
from dora_orchestrator.spec_loader import load_project_spec
from dora_orchestrator.task_graph import build_task_graph


@asset
def project_spec():
    return load_project_spec(Path("examples/dora.orchestration.json"))


@asset
def task_graph(project_spec):
    return build_task_graph(project_spec)


@asset
def plane_space(task_graph):
    class DryRunPlaneClient:
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

    return provision_project(DryRunPlaneClient(), task_graph)
