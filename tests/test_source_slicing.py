import tempfile
import unittest
from pathlib import Path

from orchestrator.source_context import SourceQuery, SourceTable
from orchestrator.source_slicing import render_query_slice


class SourceSlicingTest(unittest.TestCase):
    def test_render_query_slice_filters_tsv_by_task_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            table_path = tmp_path / "ledger.tsv"
            table_path.write_text(
                "row_id\tfrontend_surface\tbackend_contract\n"
                "F97-036\tsrc/pages/forms/list/FormListPage.tsx\tGET /api/v1/forms\n"
                "F97-037\tsrc/pages/forms/detail/FormDetailPage.tsx\tGET /api/v1/forms/{id}\n",
                encoding="utf-8",
            )
            table = SourceTable(
                id="progress_ledger",
                path=str(table_path),
                format="tsv",
                key_columns=("row_id",),
                required=True,
            )
            query = SourceQuery(
                id="current_task_row",
                table="progress_ledger",
                required=True,
                filters=({"column": "row_id", "op": "equals", "value_from": "task.row_id"},),
                columns=("row_id", "frontend_surface"),
                max_rows=10,
            )

            result = render_query_slice(
                table=table,
                query=query,
                context={"task": {"row_id": "F97-036"}},
                output_path=tmp_path / "slice.tsv",
            )

            self.assertIs(result.ok, True)
            self.assertEqual(result.row_count, 1)
            self.assertEqual(
                (tmp_path / "slice.tsv").read_text(encoding="utf-8"),
                "row_id\tfrontend_surface\n"
                "F97-036\tsrc/pages/forms/list/FormListPage.tsx\n",
            )

    def test_required_query_with_no_rows_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            table_path = tmp_path / "ledger.tsv"
            table_path.write_text("row_id\tfrontend_surface\nOTHER\tsrc/Other.tsx\n", encoding="utf-8")
            table = SourceTable("progress_ledger", str(table_path), "tsv", ("row_id",), True)
            query = SourceQuery(
                id="current_task_row",
                table="progress_ledger",
                required=True,
                filters=({"column": "row_id", "op": "equals", "value_from": "task.row_id"},),
                columns=("row_id", "frontend_surface"),
                max_rows=10,
            )

            result = render_query_slice(
                table=table,
                query=query,
                context={"task": {"row_id": "F97-036"}},
                output_path=tmp_path / "slice.tsv",
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.code, "source_query_empty")

    def test_header_only_optional_query_rejects_missing_filter_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            table_path = tmp_path / "ledger.tsv"
            table_path.write_text("row_id\tfrontend_surface\n", encoding="utf-8")
            table = SourceTable("progress_ledger", str(table_path), "tsv", ("row_id",), True)
            query = SourceQuery(
                id="optional_task_row",
                table="progress_ledger",
                required=False,
                filters=({"column": "missing", "op": "equals", "value": "F97-036"},),
                columns=("row_id",),
                max_rows=10,
            )

            result = render_query_slice(
                table=table,
                query=query,
                context={"task": {"row_id": "F97-036"}},
                output_path=tmp_path / "slice.tsv",
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.code, "source_query_filter")

    def test_header_only_optional_query_rejects_unsupported_filter_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            table_path = tmp_path / "ledger.tsv"
            table_path.write_text("row_id\tfrontend_surface\n", encoding="utf-8")
            table = SourceTable("progress_ledger", str(table_path), "tsv", ("row_id",), True)
            query = SourceQuery(
                id="optional_task_row",
                table="progress_ledger",
                required=False,
                filters=({"column": "row_id", "op": "starts_with", "value": "F97"},),
                columns=("row_id",),
                max_rows=10,
            )

            result = render_query_slice(
                table=table,
                query=query,
                context={"task": {"row_id": "F97-036"}},
                output_path=tmp_path / "slice.tsv",
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.code, "source_query_filter")

    def test_table_path_directory_returns_not_found_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            table_path = tmp_path / "ledger.tsv"
            table_path.mkdir()
            table = SourceTable("progress_ledger", str(table_path), "tsv", ("row_id",), True)
            query = SourceQuery(
                id="current_task_row",
                table="progress_ledger",
                required=True,
                filters=(),
                columns=("row_id",),
                max_rows=10,
            )

            result = render_query_slice(
                table=table,
                query=query,
                context={"task": {"row_id": "F97-036"}},
                output_path=tmp_path / "slice.tsv",
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.code, "source_table_not_found")

    def test_header_only_query_rejects_dict_value_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            table_path = tmp_path / "ledger.tsv"
            table_path.write_text("row_id\tfrontend_surface\n", encoding="utf-8")
            table = SourceTable("progress_ledger", str(table_path), "tsv", ("row_id",), True)
            query = SourceQuery(
                id="current_task_row",
                table="progress_ledger",
                required=False,
                filters=({"column": "row_id", "op": "equals", "value_from": "task.meta"},),
                columns=("row_id",),
                max_rows=10,
            )

            result = render_query_slice(
                table=table,
                query=query,
                context={"task": {"meta": {"row_id": "F97-036"}}},
                output_path=tmp_path / "slice.tsv",
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.code, "source_query_filter")

    def test_query_too_many_rows_does_not_write_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            table_path = tmp_path / "ledger.tsv"
            table_path.write_text(
                "row_id\tfrontend_surface\n"
                "F97-036\tsrc/One.tsx\n"
                "F97-036\tsrc/Two.tsx\n",
                encoding="utf-8",
            )
            table = SourceTable("progress_ledger", str(table_path), "tsv", ("row_id",), True)
            query = SourceQuery(
                id="current_task_row",
                table="progress_ledger",
                required=True,
                filters=({"column": "row_id", "op": "equals", "value_from": "task.row_id"},),
                columns=("row_id", "frontend_surface"),
                max_rows=1,
            )
            output_path = tmp_path / "slice.tsv"

            result = render_query_slice(
                table=table,
                query=query,
                context={"task": {"row_id": "F97-036"}},
                output_path=output_path,
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.code, "source_query_too_many_rows")
            self.assertEqual(result.row_count, 2)
            self.assertFalse(output_path.exists())
