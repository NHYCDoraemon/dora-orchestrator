"""Dagster Definitions for Dora orchestration."""

from dagster import Definitions

from orchestrator.dagster_defs.assets import (
    plane_space,
    project_spec,
    ready_batch_task_run,
    ready_task_run,
    task_graph,
    task_issue_batch_audit,
    task_issue_batch_submission,
)
from orchestrator.dagster_defs.jobs import (
    audit_batch,
    provision_project,
    run_ready_batch_task,
    run_ready_task,
    submit_batch,
)
from orchestrator.dagster_defs.sensors import SENSORS

defs = Definitions(
    assets=[
        project_spec,
        task_graph,
        plane_space,
        task_issue_batch_audit,
        task_issue_batch_submission,
        ready_task_run,
        ready_batch_task_run,
    ],
    jobs=[audit_batch, submit_batch, provision_project, run_ready_task, run_ready_batch_task],
    sensors=SENSORS,
)
