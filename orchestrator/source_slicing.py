"""Restricted CSV/TSV source table slicing for Execution Packet v1."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .source_context import SourceQuery, SourceTable, hash_file


@dataclass(frozen=True)
class SliceResult:
    ok: bool
    code: str
    query_id: str
    output_path: Path | None
    row_count: int
    columns: tuple[str, ...]
    sha256: str = ""
    message: str = ""


def render_query_slice(
    *,
    table: SourceTable,
    query: SourceQuery,
    context: dict[str, Any],
    output_path: Path,
    write_output: bool = True,
) -> SliceResult:
    table_path = Path(table.path)
    if not table_path.exists():
        return _fail(query, "source_table_not_found", f"source table not found: {table.path}")

    delimiter = _delimiter(table.format)
    if delimiter is None:
        return _fail(query, "source_table_format", f"unsupported source table format: {table.format}")

    with table_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        fieldnames = tuple(reader.fieldnames or ())
        if not fieldnames:
            return _fail(query, "source_table_header", "source table has no header")

        selected_columns = tuple(query.columns or fieldnames)
        missing_columns = [column for column in selected_columns if column not in fieldnames]
        if missing_columns:
            return _fail(query, "source_query_column", f"query selects missing columns: {', '.join(missing_columns)}")

        filter_message = _validate_filters(fieldnames, query, context)
        if filter_message:
            return _fail(query, "source_query_filter", filter_message, columns=selected_columns)

        rows: list[dict[str, str]] = []
        for row in reader:
            match, message = _row_matches(row, fieldnames, query, context)
            if message:
                return _fail(query, "source_query_filter", message, columns=selected_columns)
            if match:
                rows.append({column: row.get(column) or "" for column in selected_columns})

    if query.required and not rows:
        return _fail(query, "source_query_empty", "required source query returned no rows", columns=selected_columns)
    if len(rows) > query.max_rows:
        return _fail(
            query,
            "source_query_too_many_rows",
            f"source query returned {len(rows)} rows, max_rows is {query.max_rows}",
            columns=selected_columns,
            row_count=len(rows),
        )

    if not write_output:
        return SliceResult(
            ok=True,
            code="ok",
            query_id=query.id,
            output_path=None,
            row_count=len(rows),
            columns=selected_columns,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(selected_columns), delimiter=delimiter, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    return SliceResult(
        ok=True,
        code="ok",
        query_id=query.id,
        output_path=output_path,
        row_count=len(rows),
        columns=selected_columns,
        sha256=hash_file(output_path),
    )


def _delimiter(table_format: str) -> str | None:
    if table_format == "tsv":
        return "\t"
    if table_format == "csv":
        return ","
    return None


def _row_matches(
    row: dict[str, str],
    fieldnames: tuple[str, ...],
    query: SourceQuery,
    context: dict[str, Any],
) -> tuple[bool, str]:
    for source_filter in query.filters:
        column = str(source_filter.get("column") or "")
        if column not in fieldnames:
            return False, f"filter references missing column: {column}"

        op = str(source_filter.get("op") or "")
        if op not in {"equals", "contains", "regex", "in"}:
            return False, f"unsupported filter op: {op}"

        value, ok = _filter_value(source_filter, context)
        if not ok:
            return False, f"filter value_from path is missing: {source_filter.get('value_from')}"

        actual = row.get(column) or ""
        try:
            if not _matches_op(actual, op, value):
                return False, ""
        except re.error as exc:
            return False, f"invalid regex filter: {exc}"
    return True, ""


def _validate_filters(
    fieldnames: tuple[str, ...],
    query: SourceQuery,
    context: dict[str, Any],
) -> str:
    for source_filter in query.filters:
        column = str(source_filter.get("column") or "")
        if column not in fieldnames:
            return f"filter references missing column: {column}"

        op = str(source_filter.get("op") or "")
        if op not in {"equals", "contains", "regex", "in"}:
            return f"unsupported filter op: {op}"

        value, ok = _filter_value(source_filter, context)
        if not ok:
            return f"filter value_from path is missing: {source_filter.get('value_from')}"

        if op == "regex":
            try:
                re.compile(str(value))
            except re.error as exc:
                return f"invalid regex filter: {exc}"
    return ""


def _filter_value(source_filter: dict[str, object], context: dict[str, Any]) -> tuple[Any, bool]:
    if "value_from" in source_filter:
        return _value_from_path(context, str(source_filter.get("value_from") or ""))
    if "value" in source_filter:
        return source_filter.get("value"), True
    return None, False


def _value_from_path(context: dict[str, Any], path: str) -> tuple[Any, bool]:
    if not path:
        return None, False
    current: Any = context
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None, False
    return current, True


def _matches_op(actual: str, op: str, expected: Any) -> bool:
    if op == "equals":
        return actual == str(expected)
    if op == "contains":
        return str(expected) in actual
    if op == "regex":
        return re.search(str(expected), actual) is not None
    if op == "in":
        if isinstance(expected, (list, tuple, set)):
            return actual in {str(item) for item in expected}
        return actual in {item.strip() for item in str(expected).split(",")}
    return False


def _fail(
    query: SourceQuery,
    code: str,
    message: str,
    *,
    columns: tuple[str, ...] = (),
    row_count: int = 0,
) -> SliceResult:
    return SliceResult(
        ok=False,
        code=code,
        query_id=query.id,
        output_path=None,
        row_count=row_count,
        columns=columns,
        message=message,
    )
