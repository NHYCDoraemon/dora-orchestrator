"""Dagster Definitions for Dora orchestration."""

from dagster import Definitions

from dora_orchestrator.dagster_defs.assets import plane_space, project_spec, task_graph
from dora_orchestrator.dagster_defs.jobs import provision_project
from dora_orchestrator.dagster_defs.sensors import SENSORS

defs = Definitions(
    assets=[project_spec, task_graph, plane_space],
    jobs=[provision_project],
    sensors=SENSORS,
)
