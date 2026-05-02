import tempfile
import unittest
from pathlib import Path

from orchestrator.batch_audit import audit_task_issue_batch
from tests.test_batch_loader import create_batch


class BatchAuditTest(unittest.TestCase):
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
            self.assertIn("DORA-CTX-20260501A-T01", (batch_dir / "submit-preview.md").read_text(encoding="utf-8"))

    def test_rejects_empty_required_issue_packet_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)
            task = batch_dir / "tasks" / "DORA-CTX-20260501A-T01.md"
            task.write_text(task.read_text(encoding="utf-8").replace("Do not change memory persistence.", ""), encoding="utf-8")

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
