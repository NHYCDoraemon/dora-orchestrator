"""Thin Dagster op that delegates to orchestrator core.

The single op ``run_orchestrator`` calls ``run_ready_batch_task`` from
``orchestrator.run_ready_task``, passing a ``DeliveryConfig`` built from
``ProjectConfig`` so git delivery steps (worktree, commit, push, PR, merge)
run after each task.

All business logic lives in orchestrator core. This module is the
scheduling shell: claim, execute, verify, release, delivery, and batch-page
refresh are handled by ``run_ready_batch_task`` / ``_execute_one_task``.
"""

import json
import os
import subprocess
from pathlib import Path

from .project_config import ProjectConfig


def _format_progress_log(event: str, data: dict) -> str:
    if event == "executor_output":
        summary = data.get("summary")
        return str(summary) if summary is not None else ""
    return json.dumps({"event": event, **data}, ensure_ascii=False)


def _executor_env_for(executor: str, cfg: ProjectConfig) -> dict[str, str] | None:
    env: dict[str, str] = {}
    if executor == "codex":
        env["CODEX_HOME"] = str(cfg.codex_home)

    java_home = _resolve_java17_home()
    if java_home:
        env["JAVA_HOME"] = java_home
        env["PATH"] = f"{java_home}/bin:{os.environ.get('PATH', '')}"

    return env or None


def _resolve_java17_home() -> str:
    for key in ("ORCHESTRATOR_JAVA_HOME", "JAVA17_HOME"):
        value = os.environ.get(key)
        if value:
            return value

    java_home = Path("/usr/libexec/java_home")
    if java_home.exists():
        try:
            proc = subprocess.run(
                [str(java_home), "-v", "17"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().splitlines()[-1]

    common_paths = [
        Path("/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home"),
        Path("/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"),
        Path("/usr/local/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"),
    ]
    for path in common_paths:
        if path.exists():
            return str(path)
    return ""


def build_single_op(cfg: ProjectConfig):
    """Return the single orchestrator op for *cfg*.

    The closure captures *cfg* so Dagster can name the op by project slug
    without requiring global mutable state.
    """
    from dagster import MetadataValue, OpExecutionContext, op

    @op(
        name=f"{cfg.safe_name}_run_orchestrator",
        description=(
            f"Claim, execute, verify, release, and deliver one ready batch "
            f"task for project '{cfg.slug}'. All business logic delegated "
            f"to orchestrator.run_ready_task."
        ),
        tags={
            "dagster/max_runtime": str(cfg.max_runtime_seconds),
            f"{cfg.slug}/job_kind": "run_orchestrator",
        },
    )
    def run_orchestrator(context: OpExecutionContext) -> dict:
        import os

        from orchestrator.config import OrchestratorConfig
        from orchestrator.delivery import DeliveryConfig
        from orchestrator.run_ready_task import run_ready_batch_task

        from .plane_helpers import per_project_plane_client

        client = per_project_plane_client(cfg)

        executor = os.environ.get("ORCHESTRATOR_EXECUTOR", cfg.default_executor)
        config = OrchestratorConfig(
            spec_path=cfg.repo_root / "examples" / "dora.orchestration.json",
            target_repo=cfg.repo_root,
            executor=executor,
            plane_backend=os.environ.get("ORCHESTRATOR_PLANE_BACKEND", "live"),
            project_slug=cfg.slug,
            project_title=cfg.title,
            executor_env=_executor_env_for(executor, cfg),
        )

        batch_id = _batch_id_from_env() or ""

        idle_timeout = int(
            os.environ.get(
                f"{cfg.slug.upper()}_IDLE_TIMEOUT",
                str(min(600, cfg.max_runtime_seconds // 6)),
            )
        )
        delivery = DeliveryConfig(
            repo_root=cfg.repo_root,
            project_slug=cfg.slug,
            git_branch_prefix=cfg.git_branch_prefix,
            worktree_root=cfg.worktree_root,
            base_branch=cfg.git_base_branch,
            enable_commit=True,
            enable_push=cfg.enable_push,
            enable_pr=cfg.enable_pr,
            auto_merge=True,
            idle_timeout_seconds=idle_timeout,
            hard_timeout_seconds=cfg.max_runtime_seconds,
        )

        def _log_progress(event: str, data: dict) -> None:
            context.log.info(_format_progress_log(event, data))

        result = run_ready_batch_task(
            config,
            plane_client=client,
            run_id=context.run_id,
            delivery=delivery,
            on_progress=_log_progress,
        )

        # Surface key fields as Dagster metadata for the UI.
        runs = result.get("runs", [])
        for run_entry in runs:
            ext_id = run_entry.get("external_id", "")
            if not ext_id:
                continue
            context.add_output_metadata({
                "external_id": MetadataValue.text(str(ext_id)),
                "outcome": MetadataValue.text(str(run_entry.get("outcome", ""))),
                "state": MetadataValue.text(str(run_entry.get("state", ""))),
            })
            delivery_info = run_entry.get("delivery")
            if isinstance(delivery_info, dict):
                if delivery_info.get("commit_sha"):
                    context.add_output_metadata({
                        "commit_sha": MetadataValue.text(str(delivery_info["commit_sha"])),
                        "pushed": MetadataValue.bool(bool(delivery_info.get("pushed"))),
                        "pr_url": MetadataValue.text(str(delivery_info.get("pr_url", ""))),
                        "merged": MetadataValue.bool(bool(delivery_info.get("merged"))),
                    })

        context.add_output_metadata({
            "loop_count": MetadataValue.int(result.get("loop_count", 0)),
            "top_level_outcome": MetadataValue.text(str(result.get("outcome", ""))),
        })

        try:
            last_run = runs[-1] if runs else {}
            last_ext_id = str(last_run.get("external_id", ""))
            context.instance.add_run_tags(
                context.run_id,
                {
                    f"{cfg.slug}/outcome": str(result.get("outcome", "")),
                    f"{cfg.slug}/external_id": last_ext_id,
                    f"{cfg.slug}/business_completed": "true"
                    if result.get("outcome") == "agent_done"
                    else "false",
                },
            )
        except Exception:
            pass

        return result

    return run_orchestrator


def _batch_id_from_env() -> str | None:
    import os
    return os.environ.get("ORCHESTRATOR_BATCH_ID") or None
