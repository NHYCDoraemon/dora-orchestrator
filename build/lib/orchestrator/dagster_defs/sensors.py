"""Dagster sensors for Dora orchestration.

Phase 2: polls Plane for ready tasks and triggers automatic execution.
"""

from dagster import RunRequest, sensor

from orchestrator.config import load_config
from orchestrator.plane_backends import create_plane_client


@sensor(job_name="run_ready_batch_task", minimum_interval_seconds=120)
def plane_ready_task_sensor(context):
    """Poll Plane every ~2 min for issues whose dependencies are Done."""
    config = load_config()
    if not config.project_slug:
        context.log.info("sensor: no ORCHESTRATOR_PROJECT_SLUG — skipping")
        return

    client = create_plane_client(config)
    issue = client.next_ready_issue(config.project_slug)
    if issue is None:
        context.log.info("sensor: no ready issues for %s", config.project_slug)
        return

    external_id = str(issue["external_id"])
    context.log.info("sensor: found ready issue %s", external_id)
    yield RunRequest(
        run_key=external_id,
        run_config={
            "ops": {
                "ready_batch_task_run": {
                    "config": {
                        "project_slug": config.project_slug,
                        "external_id": external_id,
                    }
                }
            }
        },
    )


SENSORS = [plane_ready_task_sensor]
