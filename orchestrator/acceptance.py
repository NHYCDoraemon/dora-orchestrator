"""Acceptance-check classification, module floor, and generic runner."""

import os
import re
import subprocess
from pathlib import Path

CONTENT_KINDS = {"contains_sections", "min_matches"}
EXISTENCE_KINDS = {"file_exists", "file_min_bytes"}
ALL_KINDS = CONTENT_KINDS | EXISTENCE_KINDS | {"shell"}

DOC_MODULES = {"product", "architecture", "planning", "verification"}
CODE_MODULES = {"implementation", "operations"}

REQUIRED_PARAMS = {
    "file_exists": ("path",),
    "file_min_bytes": ("path", "min"),
    "contains_sections": ("path", "headings"),
    "min_matches": ("path", "pattern", "min"),
    "shell": ("cmd",),
}

_SHELL_TIMEOUT_SECONDS = 120


def is_trivial_shell(cmd: str) -> bool:
    c = str(cmd).strip()
    if c in {"true", ":", "test"}:
        return True
    if re.match(r"^test\s+-[sfe]\b", c):
        return True
    tokens = c.split()
    if tokens and tokens[0] in {"ls", "cat", "echo"}:
        return True
    return False


def classify_check(check: dict) -> str:
    kind = check.get("kind")
    if kind in CONTENT_KINDS:
        return "content"
    if kind == "shell":
        return "existence" if is_trivial_shell(check.get("cmd", "")) else "behavioral"
    return "existence"


def module_floor_satisfied(module: str, checks: list[dict]) -> bool:
    categories = {classify_check(c) for c in checks}
    if module in DOC_MODULES:
        return "content" in categories
    if module in CODE_MODULES:
        return "behavioral" in categories
    return bool(categories & {"content", "behavioral"})


def validate_check_structure(checks: list[dict]) -> list[str]:
    errors: list[str] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            errors.append(f"check #{index} is not a mapping")
            continue
        kind = check.get("kind")
        if kind not in ALL_KINDS:
            errors.append(f"check #{index}: unknown kind: {kind!r}")
            continue
        for param in REQUIRED_PARAMS[kind]:
            if param not in check:
                errors.append(f"check #{index} ({kind}): missing required param: {param}")
        if kind in {"file_min_bytes", "min_matches"} and "min" in check and not isinstance(check["min"], int):
            errors.append(f"check #{index} ({kind}): min must be an integer")
        if kind == "min_matches" and "pattern" in check:
            try:
                re.compile(str(check["pattern"]))
            except re.error as exc:
                errors.append(f"check #{index} (min_matches): illegal regex: {exc}")
        if kind == "contains_sections" and "headings" in check and not isinstance(check["headings"], list):
            errors.append(f"check #{index} (contains_sections): headings must be a list")
    return errors


def run_acceptance_checks(
    checks: list[dict],
    repo_root: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> dict:
    if not checks:
        return {"pass": True, "skipped": True, "results": []}
    results = []
    all_pass = True
    for check in checks:
        ok, detail = _run_one(check, repo_root, extra_env or {})
        all_pass = all_pass and ok
        results.append({"kind": check.get("kind", ""), "ok": ok, "detail": detail})
    return {"pass": all_pass, "skipped": False, "results": results}


def _run_one(check: dict, repo_root: Path, extra_env: dict[str, str]) -> tuple[bool, str]:
    kind = check.get("kind")
    if kind == "shell":
        return _run_shell(str(check.get("cmd", "")), repo_root, extra_env)
    if kind == "file_exists":
        path = repo_root / str(check.get("path", ""))
        return (path.is_file(), f"file_exists {path}")
    if kind == "file_min_bytes":
        path = repo_root / str(check.get("path", ""))
        if not path.is_file():
            return (False, f"missing file: {path}")
        size = path.stat().st_size
        return (size >= int(check.get("min", 0)), f"{size} bytes (min {check.get('min')})")
    if kind == "contains_sections":
        text = _read(repo_root / str(check.get("path", "")))
        if text is None:
            return (False, f"missing file: {check.get('path')}")
        lines = [line.strip() for line in text.splitlines()]
        missing = [h for h in (check.get("headings") or []) if not any(l.startswith(str(h)) for l in lines)]
        return (not missing, "all sections present" if not missing else f"missing sections: {missing}")
    if kind == "min_matches":
        text = _read(repo_root / str(check.get("path", "")))
        if text is None:
            return (False, f"missing file: {check.get('path')}")
        try:
            count = sum(1 for _ in re.finditer(str(check.get("pattern", "")), text, re.MULTILINE))
        except re.error as exc:
            return (False, f"illegal regex: {exc}")
        return (count >= int(check.get("min", 0)), f"{count} matches (min {check.get('min')})")
    return (False, f"unknown check kind: {kind}")


def _run_shell(cmd: str, repo_root: Path, extra_env: dict[str, str]) -> tuple[bool, str]:
    env = {**os.environ, **extra_env}
    try:
        proc = subprocess.run(
            cmd, cwd=str(repo_root), shell=True, capture_output=True,
            text=True, timeout=_SHELL_TIMEOUT_SECONDS, env=env,
        )
    except subprocess.TimeoutExpired:
        return (False, f"timeout: {cmd}")
    return (proc.returncode == 0, f"rc={proc.returncode}: {cmd}")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
