import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.batch_approval import approve_task_issue_batch
from orchestrator.batch_submit import submit_task_issue_batch
from orchestrator.in_memory_plane import InMemoryPlaneClient
from tests.test_batch_loader import create_batch


class BatchApprovalTest(unittest.TestCase):
    def test_approves_batch_and_allows_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)
            (repo / ".dora").mkdir()
            (repo / ".dora" / "project.json").write_text(
                json.dumps({"project_slug": "dora", "title": "Dora"}, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            approval = approve_task_issue_batch(
                batch_dir,
                repo_root=repo,
                approved_by="raymond",
                approved_at="2026-05-01T21:30:00+08:00",
            )

            self.assertEqual(approval["approved_task_ids"], ["DORA-CTX-20260501A-T01"])
            self.assertTrue((batch_dir / "approval.json").exists())
            self.assertIn("status: approved", (batch_dir / "batch.md").read_text(encoding="utf-8"))
            result = submit_task_issue_batch(
                batch_dir,
                repo_root=repo,
                project_slug="dora",
                project_title="Dora",
                plane_client=InMemoryPlaneClient(),
            )
            self.assertEqual(result["task_issues"], 1)

    def test_refuses_to_approve_failing_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)
            task = batch_dir / "tasks" / "DORA-CTX-20260501A-T01.md"
            task.write_text(task.read_text(encoding="utf-8").replace("Do not change memory persistence.", ""), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "failing audit"):
                approve_task_issue_batch(batch_dir, repo_root=repo, approved_by="raymond")
