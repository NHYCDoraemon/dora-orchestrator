"""Stable hashing for approved TaskIssueDraft batches."""

import hashlib
import json
from pathlib import Path

from .batch_loader import load_task_issue_batch
from .batch_models import TaskIssueBatch, TaskIssueDraft
from .source_context import resolve_repo_path, source_queries_from_batch, source_tables_from_batch


def compute_batch_hash(batch_dir: Path, *, repo_root: Path | None = None) -> str:
    batch = load_task_issue_batch(batch_dir, repo_root=repo_root)
    digest = hashlib.sha256()
    _hash_bytes(digest, batch.batch_doc.path, _normalized_batch_doc(batch.batch_doc.path).encode("utf-8"))
    _hash_file(digest, batch.program_page.path)
    for task in batch.tasks:
        _hash_file(digest, task.path)
    preview = batch.path / "submit-preview.md"
    if preview.exists():
        _hash_file(digest, preview)
    for table in source_tables_from_batch(batch):
        _hash_bytes(
            digest,
            batch.batch_doc.path,
            json.dumps(table.to_issue_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        _hash_source_table_file(digest, resolve_repo_path(batch.repo_root, table.path))
    for query in source_queries_from_batch(batch):
        _hash_bytes(
            digest,
            batch.batch_doc.path,
            json.dumps(query.to_issue_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
    _hash_optional_file(digest, batch.repo_root / ".dora" / "project.json")
    for task in batch.tasks:
        for key in ("source_pages", "source_docs", "source_summaries"):
            for value in _list_value(task.metadata.get(key)):
                _hash_file(digest, _resolve_source_path(batch, task, value))
        for commit in _list_value(task.metadata.get("source_commits")):
            digest.update(f"source_commit\0{commit}\0".encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _hash_file(digest: "hashlib._Hash", path: Path) -> None:
    resolved = path.resolve()
    _hash_bytes(digest, resolved, resolved.read_bytes())


def _hash_source_table_file(digest: "hashlib._Hash", path: Path) -> None:
    resolved = path.resolve()
    if resolved.is_file():
        _hash_file(digest, resolved)
    else:
        digest.update(f"missing_source_table\0{resolved}\0".encode("utf-8"))


def _hash_bytes(digest: "hashlib._Hash", path: Path, content: bytes) -> None:
    resolved = path.resolve()
    digest.update(f"file\0{resolved}\0".encode("utf-8"))
    digest.update(content)
    digest.update(b"\0")


def _hash_optional_file(digest: "hashlib._Hash", path: Path) -> None:
    if path.exists():
        _hash_file(digest, path)
    else:
        digest.update(f"missing\0{path.resolve()}\0".encode("utf-8"))


def _normalized_batch_doc(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return "\n".join(lines) + "\n"
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return "\n".join(lines) + "\n"
    dynamic_keys = {
        "status",
        "approved_by",
        "approved_at",
        "approved_preview_hash",
        "approved_batch_hash",
        "approved_task_count",
    }
    normalized_frontmatter = [
        line
        for line in lines[: end + 1]
        if not _frontmatter_key(line) in dynamic_keys
    ]
    return "\n".join(normalized_frontmatter + lines[end + 1 :]).strip() + "\n"


def _frontmatter_key(line: str) -> str:
    if line.startswith(" ") or ":" not in line:
        return ""
    return line.split(":", 1)[0].strip()


def _resolve_source_path(batch: TaskIssueBatch, task: TaskIssueDraft, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if value.startswith("."):
        return (task.path.parent / path).resolve()
    return (batch.repo_root / path).resolve()


def _list_value(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        return [value]
    return []
