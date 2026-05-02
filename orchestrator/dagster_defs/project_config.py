"""ProjectConfig dataclass — no dagster dependency.

Lives in its own module so `loader.py` and tests can import without dagster.
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ProjectConfig:
    """Configuration for one orchestrated business repository.

    Loaded from `~/.dora/orchestrator/projects/<slug>.json`. The slug is the
    Plane project_slug; everything else is local-to-this-machine wiring.
    """

    slug: str
    title: str
    repo_root: Path
    plane_project_id: str = ""
    plane_workspace_slug: str = ""
    schedule_cron: str = "*/2 * * * *"
    schedule_timezone: str = "Asia/Shanghai"
    default_executor: str = "noop"
    max_runtime_seconds: int = 3600
    git_branch_prefix: str = "orchestrator"
    git_base_branch: str = "main"
    worktree_root: Path = field(
        default_factory=lambda: Path.home() / ".dora" / "orchestrator" / "worktrees"
    )
    enable_push: bool = False
    enable_pr: bool = False
