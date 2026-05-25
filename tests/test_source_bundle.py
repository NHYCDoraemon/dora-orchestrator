import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.source_bundle import create_source_bundle


class SourceBundleTest(unittest.TestCase):
    def test_create_source_bundle_writes_manifest_bundle_and_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            (repo / "docs").mkdir()
            doc_path = repo / "docs" / "design.md"
            doc_path.write_text("# Design\n\nUse the real forms API.\n", encoding="utf-8")
            table_path = repo / "ledger.tsv"
            table_path.write_text(
                "row_id\tfrontend_surface\tbackend_contract\n"
                "F97-036\tsrc/pages/forms/list/FormListPage.tsx\tGET /api/v1/forms\n"
                "F97-037\tsrc/pages/forms/detail/FormDetailPage.tsx\tGET /api/v1/forms/{id}\n",
                encoding="utf-8",
            )
            issue = {
                "external_id": "PFE-P1FR-20260525C-T01",
                "key": "PFE-1",
                "execution_packet_version": 1,
                "batch_id": "B06",
                "row_id": "F97-036",
                "source_docs": [
                    {
                        "kind": "source_docs",
                        "path": "docs/design.md",
                        "required": True,
                        "sha256": "sha256:doc",
                    }
                ],
                "source_tables": [
                    {
                        "id": "progress_ledger",
                        "path": "ledger.tsv",
                        "format": "tsv",
                        "key_columns": ["row_id"],
                        "required": True,
                    }
                ],
                "source_queries": [
                    {
                        "id": "current_task_row",
                        "table": "progress_ledger",
                        "required": True,
                        "filters": [{"column": "row_id", "op": "equals", "value_from": "task.row_id"}],
                        "columns": ["row_id", "frontend_surface"],
                        "max_rows": 10,
                    }
                ],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertTrue(result.ok, result.message)
            self.assertTrue(result.bundle_path.is_file())
            self.assertTrue(result.manifest_path.is_file())
            slice_path = repo / ".dora" / "source-bundles" / "B06" / "PFE-P1FR-20260525C-T01" / "slices" / "current_task_row.tsv"
            self.assertTrue(slice_path.is_file())
            self.assertEqual(
                slice_path.read_text(encoding="utf-8"),
                "row_id\tfrontend_surface\nF97-036\tsrc/pages/forms/list/FormListPage.tsx\n",
            )
            self.assertEqual(result.required_read_paths[0], result.bundle_path)
            self.assertIn(doc_path, result.required_read_paths)
            self.assertIn(slice_path, result.required_read_paths)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["execution_packet_version"], 1)
            self.assertEqual(manifest["batch_id"], "B06")
            self.assertEqual(manifest["task_key"], "PFE-P1FR-20260525C-T01")
            self.assertEqual(manifest["slices"][0]["query_id"], "current_task_row")
            self.assertEqual(manifest["slices"][0]["row_count"], 1)
            bundle_text = result.bundle_path.read_text(encoding="utf-8")
            self.assertIn("docs/design.md", bundle_text)
            self.assertIn("current_task_row.tsv", bundle_text)
            self.assertNotIn("Use the real forms API.", bundle_text)

    def test_malicious_batch_and_task_segments_stay_under_source_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            issue = {
                "external_id": "../..",
                "key": "../..",
                "execution_packet_version": 1,
                "batch_id": "..",
                "source_docs": [],
                "source_tables": [],
                "source_queries": [],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertTrue(result.ok, result.message)
            source_bundles_root = repo / ".dora" / "source-bundles"
            self.assertEqual(result.bundle_root.relative_to(source_bundles_root), Path("unknown") / "unknown")

    def test_source_doc_outside_worktree_returns_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            secret = repo.parent / "secret.md"
            secret.write_text("do not read\n", encoding="utf-8")
            issue = {
                "external_id": "DORA-PLN-20260501B-T01",
                "execution_packet_version": 1,
                "source_docs": [{"kind": "source_docs", "path": "../secret.md", "required": True}],
                "source_tables": [],
                "source_queries": [],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertFalse(result.ok)
            self.assertIn("outside worktree", result.message)
            self.assertEqual(result.required_read_paths, ())

    def test_source_table_outside_worktree_returns_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            table = repo.parent / "ledger.tsv"
            table.write_text("row_id\nF97-036\n", encoding="utf-8")
            issue = {
                "external_id": "DORA-PLN-20260501B-T01",
                "execution_packet_version": 1,
                "source_docs": [],
                "source_tables": [
                    {
                        "id": "progress_ledger",
                        "path": str(table),
                        "format": "tsv",
                        "key_columns": ["row_id"],
                        "required": True,
                    }
                ],
                "source_queries": [],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertFalse(result.ok)
            self.assertIn("outside worktree", result.message)
            self.assertEqual(result.required_read_paths, ())


if __name__ == "__main__":
    unittest.main()
