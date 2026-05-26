"""Deterministic source bundle generation for Execution Packet v1."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .source_context import (
    EXECUTION_PACKET_VERSION,
    SOURCE_DOC_KEYS,
    SourceQuery,
    SourceTable,
    hash_file,
    resolve_repo_path,
)
from .source_slicing import SliceResult, render_query_slice


SOURCE_PACKET_KEYS = ("source_docs", "source_tables", "source_queries")
_BINARY_SOURCE_DOC_SUFFIXES = {
    ".doc",
    ".docx",
    ".pdf",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsb",
    ".xlsm",
    ".xlsx",
}


@dataclass(frozen=True)
class SourceBundleResult:
    ok: bool
    bundle_root: Path
    bundle_path: Path
    manifest_path: Path
    required_read_paths: tuple[Path, ...]
    slice_results: tuple[SliceResult, ...]
    message: str = ""


def create_source_bundle(
    *,
    issue: Mapping[str, object],
    worktree_root: Path,
    write_output: bool = True,
) -> SourceBundleResult:
    repo_root = worktree_root.resolve()
    batch_id = _batch_id(issue)
    task_key = _task_key(issue)
    bundle_root = repo_root / ".dora" / "source-bundles" / _safe_segment(batch_id) / _safe_segment(task_key)
    bundle_path = bundle_root / "source-bundle.md"
    manifest_path = bundle_root / "manifest.json"

    missing_keys = [key for key in SOURCE_PACKET_KEYS if key not in issue]
    if missing_keys:
        message = "missing source metadata keys: " + ", ".join(missing_keys)
        return _result(False, bundle_root, bundle_path, manifest_path, (), (), message)

    try:
        source_docs = _source_docs(issue, repo_root)
    except ValueError as exc:
        return _result(False, bundle_root, bundle_path, manifest_path, (), (), str(exc))
    for doc in source_docs:
        if doc["required"] and not Path(str(doc["absolute_path"])).is_file():
            return _result(
                False,
                bundle_root,
                bundle_path,
                manifest_path,
                (),
                (),
                f"required source doc not found: {doc['path']}",
            )

    try:
        source_tables = _source_tables(issue, repo_root)
        source_queries = _source_queries(issue)
    except (TypeError, ValueError) as exc:
        return _result(False, bundle_root, bundle_path, manifest_path, (), (), str(exc))

    tables_by_id = {table.id: table for table in source_tables}
    table_manifest = []
    for table in source_tables:
        table_path = Path(table.path)
        if table.required and not table_path.is_file():
            return _result(False, bundle_root, bundle_path, manifest_path, (), (), f"required source table not found: {table.path}")
        table_manifest.append(
            {
                "id": table.id,
                "path": _repo_or_abs(table_path, repo_root),
                "absolute_path": str(table_path),
                "format": table.format,
                "key_columns": list(table.key_columns),
                "required": table.required,
                "sha256": table.sha256 or (hash_file(table_path) if table_path.is_file() else ""),
            }
        )

    slice_results: list[SliceResult] = []
    slice_manifest: list[dict[str, object]] = []
    required_slice_paths: list[Path] = []
    seen_query_ids: set[str] = set()
    seen_slice_paths: set[str] = set()
    for query in source_queries:
        if not query.id:
            return _result(False, bundle_root, bundle_path, manifest_path, (), tuple(slice_results), "source query id is required")
        if query.id in seen_query_ids:
            return _result(False, bundle_root, bundle_path, manifest_path, (), tuple(slice_results), f"duplicate source query id: {query.id}")
        seen_query_ids.add(query.id)
        table = tables_by_id.get(query.table)
        if table is None:
            message = f"source query references unknown table: {query.table}"
            if query.required:
                return _result(False, bundle_root, bundle_path, manifest_path, (), tuple(slice_results), message)
            slice_results.append(SliceResult(False, "source_query_table", query.id, None, 0, (), message=message))
            continue
        output_path = bundle_root / "slices" / f"{_safe_segment(query.id)}.{table.format}"
        slice_key = output_path.name.casefold()
        if slice_key in seen_slice_paths:
            message = f"source query slice path conflicts: {_repo_or_abs(output_path, repo_root)}"
            return _result(False, bundle_root, bundle_path, manifest_path, (), tuple(slice_results), message)
        seen_slice_paths.add(slice_key)
        result = render_query_slice(
            table=table,
            query=query,
            context=_source_query_context(issue),
            output_path=output_path,
            write_output=write_output,
        )
        slice_results.append(result)
        if not result.ok:
            if query.required:
                return _result(False, bundle_root, bundle_path, manifest_path, (), tuple(slice_results), result.message)
            continue
        if result.output_path is not None:
            slice_manifest.append(
                {
                    "query_id": result.query_id,
                    "path": _repo_or_abs(result.output_path, repo_root),
                    "absolute_path": str(result.output_path),
                    "row_count": result.row_count,
                    "columns": list(result.columns),
                    "sha256": result.sha256,
                    "required": query.required,
                }
            )
            if query.required:
                required_slice_paths.append(result.output_path)

    required_doc_paths = [
        Path(str(doc["absolute_path"]))
        for doc in source_docs
        if doc["required"] and _is_direct_read_source_doc(Path(str(doc["absolute_path"])))
    ]
    required_table_paths = [Path(str(table["absolute_path"])) for table in table_manifest if table["required"]]
    required_read_paths = _unique_paths([bundle_path, *required_doc_paths, *required_table_paths, *required_slice_paths])
    manifest = {
        "execution_packet_version": EXECUTION_PACKET_VERSION,
        "batch_id": batch_id,
        "task_key": task_key,
        "source_docs": [_manifest_doc(doc, repo_root) for doc in source_docs],
        "source_tables": table_manifest,
        "source_queries": [query.to_issue_dict() for query in source_queries],
        "slices": slice_manifest,
        "required_read_paths": [str(path) for path in required_read_paths],
    }
    if write_output:
        bundle_root.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        bundle_path.write_text(_render_bundle_markdown(manifest), encoding="utf-8")

    return SourceBundleResult(
        ok=True,
        bundle_root=bundle_root,
        bundle_path=bundle_path,
        manifest_path=manifest_path,
        required_read_paths=required_read_paths,
        slice_results=tuple(slice_results),
    )


def _source_docs(issue: Mapping[str, object], repo_root: Path) -> list[dict[str, object]]:
    docs: list[dict[str, object]] = []
    for key in SOURCE_DOC_KEYS:
        for item in _list_value(issue.get(key)):
            if isinstance(item, Mapping):
                path = str(item.get("path") or "")
                required = _bool_value(item.get("required"), default=True)
                sha256 = str(item.get("sha256") or "")
                kind = str(item.get("kind") or key)
            else:
                path = str(item)
                required = True
                sha256 = ""
                kind = key
            absolute = _resolve_inside_worktree(repo_root, path)
            docs.append({"kind": kind, "path": path, "absolute_path": absolute, "required": required, "sha256": sha256})
    return docs


def _source_tables(issue: Mapping[str, object], repo_root: Path) -> tuple[SourceTable, ...]:
    tables: list[SourceTable] = []
    for item in _mapping_items(issue.get("source_tables"), "source_tables"):
        table_path = str(item.get("path") or "")
        resolved = _resolve_inside_worktree(repo_root, table_path)
        tables.append(
            SourceTable(
                id=str(item.get("id") or ""),
                path=str(resolved),
                format=str(item.get("format") or "tsv"),
                key_columns=tuple(str(value) for value in _list_value(item.get("key_columns"))),
                required=_bool_value(item.get("required"), default=True),
                sha256=str(item.get("sha256") or ""),
            )
        )
    return tuple(tables)


def _source_queries(issue: Mapping[str, object]) -> tuple[SourceQuery, ...]:
    queries: list[SourceQuery] = []
    for item in _mapping_items(issue.get("source_queries"), "source_queries"):
        queries.append(
            SourceQuery(
                id=str(item.get("id") or ""),
                table=str(item.get("table") or ""),
                required=_bool_value(item.get("required"), default=True),
                filters=tuple(dict(value) for value in _mapping_items(item.get("filters"), "source_query.filters")),
                columns=tuple(str(value) for value in _list_value(item.get("columns"))),
                max_rows=int(item.get("max_rows") or 200),
            )
        )
    return tuple(queries)


def _source_query_context(issue: Mapping[str, object]) -> dict[str, object]:
    task_context = dict(issue)
    issue_context = dict(issue)
    external_id = str(issue_context.get("external_id") or issue_context.get("task_id") or issue.get("key") or "")
    if external_id:
        task_context.setdefault("external_id", external_id)
        task_context.setdefault("task_id", external_id)
        issue_context.setdefault("external_id", external_id)
        issue_context.setdefault("task_id", external_id)
    return {"task": task_context, "issue": issue_context}


def _render_bundle_markdown(manifest: Mapping[str, object]) -> str:
    lines = [
        "# Source Bundle",
        "",
        f"- execution_packet_version: {manifest['execution_packet_version']}",
        f"- batch_id: {manifest['batch_id']}",
        f"- task_key: {manifest['task_key']}",
        "",
        "## Required Reads",
    ]
    lines.extend(f"- `{path}`" for path in manifest["required_read_paths"])
    lines.extend(["", "## Source Docs"])
    for doc in manifest["source_docs"]:
        lines.append(
            f"- `{doc['path']}` required={doc['required']} sha256={doc['sha256']}"
        )
    lines.extend(["", "## Source Tables"])
    for table in manifest["source_tables"]:
        lines.append(
            f"- `{table['path']}` id={table['id']} format={table['format']} required={table['required']} sha256={table['sha256']}"
        )
    lines.extend(["", "## Generated Slices"])
    for item in manifest["slices"]:
        lines.append(
            f"- `{item['path']}` query_id={item['query_id']} rows={item['row_count']} sha256={item['sha256']}"
        )
    return "\n".join(lines) + "\n"


def _manifest_doc(doc: Mapping[str, object], repo_root: Path) -> dict[str, object]:
    absolute = Path(str(doc["absolute_path"]))
    return {
        "kind": doc["kind"],
        "path": doc["path"],
        "absolute_path": str(absolute),
        "required": doc["required"],
        "sha256": str(doc.get("sha256") or (hash_file(absolute) if absolute.is_file() else "")),
    }


def _is_direct_read_source_doc(path: Path) -> bool:
    return path.suffix.lower() not in _BINARY_SOURCE_DOC_SUFFIXES


def _batch_id(issue: Mapping[str, object]) -> str:
    explicit = str(issue.get("batch_id") or "").strip()
    if explicit:
        return explicit
    external_id = str(issue.get("external_id") or "")
    match = re.search(r"(\d{8}[A-Z])", external_id)
    if match:
        return match.group(1)
    return "unknown-batch"


def _task_key(issue: Mapping[str, object]) -> str:
    return str(issue.get("external_id") or issue.get("key") or "unknown-task")


def _safe_segment(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-_")
    if clean in {"", ".", ".."}:
        return "unknown"
    return clean


def _resolve_inside_worktree(worktree_root: Path, declared_path: str) -> Path:
    resolved_root = worktree_root.resolve()
    resolved = resolve_repo_path(resolved_root, declared_path)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"source path outside worktree: {declared_path}") from exc
    return resolved


def _repo_or_abs(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return str(path)


def _unique_paths(paths: list[Path]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return tuple(out)


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


def _mapping_items(value: object, name: str) -> list[Mapping[str, object]]:
    items = _list_value(value)
    out: list[Mapping[str, object]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError(f"{name} entries must be mappings")
        out.append(item)
    return out


def _bool_value(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1"}


def _result(
    ok: bool,
    bundle_root: Path,
    bundle_path: Path,
    manifest_path: Path,
    required_read_paths: tuple[Path, ...],
    slice_results: tuple[SliceResult, ...],
    message: str,
) -> SourceBundleResult:
    return SourceBundleResult(
        ok=ok,
        bundle_root=bundle_root,
        bundle_path=bundle_path,
        manifest_path=manifest_path,
        required_read_paths=required_read_paths,
        slice_results=slice_results,
        message=message,
    )
