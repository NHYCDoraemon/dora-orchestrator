"""Per-project Dagster Definitions factory.

``build_project_defs(cfg)`` produces a ``Definitions`` object containing one
job, one schedule, and a handful of UI assets -- all name-spaced by the
project slug so multiple business repos can coexist in a single Dagster
code location.

The single job delegates all business logic to
``orchestrator.run_ready_task.run_ready_batch_task`` via the thin op
built by ``orchestrator.dagster_defs.ops.build_single_op``.
"""

from pathlib import Path

from dagster import (
    AssetExecutionContext,
    DagsterRunStatus,
    DefaultScheduleStatus,
    Definitions,
    MaterializeResult,
    MetadataValue,
    RetryPolicy,
    RunRequest,
    RunsFilter,
    ScheduleEvaluationContext,
    SkipReason,
    asset,
    job,
    schedule,
)

from .ops import build_single_op
from .project_config import ProjectConfig

__all__ = ["ProjectConfig", "build_project_defs"]


_ACTIVE_RUN_STATUSES = (
    DagsterRunStatus.QUEUED,
    DagsterRunStatus.NOT_STARTED,
    DagsterRunStatus.STARTING,
    DagsterRunStatus.STARTED,
)


def build_project_defs(cfg: ProjectConfig) -> Definitions:
    """Build per-project job + schedule + manual UI assets."""

    run_op = build_single_op(cfg)
    op_retry = RetryPolicy(max_retries=2, delay=5)

    job_name = f"{cfg.safe_name}_run_ready_batch_task"
    schedule_name = f"{cfg.safe_name}_every_{_cron_label(cfg.schedule_cron)}"
    concurrency_key = f"orch_{cfg.safe_name}"

    @job(
        name=job_name,
        description=(
            f"Drive ready batch tasks on '{cfg.slug}' through claim -> "
            f"execute -> verify -> release -> deliver. "
            f"Lock guarded by Plane state + Dagster active-run check; "
            f"max_runtime={cfg.max_runtime_seconds}s."
        ),
        tags={
            "dagster/max_runtime": str(cfg.max_runtime_seconds),
            "dagster/concurrency_key": concurrency_key,
            f"{cfg.slug}/job_kind": "run_ready_batch_task",
        },
    )
    def run_job():
        run_op.with_retry_policy(op_retry)()

    @schedule(
        name=schedule_name,
        cron_schedule=cfg.schedule_cron,
        job=run_job,
        execution_timezone=cfg.schedule_timezone,
        default_status=(
            DefaultScheduleStatus.RUNNING
            if cfg.schedule_enabled
            else DefaultScheduleStatus.STOPPED
        ),
        description=(
            f"Every {cfg.schedule_cron}: skip if any active run for "
            f"{job_name} exists OR if Plane has no ready issue; otherwise "
            f"launch one."
        ),
    )
    def run_schedule(context: ScheduleEvaluationContext):
        active = context.instance.get_runs(
            filters=RunsFilter(
                job_name=job_name,
                statuses=list(_ACTIVE_RUN_STATUSES),
            ),
            limit=1,
        )
        if active:
            run = active[0]
            return SkipReason(
                f"{job_name} has an active run (id={run.run_id[:8]}, "
                f"status={run.status.value}); skip this tick."
            )
        # Probe Plane before firing. The job inside the run would also
        # bail with no_ready, but each empty Dagster run still spawns a
        # code-server subprocess and burns the operator's prompt cache
        # window. When all batches are Done the schedule must be
        # genuinely silent — not "fires every tick and bails internally".
        ready_id = _probe_next_ready(cfg)
        if ready_id is None:
            return SkipReason(
                f"{cfg.slug}: no ready issue in Plane; skip empty run."
            )
        if ready_id == "_probe_failed":
            # Plane unreachable / config missing → don't fire (would just
            # crash the same way inside the op) and don't loud-fail the
            # schedule (would page the operator on a transient outage).
            return SkipReason(
                f"{cfg.slug}: plane probe failed; skip until next tick."
            )
        return RunRequest(
            run_key=None,
            tags={
                f"{cfg.slug}/triggered_by": "schedule",
                f"{cfg.slug}/probe_ready_id": str(ready_id),
            },
        )

    base_defs = Definitions(
        jobs=[run_job],
        schedules=[run_schedule],
        assets=[
            _make_status_asset(cfg),
            _make_reset_lease_asset(cfg),
            _make_list_worktrees_asset(cfg),
        ],
    )

    if cfg.qa_enabled:
        from .qa import build_qa_definitions

        return Definitions.merge(base_defs, build_qa_definitions(cfg))
    return base_defs


def _probe_next_ready(cfg: ProjectConfig) -> str | None:
    """Best-effort Plane probe used by the schedule to skip empty ticks.

    Returns the ``external_id`` of the next ready issue, ``None`` when
    Plane returned an empty queue, or the sentinel string
    ``"_probe_failed"`` when the probe could not run (Plane unreachable,
    settings incomplete, transport error). Probing failure is intentionally
    NOT raised: a transient Plane outage should silently skip the tick,
    not fail-loud the schedule and page the operator.
    """
    try:
        from .plane_helpers import per_project_plane_client
        client = per_project_plane_client(cfg)
        ready = client.next_ready_issue(cfg.slug)
    except Exception:
        return "_probe_failed"
    if ready is None:
        return None
    return str(ready.get("external_id", "")) or "_probe_failed"


