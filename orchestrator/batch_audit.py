"""Audit TaskIssueDraft batches before Plane submission."""

import hashlib
import re
from dataclasses import asdict, replace
from pathlib import Path

from .batch_loader import load_task_issue_batch
from .batch_models import (
    FIXED_MODULE_TAXONOMY,
    PROGRESS_METADATA_FIELDS,
    REQUIRED_ISSUE_PACKET_SECTIONS,
    AuditFinding,
    BatchAuditResult,
    TaskIssueBatch,
    TaskIssueDraft,
)
from .source_context import SourceQuery, SourceTable, resolve_repo_path, source_queries_from_batch, source_tables_from_batch
from .source_slicing import render_query_slice


TASK_ID_RE = re.compile(r"^(?P<project>[A-Z][A-Z0-9]*)-(?P<program>[A-Z][A-Z0-9]*)-(?P<batch>[0-9]{8}[A-Z])-T(?P<seq>[0-9]{2,3})$")
HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def audit_task_issue_batch(
    batch_dir: Path,
    *,
    repo_root: Path | None = None,
    write_generated: bool = False,
) -> BatchAuditResult:
    try:
        batch = load_task_issue_batch(batch_dir, repo_root=repo_root)
    except (FileNotFoundError, ValueError) as exc:
        result = BatchAuditResult(
            status="FAIL",
            batch_id=Path(batch_dir).name,
            task_count=0,
            findings=[AuditFinding(code="structure", message=str(exc), path=str(batch_dir))],
        )
        if write_generated:
            _write_generated_files(Path(batch_dir), result, None)
        return result

    findings: list[AuditFinding] = []
    _audit_batch_metadata(batch, findings)
    _audit_batch_language(batch, findings)
    source_tables, source_queries, invalid_source_tables = _audit_source_tables_and_queries(batch, findings)
    task_ids = {task.task_id for task in batch.tasks}
    for task in batch.tasks:
        _audit_task_metadata(batch, task, task_ids, findings)
        _audit_progress_metadata(task, findings)
        _audit_issue_packet_sections(task, findings)
        _audit_task_language(task, findings)
        _audit_source_paths(batch, task, findings)
        _audit_required_source_queries(batch, task, source_tables, source_queries, invalid_source_tables, findings)

    planned_cycles = sorted({task.cycle for task in batch.tasks if task.cycle})
    status = "FAIL" if findings else ("PASS_WITH_PLANNED_CREATES" if planned_cycles else "PASS")
    result = BatchAuditResult(
        status=status,
        batch_id=batch.batch_id,
        task_count=len(batch.tasks),
        planned_cycle_creates=planned_cycles if status != "FAIL" else [],
        findings=findings,
    )
    if write_generated:
        _write_generated_files(batch.path, result, batch)
    return result


def _audit_batch_metadata(batch: TaskIssueBatch, findings: list[AuditFinding]) -> None:
    if batch.path.name != batch.batch_id:
        findings.append(
            AuditFinding(
                code="batch_id",
                message=f"batch_id must match directory name: {batch.batch_id} != {batch.path.name}",
                path=str(batch.batch_doc.path),
            )
        )
    for key in ("batch_id", "program_id", "program_prefix", "title", "status"):
        if not batch.batch_doc.metadata.get(key):
            findings.append(
                AuditFinding(
                    code="batch_metadata",
                    message=f"batch.md missing required metadata: {key}",
                    path=str(batch.batch_doc.path),
                )
            )


