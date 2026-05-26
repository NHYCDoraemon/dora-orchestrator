"""Shared source-context visibility classification for operator CLIs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


SOURCE_EVIDENCE_MISSING_LABEL = "dora:source-evidence-missing"
SOURCE_CONTEXT_MISSING_LABEL = "dora:source-context-missing"
ORCHESTRATOR_INVALID_SUBMISSION_LABEL = "dora:orchestrator-invalid-submission"
SOURCE_PACKET_KEYS = ("source_docs", "source_tables", "source_queries")


def classify_source_context(issue: Mapping[str, object]) -> str:
    labels = set(_label_names(issue.get("labels")))
    labels.update(_label_names(issue.get("label_names")))
    if ORCHESTRATOR_INVALID_SUBMISSION_LABEL in labels:
        return "orchestrator_invalid_submission"
    if SOURCE_EVIDENCE_MISSING_LABEL in labels:
        return "source_evidence_missing"
    if SOURCE_CONTEXT_MISSING_LABEL in labels:
        return "source_context_missing"
    if issue.get("execution_packet_version") == 1 and all(
        key in issue for key in SOURCE_PACKET_KEYS
    ):
        return "packet_v1"
    return "legacy_or_missing_packet"


def _label_names(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, Mapping):
        yield from _label_names_from_mapping(value)
        return
    if not isinstance(value, Iterable):
        return
    for item in value:
        if isinstance(item, str):
            yield item
        elif isinstance(item, Mapping):
            yield from _label_names_from_mapping(item)


def _label_names_from_mapping(label: Mapping[object, object]) -> Iterable[str]:
    for key in ("name", "slug", "label"):
        value = label.get(key)
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            yield from _label_names_from_mapping(value)
    for key, value in label.items():
        if isinstance(key, str):
            yield key
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            yield from _label_names_from_mapping(value)