def _cron_label(cron: str) -> str:
    """``*/2 * * * *`` -> ``2m``, ``*/30 * * * *`` -> ``30m``, fallback -> ``cron``."""
    parts = cron.split()
    if len(parts) >= 1 and parts[0].startswith("*/") and parts[0][2:].isdigit():
        return f"{parts[0][2:]}m"
    return "cron"


# ---------------------------------------------------------------------------
# Per-project UI assets
# ---------------------------------------------------------------------------


def _make_status_asset(cfg: ProjectConfig):
    @asset(
        name=f"{cfg.safe_name}_status",
        group_name=f"{cfg.safe_name}_orchestrator",
        description=(
            f"Read-only snapshot of '{cfg.slug}' Plane queue: ready/in-progress/done "
            f"counts, current claimed task (if any), worktrees on disk."
        ),
    )
    def status(context: AssetExecutionContext) -> MaterializeResult:
        from .plane_helpers import per_project_plane_client

        client = per_project_plane_client(cfg)
        ready = client.next_ready_issue(cfg.slug)
        worktree_dir = cfg.worktree_root / cfg.slug
        worktrees = (
            sorted(p.name for p in worktree_dir.iterdir() if p.is_dir())
            if worktree_dir.exists()
            else []
        )
        meta = {
            "slug": MetadataValue.text(cfg.slug),
            "title": MetadataValue.text(cfg.title),
            "repo_root": MetadataValue.path(str(cfg.repo_root)),
            "next_ready_external_id": MetadataValue.text(
                str(ready["external_id"]) if ready else "(none)"
            ),
            "next_ready_priority": MetadataValue.text(
                str(ready.get("priority", "")) if ready else "-"
            ),
            "worktrees_on_disk": MetadataValue.json(worktrees),
            "schedule_cron": MetadataValue.text(cfg.schedule_cron),
        }
        for k, v in meta.items():
            context.log.info(f"  {k}: {v.value if hasattr(v, 'value') else v}")
        return MaterializeResult(metadata=meta)

    return status


def _make_reset_lease_asset(cfg: ProjectConfig):
    @asset(
        name=f"{cfg.safe_name}_reset_lease",
        group_name=f"{cfg.safe_name}_orchestrator",
        description=(
            f"Recovery: any task that's been In Progress for >60 min is force-released "
            f"back to Backlog with a `needs:review` label, so the schedule can re-pick it. "
            f"Use after a Dagster run was killed mid-task."
        ),
    )
    def reset_lease(context: AssetExecutionContext) -> MaterializeResult:
        from datetime import datetime, timezone

        from .plane_helpers import per_project_plane_client

        client = per_project_plane_client(cfg)
        api = client.api
        path = (
            f"/api/v1/workspaces/{client.settings.workspace_slug}/projects/"
            f"{client.project_id}/issues/"
        )
        now = datetime.now(timezone.utc)
        reset_count = 0
        details: list[dict] = []
        states = client._states()
        in_progress_id = states.get("In Progress")
        backlog_id = states.get("Backlog")
        for issue in api.paginate_v1(path):
            if issue.get("state") != in_progress_id:
                continue
            updated = issue.get("updated_at") or ""
            try:
                updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except ValueError:
                continue
            age_min = (now - updated_dt).total_seconds() / 60
            if age_min < 60:
                continue
            issue_id = issue["id"]
            api.v1(
                "PATCH",
                f"/api/v1/workspaces/{client.settings.workspace_slug}/projects/"
                f"{client.project_id}/issues/{issue_id}/",
                {"state": backlog_id, "assignees": []},
            )
            client.add_label(cfg.slug, issue.get("external_id", ""), "needs:review")
            reset_count += 1
            details.append(
                {"external_id": issue.get("external_id"), "age_min": int(age_min)}
            )
        context.log.info(f"reset {reset_count} stale lease(s)")
        return MaterializeResult(
            metadata={
                "reset_count": MetadataValue.int(reset_count),
                "details": MetadataValue.json(details),
            }
        )

    return reset_lease


def _make_list_worktrees_asset(cfg: ProjectConfig):
    @asset(
        name=f"{cfg.safe_name}_worktrees",
        group_name=f"{cfg.safe_name}_orchestrator",
        description=(
            f"List git worktrees this orchestrator created for '{cfg.slug}' under "
            f"{cfg.worktree_root / cfg.slug}. Useful for cleanup planning."
        ),
    )
    def list_worktrees(context: AssetExecutionContext) -> MaterializeResult:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(cfg.repo_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        rows: list[dict] = []
        current: dict = {}
        for line in proc.stdout.splitlines():
            if not line.strip():
                if current:
                    rows.append(current)
                    current = {}
                continue
            if " " in line:
                key, value = line.split(" ", 1)
                current[key] = value
            else:
                current[line] = True
        if current:
            rows.append(current)
        return MaterializeResult(
            metadata={
                "worktree_count": MetadataValue.int(len(rows)),
                "worktrees": MetadataValue.json(rows),
            }
        )

    return list_worktrees