def _audit_task_metadata(
    batch: TaskIssueBatch,
    task: TaskIssueDraft,
    task_ids: set[str],
    findings: list[AuditFinding],
) -> None:
    if task.path.stem != task.task_id:
        findings.append(
            AuditFinding(
                code="task_filename",
                message=f"task_id must match file name: {task.task_id} != {task.path.stem}",
                path=str(task.path),
            )
        )
    match = TASK_ID_RE.match(task.task_id)
    if not match:
        findings.append(
            AuditFinding(
                code="task_id",
                message=f"task_id does not follow batch-native scheme: {task.task_id}",
                path=str(task.path),
            )
        )
        return
    if match.group("batch") != batch.batch_id:
        findings.append(
            AuditFinding(
                code="task_batch",
                message=f"task_id batch segment must match batch_id: {task.task_id}",
                path=str(task.path),
            )
        )
    if match.group("program") != batch.program_prefix:
        findings.append(
            AuditFinding(
                code="task_program",
                message=f"task_id program segment must match program_prefix: {task.task_id}",
                path=str(task.path),
            )
        )
    if int(match.group("seq")) != int(task.metadata.get("sequence", -1)):
        findings.append(
            AuditFinding(
                code="task_sequence",
                message=f"task_id sequence must match sequence metadata: {task.task_id}",
                path=str(task.path),
            )
        )
    if task.metadata.get("batch_id") != batch.batch_id:
        findings.append(
            AuditFinding(
                code="task_batch",
                message=f"task batch_id must match batch.md: {task.metadata.get('batch_id')}",
                path=str(task.path),
            )
        )
    if task.metadata.get("program_prefix") != batch.program_prefix:
        findings.append(
            AuditFinding(
                code="task_program",
                message=f"task program_prefix must match batch.md: {task.metadata.get('program_prefix')}",
                path=str(task.path),
            )
        )
    if task.module not in FIXED_MODULE_TAXONOMY:
        findings.append(
            AuditFinding(
                code="module",
                message=f"task module is not in fixed taxonomy: {task.module}",
                path=str(task.path),
            )
        )
    for dep in _list_value(task.metadata.get("depends_on")):
        if dep not in task_ids:
            findings.append(
                AuditFinding(
                    code="dependency",
                    message=f"depends_on does not reference a same-batch task: {dep}",
                    path=str(task.path),
                )
            )


def _audit_issue_packet_sections(task: TaskIssueDraft, findings: list[AuditFinding]) -> None:
    last_index = -1
    for section in REQUIRED_ISSUE_PACKET_SECTIONS:
        if section not in task.sections:
            findings.append(
                AuditFinding(
                    code="section_missing",
                    message=f"missing required Issue Packet section: {section}",
                    path=str(task.path),
                )
            )
            continue
        if not task.sections[section].strip():
            findings.append(
                AuditFinding(
                    code="section_empty",
                    message=f"required Issue Packet section is empty: {section}",
                    path=str(task.path),
                )
            )
        current_index = list(task.sections).index(section)
        if current_index < last_index:
            findings.append(
                AuditFinding(
                    code="section_order",
                    message=f"Issue Packet section order is invalid near: {section}",
                    path=str(task.path),
                )
            )
        last_index = current_index


def _audit_source_paths(batch: TaskIssueBatch, task: TaskIssueDraft, findings: list[AuditFinding]) -> None:
    for key in ("source_pages", "source_docs", "source_summaries"):
        values = _list_value(task.metadata.get(key))
        if not values:
            findings.append(
                AuditFinding(
                    code="source_missing",
                    message=f"task must declare at least one {key} entry",
                    path=str(task.path),
                )
            )
        for value in values:
            resolved = _resolve_source_path(batch, task, value)
            if not resolved.exists():
                findings.append(
                    AuditFinding(
                        code="source_not_found",
                        message=f"{key} path does not exist: {value}",
                        path=str(task.path),
                    )
                )


