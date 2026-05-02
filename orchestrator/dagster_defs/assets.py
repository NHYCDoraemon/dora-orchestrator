"""Dagster assets for the initial Dora orchestration graph."""

import os
from dataclasses import asdict

from dagster import asset

from orchestrator.batch_audit import audit_task_issue_batch
from orchestrator.batch_submit import submit_task_issue_batch
from orchestrator.config import load_config
from orchestrator.plane_backends import create_plane_client
from orchestrator.plane_provisioner import provision_project
from orchestrator.run_ready_task import (
    run_ready_batch_task as run_one_ready_batch_task,
    run_ready_task as run_one_ready_task,
)
from orchestrator.spec_loader import load_project_spec
from orchestrator.task_graph import build_task_graph


@asset
def project_spec():
    return load_project_spec(load_config().spec_path)


@asset
def task_graph(project_spec):
    return build_task_graph(project_spec)


@asset
def plane_space(task_graph):
    return provision_project(create_plane_client(load_config()), task_graph)


@asset
def task_issue_batch_audit():
    config = load_config()
    if config.batch_path is None:
        raise ValueError("ORCHESTRATOR_BATCH_PATH is required for task_issue_batch_audit")
    return asdict(
        audit_task_issue_batch(
            config.batch_path,
            repo_root=config.target_repo,
            write_generated=True,
        )
    )


@asset
def task_issue_batch_submission(task_issue_batch_audit):
    config = load_config()
    if task_issue_batch_audit["status"] == "FAIL":
        raise ValueError("cannot submit batch with failing audit")
    if config.batch_path is None:
        raise ValueError("ORCHESTRATOR_BATCH_PATH is required for task_issue_batch_submission")
    if not config.project_slug or not config.project_title:
        raise ValueError("ORCHESTRATOR_PROJECT_SLUG and ORCHESTRATOR_PROJECT_TITLE are required for task_issue_batch_submission")
    return submit_task_issue_batch(
        config.batch_path,
        repo_root=config.target_repo,
        project_slug=config.project_slug,
        project_title=config.project_title,
        plane_client=create_plane_client(config),
    )


@asset
def ready_task_run():
    config = load_config()
    run_id = os.environ.get("ORCHESTRATOR_RUN_ID", "dagster-dry-run")
    return run_one_ready_task(config, run_id=run_id)


@asset
def ready_batch_task_run():
    config = load_config()
    run_id = os.environ.get("ORCHESTRATOR_RUN_ID", "dagster-dry-run")
    return run_one_ready_batch_task(config, run_id=run_id)
