"""Submit approved TaskIssueDraft batches into a Plane-compatible backend."""

import hashlib
import json
from pathlib import Path

from .batch_audit import TASK_ID_RE, audit_task_issue_batch
from .batch_hash import compute_batch_hash
from .batch_loader import load_task_issue_batch
from .batch_models import (
    FIXED_MODULE_TAXONOMY,
    PROGRESS_METADATA_FIELDS,
    REQUIRED_ISSUE_PACKET_SECTIONS,
    SECTION_DISPLAY_TITLES,
    TaskIssueBatch,
    TaskIssueDraft,
)
from .source_context import (
    EXECUTION_PACKET_VERSION,
    source_docs_for_task,
    source_queries_from_batch,
    source_tables_from_batch,
)


def submit_task_issue_batch(
    batch_dir: Path,
    *,
    repo_root: Path,
    project_slug: str,
    project_title: str,
    plane_client,
) -> dict[str, int]:
    batch = load_task_issue_batch(batch_dir, repo_root=repo_root)
    audit = audit_task_issue_batch(batch_dir, repo_root=repo_root)
    if audit.status == "FAIL":
        raise ValueError("cannot submit batch with failing audit")
    approval = _load_approval(batch)
    _validate_approval(batch, approval, repo_root)
    batch_hash = compute_batch_hash(batch.path, repo_root=repo_root)
    source_tables = [table.to_issue_dict() for table in source_tables_from_batch(batch)]
    source_queries = [query.to_issue_dict() for query in source_queries_from_batch(batch)]

    plane_client.upsert_project(project_slug, project_title)
    for module in sorted(FIXED_MODULE_TAXONOMY):
        plane_client.upsert_module(project_slug, module)
    for cycle in audit.planned_cycle_creates:
        plane_client.upsert_cycle(project_slug, cycle)

    plane_client.upsert_page(
        project_slug,
        f"program-{batch.program_id}",
        {
            "title": f"计划：{batch.program_id}",
            "body": batch.program_page.body,
            "page_type": "program",
            "batch_id": batch.batch_id,
        },
    )
    plane_client.upsert_page(
        project_slug,
        f"batch-{batch.batch_id}",
        {
            "title": f"批次：{batch.title}",
            "body": _render_batch_page(batch),
            "page_type": "batch",
            "batch_id": batch.batch_id,
        },
    )

    root_external_id = _root_external_id(batch)
    plane_client.upsert_issue(
        project_slug,
        root_external_id,
        {
            "name": f"[批次] {batch.title}",
            "body": _render_root_epic_body(batch),
            "issue_type": "root_epic",
            "cycle": batch.tasks[0].cycle,
            "module": "planning",
            "priority": "P1",
            "depends_on": [],
            "source_hash": batch_hash,
            "agent_hint": "noop",
            "risk": "medium",
            "acceptance": [],
            "verification_level": ["L1", "L2", "L3"],
        },
    )
    for task in batch.tasks:
        source_docs = [doc.to_issue_dict(batch.repo_root) for doc in source_docs_for_task(batch, task)]
        risk = str(task.metadata.get("risk") or "medium")
        priority = task.priority or "P3"
        progress_metadata = {
            key: task.metadata.get(key)
            for key in PROGRESS_METADATA_FIELDS
            if task.metadata.get(key) is not None
        }
        plane_client.upsert_issue(
            project_slug,
            task.task_id,
            {
                "name": task.title,
                "body": _render_task_body(task),
                "issue_type": "task",
                "parent_external_id": root_external_id,
                "cycle": task.cycle,
                "module": task.module,
                "priority": task.priority,
                "depends_on": _list_value(task.metadata.get("depends_on")),
                "depends_on_legacy": _list_value(task.metadata.get("depends_on_legacy")),
                "legacy_refs": _list_value(task.metadata.get("legacy_refs")),
                "source_hash": _task_hash(task),
                "agent_hint": str(task.metadata.get("agent_hint") or "claude"),
                "risk": str(task.metadata.get("risk") or "medium"),
                "acceptance": _parse_acceptance_bullets(task.sections.get("Acceptance", "")),
                "verification_level": _list_value(task.metadata.get("verification_level")) or ["L1", "L2", "L3"],
                "verification_commands": _list_value(task.metadata.get("verification_commands")),
                "execution_packet_version": EXECUTION_PACKET_VERSION,
                "execution_packet_hash": batch_hash,
                "source_docs": source_docs,
                "source_tables": source_tables,
                "source_queries": source_queries,
                "required_skills": _list_value(task.metadata.get("required_skills")),
                "suggested_skills": _list_value(task.metadata.get("suggested_skills")),
                "forbidden_skills": _list_value(task.metadata.get("forbidden_skills")),
                "program_page_slug": f"program-{batch.program_id}",
                "batch_page_slug": f"batch-{batch.batch_id}",
                **progress_metadata,
            },
        )
        if hasattr(plane_client, "add_label"):
            plane_client.add_label(project_slug, task.task_id, f"risk:{risk}")
            plane_client.add_label(project_slug, task.task_id, f"priority:{priority.lower()}")
            plane_client.add_label(project_slug, task.task_id, f"batch:{batch.batch_id}")

    return {
        "projects": 1,
        "cycles": len(audit.planned_cycle_creates),
        "modules": len(FIXED_MODULE_TAXONOMY),
        "pages": 2,
        "root_epic_issues": 1,
        "task_issues": len(batch.tasks),
    }