def _audit_source_tables_and_queries(
    batch: TaskIssueBatch,
    findings: list[AuditFinding],
) -> tuple[dict[str, SourceTable], tuple[SourceQuery, ...], set[str]]:
    try:
        tables = source_tables_from_batch(batch)
        queries = source_queries_from_batch(batch)
    except ValueError as exc:
        findings.append(AuditFinding(code="source_context", message=str(exc), path=str(batch.batch_doc.path)))
        return {}, (), set()

    tables_by_id: dict[str, SourceTable] = {}
    invalid_tables: set[str] = set()
    for table in tables:
        if not table.id:
            findings.append(
                AuditFinding(code="source_table_id", message="source table id is required", path=str(batch.batch_doc.path))
            )
            continue
        tables_by_id[table.id] = table
        if table.format not in {"tsv", "csv"}:
            findings.append(
                AuditFinding(
                    code="source_table_format",
                    message=f"{table.id}: unsupported source table format: {table.format}",
                    path=str(batch.batch_doc.path),
                )
            )
            invalid_tables.add(table.id)
        table_path = resolve_repo_path(batch.repo_root, table.path)
        if table.required and not table_path.is_file():
            findings.append(
                AuditFinding(
                    code="source_table_not_found",
                    message=f"{table.id}: source table file does not exist: {table.path}",
                    path=str(batch.batch_doc.path),
                )
            )
            invalid_tables.add(table.id)

    valid_queries: list[SourceQuery] = []
    seen_query_ids: set[str] = set()
    seen_slice_names: set[str] = set()
    for query in queries:
        if not query.id:
            findings.append(
                AuditFinding(
                    code="source_query_id",
                    message="source query id is required",
                    path=str(batch.batch_doc.path),
                )
            )
            continue
        if query.id in seen_query_ids:
            findings.append(
                AuditFinding(
                    code="source_query_id",
                    message=f"duplicate source query id: {query.id}",
                    path=str(batch.batch_doc.path),
                )
            )
            continue
        seen_query_ids.add(query.id)
        table = tables_by_id.get(query.table)
        if table is None:
            findings.append(
                AuditFinding(
                    code="source_query_table",
                    message=f"{query.id}: query references unknown source table: {query.table}",
                    path=str(batch.batch_doc.path),
                )
            )
            continue
        slice_name = f"{_safe_source_query_segment(query.id)}.{table.format}"
        if slice_name in seen_slice_names:
            findings.append(
                AuditFinding(
                    code="source_query_id",
                    message=f"{query.id}: source query slice path conflicts: {slice_name}",
                    path=str(batch.batch_doc.path),
                )
            )
            continue
        seen_slice_names.add(slice_name)
        valid_queries.append(query)
    return tables_by_id, tuple(valid_queries), invalid_tables


def _audit_required_source_queries(
    batch: TaskIssueBatch,
    task: TaskIssueDraft,
    tables_by_id: dict[str, SourceTable],
    queries: tuple[SourceQuery, ...],
    invalid_tables: set[str],
    findings: list[AuditFinding],
) -> None:
    issue_context = dict(task.metadata)
    issue_context["external_id"] = task.task_id
    context = {"task": dict(task.metadata), "issue": issue_context}
    for query in queries:
        if not query.required:
            continue
        table = tables_by_id.get(query.table)
        if table is None or query.table in invalid_tables:
            continue
        result = render_query_slice(
            table=replace(table, path=str(resolve_repo_path(batch.repo_root, table.path))),
            query=query,
            context=context,
            output_path=task.path.parent / f"{query.id}.slice.{table.format}",
            write_output=False,
        )
        if not result.ok:
            findings.append(AuditFinding(code=result.code, message=f"{query.id}: {result.message}", path=str(task.path)))


