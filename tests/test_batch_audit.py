import tempfile
import unittest
from pathlib import Path

from orchestrator.batch_audit import audit_task_issue_batch
from tests.test_batch_loader import create_batch


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
