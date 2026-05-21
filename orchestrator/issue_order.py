"""Ordering helpers for generated TaskIssueDraft IDs."""

from __future__ import annotations

import re

_BATCH_TASK_RE = re.compile(r"^.+-(?P<batch>\d{8}[A-Z])-T(?P<seq>\d+)$")


def batch_sort_key(external_id: str) -> str:
    """Extract the batch-date segment from a generated external_id."""
    parts = external_id.split("-")
    if len(parts) >= 3:
        return parts[2]
    return external_id


def batch_task_order_key(external_id: str) -> tuple[str, int, str] | None:
    """Return chronological task order for generated batch task IDs.

    Generated task IDs have the form ``<PROJECT>-<PROGRAM>-<YYYYMMDDX>-TNN``.
    Non-generated IDs intentionally return ``None`` so legacy ad-hoc tasks keep
    their existing priority-based behavior.
    """
    match = _BATCH_TASK_RE.match(external_id)
    if match is None:
        return None
    return (match.group("batch"), int(match.group("seq")), external_id)
