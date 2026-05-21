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
