"""Approve audited TaskIssueDraft batches."""

import json
from datetime import datetime, timezone
from pathlib import Path

from .batch_audit import audit_task_issue_batch
from .batch_hash import compute_batch_hash
from .batch_loader import load_task_issue_batch


def approve_task_issue_batch(
    batch_dir: Path,
    *,
    repo_root: Path,
    approved_by: str,
    approved_at: str | None = None,
) -> dict[str, object]:
    audit = audit_task_issue_batch(batch_dir, repo_root=repo_root, write_generated=True)
    if audit.status == "FAIL":
        raise ValueError("cannot approve batch with failing audit")
    batch = load_task_issue_batch(batch_dir, repo_root=repo_root)
    approved_at = approved_at or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    batch_hash = compute_batch_hash(batch.path, repo_root=repo_root)
    approval = {
        "approval_version": 1,
        "batch_id": batch.batch_id,
        "program_id": batch.program_id,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "approved_batch_hash": batch_hash,
        "approved_task_ids": [task.task_id for task in batch.tasks],
        "approved_plane_creates": {
            "cycles": audit.planned_cycle_creates,
            "root_epic_issue": True,
            "task_issues": len(batch.tasks),
            "pages": ["Program Page", "Batch Page"],
        },
        "policy": {
            "allow_create_cycles": True,
            "allow_create_root_epic": True,
            "allow_create_task_issues": True,
            "allow_update_existing_submitted_issues": False,
        },
    }
    _update_batch_frontmatter(
        batch.path / "batch.md",
        {
            "status": "approved",
            "approved_by": approved_by,
            "approved_at": approved_at,
            "approved_batch_hash": batch_hash,
            "approved_task_count": str(len(batch.tasks)),
        },
    )
    (batch.path / "approval.json").write_text(
        json.dumps(approval, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return approval


def _update_batch_frontmatter(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"missing YAML frontmatter: {path}")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError(f"unterminated YAML frontmatter: {path}") from exc
    update_keys = set(updates)
    frontmatter = [
        line
        for line in lines[1:end]
        if _frontmatter_key(line) not in update_keys
    ]
    for key, value in updates.items():
        frontmatter.append(f"{key}: {value}")
    updated = ["---", *frontmatter, "---", *lines[end + 1 :]]
    path.write_text("\n".join(updated).strip() + "\n", encoding="utf-8")


def _frontmatter_key(line: str) -> str:
    if line.startswith(" ") or ":" not in line:
        return ""
    return line.split(":", 1)[0].strip()
