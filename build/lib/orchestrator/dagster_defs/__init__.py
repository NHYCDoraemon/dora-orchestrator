"""Dagster entry points for orchestrator-driven business repos.

Public API:

    from orchestrator.dagster_defs import (
        ProjectConfig,                       # always importable (no dagster needed)
        load_project_configs,                # always importable
        build_project_defs,                  # requires dagster
        build_orchestrated_projects_defs,    # requires dagster
    )

`build_orchestrated_projects_defs(config_dir)` is the high-level helper meant
for `~/dagster/infra_ops/__init__.py`: it loads every config under
`~/.dora/orchestrator/projects/*.json`, builds per-project Definitions, and
merges them into a single object ready to be combined with the user's other
Dagster code.

Functions that need `dagster` import it lazily so this package stays usable
in environments where dagster isn't installed (eg. the orchestrator's own
test suite without the `dagster` extra).
"""

from pathlib import Path

from .loader import DEFAULT_CONFIG_DIR, load_project_configs
from .project_config import ProjectConfig


def build_project_defs(cfg: ProjectConfig):
    """Per-project Definitions. Requires `dagster` to be installed."""
    from .factory import build_project_defs as _build

    return _build(cfg)


def build_orchestrated_projects_defs(config_dir: Path | None = None):
    """Load all project configs and merge them into one Definitions."""
    from dagster import Definitions

    configs = load_project_configs(config_dir)
    if not configs:
        return Definitions()
    parts = [build_project_defs(cfg) for cfg in configs]
    if len(parts) == 1:
        return parts[0]
    return Definitions.merge(*parts)


__all__ = [
    "DEFAULT_CONFIG_DIR",
    "ProjectConfig",
    "build_orchestrated_projects_defs",
    "build_project_defs",
    "load_project_configs",
]
