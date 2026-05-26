"""Shared source-context preflight for batch audit and submit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .batch_models import PROGRESS_METADATA_FIELDS, TaskIssueBatch, TaskIssueDraft
from .source_bundle import SourceBundleResult, create_source_bundle
from .source_context import (
    EXECUTION_PACKET_VERSION,
    source_docs_for_task,
    source_queries_from_batch,
    source_tables_from_batch,
)


@dataclass(frozen=True)
class SourcePreflightFinding:
    task_id: str
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class SourcePreflightTaskResult:
    task_id: str
    issue: dict[str, object]
    bundle: SourceBundleResult


@dataclass(frozen=True)
class SourcePreflightResult:
    ok: bool
    task_results: tuple[SourcePreflightTaskResult, ...]
    findings: tuple[SourcePreflightFinding, ...]


def preflight_batch_source_context(
    batch: TaskIssueBatch,
    *,
    execution_packet_hash: str,
) -> SourcePreflightResult:
    """Validate per-task source context using submit-equivalent metadata."""
    try:
        source_tables = [table.to_issue_dict() for table in source_tables_from_batch(batch)]
        source_queries = [query.to_issue_dict() for query in source_queries_from_batch(batch)]
    except ValueError as exc:
        return SourcePreflightResult(
            ok=False,
            task_results=(),
            findings=(
                SourcePreflightFinding(
                    task_id="",
                    path=str(batch.batch_doc.path),
                    code="source_context",
                    message=str(exc),
                ),
            ),
        )

    task_results: list[SourcePreflightTaskResult] = []
    findings: list[SourcePreflightFinding] = []
    for task in batch.tasks:
        issue = build_task_issue_source_metadata(
            batch,
            task,
            execution_packet_hash=execution_packet_hash,
            source_tables=source_tables,
            source_queries=source_queries,
        )
        bundle = create_source_bundle(issue=issue, worktree_root=batch.repo_root, write_output=False)
        task_results.append(SourcePreflightTaskResult(task_id=task.task_id, issue=issue, bundle=bundle))
        if not bundle.ok:
            findings.append(
                SourcePreflightFinding(
                    task_id=task.task_id,
                    path=str(task.path),
                    code="source_bundle_preflight",
                    message=bundle.message or "source context bundle preflight failed",
                )
            )
    return SourcePreflightResult(ok=not findings, task_results=tuple(task_results), findings=tuple(findings))


def build_task_issue_source_metadata(
    batch: TaskIssueBatch,
    task: TaskIssueDraft,
    *,
    execution_packet_hash: str,
    source_tables: list[Mapping[str, object]] | None = None,
    source_queries: list[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    issue: dict[str, object] = {
        "external_id": task.task_id,
        "task_id": task.task_id,
        "key": task.task_id,
        "batch_id": batch.batch_id,
        "execution_packet_version": EXECUTION_PACKET_VERSION,
        "execution_packet_hash": execution_packet_hash,
        "source_docs": [doc.to_issue_dict(batch.repo_root) for doc in source_docs_for_task(batch, task)],
        "source_tables": list(source_tables) if source_tables is not None else [table.to_issue_dict() for table in source_tables_from_batch(batch)],
        "source_queries": list(source_queries) if source_queries is not None else [query.to_issue_dict() for query in source_queries_from_batch(batch)],
    }
    for key in PROGRESS_METADATA_FIELDS:
        value = task.metadata.get(key)
        if value is not None:
            issue[key] = value
    return issue
