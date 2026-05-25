"""Evaluate whether executor logs prove required source reads occurred."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

_READ_COMMANDS = {"cat", "sed", "nl", "head", "tail", "less", "more", "grep", "rg"}
_REDIRECT_OPERATORS = {">", ">>", ">|", "<>", "2>", "2>>", "&>", "&>>"}
_OUTPUT_REDIRECT_RE = re.compile(r"^(?:\d*)>>?|&>>?|>\|")
_SHELL_SEGMENT_SEPARATORS = {"&&", "||", ";", "|"}
_SHELL_COMMANDS = {"sh", "bash", "zsh", "dash", "ksh"}
_GREP_VALUE_OPTIONS = {
    "--after-context",
    "--before-context",
    "--context",
    "--glob",
    "--label",
    "--max-count",
    "--type",
    "-A",
    "-B",
    "-C",
    "-g",
    "-m",
    "-t",
}
_GREP_PATTERN_OPTIONS = {"--file", "--regexp", "-e", "-f"}


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
    observed = _observed_paths(events, root, required, relative_required)
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


def _observed_paths(
    events: Iterable[Mapping[str, Any]],
    worktree_root: Path,
    required_paths: tuple[Path, ...],
    relative_required_paths: Mapping[str, Path],
) -> tuple[Path, ...]:
    codex_paths: list[Path] = []
    claude_tool_paths: dict[str, tuple[Path, ...]] = {}
    successful_tool_ids: set[str] = set()

    for event in events:
        _collect_claude_tool_uses(
            event,
            worktree_root,
            required_paths,
            relative_required_paths,
            claude_tool_paths,
        )
        successful_tool_ids.update(_successful_claude_tool_result_ids(event))
        codex_paths.extend(_codex_completed_command_paths(event, worktree_root, required_paths, relative_required_paths))

    claude_paths = [
        path
        for tool_id in successful_tool_ids
        for path in claude_tool_paths.get(tool_id, ())
    ]
    return _unique_paths([*codex_paths, *claude_paths])


def _collect_claude_tool_uses(
    event: Mapping[str, Any],
    worktree_root: Path,
    required_paths: tuple[Path, ...],
    relative_required_paths: Mapping[str, Path],
    tool_paths: dict[str, tuple[Path, ...]],
) -> None:

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
                tool_id = str(block.get("id") or "")
                if not tool_id:
                    continue
                paths: list[Path] = []
                if name in {"Read", "Grep"}:
                    paths.extend(_normalize_path(path, worktree_root) for path in _input_path_values(inp))
                elif name == "Bash":
                    paths.extend(_command_paths(inp.get("command"), worktree_root, required_paths, relative_required_paths))
                if paths:
                    tool_paths[tool_id] = tuple(paths)


def _successful_claude_tool_result_ids(event: Mapping[str, Any]) -> tuple[str, ...]:
    ids: list[str] = []
    if event.get("type") != "user":
        return ()
    message = event.get("message")
    if not isinstance(message, Mapping):
        return ()
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") != "tool_result":
            continue
        tool_id = str(block.get("tool_use_id") or "")
        if tool_id and _tool_result_success(block):
            ids.append(tool_id)
    return tuple(ids)


def _tool_result_success(block: Mapping[str, Any]) -> bool:
    if block.get("is_error") is True:
        return False
    if "content" not in block:
        return False
    content = block.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return bool(content)
    if isinstance(content, Mapping):
        return bool(content)
    return content is not None


def _codex_completed_command_paths(
    event: Mapping[str, Any],
    worktree_root: Path,
    required_paths: tuple[Path, ...],
    relative_required_paths: Mapping[str, Path],
) -> tuple[Path, ...]:
    if event.get("type") != "item.completed":
        return ()
    item = event.get("item")
    if isinstance(item, Mapping):
        if item.get("type") != "command_execution":
            return ()
        status = str(item.get("status") or "completed")
        if status != "completed":
            return ()
        if item.get("exit_code") != 0:
            return ()
        command = item.get("command")
        if command is not None:
            return _command_paths(command, worktree_root, required_paths, relative_required_paths)
    return ()


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
        if any(_contains_shell_substitution(part) for part in parts):
            return ()
    elif isinstance(command, str):
        if _contains_shell_substitution(command):
            return ()
        parts = _split_command(command)
    else:
        return ()

    segments = _simple_command_segments(parts)
    if not segments or any(_has_output_redirection(segment) for segment in segments):
        return ()
    inner_commands = _shell_wrapper_inner_commands(parts)
    if inner_commands:
        paths: list[Path] = []
        for inner in inner_commands:
            paths.extend(_command_paths(inner, worktree_root, required_paths, relative_required_paths))
        return tuple(paths)
    if any(not _is_read_command(segment) for segment in segments):
        return ()
    paths: list[Path] = []
    for segment in segments:
        for part in _path_candidate_parts(segment):
            if _is_output_redirection_token(part):
                break
            token = _clean_token(part)
            paths.extend(_required_path_matches(token, worktree_root, required_paths, relative_required_paths))
    return tuple(paths)


def _split_command(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.split()


def _shell_wrapper_inner_commands(parts: list[str]) -> tuple[str, ...]:
    if _command_name(parts) not in _SHELL_COMMANDS:
        return ()
    for index, part in enumerate(parts[1:-1], start=1):
        if part == "--":
            return ()
        if part in {"-c", "-lc"}:
            return (parts[index + 1],)
        if not part.startswith("-"):
            return ()
    return ()


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
    if command == "sed" and not _sed_is_read_only(parts):
        return False
    return True


def _command_name(parts: list[str]) -> str:
    if not parts:
        return ""
    return Path(parts[0]).name


def _sed_is_in_place(parts: list[str]) -> bool:
    for part in parts[1:]:
        if part.startswith("--in-place"):
            return True
        if part.startswith("-") and not part.startswith("--") and "i" in part[1:]:
            return True
    return False


def _sed_is_read_only(parts: list[str]) -> bool:
    if _sed_is_in_place(parts):
        return False
    return not _sed_has_write_script(parts[1:])


def _sed_has_write_script(parts: list[str]) -> bool:
    script_seen = False
    index = 0
    while index < len(parts):
        part = parts[index]
        index += 1
        if part == "--":
            return False
        if part == "-e" or part == "--expression":
            if index >= len(parts):
                return False
            script_seen = True
            if _sed_script_writes(parts[index]):
                return True
            index += 1
            continue
        if part.startswith("--expression="):
            script_seen = True
            if _sed_script_writes(part.split("=", 1)[1]):
                return True
            continue
        if part == "-f" or part == "--file" or part.startswith("--file="):
            return True
        if part.startswith("-f") and len(part) > 2:
            return True
        if part.startswith("-e") and len(part) > 2:
            script_seen = True
            if _sed_script_writes(part[2:]):
                return True
            continue
        if part.startswith("-") and part != "-":
            continue
        if not script_seen:
            script_seen = True
            if _sed_script_writes(part):
                return True
    return False


def _sed_script_writes(script: str) -> bool:
    return any(_sed_command_writes(part) for part in re.split(r"[;{}\n]", script))


def _sed_command_writes(command: str) -> bool:
    stripped = command.strip()
    if not stripped:
        return False
    index = _sed_command_index(stripped)
    if index >= len(stripped):
        return False
    command_char = stripped[index]
    if command_char in {"e", "w", "W"}:
        return True
    if command_char == "s":
        return _sed_substitution_writes(stripped[index:])
    return False


def _sed_command_index(command: str) -> int:
    index = 0
    for address_number in range(2):
        index = _skip_space(command, index)
        next_index = _skip_sed_address(command, index)
        if next_index == index:
            break
        index = _skip_space(command, next_index)
        if address_number == 0 and index < len(command) and command[index] == ",":
            index += 1
            continue
        break
    index = _skip_space(command, index)
    if index < len(command) and command[index] == "!":
        index = _skip_space(command, index + 1)
    return index


def _skip_space(value: str, index: int) -> int:
    while index < len(value) and value[index].isspace():
        index += 1
    return index


def _skip_sed_address(command: str, index: int) -> int:
    if index >= len(command):
        return index
    if command[index].isdigit():
        index += 1
        while index < len(command) and command[index].isdigit():
            index += 1
        if index < len(command) and command[index] == "~":
            index += 1
            while index < len(command) and command[index].isdigit():
                index += 1
        return index
    if command[index] == "$":
        return index + 1
    if command[index] in {"+", "~"} and index + 1 < len(command) and command[index + 1].isdigit():
        index += 2
        while index < len(command) and command[index].isdigit():
            index += 1
        return index
    if command[index] == "/":
        return _skip_sed_delimited(command, index + 1, "/")
    if command[index] == "\\" and index + 1 < len(command):
        return _skip_sed_delimited(command, index + 2, command[index + 1])
    return index


def _skip_sed_delimited(value: str, index: int, delimiter: str) -> int:
    escaped = False
    while index < len(value):
        char = value[index]
        index += 1
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == delimiter:
            return index
    return len(value)


def _sed_substitution_writes(command: str) -> bool:
    if len(command) < 2:
        return False
    delimiter = command[1]
    if delimiter.isspace():
        return False
    index = _skip_sed_delimited(command, 2, delimiter)
    if index >= len(command):
        return False
    index = _skip_sed_delimited(command, index, delimiter)
    flags = command[index:]
    return bool(re.search(r"[eE]|[wW]\s*\S", flags))


def _path_candidate_parts(parts: list[str]) -> tuple[str, ...]:
    command = _command_name(parts)
    candidates = parts[1:]
    if command in {"grep", "rg"}:
        return _grep_file_operands(candidates)
    if command == "sed":
        return _sed_file_operands(candidates)
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


def _sed_file_operands(parts: list[str]) -> tuple[str, ...]:
    operands: list[str] = []
    script_seen = False
    index = 0
    options_done = False
    while index < len(parts):
        part = parts[index]
        index += 1
        if part == "--":
            options_done = True
            continue
        if not options_done and (part == "-e" or part == "--expression"):
            script_seen = True
            index += 1
            continue
        if not options_done and (part == "-f" or part == "--file"):
            script_seen = True
            index += 1
            continue
        if not options_done and (part.startswith("--expression=") or part.startswith("--file=")):
            script_seen = True
            continue
        if not options_done and (part.startswith("-e") or part.startswith("-f")) and len(part) > 2:
            script_seen = True
            continue
        if not options_done and part.startswith("-") and part != "-":
            continue
        if not script_seen:
            script_seen = True
            continue
        operands.append(part)
    return tuple(operands)


def _is_output_redirection_token(part: str) -> bool:
    return bool(_OUTPUT_REDIRECT_RE.match(part))


def _contains_shell_substitution(command: str) -> bool:
    return "$(" in command or "`" in command or "<(" in command or ">(" in command or "=(" in command


def _has_output_redirection(parts: tuple[str, ...]) -> bool:
    return any(_is_output_redirection_token(part) for part in parts)


def _grep_file_operands(parts: list[str]) -> tuple[str, ...]:
    operands: list[str] = []
    pattern_seen = False
    index = 0
    options_done = False
    while index < len(parts):
        part = parts[index]
        index += 1
        if part == "--":
            options_done = True
            continue
        if not options_done and _grep_option_consumes_value(part):
            index += 1
            continue
        if not options_done and _grep_option_embeds_value(part):
            continue
        if not options_done and _grep_option_supplies_pattern(part):
            pattern_seen = True
            index += 1
            continue
        if not options_done and _grep_pattern_option_embeds_value(part):
            pattern_seen = True
            continue
        if not options_done and part.startswith("-"):
            continue
        if not pattern_seen:
            pattern_seen = True
            continue
        operands.append(part)
    return tuple(operands)


def _grep_option_consumes_value(part: str) -> bool:
    return part in _GREP_VALUE_OPTIONS or part in {"--glob", "--type"}


def _grep_option_embeds_value(part: str) -> bool:
    if part.startswith("--"):
        name, sep, _value = part.partition("=")
        return bool(sep and name in _GREP_VALUE_OPTIONS)
    return (
        len(part) > 2
        and part[:2] in {"-A", "-B", "-C", "-g", "-m", "-t"}
    )


def _grep_option_supplies_pattern(part: str) -> bool:
    return part in _GREP_PATTERN_OPTIONS


def _grep_pattern_option_embeds_value(part: str) -> bool:
    if part.startswith("--"):
        name, sep, _value = part.partition("=")
        return bool(sep and name in _GREP_PATTERN_OPTIONS)
    return len(part) > 2 and part[:2] in {"-e", "-f"}


def _clean_token(token: str) -> str:
    return token.strip()


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
