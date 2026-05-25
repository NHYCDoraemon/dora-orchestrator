"""Evaluate whether executor logs prove required source reads occurred."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class SourceEvidenceResult:
    ok: bool
    required_paths: tuple[Path, ...]
    observed_paths: tuple[Path, ...]
    missing_paths: tuple[Path, ...]
    message: str


def evaluate_source_evidence_from_event_path(
    event_path: Path,
    *,
    worktree_root: Path,
    required_paths: Iterable[Path],
) -> SourceEvidenceResult:
    events: list[Mapping[str, Any]] = []
    try:
        with event_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(event, Mapping):
                    events.append(event)
    except FileNotFoundError:
        pass
    return evaluate_source_evidence(events=events, worktree_root=worktree_root, required_paths=required_paths)


def evaluate_source_evidence(
    *,
    events: Iterable[Mapping[str, Any]],
    worktree_root: Path,
    required_paths: Iterable[Path],
) -> SourceEvidenceResult:
    root = worktree_root.resolve()
    required = tuple(_normalize_path(path, root) for path in required_paths)
    observed = _unique_paths(_normalize_path(path, root) for event in events for path in _event_paths(event))
    observed_set = set(observed)
    missing = tuple(path for path in required if path not in observed_set)
    ok = not missing
    if ok:
        message = f"source evidence satisfied: {len(required)} required read(s) observed"
    else:
        message = "missing source read evidence: " + ", ".join(str(path) for path in missing)
    return SourceEvidenceResult(
        ok=ok,
        required_paths=required,
        observed_paths=observed,
        missing_paths=missing,
        message=message,
    )


def _event_paths(event: Mapping[str, Any]) -> tuple[Path, ...]:
    paths: list[Path] = []

    message = event.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "")
                inp = block.get("input")
                if not isinstance(inp, Mapping):
                    continue
                if name in {"Read", "Grep", "Glob"}:
                    paths.extend(_input_path_values(inp))
                elif name == "Bash":
                    paths.extend(_command_paths(inp.get("command")))

    item = event.get("item")
    if isinstance(item, Mapping):
        command = item.get("command")
        if command is not None:
            paths.extend(_command_paths(command))

    return tuple(paths)


def _input_path_values(inp: Mapping[str, Any]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for key in ("file_path", "path"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(Path(value.strip()))
    return tuple(paths)


def _command_paths(command: Any) -> tuple[Path, ...]:
    if isinstance(command, list):
        parts = [str(item) for item in command]
    elif isinstance(command, str):
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = command.split()
    else:
        return ()

    paths: list[Path] = []
    for part in parts:
        token = _clean_token(part)
        if _is_path_like(token):
            paths.append(Path(token))
    return tuple(paths)


def _clean_token(token: str) -> str:
    cleaned = token.strip().strip("\"'`,;()[]{}<>")
    cleaned = re.sub(r"(?<=\S):\d+(?::\d+)?$", "", cleaned)
    return cleaned


def _is_path_like(token: str) -> bool:
    if not token:
        return False
    return (
        token.startswith("/")
        or token.startswith("./")
        or token.startswith("../")
        or token.startswith("docs/")
        or ".dora/source-bundles/" in token
    )


def _normalize_path(path: Path, worktree_root: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    return (worktree_root / path).resolve()


def _unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    return tuple(ordered)
