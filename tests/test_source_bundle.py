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
            self.assertIn(table_path, result.required_read_paths)
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

    def test_create_source_bundle_no_write_validates_without_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            (repo / "docs").mkdir()
            (repo / "docs" / "design.md").write_text("# Design\n", encoding="utf-8")
            (repo / "ledger.tsv").write_text("row_id\tvalue\nF97-036\tready\n", encoding="utf-8")
            issue = {
                "external_id": "DORA-PLN-20260501B-T01",
                "execution_packet_version": 1,
                "row_id": "F97-036",
                "source_docs": [
                    {"kind": "source_docs", "path": "docs/design.md", "required": True}
                ],
                "source_tables": [
                    {"id": "ledger", "path": "ledger.tsv", "format": "tsv", "required": True}
                ],
                "source_queries": [
                    {
                        "id": "current_task",
                        "table": "ledger",
                        "required": True,
                        "filters": [{"column": "row_id", "op": "equals", "value_from": "task.row_id"}],
                        "columns": ["row_id", "value"],
                        "max_rows": 10,
                    }
                ],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo, write_output=False)

            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.slice_results[0].row_count, 1)
            self.assertFalse((repo / ".dora" / "source-bundles").exists())

    def test_binary_source_docs_are_attested_but_not_required_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            (repo / "docs").mkdir()
            design = repo / "docs" / "design.md"
            design.write_text("# Design\n", encoding="utf-8")
            spreadsheet = repo / "ledger.xlsx"
            spreadsheet.write_bytes(b"xlsx")
            issue = {
                "external_id": "DORA-PLN-20260501B-T01",
                "execution_packet_version": 1,
                "source_docs": [
                    {"kind": "source_docs", "path": "docs/design.md", "required": True},
                    {"kind": "source_docs", "path": "ledger.xlsx", "required": True},
                ],
                "source_tables": [],
                "source_queries": [],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertTrue(result.ok, result.message)
            self.assertIn(design, result.required_read_paths)
            self.assertNotIn(spreadsheet, result.required_read_paths)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertIn(str(spreadsheet), [doc["absolute_path"] for doc in manifest["source_docs"]])
            bundle_text = result.bundle_path.read_text(encoding="utf-8")
            required_reads_section = bundle_text.split("## Source Docs", 1)[0]
            self.assertIn("docs/design.md", required_reads_section)
            self.assertNotIn("ledger.xlsx", required_reads_section)
            self.assertIn("ledger.xlsx", bundle_text)

    def test_issue_external_id_filter_matches_audit_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            table_path = repo / "ledger.tsv"
            table_path.write_text(
                "external_id\tvalue\nDORA-PLN-20260501B-T01\tready\n",
                encoding="utf-8",
            )
            issue = {
                "external_id": "DORA-PLN-20260501B-T01",
                "execution_packet_version": 1,
                "source_docs": [],
                "source_tables": [
                    {
                        "id": "ledger",
                        "path": "ledger.tsv",
                        "format": "tsv",
                        "required": True,
                    }
                ],
                "source_queries": [
                    {
                        "id": "current_issue",
                        "table": "ledger",
                        "required": True,
                        "filters": [{"column": "external_id", "op": "equals", "value_from": "issue.external_id"}],
                        "columns": ["external_id"],
                        "max_rows": 10,
                    }
                ],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertTrue(result.ok, result.message)
            slice_path = repo / ".dora" / "source-bundles" / "20260501B" / "DORA-PLN-20260501B-T01" / "slices" / "current_issue.tsv"
            self.assertEqual(slice_path.read_text(encoding="utf-8"), "external_id\nDORA-PLN-20260501B-T01\n")

    def test_issue_task_id_filter_uses_external_id_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            table_path = repo / "ledger.tsv"
            table_path.write_text(
                "task_id\tvalue\nDORA-PLN-20260501B-T01\tready\n",
                encoding="utf-8",
            )
            issue = {
                "external_id": "DORA-PLN-20260501B-T01",
                "execution_packet_version": 1,
                "source_docs": [],
                "source_tables": [
                    {
                        "id": "ledger",
                        "path": "ledger.tsv",
                        "format": "tsv",
                        "required": True,
                    }
                ],
                "source_queries": [
                    {
                        "id": "current_issue",
                        "table": "ledger",
                        "required": True,
                        "filters": [{"column": "task_id", "op": "equals", "value_from": "issue.task_id"}],
                        "columns": ["task_id"],
                        "max_rows": 10,
                    }
                ],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertTrue(result.ok, result.message)
            slice_path = repo / ".dora" / "source-bundles" / "20260501B" / "DORA-PLN-20260501B-T01" / "slices" / "current_issue.tsv"
            self.assertEqual(slice_path.read_text(encoding="utf-8"), "task_id\nDORA-PLN-20260501B-T01\n")

    def test_task_task_id_filter_uses_external_id_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            table_path = repo / "ledger.tsv"
            table_path.write_text(
                "task_id\tvalue\nDORA-PLN-20260501B-T01\tready\n",
                encoding="utf-8",
            )
            issue = {
                "external_id": "DORA-PLN-20260501B-T01",
                "execution_packet_version": 1,
                "source_docs": [],
                "source_tables": [
                    {
                        "id": "ledger",
                        "path": "ledger.tsv",
                        "format": "tsv",
                        "required": True,
                    }
                ],
                "source_queries": [
                    {
                        "id": "current_task",
                        "table": "ledger",
                        "required": True,
                        "filters": [{"column": "task_id", "op": "equals", "value_from": "task.task_id"}],
                        "columns": ["task_id"],
                        "max_rows": 10,
                    }
                ],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertTrue(result.ok, result.message)
            slice_path = repo / ".dora" / "source-bundles" / "20260501B" / "DORA-PLN-20260501B-T01" / "slices" / "current_task.tsv"
            self.assertEqual(slice_path.read_text(encoding="utf-8"), "task_id\nDORA-PLN-20260501B-T01\n")

    def test_duplicate_source_query_id_returns_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            table_path = repo / "ledger.tsv"
            table_path.write_text(
                "row_id\tvalue\nA\tfirst\nB\tsecond\n",
                encoding="utf-8",
            )
            issue = {
                "external_id": "DORA-PLN-20260501B-T01",
                "execution_packet_version": 1,
                "source_docs": [],
                "source_tables": [
                    {
                        "id": "ledger",
                        "path": "ledger.tsv",
                        "format": "tsv",
                        "required": True,
                    }
                ],
                "source_queries": [
                    {
                        "id": "dup",
                        "table": "ledger",
                        "required": True,
                        "filters": [{"column": "row_id", "op": "equals", "value": "A"}],
                    },
                    {
                        "id": "dup",
                        "table": "ledger",
                        "required": True,
                        "filters": [{"column": "row_id", "op": "equals", "value": "B"}],
                    },
                ],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertFalse(result.ok)
            self.assertIn("duplicate source query id: dup", result.message)

    def test_case_insensitive_source_query_slice_collision_returns_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            table_path = repo / "ledger.tsv"
            table_path.write_text(
                "row_id\tvalue\nA\tfirst\nB\tsecond\n",
                encoding="utf-8",
            )
            issue = {
                "external_id": "DORA-PLN-20260501B-T01",
                "execution_packet_version": 1,
                "source_docs": [],
                "source_tables": [
                    {
                        "id": "ledger",
                        "path": "ledger.tsv",
                        "format": "tsv",
                        "required": True,
                    }
                ],
                "source_queries": [
                    {
                        "id": "Foo",
                        "table": "ledger",
                        "required": True,
                        "filters": [{"column": "row_id", "op": "equals", "value": "A"}],
                    },
                    {
                        "id": "foo",
                        "table": "ledger",
                        "required": True,
                        "filters": [{"column": "row_id", "op": "equals", "value": "B"}],
                    },
                ],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertFalse(result.ok)
            self.assertIn("source query slice path conflicts", result.message)

    def test_required_source_table_without_query_is_required_read_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            table_path = repo / "ledger.tsv"
            table_path.write_text("row_id\tvalue\nF97-036\tready\n", encoding="utf-8")
            issue = {
                "external_id": "DORA-PLN-20260501B-T01",
                "execution_packet_version": 1,
                "source_docs": [],
                "source_tables": [
                    {
                        "id": "ledger",
                        "path": "ledger.tsv",
                        "format": "tsv",
                        "required": True,
                    }
                ],
                "source_queries": [],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertTrue(result.ok, result.message)
            self.assertIn(table_path, result.required_read_paths)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertIn(str(table_path), manifest["required_read_paths"])

    def test_optional_source_table_is_not_required_read_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            table_path = repo / "ledger.tsv"
            table_path.write_text("row_id\tvalue\nF97-036\tready\n", encoding="utf-8")
            issue = {
                "external_id": "DORA-PLN-20260501B-T01",
                "execution_packet_version": 1,
                "source_docs": [],
                "source_tables": [
                    {
                        "id": "ledger",
                        "path": "ledger.tsv",
                        "format": "tsv",
                        "required": False,
                    }
                ],
                "source_queries": [],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertTrue(result.ok, result.message)
            self.assertNotIn(table_path, result.required_read_paths)

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

    def test_missing_source_metadata_keys_returns_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            issue = {
                "external_id": "DORA-PLN-20260501B-T01",
                "execution_packet_version": 1,
                "source_docs": [],
            }

            result = create_source_bundle(issue=issue, worktree_root=repo)

            self.assertFalse(result.ok)
            self.assertIn("missing source metadata keys", result.message)
            self.assertIn("source_tables", result.message)
            self.assertIn("source_queries", result.message)
            self.assertNotIn("source_docs", result.message)
            self.assertEqual(result.required_read_paths, ())


if __name__ == "__main__":
    unittest.main()
