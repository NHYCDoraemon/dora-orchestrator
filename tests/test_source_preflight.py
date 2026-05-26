import tempfile
import unittest
from pathlib import Path

from orchestrator.batch_loader import load_task_issue_batch
from orchestrator.source_preflight import preflight_batch_source_context
from tests.test_batch_loader import create_batch


class SourcePreflightTest(unittest.TestCase):
    def test_preflight_builds_task_issue_metadata_without_plane_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
            batch = load_task_issue_batch(batch_dir, repo_root=repo)

            result = preflight_batch_source_context(batch, execution_packet_hash="sha256:test")

            self.assertTrue(result.ok, result.findings)
            task_result = result.task_results[0]
            self.assertEqual(task_result.task_id, "DORA-CTX-20260501A-T01")
            self.assertEqual(task_result.issue["execution_packet_version"], 1)
            self.assertEqual(task_result.issue["execution_packet_hash"], "sha256:test")
            self.assertIn("source_docs", task_result.issue)
            self.assertIn("source_tables", task_result.issue)
            self.assertIn("source_queries", task_result.issue)
            self.assertFalse((repo / ".dora" / "source-bundles").exists())

    def test_preflight_reports_missing_required_doc(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)
            (repo / "docs" / "design.md").unlink()
            batch = load_task_issue_batch(batch_dir, repo_root=repo)

            result = preflight_batch_source_context(batch, execution_packet_hash="sha256:test")

            self.assertFalse(result.ok)
            self.assertEqual(result.findings[0].task_id, "DORA-CTX-20260501A-T01")
            self.assertIn("required source doc not found", result.findings[0].message)


if __name__ == "__main__":
    unittest.main()
