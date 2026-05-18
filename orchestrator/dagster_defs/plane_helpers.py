"""Helpers shared by ops.py / factory.py / qa.py for per-project Plane wiring.

`LivePlaneSettings.from_env()` only knows ONE PLANE_PROJECT_ID (and one
workspace_slug) — read from env + `~/.dora/plane.env`. With multiple
projects in the registry (dora + process-engine + ...) every Dagster job
that uses the env-resolved settings would route to the SAME Plane project,
regardless of which `ProjectConfig` triggered it. That's the bug a 2026-05-18
process-engine smoke run surfaced: the daemon-spawned job for process-engine
queried dora's Plane project and always got `no_ready`.

`per_project_plane_client(cfg)` reads the env defaults, then layers the
project registry's `plane_project_id` / `plane_workspace_slug` on top when
they're non-empty. Empty fields fall back to the env (preserves single-project
setups that never filled the registry).
"""

import dataclasses

from orchestrator.plane_live import LivePlaneClient, LivePlaneSettings

from .project_config import ProjectConfig


def per_project_plane_client(cfg: ProjectConfig) -> LivePlaneClient:
    settings = LivePlaneSettings.from_env()
    if cfg.plane_project_id:
        settings = dataclasses.replace(settings, project_id=cfg.plane_project_id)
    if cfg.plane_workspace_slug:
        settings = dataclasses.replace(settings, workspace_slug=cfg.plane_workspace_slug)
    return LivePlaneClient(settings)
