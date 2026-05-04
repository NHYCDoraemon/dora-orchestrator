"""Load `ProjectConfig`s from `~/.dora/orchestrator/projects/*.json`.

Each file is one orchestrated business repo. Format:

    {
      "slug": "dora",
      "title": "Dora",
      "repo_root": "/path/to/your-project",
      "plane_project_id": "c185b980-...",
      "plane_workspace_slug": "doraemon",
      "schedule_cron": "*/2 * * * *",
      "schedule_timezone": "Asia/Shanghai",
      "default_executor": "noop",
      "max_runtime_seconds": 3600,
      "git_branch_prefix": "orchestrator",
      "git_base_branch": "main",
      "enable_push": false,
      "enable_pr": false
    }

`slug`, `title`, and `repo_root` are required; everything else has a default.
"""

import json
import os
from pathlib import Path

from .project_config import ProjectConfig


DEFAULT_CONFIG_DIR = Path.home() / ".dora" / "orchestrator" / "projects"


def load_project_configs(config_dir: Path | None = None) -> list[ProjectConfig]:
    """Load every `*.json` under `config_dir` (default `~/.dora/orchestrator/projects/`).

    Files that fail to parse are skipped with a warning to stderr — we don't want
    a single bad config to break the whole Dagster code location.
    """
    config_dir = (config_dir or DEFAULT_CONFIG_DIR).expanduser()
    if not config_dir.exists():
        return []
    configs: list[ProjectConfig] = []
    for path in sorted(config_dir.glob("*.json")):
        try:
            configs.append(_parse_config_file(path))
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"warn: skipping {path}: {exc}", file=os.sys.stderr)
    return configs


def _parse_config_file(path: Path) -> ProjectConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    for required in ("slug", "title", "repo_root"):
        if not data.get(required):
            raise ValueError(f"{path}: missing required field {required!r}")
    repo_root = Path(data["repo_root"]).expanduser()
    worktree_root = (
        Path(data["worktree_root"]).expanduser()
        if data.get("worktree_root")
        else Path.home() / ".dora" / "orchestrator" / "worktrees"
    )
    return ProjectConfig(
        slug=str(data["slug"]),
        title=str(data["title"]),
        repo_root=repo_root,
        plane_project_id=str(data.get("plane_project_id") or ""),
        plane_workspace_slug=str(data.get("plane_workspace_slug") or ""),
        schedule_cron=str(data.get("schedule_cron") or "*/2 * * * *"),
        schedule_timezone=str(data.get("schedule_timezone") or "Asia/Shanghai"),
        default_executor=str(data.get("default_executor") or "noop"),
        max_runtime_seconds=int(data.get("max_runtime_seconds") or 3600),
        git_branch_prefix=str(data.get("git_branch_prefix") or "orchestrator"),
        git_base_branch=str(data.get("git_base_branch") or "main"),
        worktree_root=worktree_root,
        enable_push=bool(data.get("enable_push") or False),
        enable_pr=bool(data.get("enable_pr") or False),
        qa_enabled=bool(data.get("qa_enabled") or False),
    )
