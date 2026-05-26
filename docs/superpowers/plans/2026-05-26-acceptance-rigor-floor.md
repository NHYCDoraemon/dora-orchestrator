# Acceptance Rigor Floor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an output-side acceptance boundary so weak verification (e.g. `test -s file`) can no longer let hollow/framework deliverables be marked Done.

**Architecture:** A new structured `acceptance_checks` schema in task frontmatter, classifiable by audit; a generic `orchestrator/acceptance.py` runner whose output is shape-compatible with the existing verification result; a module-aware rigor floor enforced at audit; loop Done wiring that runs `acceptance_checks` when present and falls back to `verification_commands` otherwise. The live backend round-trips the new field through its existing JSON metadata block.

**Tech Stack:** Python 3.10+, `unittest` (no pytest), existing `orchestrator` package, `dataclass(frozen=True)`, `pathlib.Path`, custom frontmatter parser in `batch_loader.py`.

Reference spec: `docs/specs/2026-05-01-dora-orchestration-design.md` and `docs/superpowers/specs/2026-05-26-acceptance-rigor-floor-design.md`.

---

## File Structure

- Modify `orchestrator/batch_loader.py`: parse `acceptance_checks` (mapping list with nested `headings` scalar list) into `task.metadata`.
- Create `orchestrator/acceptance.py`: check classification + module floor + structure validation + generic runner.
- Modify `orchestrator/batch_audit.py`: enforce the module-aware rigor floor as a new `acceptance_rigor` finding.
- Modify `orchestrator/batch_submit.py`: propagate `acceptance_checks` into the `upsert_issue` payload.
- Modify `orchestrator/plane_live.py`: add `"acceptance_checks"` to `_DORA_METADATA_KEYS`.
- Modify `orchestrator/run_ready_task.py`: run `acceptance_checks` when present, else fall back to `verification_commands`.
- Tests: `tests/test_acceptance.py` (new), and additions to `tests/test_batch_audit.py`, `tests/test_batch_submit.py`, `tests/test_live_plane.py`, `tests/test_run_ready_batch_task.py`, `tests/test_source_bundle.py` (loader).

Run the suite with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
```

---

### Task 1: Loader parses `acceptance_checks`

The frontmatter parser is hand-rolled and only understands specific nested keys. `acceptance_checks` is a mapping list (like `source_queries`) whose items have keys `kind, path, headings, pattern, min, cmd`, where `headings` is a nested scalar list.

**Files:**
- Modify: `orchestrator/batch_loader.py`
- Test: `tests/test_batch_loader.py` (create if absent)

- [ ] **Step 1: Write the failing test**

Create/append `tests/test_batch_loader.py`:

```python
import tempfile
import unittest
from pathlib import Path

from orchestrator.batch_loader import load_task_issue_batch


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_TASK_SECTIONS = """
# Task Summary

概要。

# Development Context

背景。

# Scope

范围。

# Non-goals

非目标。

# Implementation Detail

实现。

# Acceptance

验收。

# Verification

验证。

# Stop Conditions

停止。

# Executor Prompt Contract

契约。
"""