def _load_approval(batch: TaskIssueBatch) -> dict[str, object]:
    path = batch.path / "approval.json"
    if not path.exists():
        raise ValueError(f"missing approval.json: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_approval(batch: TaskIssueBatch, approval: dict[str, object], repo_root: Path) -> None:
    if approval.get("batch_id") != batch.batch_id:
        raise ValueError("approval batch_id does not match current batch")
    approved_task_ids = approval.get("approved_task_ids")
    if sorted(approved_task_ids or []) != sorted(task.task_id for task in batch.tasks):
        raise ValueError("approval task ids do not match current batch")
    current_hash = compute_batch_hash(batch.path, repo_root=repo_root)
    if approval.get("approved_batch_hash") != current_hash:
        raise ValueError("approval hash does not match current batch contents")
    policy = approval.get("policy", {})
    if isinstance(policy, dict) and policy.get("allow_update_existing_submitted_issues") is not False:
        raise ValueError("approval policy must disallow updating existing submitted issues")


def _root_external_id(batch: TaskIssueBatch) -> str:
    match = TASK_ID_RE.match(batch.tasks[0].task_id)
    if not match:
        return f"{batch.program_prefix}-{batch.batch_id}-ROOT"
    return f"{match.group('project')}-{match.group('program')}-{match.group('batch')}-ROOT"


def _render_batch_page(batch: TaskIssueBatch) -> str:
    preview_path = batch.path / "submit-preview.md"
    preview = preview_path.read_text(encoding="utf-8") if preview_path.exists() else ""
    return "\n\n".join([batch.batch_doc.body.strip(), preview.strip()]).strip() + "\n"


def _render_root_epic_body(batch: TaskIssueBatch) -> str:
    lines = [
        f"# 批次根任务：{batch.title}",
        "",
        f"- 批次编号：`{batch.batch_id}`",
        f"- 所属计划：`{batch.program_id}`",
        f"- 任务数量：{len(batch.tasks)}",
        "",
        "## 任务列表",
        "",
    ]
    lines.extend(f"- {task.task_id}: {task.title}" for task in batch.tasks)
    lines.extend(
        [
            "",
            "## 停止条件",
            "",
            "- 如果批次审批哈希与本地内容不一致，停止提交。",
            "- 如果任务要求修改已提交的问题历史，停止提交并重新审批。",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_task_body(task: TaskIssueDraft) -> str:
    lines = [f"# {task.title}", ""]
    for section in REQUIRED_ISSUE_PACKET_SECTIONS:
        title = SECTION_DISPLAY_TITLES[section]
        body = task.sections.get(section, "").strip()
        lines.extend([f"## {title}", "", body, ""])
    return "\n".join(lines).strip() + "\n"


def _parse_acceptance_bullets(section: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    for raw in section.splitlines():
        if raw.startswith("- "):
            if current:
                items.append(" ".join(current).strip())
            current = [raw[2:].strip()]
        elif current and raw.startswith("  "):
            current.append(raw.strip())
        else:
            if current:
                items.append(" ".join(current).strip())
                current = []
    if current:
        items.append(" ".join(current).strip())
    return items


def _task_hash(task: TaskIssueDraft) -> str:
    digest = hashlib.sha256()
    digest.update(task.body.encode("utf-8"))
    digest.update(str(task.metadata).encode("utf-8"))
    return digest.hexdigest()


def _list_value(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        return [value]
    return []
