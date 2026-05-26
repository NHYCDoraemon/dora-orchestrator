import tempfile
import unittest
from pathlib import Path

from orchestrator.batch_audit import audit_task_issue_batch
from orchestrator.batch_hash import compute_batch_hash
from tests.test_batch_loader import create_batch


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


class BatchAuditTest(unittest.TestCase):
    def test_rejects_batch_without_chinese_management_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)
            task = batch_dir / "tasks" / "DORA-CTX-20260501A-T01.md"
            text = task.read_text(encoding="utf-8")
            text = text.replace("title: CLI 上下文检查能力", "title: CLI context inspect surface")
            text = text.replace("为上下文装配增加 CLI 检查入口，便于开发人员快速确认上下文内容和来源。", "Add a CLI inspection surface.")
            task.write_text(text, encoding="utf-8")

            result = audit_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(result.status, "FAIL")
            self.assertTrue(any(finding.code == "language" for finding in result.findings))

    def test_complete_batch_passes_and_writes_submit_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)

            result = audit_task_issue_batch(batch_dir, repo_root=repo, write_generated=True)

            self.assertEqual(result.status, "PASS_WITH_PLANNED_CREATES")
            self.assertEqual(result.task_count, 1)
            self.assertEqual(result.planned_cycle_creates, ["S1.5 Phase 4"])
            self.assertEqual(result.findings, [])
            self.assertTrue((batch_dir / "submit-preview.md").exists())
            preview = (batch_dir / "submit-preview.md").read_text(encoding="utf-8")
            self.assertIn("# 提交预览", preview)
            self.assertIn("## 计划写入 Plane", preview)
            self.assertIn("DORA-CTX-20260501A-T01", preview)

    def test_rejects_empty_required_issue_packet_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)
            task = batch_dir / "tasks" / "DORA-CTX-20260501A-T01.md"
            task.write_text(task.read_text(encoding="utf-8").replace("不修改记忆持久化逻辑。", ""), encoding="utf-8")

            result = audit_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(result.status, "FAIL")
            self.assertTrue(any("Non-goals" in finding.message for finding in result.findings))

    def test_rejects_task_id_that_does_not_match_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)
            task = batch_dir / "tasks" / "DORA-CTX-20260501A-T01.md"
            task.rename(batch_dir / "tasks" / "DORA-CTX-20260501A-T02.md")

            result = audit_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(result.status, "FAIL")
            self.assertTrue(any("file name" in finding.message for finding in result.findings))

    def test_rejects_progress_task_missing_required_state_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)
            task = batch_dir / "tasks" / "DORA-CTX-20260501A-T01.md"
            text = task.read_text(encoding="utf-8")
            task.write_text(
                text.replace(
                    "priority: P1\n",
                    "priority: P1\nprogress_schema: pfe-progress/v1\nprogress_task_id: B06-F97-036-form-list\n",
                ),
                encoding="utf-8",
            )

            result = audit_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(result.status, "FAIL")
            self.assertTrue(any(finding.code == "progress_metadata" for finding in result.findings))

    def test_rejects_missing_required_source_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
            (repo / "docs" / "progress" / "ledger.tsv").unlink()

            result = audit_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(result.status, "FAIL")
            self.assertTrue(any(finding.code == "source_table_not_found" for finding in result.findings))

    def test_rejects_empty_required_source_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
            (repo / "docs" / "progress" / "ledger.tsv").write_text(
                "row_id\tfrontend_surface\tbackend_contract\tacceptance_signal\n"
                "OTHER\tsrc/Other.tsx\tGET /other\t其他信号。\n",
                encoding="utf-8",
            )

            result = audit_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(result.status, "FAIL")
            self.assertTrue(any(finding.code == "source_query_empty" for finding in result.findings))

    def test_skips_rendering_optional_source_query_during_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
            batch_path = batch_dir / "batch.md"
            batch_path.write_text(
                batch_path.read_text(encoding="utf-8")
                .replace("    required: true\n    filters:", "    required: false\n    filters:")
                .replace("      - acceptance_signal\n", "      - acceptance_signal\n      - missing_column\n"),
                encoding="utf-8",
            )

            result = audit_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(result.status, "PASS_WITH_PLANNED_CREATES")
            self.assertFalse(any(finding.code == "source_query_column" for finding in result.findings))

    def test_rejects_query_context_that_submit_would_not_serialize(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
            batch_path = batch_dir / "batch.md"
            batch_path.write_text(
                batch_path.read_text(encoding="utf-8").replace(
                    "value_from: task.row_id",
                    "value_from: task.custom_context",
                ),
                encoding="utf-8",
            )
            task_path = batch_dir / "tasks" / "DORA-CTX-20260501A-T01.md"
            task_path.write_text(
                task_path.read_text(encoding="utf-8").replace(
                    "row_id: F97-036\n",
                    "row_id: F97-036\ncustom_context: F97-036\n",
                ),
                encoding="utf-8",
            )

            result = audit_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(result.status, "FAIL")
            self.assertTrue(any(finding.code == "source_bundle_preflight" for finding in result.findings))
            self.assertTrue(any("task.custom_context" in finding.message for finding in result.findings))

    def test_optional_missing_source_table_passes_audit_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
            batch_path = batch_dir / "batch.md"
            batch_path.write_text(
                batch_path.read_text(encoding="utf-8")
                .replace("    required: true\nsource_queries:", "    required: false\nsource_queries:")
                .replace("    required: true\n    filters:", "    required: false\n    filters:"),
                encoding="utf-8",
            )
            (repo / "docs" / "progress" / "ledger.tsv").unlink()

            result = audit_task_issue_batch(batch_dir, repo_root=repo)
            batch_hash = compute_batch_hash(batch_dir, repo_root=repo)

            self.assertEqual(result.status, "PASS_WITH_PLANNED_CREATES")
            self.assertTrue(batch_hash.startswith("sha256:"))

    def test_missing_required_source_table_finding_is_not_duplicated_per_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
            first_task = batch_dir / "tasks" / "DORA-CTX-20260501A-T01.md"
            second_task = batch_dir / "tasks" / "DORA-CTX-20260501A-T02.md"
            second_task.write_text(
                first_task.read_text(encoding="utf-8")
                .replace("task_id: DORA-CTX-20260501A-T01", "task_id: DORA-CTX-20260501A-T02")
                .replace("sequence: 1", "sequence: 2"),
                encoding="utf-8",
            )
            (repo / "docs" / "progress" / "ledger.tsv").unlink()

            result = audit_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(
                sum(1 for finding in result.findings if finding.code == "source_table_not_found"),
                1,
            )

    def test_rejects_duplicate_source_query_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
            batch_path = batch_dir / "batch.md"
            text = batch_path.read_text(encoding="utf-8")
            text = text.replace(
                "    max_rows: 10\n---",
                "    max_rows: 10\n"
                "  - id: current_task_row\n"
                "    table: progress_ledger\n"
                "    required: true\n"
                "    filters:\n"
                "      - column: row_id\n"
                "        op: equals\n"
                "        value: OTHER\n"
                "    columns:\n"
                "      - row_id\n"
                "    max_rows: 10\n---",
            )
            batch_path.write_text(text, encoding="utf-8")

            result = audit_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(result.status, "FAIL")
            self.assertTrue(any(finding.code == "source_query_id" and "duplicate" in finding.message for finding in result.findings))

    def test_rejects_case_insensitive_source_query_slice_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
            batch_path = batch_dir / "batch.md"
            text = batch_path.read_text(encoding="utf-8")
            text = text.replace(
                "  - id: current_task_row\n",
                "  - id: Current_Task_Row\n",
            ).replace(
                "    max_rows: 10\n---",
                "    max_rows: 10\n"
                "  - id: current_task_row\n"
                "    table: progress_ledger\n"
                "    required: true\n"
                "    filters:\n"
                "      - column: row_id\n"
                "        op: equals\n"
                "        value: OTHER\n"
                "    columns:\n"
                "      - row_id\n"
                "    max_rows: 10\n---",
            )
            batch_path.write_text(text, encoding="utf-8")

            result = audit_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(result.status, "FAIL")
            self.assertTrue(any(finding.code == "source_query_id" and "slice path conflicts" in finding.message for finding in result.findings))

    def test_task_external_id_filter_matches_runtime_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
            (repo / "docs" / "progress" / "ledger.tsv").write_text(
                "task_id\tfrontend_surface\tbackend_contract\tacceptance_signal\n"
                "DORA-CTX-20260501A-T01\tsrc/App.tsx\tGET /ctx\t验收信号。\n",
                encoding="utf-8",
            )
            batch_path = batch_dir / "batch.md"
            text = batch_path.read_text(encoding="utf-8")
            text = text.replace("column: row_id", "column: task_id")
            text = text.replace("value_from: task.row_id", "value_from: task.external_id")
            text = text.replace("- row_id", "- task_id")
            batch_path.write_text(text, encoding="utf-8")

            result = audit_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(result.status, "PASS_WITH_PLANNED_CREATES")
            self.assertFalse(any(finding.code == "source_query_filter" for finding in result.findings))


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
