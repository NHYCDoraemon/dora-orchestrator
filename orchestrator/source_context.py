"""Execution Packet v1 source-context parsing and path normalization."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .batch_models import TaskIssueBatch, TaskIssueDraft


EXECUTION_PACKET_VERSION = 1
SOURCE_TABLE_FORMATS = {"tsv", "csv"}
SOURCE_FILTER_OPS = {"equals", "contains", "regex", "in"}
SOURCE_DOC_KEYS = ("source_pages", "source_docs", "source_summaries")


@dataclass(frozen=True)
class SourceDoc:
    kind: str
    path: str
    absolute_path: Path
    required: bool = True
    sha256: str = ""

    def to_issue_dict(self, repo_root: Path) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": _repo_relative(self.absolute_path, repo_root),
            "required": self.required,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class SourceTable:
    id: str
    path: str
    format: str
    key_columns: tuple[str, ...]
    required: bool = True
    sha256: str = ""

    def to_issue_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "path": self.path,
            "format": self.format,
            "key_columns": list(self.key_columns),
            "required": self.required,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class SourceQuery:
    id: str
    table: str
    required: bool
    filters: tuple[dict[str, object], ...]
    columns: tuple[str, ...]
    max_rows: int

    def to_issue_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "table": self.table,
            "required": self.required,
            "filters": [dict(item) for item in self.filters],
            "columns": list(self.columns),
            "max_rows": self.max_rows,
        }


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def source_tables_from_batch(batch: TaskIssueBatch) -> tuple[SourceTable, ...]:
    raw = batch.batch_doc.metadata.get("source_tables") or []
    if not isinstance(raw, list):
        raise ValueError("source_tables must be a list")
    tables: list[SourceTable] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("source_tables entries must be mappings")
        table_path = str(item.get("path") or "")
        resolved = resolve_repo_path(batch.repo_root, table_path)
        tables.append(
            SourceTable(
                id=str(item.get("id") or ""),
                path=table_path,
                format=str(item.get("format") or "tsv"),
                key_columns=tuple(str(value) for value in _list_value(item.get("key_columns"))),
                required=_bool_value(item.get("required"), default=True),
                sha256=hash_file(resolved) if resolved.is_file() else "",
            )
        )
    return tuple(tables)


def source_queries_from_batch(batch: TaskIssueBatch) -> tuple[SourceQuery, ...]:
    raw = batch.batch_doc.metadata.get("source_queries") or []
    if not isinstance(raw, list):
        raise ValueError("source_queries must be a list")
    queries: list[SourceQuery] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("source_queries entries must be mappings")
        queries.append(
            SourceQuery(
                id=str(item.get("id") or ""),
                table=str(item.get("table") or ""),
                required=_bool_value(item.get("required"), default=True),
                filters=tuple(dict(value) for value in _mapping_list(item.get("filters"))),
                columns=tuple(str(value) for value in _list_value(item.get("columns"))),
                max_rows=int(item.get("max_rows") or 200),
            )
        )
    return tuple(queries)


def source_docs_for_task(batch: TaskIssueBatch, task: TaskIssueDraft) -> tuple[SourceDoc, ...]:
    docs: list[SourceDoc] = []
    for key in SOURCE_DOC_KEYS:
        for declared in _list_value(task.metadata.get(key)):
            path = str(declared)
            absolute = resolve_task_source_path(batch, task, path)
            docs.append(
                SourceDoc(
                    kind=key,
                    path=path,
                    absolute_path=absolute,
                    required=True,
                    sha256=hash_file(absolute) if absolute.is_file() else "",
                )
            )
    return tuple(docs)


def resolve_repo_path(repo_root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


def resolve_task_source_path(batch: TaskIssueBatch, task: TaskIssueDraft, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    if value.startswith("."):
        return (task.path.parent / path).resolve()
    return (batch.repo_root / path).resolve()


def _repo_relative(path: Path, repo_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _list_value(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value:
        return [value]
    return []


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("expected list of mappings")
    out: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("expected mapping entry")
        out.append(item)
    return out


def _bool_value(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1"}
