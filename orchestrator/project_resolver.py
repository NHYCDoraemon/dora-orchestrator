"""Resolve project config from .dora/project.json, project registry, or CLI args.

Priority: explicit CLI args > --project <slug> registry > cwd/.dora/project.json discovery.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

REGISTRY_DIR = Path.home() / ".dora" / "orchestrator" / "projects"
BATCHES_DIR = Path("docs") / "dora" / "batches"


@dataclass(frozen=True)
class ResolvedConfig:
    repo_root: Path
    project_slug: str
    project_title: str
    batch_path: Path | None = None


@dataclass(frozen=True)
class BatchSummary:
    batch_id: str
    title: str
    task_count: int
    path: Path


def resolve_project_config(
    *,
    batch_id: str | None = None,
    repo: str | None = None,
    project_slug: str | None = None,
    project_title: str | None = None,
    project: str | None = None,
    registry_dir: Path | None = None,
) -> ResolvedConfig:
    """Resolve project configuration from available sources.

    Priority: explicit args > --project registry lookup > cwd .dora/project.json.
    """
    repo_root = Path(repo).expanduser().resolve() if repo else None
    slug = project_slug
    title = project_title

    # Try --project <slug> registry lookup
    if project:
        entry = _load_registry_entry(project, registry_dir=registry_dir)
        if entry:
            if repo_root is None:
                repo_root = entry.get("repo_root")
            if slug is None:
                slug = entry.get("slug")
            if title is None:
                title = entry.get("title")

    # Try cwd .dora/project.json discovery
    if repo_root is None or slug is None or title is None:
        discovered = _discover_from_cwd()
        if discovered:
            if repo_root is None:
                repo_root = discovered.get("repo_root")
            if slug is None:
                slug = discovered.get("slug")
            if title is None:
                title = discovered.get("title")

    if repo_root is None:
        repo_root = Path(".").resolve()
    if slug is None:
        slug = ""
    if title is None:
        title = ""

    # Resolve batch path
    batch_path = None
    if batch_id:
        batch_path = (repo_root / BATCHES_DIR / batch_id).resolve()

    return ResolvedConfig(
        repo_root=repo_root,
        project_slug=slug,
        project_title=title,
        batch_path=batch_path,
    )


def list_available_batches(repo_root: Path) -> list[BatchSummary]:
    """Scan `<repo_root>/docs/dora/batches/*/batch.md` and return summaries."""
    batches_dir = (repo_root / BATCHES_DIR).resolve()
    if not batches_dir.exists():
        return []
    summaries: list[BatchSummary] = []
    for batch_dir in sorted(batches_dir.iterdir()):
        if not batch_dir.is_dir():
            continue
        summary = _read_batch_summary(batch_dir)
        if summary:
            summaries.append(summary)
    return summaries


def _discover_from_cwd() -> dict | None:
    """Walk up from cwd to find .dora/project.json, return repo_root/slug/title."""
    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        project_json = parent / ".dora" / "project.json"
        if project_json.exists():
            try:
                data = json.loads(project_json.read_text(encoding="utf-8"))
                return {
                    "repo_root": parent,
                    "slug": data.get("project_slug", ""),
                    "title": data.get("title", ""),
                }
            except (json.JSONDecodeError, OSError):
                return None
    return None


def _load_registry_entry(project_slug: str, registry_dir: Path | None = None) -> dict | None:
    """Load a project entry from `~/.dora/orchestrator/projects/<slug>.json`."""
    base = registry_dir or REGISTRY_DIR
    entry_path = base / f"{project_slug}.json"
    if not entry_path.exists():
        return None
    try:
        data = json.loads(entry_path.read_text(encoding="utf-8"))
        return {
            "repo_root": Path(data["repo_root"]).expanduser().resolve()
            if data.get("repo_root")
            else None,
            "slug": data.get("slug", project_slug),
            "title": data.get("title", ""),
        }
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def _read_batch_summary(batch_dir: Path) -> BatchSummary | None:
    """Parse batch.md frontmatter for summary info."""
    batch_md = batch_dir / "batch.md"
    if not batch_md.exists():
        return None
    try:
        doc = _parse_markdown_frontmatter(batch_md)
    except (ValueError, OSError):
        return None
    meta = doc["metadata"]
    batch_id = str(meta.get("batch_id", batch_dir.name))
    title = str(meta.get("title", ""))
    tasks_dir = batch_dir / "tasks"
    task_count = len(list(tasks_dir.glob("*.md"))) if tasks_dir.exists() else 0
    return BatchSummary(
        batch_id=batch_id,
        title=title,
        task_count=task_count,
        path=batch_dir.resolve(),
    )


def _parse_markdown_frontmatter(path: Path) -> dict:
    """Parse a markdown file with YAML frontmatter.

    Returns {"metadata": dict, "body": str}.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"missing YAML frontmatter: {path}")
    try:
        end = next(
            i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ValueError(f"unterminated YAML frontmatter: {path}") from exc

    metadata = _parse_simple_yaml(lines[1:end])
    body = "\n".join(lines[end + 1 :]).strip() + "\n"
    return {"metadata": metadata, "body": body}


def _parse_simple_yaml(lines: list[str]) -> dict[str, object]:
    data: dict[str, object] = {}
    current_key = ""
    for raw in lines:
        if not raw.strip():
            continue
        if raw.startswith("  - "):
            if not current_key:
                raise ValueError(f"list item without key: {raw}")
            value = data.setdefault(current_key, [])
            if not isinstance(value, list):
                raise ValueError(f"key is not a list: {current_key}")
            value.append(_parse_scalar(raw[4:].strip()))
            continue
        if raw.startswith(" "):
            raise ValueError(f"unsupported nested YAML: {raw}")
        if ":" not in raw:
            raise ValueError(f"invalid YAML line: {raw}")
        key, raw_value = raw.split(":", 1)
        current_key = key.strip()
        value = raw_value.strip()
        if value == "":
            data[current_key] = []
        else:
            data[current_key] = _parse_scalar(value)
    return data


def _parse_scalar(value: str) -> object:
    if value == "[]":
        return []
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value.isdigit():
        return int(value)
    return value


def write_project_registry(
    project_slug: str,
    title: str,
    repo_root: Path,
    *,
    registry_dir: Path | None = None,
) -> Path:
    """Write a project entry to `~/.dora/orchestrator/projects/<slug>.json`.

    The format is compatible with Dagster's ProjectConfig loader.
    """
    base = registry_dir or REGISTRY_DIR
    base.mkdir(parents=True, exist_ok=True)
    entry_path = base / f"{project_slug}.json"
    payload = {
        "slug": project_slug,
        "title": title,
        "repo_root": str(repo_root.resolve()),
        "plane_project_id": "",
        "plane_workspace_slug": "",
        "schedule_cron": "*/2 * * * *",
        "schedule_timezone": "Asia/Shanghai",
        "default_executor": "noop",
        "max_runtime_seconds": 3600,
        "git_branch_prefix": "orchestrator",
        "git_base_branch": "main",
        "enable_push": False,
        "enable_pr": False,
    }
    entry_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return entry_path
