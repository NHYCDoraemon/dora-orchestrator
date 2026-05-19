"""Plane backend factory."""

import dataclasses

from .config import OrchestratorConfig
from .in_memory_plane import InMemoryPlaneClient
from .local_plane import LocalPlaneClient
from .plane_live import LivePlaneClient, LivePlaneSettings
from .project_resolver import ResolvedConfig


def create_plane_client(
    config: OrchestratorConfig,
    *,
    resolved_project: ResolvedConfig | None = None,
):
    if config.plane_backend == "memory":
        return InMemoryPlaneClient()
    if config.plane_backend == "local":
        return LocalPlaneClient(config.target_repo / ".dora" / "orchestrator-plane-state.json")
    if config.plane_backend == "live":
        settings = LivePlaneSettings.from_env()
        if resolved_project is not None:
            if resolved_project.plane_project_id:
                settings = dataclasses.replace(
                    settings,
                    project_id=resolved_project.plane_project_id,
                )
            if resolved_project.plane_workspace_slug:
                settings = dataclasses.replace(
                    settings,
                    workspace_slug=resolved_project.plane_workspace_slug,
                )
        return LivePlaneClient(settings)
    raise ValueError(f"unsupported Plane backend: {config.plane_backend}")
