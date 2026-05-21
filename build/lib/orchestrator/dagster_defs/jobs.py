"""Dagster jobs for Dora orchestration."""

from dagster import AssetSelection, define_asset_job

audit_batch = define_asset_job("audit_batch", selection=AssetSelection.assets("task_issue_batch_audit"))
provision_project = define_asset_job("provision_project")
run_ready_task = define_asset_job("run_ready_task", selection=AssetSelection.assets("ready_task_run"))
run_ready_batch_task = define_asset_job(
    "run_ready_batch_task",
    selection=AssetSelection.assets("ready_batch_task_run"),
)
submit_batch = define_asset_job(
    "submit_batch",
    selection=AssetSelection.assets("task_issue_batch_audit", "task_issue_batch_submission"),
)
