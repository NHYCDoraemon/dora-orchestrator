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

            assert result.ok is True
            assert result.row_count == 1
            assert (tmp_path / "slice.tsv").read_text(encoding="utf-8") == (
                "row_id\tfrontend_surface\n"
                "F97-036\tsrc/pages/forms/list/FormListPage.tsx\n"
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

            assert result.ok is False
            assert result.code == "source_query_empty"