class LoaderAcceptanceChecksTest(unittest.TestCase):
    def test_loads_acceptance_checks_mapping_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            base = repo / "docs" / "dora" / "batches" / "20260526A"
            _write(base / "batch.md", (
                "---\n"
                "batch_id: 20260526A\n"
                "program_id: ACC\n"
                "program_prefix: ACC\n"
                "title: \"验收下限\"\n"
                "status: draft\n"
                "---\n\n# 批次\n\n说明。\n"
            ))
            _write(base / "program-page.md", "# 计划\n\n说明。\n")
            _write(base / "tasks" / "ACC-ACC-20260526A-T01.md", (
                "---\n"
                "task_id: ACC-ACC-20260526A-T01\n"
                "title: \"验收任务\"\n"
                "module: verification\n"
                "sequence: 1\n"
                "batch_id: 20260526A\n"
                "program_prefix: ACC\n"
                "acceptance_checks:\n"
                "  - kind: contains_sections\n"
                "    path: docs/audit/report.md\n"
                "    headings:\n"
                "      - \"## A.\"\n"
                "      - \"## B.\"\n"
                "  - kind: min_matches\n"
                "    path: docs/audit/report.md\n"
                "    pattern: '^\\| .+ \\|'\n"
                "    min: 10\n"
                "---\n"
                f"{_TASK_SECTIONS}"
            ))
            batch = load_task_issue_batch(base, repo_root=repo)
            checks = batch.tasks[0].metadata["acceptance_checks"]
            self.assertEqual(checks[0]["kind"], "contains_sections")
            self.assertEqual(checks[0]["headings"], ["## A.", "## B."])
            self.assertEqual(checks[1]["kind"], "min_matches")
            self.assertEqual(checks[1]["min"], 10)
            self.assertEqual(checks[1]["pattern"], "^\\| .+ \\|")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_batch_loader.LoaderAcceptanceChecksTest.test_loads_acceptance_checks_mapping_list`
Expected: FAIL with `ValueError: unsupported nested YAML ...` (the parser rejects the unknown nested block).

- [ ] **Step 3: Implement the parser branch**

In `orchestrator/batch_loader.py`, add a key set near the existing ones (after line 12, the `SOURCE_FILTER_KEYS` definition):

```python
ACCEPTANCE_CHECK_KEYS = {"kind", "path", "headings", "pattern", "min", "cmd"}
```

In `_parse_yaml_block`, add a branch before the existing `if key == "source_queries":` block (so it is handled like a mapping list):

```python
    if key == "acceptance_checks":
        return _parse_yaml_mapping_list(
            lines,
            index,
            indent=indent,
            path=path,
            allowed_keys=ACCEPTANCE_CHECK_KEYS,
            bool_scalar_keys=set(),
            nested_scalar_lists={"headings"},
            nested_mapping_lists=set(),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_batch_loader.LoaderAcceptanceChecksTest.test_loads_acceptance_checks_mapping_list`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/batch_loader.py tests/test_batch_loader.py
git commit -m "feat(loader): parse acceptance_checks mapping list"
```

---

### Task 2: Acceptance runner + classification module

`orchestrator/acceptance.py` owns: check classification, the trivial-shell deny-list, the module floor predicate, structure validation (for audit), and the generic runner (for the loop). The runner output is shape-compatible with `_run_verification_commands` (`{"pass", "skipped", "results"}`).

**Files:**
- Create: `orchestrator/acceptance.py`
- Test: `tests/test_acceptance.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_acceptance.py`:

```python
import tempfile
import unittest
from pathlib import Path

from orchestrator.acceptance import (
    classify_check,
    is_trivial_shell,
    module_floor_satisfied,
    run_acceptance_checks,
    validate_check_structure,
)


class ClassifyTest(unittest.TestCase):
    def test_content_checks(self):
        self.assertEqual(classify_check({"kind": "contains_sections"}), "content")
        self.assertEqual(classify_check({"kind": "min_matches"}), "content")

    def test_existence_checks(self):
        self.assertEqual(classify_check({"kind": "file_exists"}), "existence")
        self.assertEqual(classify_check({"kind": "file_min_bytes"}), "existence")

    def test_shell_behavioral_vs_trivial(self):
        self.assertEqual(classify_check({"kind": "shell", "cmd": "go test ./..."}), "behavioral")
        self.assertEqual(classify_check({"kind": "shell", "cmd": "test -s file.md"}), "existence")

    def test_unknown_kind_is_weak(self):
        self.assertEqual(classify_check({"kind": "nonsense"}), "existence")


class TrivialShellTest(unittest.TestCase):
    def test_trivial(self):
        for cmd in ["test -s x", "test -f x", "test -e x", "test", "true", ":", "ls", "cat x", "echo hi"]:
            self.assertTrue(is_trivial_shell(cmd), cmd)

    def test_non_trivial(self):
        for cmd in ["go test ./...", "grep -E '^\\| .+ \\|' file", "pytest -q", "go build ./..."]:
            self.assertFalse(is_trivial_shell(cmd), cmd)


class ModuleFloorTest(unittest.TestCase):
    def test_doc_module_requires_content(self):
        self.assertFalse(module_floor_satisfied("verification", [{"kind": "file_exists"}]))
        self.assertTrue(module_floor_satisfied("verification", [{"kind": "contains_sections"}]))

    def test_code_module_requires_behavioral(self):
        self.assertFalse(module_floor_satisfied("implementation", [{"kind": "shell", "cmd": "test -s x"}]))
        self.assertTrue(module_floor_satisfied("implementation", [{"kind": "shell", "cmd": "go test ./..."}]))

    def test_governance_accepts_either(self):
        self.assertTrue(module_floor_satisfied("governance", [{"kind": "min_matches"}]))
        self.assertTrue(module_floor_satisfied("governance", [{"kind": "shell", "cmd": "go test ./..."}]))
        self.assertFalse(module_floor_satisfied("governance", [{"kind": "file_exists"}]))


class ValidateStructureTest(unittest.TestCase):
    def test_unknown_kind(self):
        errs = validate_check_structure([{"kind": "bogus"}])
        self.assertTrue(any("unknown" in e for e in errs))

    def test_missing_param(self):
        errs = validate_check_structure([{"kind": "min_matches", "path": "x", "min": 1}])
        self.assertTrue(any("pattern" in e for e in errs))

    def test_bad_regex(self):
        errs = validate_check_structure([{"kind": "min_matches", "path": "x", "pattern": "(", "min": 1}])
        self.assertTrue(any("regex" in e for e in errs))

    def test_min_must_be_int(self):
        errs = validate_check_structure([{"kind": "file_min_bytes", "path": "x", "min": "ten"}])
        self.assertTrue(any("min" in e for e in errs))

    def test_valid(self):
        self.assertEqual(validate_check_structure([
            {"kind": "contains_sections", "path": "x", "headings": ["## A."]},
            {"kind": "shell", "cmd": "go test ./..."},
        ]), [])


class RunnerTest(unittest.TestCase):
    def test_empty_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_acceptance_checks([], Path(tmp))
            self.assertTrue(result["pass"])
            self.assertTrue(result["skipped"])

    def test_contains_sections_pass_and_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "doc.md").write_text("## A. intro\nbody\n## B. seams\n", encoding="utf-8")
            ok = run_acceptance_checks(
                [{"kind": "contains_sections", "path": "doc.md", "headings": ["## A.", "## B."]}], repo)
            self.assertTrue(ok["pass"])
            bad = run_acceptance_checks(
                [{"kind": "contains_sections", "path": "doc.md", "headings": ["## C."]}], repo)
            self.assertFalse(bad["pass"])

    def test_min_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "doc.md").write_text("| a | b |\n| c | d |\n| e | f |\n", encoding="utf-8")
            ok = run_acceptance_checks(
                [{"kind": "min_matches", "path": "doc.md", "pattern": r"^\| .+ \|", "min": 3}], repo)
            self.assertTrue(ok["pass"])
            bad = run_acceptance_checks(
                [{"kind": "min_matches", "path": "doc.md", "pattern": r"^\| .+ \|", "min": 5}], repo)
            self.assertFalse(bad["pass"])

    def test_missing_file_fails_not_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_acceptance_checks(
                [{"kind": "contains_sections", "path": "nope.md", "headings": ["## A."]}], Path(tmp))
            self.assertFalse(result["pass"])

    def test_file_min_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "doc.md").write_text("0123456789", encoding="utf-8")
            self.assertTrue(run_acceptance_checks(
                [{"kind": "file_min_bytes", "path": "doc.md", "min": 10}], repo)["pass"])
            self.assertFalse(run_acceptance_checks(
                [{"kind": "file_min_bytes", "path": "doc.md", "min": 11}], repo)["pass"])

    def test_shell_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(run_acceptance_checks([{"kind": "shell", "cmd": "true"}], Path(tmp))["pass"])
            self.assertFalse(run_acceptance_checks([{"kind": "shell", "cmd": "false"}], Path(tmp))["pass"])

    def test_unknown_kind_at_runtime_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(run_acceptance_checks([{"kind": "bogus"}], Path(tmp))["pass"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_acceptance`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.acceptance'`.

- [ ] **Step 3: Implement `orchestrator/acceptance.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_acceptance`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/acceptance.py tests/test_acceptance.py
git commit -m "feat(acceptance): add check classification, module floor, and runner"
```

---

### Task 3: Audit rigor gate

Audit validates `acceptance_checks` structure and enforces the module floor for every task, emitting `acceptance_rigor` findings on failure.

**Files:**
- Modify: `orchestrator/batch_audit.py`
- Test: `tests/test_batch_audit.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_batch_audit.py` (reuse the file's existing batch-building helpers; if it builds batches inline, mirror that style). These tests assume a helper that writes a one-task batch and returns the batch dir — adapt the names to the existing harness in that file. If the file already has a `_make_batch`/`create_batch` helper, call it; otherwise add the inline writer shown here:

```python
import tempfile
import unittest
from pathlib import Path

from orchestrator.batch_audit import audit_task_issue_batch


_SECTIONS = (
    "# Task Summary\n\n概要。\n\n# Development Context\n\n背景。\n\n# Scope\n\n范围。\n\n"
    "# Non-goals\n\n非目标。\n\n# Implementation Detail\n\n实现。\n\n# Acceptance\n\n验收。\n\n"
    "# Verification\n\n验证。\n\n# Stop Conditions\n\n停止。\n\n# Executor Prompt Contract\n\n契约。\n"
)


def _build_batch(tmp: str, *, module: str, acceptance_block: str) -> Path:
    repo = Path(tmp)
    base = repo / "docs" / "dora" / "batches" / "20260526A"
    (base / "tasks").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "audit").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "audit" / "report.md").write_text("## A. x\n| a | b |\n", encoding="utf-8")
    (base / "batch.md").write_text(
        "---\nbatch_id: 20260526A\nprogram_id: ACC\nprogram_prefix: ACC\n"
        "title: \"验收下限\"\nstatus: draft\n---\n\n# 批次\n\n说明。\n", encoding="utf-8")
    (base / "program-page.md").write_text("# 计划\n\n说明。\n", encoding="utf-8")
    (base / "tasks" / "ACC-ACC-20260526A-T01.md").write_text(
        "---\n"
        "task_id: ACC-ACC-20260526A-T01\n"
        "title: \"验收任务\"\n"
        f"module: {module}\n"
        "sequence: 1\n"
        "batch_id: 20260526A\n"
        "program_prefix: ACC\n"
        "source_pages:\n  - ../program-page.md\n"
        "source_docs:\n  - docs/audit/report.md\n"
        "source_summaries:\n  - docs/audit/report.md\n"
        f"{acceptance_block}"
        "---\n"
        f"{_SECTIONS}", encoding="utf-8")
    return base


