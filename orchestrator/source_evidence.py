"""Evaluate whether executor logs prove required source reads occurred."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

_READ_COMMANDS = {"cat", "sed", "nl", "head", "tail", "less", "more", "grep", "rg", "awk"}
_REDIRECT_OPERATORS = {">", ">>", ">|", "<>", "2>", "2>>", "&>", "&>>"}
_SHELL_SEGMENT_SEPARATORS = {"&&", "||", ";", "|"}


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
    relative_required = _required_relative_paths(required, root)
    observed = _unique_paths(path for event in events for path in _event_paths(event, root, required, relative_required))
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


def _event_paths(
    event: Mapping[str, Any],
    worktree_root: Path,
    required_paths: tuple[Path, ...],
    relative_required_paths: Mapping[str, Path],
) -> tuple[Path, ...]:
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
                    paths.extend(_normalize_path(path, worktree_root) for path in _input_path_values(inp))
                elif name == "Bash":
                    paths.extend(_command_paths(inp.get("command"), worktree_root, required_paths, relative_required_paths))

    item = event.get("item")
    if isinstance(item, Mapping):
        command = item.get("command")
        if command is not None:
            paths.extend(_command_paths(command, worktree_root, required_paths, relative_required_paths))

    return tuple(paths)


def _input_path_values(inp: Mapping[str, Any]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for key in ("file_path", "path"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(Path(value.strip()))
    return tuple(paths)


def _command_paths(
    command: Any,
    worktree_root: Path,
    required_paths: tuple[Path, ...],
    relative_required_paths: Mapping[str, Path],
) -> tuple[Path, ...]:
    if isinstance(command, list):
        parts = [str(item) for item in command]
    elif isinstance(command, str):
        parts = _split_command(command)
    else:
        return ()

    paths: list[Path] = []
    for inner in _shell_wrapper_inner_commands(parts):
        paths.extend(_command_paths(inner, worktree_root, required_paths, relative_required_paths))
    for segment in _simple_command_segments(parts):
        if not _is_read_command(segment):
            continue
        for part in _path_candidate_parts(segment):
            token = _clean_token(part)
            paths.extend(_required_path_matches(token, worktree_root, required_paths, relative_required_paths))
    return tuple(paths)


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _shell_wrapper_inner_commands(parts: list[str]) -> tuple[str, ...]:
    commands: list[str] = []
    for index, part in enumerate(parts[:-1]):
        if part in {"-c", "-lc"}:
            commands.append(parts[index + 1])
    return tuple(commands)


def _simple_command_segments(parts: list[str]) -> tuple[tuple[str, ...], ...]:
    segments: list[tuple[str, ...]] = []
    current: list[str] = []
    for part in parts:
        if part in _SHELL_SEGMENT_SEPARATORS:
            if current:
                segments.append(tuple(current))
                current = []
            continue
        current.append(part)
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def _is_read_command(parts: list[str]) -> bool:
    command = _command_name(parts)
    if command not in _READ_COMMANDS:
        return False
    if any(part in _REDIRECT_OPERATORS for part in parts):
        return False
    return True


def _command_name(parts: list[str]) -> str:
    if not parts:
        return ""
    return Path(parts[0]).name


def _path_candidate_parts(parts: list[str]) -> tuple[str, ...]:
    command = _command_name(parts)
    candidates = parts[1:]
    if command in {"grep", "rg"}:
        return _grep_file_operands(candidates)
    if command == "awk":
        for index, part in enumerate(candidates):
            if part == "--":
                return tuple(candidates[index + 1:])
            if _looks_like_awk_program(part):
                return tuple(candidates[index + 1:])
        return ()
    return tuple(candidates)


def _looks_like_awk_program(part: str) -> bool:
    return "{" in part or "}" in part


def _grep_file_operands(parts: list[str]) -> tuple[str, ...]:
    operands: list[str] = []
    pattern_seen = False
    for part in parts:
        if part == "--":
            continue
        if part.startswith("-"):
            continue
        if not pattern_seen:
            pattern_seen = True
            continue
        operands.append(part)
    return tuple(operands)


def _clean_token(token: str) -> str:
    cleaned = token.strip().strip("\"'`,;()[]{}<>")
    cleaned = re.sub(r"(?<=\S):\d+(?::\d+)?$", "", cleaned)
    return cleaned


def _required_path_matches(
    token: str,
    worktree_root: Path,
    required_paths: tuple[Path, ...],
    relative_required_paths: Mapping[str, Path],
) -> tuple[Path, ...]:
    if not token:
        return ()
    normalized = _normalize_path(Path(token), worktree_root)
    if normalized in required_paths:
        return (normalized,)
    relative_token = token.removeprefix("./").replace("\\", "/")
    match = relative_required_paths.get(relative_token)
    return (match,) if match is not None else ()


def _required_relative_paths(required_paths: tuple[Path, ...], worktree_root: Path) -> dict[str, Path]:
    relative: dict[str, Path] = {}
    for path in required_paths:
        try:
            relative[path.relative_to(worktree_root).as_posix()] = path
        except ValueError:
            continue
    return relative


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