def _safe_source_query_segment(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-_")
    if clean in {"", ".", ".."}:
        return "unknown"
    return clean


def _audit_progress_metadata(task: TaskIssueDraft, findings: list[AuditFinding]) -> None:
    """If a task opts into dev-loop progress control, require all signals."""
    opted_in = bool(task.metadata.get("progress_schema") or task.metadata.get("progress_task_id"))
    if not opted_in:
        return
    for key in PROGRESS_METADATA_FIELDS:
        if not str(task.metadata.get(key) or "").strip():
            findings.append(
                AuditFinding(
                    code="progress_metadata",
                    message=f"progress-controlled task missing required metadata: {key}",
                    path=str(task.path),
                )
            )
    schema = str(task.metadata.get("progress_schema") or "")
    if schema and schema != "pfe-progress/v1":
        findings.append(
            AuditFinding(
                code="progress_metadata",
                message=f"unsupported progress_schema: {schema}",
                path=str(task.path),
            )
        )


def _resolve_source_path(batch: TaskIssueBatch, task: TaskIssueDraft, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if value.startswith("."):
        return (task.path.parent / path).resolve()
    return (batch.repo_root / path).resolve()


def _audit_batch_language(batch: TaskIssueBatch, findings: list[AuditFinding]) -> None:
    _require_chinese("batch title must be Chinese-readable", batch.title, str(batch.batch_doc.path), findings)
    _require_chinese("batch body must be Chinese-readable", batch.batch_doc.body, str(batch.batch_doc.path), findings)
    _require_chinese(
        "program page must be Chinese-readable",
        batch.program_page.body,
        str(batch.program_page.path),
        findings,
    )


def _audit_task_language(task: TaskIssueDraft, findings: list[AuditFinding]) -> None:
    _require_chinese("task title must be Chinese-readable", task.title, str(task.path), findings)
    for section in REQUIRED_ISSUE_PACKET_SECTIONS:
        body = task.sections.get(section, "")
        _require_chinese(f"task section must be Chinese-readable: {section}", body, str(task.path), findings)


def _require_chinese(message: str, text: str, path: str, findings: list[AuditFinding]) -> None:
    if not HAN_RE.search(text or ""):
        findings.append(AuditFinding(code="language", message=message, path=path))


def _list_value(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        return [value]
    return []


def _write_generated_files(batch_dir: Path, result: BatchAuditResult, batch: TaskIssueBatch | None) -> None:
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "audit-report.md").write_text(_render_audit_report(result), encoding="utf-8")
    (batch_dir / "submit-preview.md").write_text(_render_submit_preview(result, batch), encoding="utf-8")


def _render_audit_report(result: BatchAuditResult) -> str:
    lines = [
        "# 批次审计报告",
        "",
        f"- 状态：{result.status}",
        f"- 批次：{result.batch_id}",
        f"- 任务数量：{result.task_count}",
    ]
    if result.planned_cycle_creates:
        lines.append(f"- 计划创建周期：{', '.join(result.planned_cycle_creates)}")
    lines.extend(["", "## 审计发现", ""])
    if result.findings:
        for finding in result.findings:
            location = f" ({finding.path})" if finding.path else ""
            lines.append(f"- [{finding.severity}] {finding.code}: {finding.message}{location}")
    else:
        lines.append("- 无")
    return "\n".join(lines) + "\n"


def _render_submit_preview(result: BatchAuditResult, batch: TaskIssueBatch | None) -> str:
    lines = [
        "# 提交预览",
        "",
        f"- 状态：{result.status}",
        f"- 批次：{result.batch_id}",
        "",
        "## 计划写入 Plane",
        "",
        "- 计划页面：创建或追加批次历史",
        "- 批次页面：创建不可变批次页面",
        "- 根任务：创建批次根任务",
        f"- 任务问题：{result.task_count}",
    ]
    if result.planned_cycle_creates:
        lines.extend(["", "## 计划创建周期", ""])
        lines.extend(f"- {cycle}" for cycle in result.planned_cycle_creates)
    if batch:
        lines.extend(["", "## 任务问题", ""])
        lines.extend(f"- {task.task_id}: {task.title}" for task in batch.tasks)
        lines.extend(["", f"预览哈希：sha256:{_preview_hash(batch, result)}"])
    return "\n".join(lines) + "\n"


def _preview_hash(batch: TaskIssueBatch, result: BatchAuditResult) -> str:
    digest = hashlib.sha256()
    digest.update(batch.batch_doc.body.encode("utf-8"))
    digest.update(batch.program_page.body.encode("utf-8"))
    for task in batch.tasks:
        digest.update(task.body.encode("utf-8"))
    digest.update(str(asdict(result)).encode("utf-8"))
    return digest.hexdigest()
