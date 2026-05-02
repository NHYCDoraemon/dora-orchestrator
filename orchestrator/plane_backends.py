"""Plane backend factory."""

from .config import OrchestratorConfig
from .in_memory_plane import InMemoryPlaneClient
from .local_plane import LocalPlaneClient
from .plane_live import LivePlaneClient, LivePlaneSettings


def create_plane_client(config: OrchestratorConfig):
    if config.plane_backend == "memory":
        return InMemoryPlaneClient()
    if config.plane_backend == "local":
        return LocalPlaneClient(config.target_repo / ".dora" / "orchestrator-plane-state.json")
    if config.plane_backend == "live":
        return LivePlaneClient(LivePlaneSettings.from_env())
    raise ValueError(f"unsupported Plane backend: {config.plane_backend}")
