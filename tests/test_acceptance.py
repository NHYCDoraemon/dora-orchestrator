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