class AcceptanceRigorAuditTest(unittest.TestCase):
    def test_doc_module_with_only_file_exists_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = (
                "acceptance_checks:\n"
                "  - kind: file_exists\n"
                "    path: docs/audit/report.md\n"
            )
            base = _build_batch(tmp, module="verification", acceptance_block=block)
            result = audit_task_issue_batch(base, repo_root=Path(tmp))
            self.assertEqual(result.status, "FAIL")
            self.assertTrue(any(f.code == "acceptance_rigor" for f in result.findings))

    def test_doc_module_with_content_check_passes_rigor(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = (
                "acceptance_checks:\n"
                "  - kind: contains_sections\n"
                "    path: docs/audit/report.md\n"
                "    headings:\n      - \"## A.\"\n"
            )
            base = _build_batch(tmp, module="verification", acceptance_block=block)
            result = audit_task_issue_batch(base, repo_root=Path(tmp))
            self.assertFalse(any(f.code == "acceptance_rigor" for f in result.findings),
                             [f.message for f in result.findings])

    def test_no_acceptance_checks_fails_rigor(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = _build_batch(tmp, module="verification", acceptance_block="")
            result = audit_task_issue_batch(base, repo_root=Path(tmp))
            self.assertTrue(any(f.code == "acceptance_rigor" for f in result.findings))

    def test_implementation_trivial_shell_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = (
                "acceptance_checks:\n"
                "  - kind: shell\n"
                "    cmd: \"test -s docs/audit/report.md\"\n"
            )
            base = _build_batch(tmp, module="implementation", acceptance_block=block)
            result = audit_task_issue_batch(base, repo_root=Path(tmp))
            self.assertTrue(any(f.code == "acceptance_rigor" for f in result.findings))

    def test_implementation_real_shell_passes_rigor(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = (
                "acceptance_checks:\n"
                "  - kind: shell\n"
                "    cmd: \"go test ./...\"\n"
            )
            base = _build_batch(tmp, module="implementation", acceptance_block=block)
            result = audit_task_issue_batch(base, repo_root=Path(tmp))
            self.assertFalse(any(f.code == "acceptance_rigor" for f in result.findings),
                             [f.message for f in result.findings])

    def test_illegal_regex_fails_rigor(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = (
                "acceptance_checks:\n"
                "  - kind: min_matches\n"
                "    path: docs/audit/report.md\n"
                "    pattern: '('\n"
                "    min: 1\n"
            )
            base = _build_batch(tmp, module="verification", acceptance_block=block)
            result = audit_task_issue_batch(base, repo_root=Path(tmp))
            self.assertTrue(any(f.code == "acceptance_rigor" and "regex" in f.message
                                for f in result.findings))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_batch_audit.AcceptanceRigorAuditTest`
Expected: FAIL — no `acceptance_rigor` findings are produced yet (audit does not check `acceptance_checks`).

- [ ] **Step 3: Implement the audit gate**

In `orchestrator/batch_audit.py`, add the import near the top (after the existing `from .source_preflight import ...` line):

```python
from .acceptance import module_floor_satisfied, validate_check_structure
```

Add a new audit function (place it after `_audit_progress_metadata`):

```python
def _audit_acceptance_rigor(task: TaskIssueDraft, findings: list[AuditFinding]) -> None:
    raw = task.metadata.get("acceptance_checks")
    checks = [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
    if not checks:
        findings.append(
            AuditFinding(
                code="acceptance_rigor",
                message="task must declare acceptance_checks meeting its module rigor floor",
                path=str(task.path),
            )
        )
        return
    for error in validate_check_structure(checks):
        findings.append(AuditFinding(code="acceptance_rigor", message=error, path=str(task.path)))
    if not module_floor_satisfied(task.module, checks):
        findings.append(
            AuditFinding(
                code="acceptance_rigor",
                message=f"module '{task.module}' requires a stronger acceptance check (content or non-trivial behavioral)",
                path=str(task.path),
            )
        )
```

Call it inside the per-task loop in `audit_task_issue_batch` (after `_audit_progress_metadata(task, findings)`):

```python
        _audit_acceptance_rigor(task, findings)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_batch_audit.AcceptanceRigorAuditTest`
Expected: PASS.

- [ ] **Step 5: Run the full audit test module to catch regressions**

Run: `python3 -m unittest tests.test_batch_audit`
Expected: PASS. If pre-existing batch-audit tests now FAIL because their fixtures lack `acceptance_checks`, that is expected — the floor is new. Update those fixtures to include a minimal module-appropriate check (e.g. add a `contains_sections`/`shell` block matching each fixture's module). Keep edits limited to test fixtures, not production logic.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/batch_audit.py tests/test_batch_audit.py
git commit -m "feat(audit): enforce module-aware acceptance rigor floor"
```

---

### Task 4: Submit propagates `acceptance_checks`

The submit path must carry `acceptance_checks` into the issue payload so the loop can read it.

**Files:**
- Modify: `orchestrator/batch_submit.py`
- Test: `tests/test_batch_submit.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_batch_submit.py` (reuse the file's existing approved-batch helper; the snippet below assumes one named `create_approved_batch` that yields a submittable batch dir — adapt to the actual helper name in that file, and ensure its task declares an `acceptance_checks` block with a `contains_sections` check):

```python
def test_submit_propagates_acceptance_checks_into_issue(self):
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        batch_dir = create_approved_batch(repo)  # existing helper in this test module
        client = InMemoryPlaneClient()
        submit_task_issue_batch(
            batch_dir, repo_root=repo,
            project_slug="dora", project_title="Dora", plane_client=client,
        )
        issue = client.issues[("dora", "ACC-ACC-20260526A-T01")]
        self.assertIsInstance(issue["acceptance_checks"], list)
        self.assertEqual(issue["acceptance_checks"][0]["kind"], "contains_sections")
```

If `create_approved_batch` does not already attach `acceptance_checks`, extend that helper so its task frontmatter includes:

```yaml
acceptance_checks:
  - kind: contains_sections
    path: docs/audit/report.md
    headings:
      - "## A."
```

and recompute the approval hash if the helper writes `approval.json` (the existing helper presumably already calls audit+approve; re-run it so the hash matches the new frontmatter).

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_batch_submit` (the new test)
Expected: FAIL with `KeyError: 'acceptance_checks'` — submit does not include the field yet.

- [ ] **Step 3: Implement propagation**

In `orchestrator/batch_submit.py`, inside the `plane_client.upsert_issue(...)` payload for tasks (the dict starting at line ~102), add a line alongside `verification_commands` (line 118):

```python
                "acceptance_checks": _acceptance_checks_value(task.metadata.get("acceptance_checks")),
```

Add the helper near `_list_value` (bottom of the file):

```python
def _acceptance_checks_value(value: object) -> list[dict]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_batch_submit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/batch_submit.py tests/test_batch_submit.py
git commit -m "feat(submit): propagate acceptance_checks into issue payload"
```

---

### Task 5: Live backend round-trips `acceptance_checks`

Add `acceptance_checks` to the live JSON metadata block so it survives the write→read cycle exactly like `source_queries`.

**Files:**
- Modify: `orchestrator/plane_live.py`
- Test: `tests/test_live_plane.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_live_plane.py`. Use the file's existing metadata round-trip pattern — most likely `_append_metadata_block` + `_extract_metadata_block` are tested directly. Mirror that:

```python
from orchestrator.plane_live import (
    _DORA_METADATA_KEYS,
    _append_metadata_block,
    _extract_metadata_block,
    _metadata_payload,
)


class AcceptanceChecksMetadataTest(unittest.TestCase):
    def test_acceptance_checks_round_trips_through_metadata_block(self):
        self.assertIn("acceptance_checks", _DORA_METADATA_KEYS)
        payload = {"acceptance_checks": [
            {"kind": "contains_sections", "path": "docs/x.md", "headings": ["## A.", "## B."]},
            {"kind": "min_matches", "path": "docs/x.md", "pattern": r"^\| .+ \|", "min": 10},
        ]}
        block = _append_metadata_block("body", _metadata_payload(payload))
        # _extract_metadata_block reads from a <pre>-wrapped html body
        html_body = "<pre>" + block.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</pre>"
        extracted = _extract_metadata_block(html_body)
        self.assertEqual(extracted["acceptance_checks"], payload["acceptance_checks"])
```

If `tests/test_live_plane.py` already has a helper that builds the `<pre>`-wrapped description (look for `_markdown_to_html`), use it instead of the inline escaping above:

```python
from orchestrator.plane_live import _markdown_to_html
html_body = _markdown_to_html(block)
extracted = _extract_metadata_block(html_body)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_live_plane.AcceptanceChecksMetadataTest`
Expected: FAIL on `assertIn("acceptance_checks", _DORA_METADATA_KEYS)`.

- [ ] **Step 3: Implement**

In `orchestrator/plane_live.py`, add `"acceptance_checks"` to `_DORA_METADATA_KEYS` (line 25-32):

```python
_DORA_METADATA_KEYS = [
    "execution_packet_version",
    "execution_packet_hash",
    "source_docs",
    "source_tables",
    "source_queries",
    "verification_commands",
    "acceptance_checks",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_live_plane.AcceptanceChecksMetadataTest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/plane_live.py tests/test_live_plane.py
git commit -m "feat(plane-live): round-trip acceptance_checks via metadata block"
```

---

### Task 6: Loop runs `acceptance_checks`, falls back to `verification_commands`

The Done decision must run `acceptance_checks` when present and otherwise fall back to `verification_commands`. The result feeds the existing outcome logic unchanged.

**Files:**
- Modify: `orchestrator/run_ready_task.py`
- Test: `tests/test_run_ready_batch_task.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_run_ready_batch_task.py`. Reuse the module's existing harness (it already submits a batch to an `InMemoryPlaneClient`, runs `run_ready_batch_task` with a `noop`/fake executor, and asserts outcomes). Add a test where the issue carries an `acceptance_checks` content check that fails, and assert the task lands Partial / `agent_unverified` rather than Done:

```python
def test_failing_acceptance_check_blocks_done(self):
    # Build a one-task batch whose acceptance_checks require a section that
    # the executor never writes, using the existing in-memory harness.
    client = InMemoryPlaneClient()
    client.upsert_issue("dora", "ACC-ACC-20260526A-T01", {
        "name": "Acceptance task",
        "issue_type": "task",
        "priority": "P1",
        "depends_on": [],
        "execution_packet_version": EXECUTION_PACKET_VERSION,
        "source_docs": [],
        "source_tables": [],
        "source_queries": [],
        "acceptance_checks": [
            {"kind": "contains_sections", "path": "docs/never.md", "headings": ["## Z."]},
        ],
        "verification_commands": [],
    })
    # Run with an executor stub that returns agent_done but writes nothing.
    config = _config_for(client)  # existing helper; executor="noop"
    result = run_ready_batch_task(config, plane_client=client, run_id="run-acc-1", max_loops=1)
    run = result["runs"][0]
    self.assertNotEqual(run["state"], "Done")
    self.assertIn(run["outcome"], {"agent_unverified", "source_evidence_missing"})
```

Adapt `_config_for` / the executor stub to the actual helpers in this test module. If the module already has a "verification fails → Partial" test, copy its scaffolding and swap `verification_commands` for `acceptance_checks`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_run_ready_batch_task.RunReadyBatchTaskTest.test_failing_acceptance_check_blocks_done`
Expected: FAIL — the loop currently ignores `acceptance_checks` and (with empty `verification_commands`) treats verification as skipped→pass, marking the task Done.

- [ ] **Step 3: Implement the loop wiring**

In `orchestrator/run_ready_task.py`, add the import (top, near the other `from .` imports):

```python
from .acceptance import run_acceptance_checks
```

Add a helper near `_run_verification_commands` (after it, ~line 1593):

```python
def _issue_acceptance_checks(issue: Mapping[str, object]) -> list[dict]:
    value = issue.get("acceptance_checks")
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    return []
```

Replace the verification call in `_execute_one_task` (lines 402-406) with the branch:

```python
        acceptance_checks = _issue_acceptance_checks(claimed)
        if acceptance_checks:
            verification = run_acceptance_checks(
                acceptance_checks, repo_root, extra_env=config.executor_env or {},
            )
        else:
            verification = _run_verification_commands(
                list(claimed.get("verification_commands") or []),
                repo_root,
                extra_env=config.executor_env or {},
            )
```

Everything downstream (`_format_verification`, the outcome logic at 422-455, `publish_run_report`) consumes the same `{"pass","skipped","results"}` shape and needs no change. Note `run_acceptance_checks` results carry a `"detail"` key per item while `_run_verification_commands` carries `"command"`/`"returncode"`; `_format_verification` reads `r['command']`/`r['returncode']`. Update `_format_verification` (line 982) to be tolerant:

```python
def _format_verification(verification: dict) -> str:
    if verification.get("skipped"):
        return "verification: skipped (no commands declared)"
    lines = [f"verification: {'pass' if verification['pass'] else 'fail'}"]
    for r in verification.get("results", []):
        label = r.get("command") or r.get("kind") or ""
        rc = r.get("returncode")
        rc_text = f"rc={rc} " if rc is not None else ""
        detail = f" — {r['detail']}" if r.get("detail") else ""
        lines.append(f"  - {rc_text}{'ok' if r['ok'] else 'FAIL'}: {label}{detail}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_run_ready_batch_task.RunReadyBatchTaskTest.test_failing_acceptance_check_blocks_done`
Expected: PASS.

- [ ] **Step 5: Run the loop test module to catch regressions**

Run: `python3 -m unittest tests.test_run_ready_batch_task`
Expected: PASS (existing `verification_commands`-based tests still pass via the fallback branch).

- [ ] **Step 6: Commit**

```bash
git add orchestrator/run_ready_task.py tests/test_run_ready_batch_task.py
git commit -m "feat(loop): gate Done on acceptance_checks with verification_commands fallback"
```

---

### Task 7: Final verification

**Files:**
- No code changes.

- [ ] **Step 1: Run the focused suite**

```bash
python3 -m unittest \
  tests.test_batch_loader \
  tests.test_acceptance \
  tests.test_batch_audit \
  tests.test_batch_submit \
  tests.test_live_plane \
  tests.test_run_ready_batch_task
```

Expected: all pass.

- [ ] **Step 2: Run the full suite**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
```

Expected: all pass; Dagster-dependent tests skip if `dagster` is unavailable.

- [ ] **Step 3: Commit any fixture updates made during the run**

```bash
git add -A
git commit -m "test: align existing fixtures with acceptance rigor floor"
```
