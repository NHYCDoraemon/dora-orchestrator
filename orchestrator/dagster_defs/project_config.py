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
    schedule_enabled: bool = False
    default_executor: str = "codex"
    codex_home: Path = field(default_factory=lambda: Path.home() / ".codex")
    max_runtime_seconds: int = 3600
    git_branch_prefix: str = "orchestrator"
    git_base_branch: str = "main"
    worktree_root: Path = field(
        default_factory=lambda: Path.home() / ".dora" / "orchestrator" / "worktrees"
    )
    enable_push: bool = False
    enable_pr: bool = False
    qa_enabled: bool = False

    @property
    def safe_name(self) -> str:
        """Slug coerced to a Dagster-legal identifier ([A-Za-z0-9_]+).

        ``ProjectConfig.slug`` is the Plane / repo slug — it may contain
        hyphens (e.g. ``process-engine``), which Dagster rejects in op /
        job / schedule / asset names. Use this for any identifier handed
        to a Dagster decorator. Plane-facing calls and free-form tag
        VALUES keep the original ``slug``.
        """
        return self.slug.replace("-", "_")
