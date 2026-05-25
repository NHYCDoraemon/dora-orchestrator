"""Runtime configuration for the orchestrator."""

import os
from dataclasses import dataclass
from pathlib import Path

from .project_resolver import resolve_project_config


@dataclass(frozen=True)
class OrchestratorConfig:
    spec_path: Path
    target_repo: Path
    executor: str = "claude"
    plane_backend: str = "memory"
    batch_path: Path | None = None
    project_slug: str = ""
    project_title: str = ""
    executor_env: dict[str, str] | None = None


def load_config(environ: dict[str, str] | None = None) -> OrchestratorConfig:
    env = os.environ if environ is None else environ
    spec_path = Path(env.get("ORCHESTRATOR_SPEC", "examples/dora.orchestration.json"))
    executor = env.get("ORCHESTRATOR_EXECUTOR", "claude")
    plane_backend = env.get("ORCHESTRATOR_PLANE_BACKEND", "memory")

    batch_path = env.get("ORCHESTRATOR_BATCH_PATH")
    resolved_batch = Path(batch_path).expanduser().resolve() if batch_path else None

    # Discover project config from env or auto-discovery
    target_repo = env.get("DORA_TARGET_REPO")
    project_slug = env.get("ORCHESTRATOR_PROJECT_SLUG", "")
    project_title = env.get("ORCHESTRATOR_PROJECT_TITLE", "")

    if not target_repo or not project_slug:
        resolved = resolve_project_config(
            repo=target_repo,
            project_slug=project_slug or None,
            project_title=project_title or None,
        )
        target_repo = str(resolved.repo_root)
        if not project_slug:
            project_slug = resolved.project_slug
        if not project_title:
            project_title = resolved.project_title

    return OrchestratorConfig(
        spec_path=spec_path.expanduser().resolve(),
        target_repo=Path(target_repo).expanduser().resolve(),
        executor=executor,
        plane_backend=plane_backend,
        batch_path=resolved_batch,
        project_slug=project_slug,
        project_title=project_title,
    )
