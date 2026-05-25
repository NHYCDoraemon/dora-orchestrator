# Orchestrator Execution Packet Source Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure every executor-claimed task receives deterministic source docs and TSV/CSV slices, and block tasks when source context or source-read evidence is missing.

**Architecture:** Keep the existing markdown batch directory model. Add an Execution Packet v1 source layer that parses source table/query metadata from `batch.md`, normalizes per-task source docs from task frontmatter, stores source metadata in Plane issues, generates `.dora/source-bundles/` before claim, injects bundle paths into executor prompts, and evaluates executor JSONL logs for read evidence before delivery/auto-merge.

**Tech Stack:** Python 3.11, stdlib `dataclasses`, `json`, `csv`, `hashlib`, `pathlib`, `re`, existing Plane-compatible clients, pytest/unittest.

---

## File Map

- Create `/Users/raymond/projects/dora-orchestrator/orchestrator/source_context.py`: Execution Packet constants, source dataclasses, metadata parsing, path normalization, source file hashing.
- Create `/Users/raymond/projects/dora-orchestrator/orchestrator/source_slicing.py`: restricted TSV/CSV query evaluator and generated slice writer.
- Create `/Users/raymond/projects/dora-orchestrator/orchestrator/source_bundle.py`: per-task bundle and manifest generation under `.dora/source-bundles/`.
- Create `/Users/raymond/projects/dora-orchestrator/orchestrator/source_evidence.py`: parser for Claude/Codex JSONL event logs and required-read evidence evaluation.
- Modify `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_loader.py`: parse restricted nested frontmatter used by `source_tables` and `source_queries`.
- Modify `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_audit.py`: validate table/query definitions and required query results.
- Modify `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_hash.py`: include source table files and query definitions in approval hashes.
- Modify `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_submit.py`: attach Execution Packet v1 metadata to task issues.
- Modify `/Users/raymond/projects/dora-orchestrator/orchestrator/plane_live.py`: preserve nested packet metadata through markdown/HTML round trip.
- Modify `/Users/raymond/projects/dora-orchestrator/orchestrator/run_ready_task.py`: preflight before claim, prompt injection, evidence gate before delivery.
- Modify `/Users/raymond/projects/dora-orchestrator/orchestrator/status.py` and `/Users/raymond/projects/dora-orchestrator/orchestrator/query_issues.py`: surface source context state.
- Add tests in `/Users/raymond/projects/dora-orchestrator/tests/test_source_context.py`, `/Users/raymond/projects/dora-orchestrator/tests/test_source_slicing.py`, `/Users/raymond/projects/dora-orchestrator/tests/test_source_bundle.py`, `/Users/raymond/projects/dora-orchestrator/tests/test_source_evidence.py`, plus focused changes to existing batch/Plane/run tests.

## Batch Metadata Contract

`batch.md` frontmatter gains batch-level table/query definitions:

```yaml
source_tables:
  - id: progress_ledger
    path: docs/progress/ledger.tsv
    format: tsv
    key_columns:
      - row_id
    required: true
source_queries:
  - id: current_task_row
    table: progress_ledger
    required: true
    filters:
      - column: row_id
        op: equals
        value_from: task.row_id
    columns:
      - row_id
      - frontend_surface
      - backend_contract
      - acceptance_signal
    max_rows: 10
```

Task frontmatter keeps existing source doc fields:

```yaml
source_pages:
  - ../program-page.md
source_docs:
  - docs/design.md
source_summaries:
  - docs/summaries/S1.5-P3-05.md
```

At submit time these are normalized into issue metadata:

```json
{
  "execution_packet_version": 1,
  "source_docs": [{"kind": "source_docs", "path": "docs/design.md", "sha256": "sha256:0123456789abcdef"}],
  "source_tables": [{"id": "progress_ledger", "path": "docs/progress/ledger.tsv", "format": "tsv"}],
  "source_queries": [{"id": "current_task_row", "table": "progress_ledger", "filters": []}]
}
```

## Task 1: Add Failing Tests For The Contract

**Files:**
- Modify: `/Users/raymond/projects/dora-orchestrator/tests/test_batch_loader.py`
- Modify: `/Users/raymond/projects/dora-orchestrator/tests/test_batch_audit.py`
- Modify: `/Users/raymond/projects/dora-orchestrator/tests/test_batch_submit.py`
- Modify: `/Users/raymond/projects/dora-orchestrator/tests/test_live_plane.py`
- Modify: `/Users/raymond/projects/dora-orchestrator/tests/test_run_ready_batch_task.py`
- Create: `/Users/raymond/projects/dora-orchestrator/tests/test_source_slicing.py`
- Create: `/Users/raymond/projects/dora-orchestrator/tests/test_source_evidence.py`

- [ ] **Step 1.1: Extend `create_batch()` with optional source table/query metadata**

  In `/Users/raymond/projects/dora-orchestrator/tests/test_batch_loader.py`, change the helper signature:

  ```python
  def create_batch(repo: Path, *, with_source_table: bool = False, with_task_row_id: bool = False) -> Path:
  ```

  In the helper, create a TSV when requested:

  ```python
      if with_source_table:
          (repo / "docs" / "progress").mkdir(parents=True, exist_ok=True)
          (repo / "docs" / "progress" / "ledger.tsv").write_text(
              "row_id\tfrontend_surface\tbackend_contract\tacceptance_signal\n"
              "F97-036\tsrc/pages/forms/list/FormListPage.tsx\tGET /api/v1/forms\t真实接口响应驱动页面。\n",
              encoding="utf-8",
          )
  ```

  Insert this source metadata into `batch.md` frontmatter when `with_source_table` is true:

  ```python
  source_metadata = (
      "source_tables:\n"
      "  - id: progress_ledger\n"
      "    path: docs/progress/ledger.tsv\n"
      "    format: tsv\n"
      "    key_columns:\n"
      "      - row_id\n"
      "    required: true\n"
      "source_queries:\n"
      "  - id: current_task_row\n"
      "    table: progress_ledger\n"
      "    required: true\n"
      "    filters:\n"
      "      - column: row_id\n"
      "        op: equals\n"
      "        value_from: task.row_id\n"
      "    columns:\n"
      "      - row_id\n"
      "      - frontend_surface\n"
      "      - backend_contract\n"
      "      - acceptance_signal\n"
      "    max_rows: 10\n"
      if with_source_table
      else ""
  )
  ```

  Add `row_id` to task frontmatter when requested:

  ```python
  row_id_metadata = "row_id: F97-036\n" if with_task_row_id else ""
  ```

- [ ] **Step 1.2: Add loader assertion for nested metadata**

  Add to `BatchLoaderTest`:

  ```python
      def test_loads_batch_source_tables_and_queries(self):
          with tempfile.TemporaryDirectory() as tmp:
              repo = Path(tmp)
              batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)

              batch = load_task_issue_batch(batch_dir, repo_root=repo)

              self.assertEqual(batch.batch_doc.metadata["source_tables"][0]["id"], "progress_ledger")
              self.assertEqual(batch.batch_doc.metadata["source_tables"][0]["key_columns"], ["row_id"])
              query = batch.batch_doc.metadata["source_queries"][0]
              self.assertEqual(query["id"], "current_task_row")
              self.assertEqual(query["filters"][0]["value_from"], "task.row_id")
              self.assertEqual(query["columns"], ["row_id", "frontend_surface", "backend_contract", "acceptance_signal"])
              self.assertEqual(query["max_rows"], 10)
  ```

  Run:

  ```bash
  pytest tests/test_batch_loader.py::BatchLoaderTest::test_loads_batch_source_tables_and_queries -q
  ```

  Expected before implementation: `FAIL` with `unsupported nested YAML`.

- [ ] **Step 1.3: Add audit tests for missing table and empty required query**

  Add to `/Users/raymond/projects/dora-orchestrator/tests/test_batch_audit.py`:

  ```python
      def test_rejects_missing_required_source_table(self):
          with tempfile.TemporaryDirectory() as tmp:
              repo = Path(tmp)
              batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
              (repo / "docs" / "progress" / "ledger.tsv").unlink()

              result = audit_task_issue_batch(batch_dir, repo_root=repo)

              self.assertEqual(result.status, "FAIL")
              self.assertTrue(any(finding.code == "source_table_not_found" for finding in result.findings))
  ```

  ```python
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
  ```

  Run:

  ```bash
  pytest tests/test_batch_audit.py -k "source_table or source_query" -q
  ```

  Expected before implementation: `FAIL` because audit has no table/query validation.

- [ ] **Step 1.4: Add source slicing unit tests**

  Create `/Users/raymond/projects/dora-orchestrator/tests/test_source_slicing.py`:

  ```python
  from pathlib import Path

  from orchestrator.source_context import SourceQuery, SourceTable
  from orchestrator.source_slicing import render_query_slice


  def test_render_query_slice_filters_tsv_by_task_metadata(tmp_path: Path) -> None:
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
  ```

  Add a second test:

  ```python
  def test_required_query_with_no_rows_fails(tmp_path: Path) -> None:
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

      result = render_query_slice(table=table, query=query, context={"task": {"row_id": "F97-036"}}, output_path=tmp_path / "slice.tsv")

      assert result.ok is False
      assert result.code == "source_query_empty"
  ```

  Run:

  ```bash
  pytest tests/test_source_slicing.py -q
  ```

  Expected before implementation: `ModuleNotFoundError`.

- [ ] **Step 1.5: Add submit tests for issue packet metadata**

  Add to `/Users/raymond/projects/dora-orchestrator/tests/test_batch_submit.py`:

  ```python
      def test_submits_execution_packet_source_context_to_task_issue(self):
          with tempfile.TemporaryDirectory() as tmp:
              repo = Path(tmp)
              batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
              task_path = batch_dir / "tasks" / "DORA-CTX-20260501A-T01.md"
              task_text = task_path.read_text(encoding="utf-8")
              task_path.write_text(
                  task_text.replace(
                      "verification_level: []\n",
                      "verification_level: []\nverification_commands:\n  - pytest tests/test_gateway.py -q\n",
                  ),
                  encoding="utf-8",
              )
              batch_dir = approve_batch(repo, batch_dir)
              client = InMemoryPlaneClient()

              submit_task_issue_batch(
                  batch_dir,
                  repo_root=repo,
                  project_slug="dora",
                  project_title="Dora",
                  plane_client=client,
              )

              task = client.issues[("dora", "DORA-CTX-20260501A-T01")]
              self.assertEqual(task["execution_packet_version"], 1)
              self.assertEqual(task["source_tables"][0]["id"], "progress_ledger")
              self.assertEqual(task["source_queries"][0]["id"], "current_task_row")
              self.assertEqual(task["verification_commands"], ["pytest tests/test_gateway.py -q"])
              self.assertTrue(any(item["path"] == "docs/design.md" for item in task["source_docs"]))
  ```

  Run:

  ```bash
  pytest tests/test_batch_submit.py::BatchSubmitTest::test_submits_execution_packet_source_context_to_task_issue -q
  ```

  Expected before implementation: `FAIL` because task payload lacks source packet fields.

- [ ] **Step 1.6: Add live Plane markdown round-trip test**

  In `/Users/raymond/projects/dora-orchestrator/tests/test_live_plane.py`, import helpers already tested in this file and add:

  ```python
  def test_issue_markdown_round_trips_execution_packet_metadata():
      payload = {
          "name": "Gateway source",
          "body": "# Gateway source\n",
          "source_hash": "sha256:task",
          "agent_hint": "claude",
          "risk": "medium",
          "depends_on": [],
          "verification_level": ["L1"],
          "verification_commands": ["pytest tests/test_gateway.py -q"],
          "execution_packet_version": 1,
          "execution_packet_hash": "sha256:packet",
          "source_docs": [{"kind": "source_docs", "path": "docs/design.md", "sha256": "sha256:doc", "required": True}],
          "source_tables": [{"id": "progress_ledger", "path": "docs/progress/ledger.tsv", "format": "tsv", "required": True}],
          "source_queries": [{"id": "current_task_row", "table": "progress_ledger", "required": True, "filters": []}],
      }

      markdown = _issue_markdown("DORA-CTX-20260501A-T01", payload)
      adapted = _adapt_issue(
          {
              "id": "plane-1",
              "external_id": "DORA-CTX-20260501A-T01",
              "description_html": _markdown_to_html(markdown),
          }
      )

      assert adapted["execution_packet_version"] == 1
      assert adapted["source_tables"] == payload["source_tables"]
      assert adapted["source_queries"] == payload["source_queries"]
      assert adapted["verification_commands"] == payload["verification_commands"]
  ```

  Run:

  ```bash
  pytest tests/test_live_plane.py -k execution_packet_metadata -q
  ```

  Expected before implementation: `FAIL`.

- [ ] **Step 1.7: Add evidence gate unit tests**

  Create `/Users/raymond/projects/dora-orchestrator/tests/test_source_evidence.py`:

  ```python
  import json
  from pathlib import Path

  from orchestrator.source_evidence import evaluate_source_evidence_from_event_path


  def test_claude_read_event_satisfies_required_paths(tmp_path: Path) -> None:
      bundle = tmp_path / ".dora" / "source-bundles" / "20260501A" / "T01" / "source-bundle.md"
      doc = tmp_path / "docs" / "design.md"
      slice_file = tmp_path / ".dora" / "source-bundles" / "20260501A" / "T01" / "slices" / "current_task_row.tsv"
      for path in (bundle, doc, slice_file):
          path.parent.mkdir(parents=True, exist_ok=True)
          path.write_text("x\n", encoding="utf-8")
      event_path = tmp_path / "events.ndjson"
      events = [
          {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": str(bundle)}}]}},
          {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": str(doc)}}]}},
          {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": str(slice_file)}}]}},
      ]
      event_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

      result = evaluate_source_evidence_from_event_path(event_path, worktree_root=tmp_path, required_paths=[bundle, doc, slice_file])

      assert result.ok is True
      assert result.missing_paths == ()
  ```

  ```python
  def test_missing_required_read_evidence_fails(tmp_path: Path) -> None:
      event_path = tmp_path / "events.ndjson"
      event_path.write_text("", encoding="utf-8")
      bundle = tmp_path / ".dora" / "source-bundles" / "source-bundle.md"

      result = evaluate_source_evidence_from_event_path(event_path, worktree_root=tmp_path, required_paths=[bundle])

      assert result.ok is False
      assert result.missing_paths == (bundle.resolve(),)
  ```

  Run:

  ```bash
  pytest tests/test_source_evidence.py -q
  ```

  Expected before implementation: `ModuleNotFoundError`.

## Task 2: Source Context Data Model And Nested Frontmatter Parser

**Files:**
- Create: `/Users/raymond/projects/dora-orchestrator/orchestrator/source_context.py`
- Modify: `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_loader.py`
- Test: `/Users/raymond/projects/dora-orchestrator/tests/test_batch_loader.py`

- [ ] **Step 2.1: Create source context dataclasses**

  Create `/Users/raymond/projects/dora-orchestrator/orchestrator/source_context.py`:

  ```python
  """Execution Packet v1 source-context parsing and path normalization."""

  from __future__ import annotations

  import hashlib
  from dataclasses import dataclass
  from pathlib import Path
  from typing import Any, Mapping

  from .batch_models import TaskIssueBatch, TaskIssueDraft

  EXECUTION_PACKET_VERSION = 1
  SOURCE_TABLE_FORMATS = {"tsv", "csv"}
  SOURCE_FILTER_OPS = {"equals", "contains", "regex", "in"}
  SOURCE_DOC_KEYS = ("source_pages", "source_docs", "source_summaries")


  @dataclass(frozen=True)
  class SourceDoc:
      kind: str
      path: str
      absolute_path: Path
      required: bool = True
      sha256: str = ""

      def to_issue_dict(self, repo_root: Path) -> dict[str, object]:
          return {
              "kind": self.kind,
              "path": _repo_relative(self.absolute_path, repo_root),
              "required": self.required,
              "sha256": self.sha256,
          }


  @dataclass(frozen=True)
  class SourceTable:
      id: str
      path: str
      format: str
      key_columns: tuple[str, ...]
      required: bool = True
      sha256: str = ""

      def to_issue_dict(self) -> dict[str, object]:
          return {
              "id": self.id,
              "path": self.path,
              "format": self.format,
              "key_columns": list(self.key_columns),
              "required": self.required,
              "sha256": self.sha256,
          }


  @dataclass(frozen=True)
  class SourceQuery:
      id: str
      table: str
      required: bool
      filters: tuple[dict[str, object], ...]
      columns: tuple[str, ...]
      max_rows: int

      def to_issue_dict(self) -> dict[str, object]:
          return {
              "id": self.id,
              "table": self.table,
              "required": self.required,
              "filters": [dict(item) for item in self.filters],
              "columns": list(self.columns),
              "max_rows": self.max_rows,
          }


  def hash_file(path: Path) -> str:
      digest = hashlib.sha256()
      with path.open("rb") as handle:
          for chunk in iter(lambda: handle.read(1024 * 1024), b""):
              digest.update(chunk)
      return "sha256:" + digest.hexdigest()
  ```

  Add parsing helpers in the same file:

  ```python
  def source_tables_from_batch(batch: TaskIssueBatch) -> tuple[SourceTable, ...]:
      raw = batch.batch_doc.metadata.get("source_tables") or []
      if not isinstance(raw, list):
          raise ValueError("source_tables must be a list")
      tables: list[SourceTable] = []
      for item in raw:
          if not isinstance(item, Mapping):
              raise ValueError("source_tables entries must be mappings")
          table_path = str(item.get("path") or "")
          resolved = resolve_repo_path(batch.repo_root, table_path)
          tables.append(
              SourceTable(
                  id=str(item.get("id") or ""),
                  path=table_path,
                  format=str(item.get("format") or "tsv"),
                  key_columns=tuple(str(value) for value in _list_value(item.get("key_columns"))),
                  required=_bool_value(item.get("required"), default=True),
                  sha256=hash_file(resolved) if resolved.is_file() else "",
              )
          )
      return tuple(tables)
  ```

  ```python
  def source_queries_from_batch(batch: TaskIssueBatch) -> tuple[SourceQuery, ...]:
      raw = batch.batch_doc.metadata.get("source_queries") or []
      if not isinstance(raw, list):
          raise ValueError("source_queries must be a list")
      queries: list[SourceQuery] = []
      for item in raw:
          if not isinstance(item, Mapping):
              raise ValueError("source_queries entries must be mappings")
          queries.append(
              SourceQuery(
                  id=str(item.get("id") or ""),
                  table=str(item.get("table") or ""),
                  required=_bool_value(item.get("required"), default=True),
                  filters=tuple(dict(value) for value in _mapping_list(item.get("filters"))),
                  columns=tuple(str(value) for value in _list_value(item.get("columns"))),
                  max_rows=int(item.get("max_rows") or 200),
              )
          )
      return tuple(queries)
  ```

  ```python
  def source_docs_for_task(batch: TaskIssueBatch, task: TaskIssueDraft) -> tuple[SourceDoc, ...]:
      docs: list[SourceDoc] = []
      for key in SOURCE_DOC_KEYS:
          for declared in _list_value(task.metadata.get(key)):
              absolute = resolve_task_source_path(batch, task, declared)
              docs.append(
                  SourceDoc(
                      kind=key,
                      path=declared,
                      absolute_path=absolute,
                      required=True,
                      sha256=hash_file(absolute) if absolute.is_file() else "",
                  )
              )
      return tuple(docs)
  ```

  ```python
  def resolve_repo_path(repo_root: Path, value: str) -> Path:
      path = Path(value)
      if path.is_absolute():
          return path.resolve()
      return (repo_root / path).resolve()


  def resolve_task_source_path(batch: TaskIssueBatch, task: TaskIssueDraft, value: str) -> Path:
      path = Path(value)
      if path.is_absolute():
          return path.resolve()
      if value.startswith("."):
          return (task.path.parent / path).resolve()
      return (batch.repo_root / path).resolve()
  ```

  ```python
  def _repo_relative(path: Path, repo_root: Path) -> str:
      resolved = path.resolve()
      try:
          return resolved.relative_to(repo_root.resolve()).as_posix()
      except ValueError:
          return str(resolved)


  def _list_value(value: object) -> list[object]:
      if value is None:
          return []
      if isinstance(value, list):
          return list(value)
      if isinstance(value, tuple):
          return list(value)
      if isinstance(value, str) and value:
          return [value]
      return []


  def _mapping_list(value: object) -> list[Mapping[str, object]]:
      if value is None:
          return []
      if not isinstance(value, list):
          raise ValueError("expected list of mappings")
      out: list[Mapping[str, object]] = []
      for item in value:
          if not isinstance(item, Mapping):
              raise ValueError("expected mapping entry")
          out.append(item)
      return out


  def _bool_value(value: object, *, default: bool) -> bool:
      if value is None:
          return default
      if isinstance(value, bool):
          return value
      return str(value).strip().lower() in {"true", "yes", "1"}
  ```

- [ ] **Step 2.2: Extend scalar parsing for booleans**

  Modify `_parse_scalar()` in `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_loader.py`:

  ```python
  def _parse_scalar(value: str) -> object:
      if value == "[]":
          return []
      if value in {"true", "True"}:
          return True
      if value in {"false", "False"}:
          return False
      if value.startswith('"') and value.endswith('"'):
          return value[1:-1]
      if value.startswith("'") and value.endswith("'"):
          return value[1:-1]
      if value.isdigit():
          return int(value)
      return value
  ```

- [ ] **Step 2.3: Replace `_parse_simple_yaml()` with restricted nested parser**

  In `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_loader.py`, replace `_parse_simple_yaml()` with an indentation-aware parser that supports only current needs:

  ```python
  def _parse_simple_yaml(lines: list[str], path: Path) -> dict[str, object]:
      normalized = [
          (index + 1, len(raw) - len(raw.lstrip(" ")), raw.strip())
          for index, raw in enumerate(lines)
          if raw.strip()
      ]
      data: dict[str, object] = {}
      index = 0
      while index < len(normalized):
          line_no, indent, text = normalized[index]
          if indent != 0:
              raise ValueError(f"unsupported nested YAML in {path}: line {line_no}: {text}")
          if ":" not in text:
              raise ValueError(f"invalid YAML line in {path}: {text}")
          key, raw_value = text.split(":", 1)
          key = key.strip()
          value = raw_value.strip()
          if value:
              data[key] = _parse_scalar(value)
              index += 1
              continue
          block, index = _parse_yaml_block(normalized, index + 1, parent_indent=0, path=path)
          data[key] = block
      return data
  ```

  Add helper functions:

  ```python
  def _parse_yaml_block(
      lines: list[tuple[int, int, str]],
      index: int,
      *,
      parent_indent: int,
      path: Path,
  ) -> tuple[object, int]:
      if index >= len(lines) or lines[index][1] <= parent_indent:
          return [], index
      line_no, indent, text = lines[index]
      if text.startswith("- "):
          return _parse_yaml_list(lines, index, indent=indent, path=path)
      return _parse_yaml_mapping(lines, index, indent=indent, path=path)
  ```

  ```python
  def _parse_yaml_list(
      lines: list[tuple[int, int, str]],
      index: int,
      *,
      indent: int,
      path: Path,
  ) -> tuple[list[object], int]:
      out: list[object] = []
      while index < len(lines):
          line_no, current_indent, text = lines[index]
          if current_indent < indent:
              break
          if current_indent != indent or not text.startswith("- "):
              raise ValueError(f"unsupported nested YAML in {path}: line {line_no}: {text}")
          item_text = text[2:].strip()
          if not item_text:
              item, index = _parse_yaml_block(lines, index + 1, parent_indent=indent, path=path)
              out.append(item)
              continue
          if ":" in item_text:
              key, raw_value = item_text.split(":", 1)
              item: dict[str, object] = {key.strip(): _parse_scalar(raw_value.strip()) if raw_value.strip() else []}
              index += 1
              while index < len(lines) and lines[index][1] > indent:
                  child_line_no, child_indent, child_text = lines[index]
                  if child_indent != indent + 2:
                      raise ValueError(f"unsupported nested YAML in {path}: line {child_line_no}: {child_text}")
                  if ":" not in child_text:
                      raise ValueError(f"invalid YAML line in {path}: line {child_line_no}: {child_text}")
                  child_key, child_raw_value = child_text.split(":", 1)
                  child_key = child_key.strip()
                  child_value = child_raw_value.strip()
                  if child_value:
                      item[child_key] = _parse_scalar(child_value)
                      index += 1
                  else:
                      nested, index = _parse_yaml_block(lines, index + 1, parent_indent=child_indent, path=path)
                      item[child_key] = nested
              out.append(item)
              continue
          out.append(_parse_scalar(item_text))
          index += 1
      return out, index
  ```

  ```python
  def _parse_yaml_mapping(
      lines: list[tuple[int, int, str]],
      index: int,
      *,
      indent: int,
      path: Path,
  ) -> tuple[dict[str, object], int]:
      out: dict[str, object] = {}
      while index < len(lines):
          line_no, current_indent, text = lines[index]
          if current_indent < indent:
              break
          if current_indent != indent:
              raise ValueError(f"unsupported nested YAML in {path}: line {line_no}: {text}")
          if ":" not in text:
              raise ValueError(f"invalid YAML line in {path}: line {line_no}: {text}")
          key, raw_value = text.split(":", 1)
          key = key.strip()
          value = raw_value.strip()
          if value:
              out[key] = _parse_scalar(value)
              index += 1
          else:
              nested, index = _parse_yaml_block(lines, index + 1, parent_indent=indent, path=path)
              out[key] = nested
      return out, index
  ```

- [ ] **Step 2.4: Run loader tests**

  ```bash
  pytest tests/test_batch_loader.py -q
  ```

  Expected after implementation: all loader tests pass.

## Task 3: Source Slicing, Audit, And Hashing

**Files:**
- Create: `/Users/raymond/projects/dora-orchestrator/orchestrator/source_slicing.py`
- Modify: `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_audit.py`
- Modify: `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_hash.py`
- Test: `/Users/raymond/projects/dora-orchestrator/tests/test_source_slicing.py`
- Test: `/Users/raymond/projects/dora-orchestrator/tests/test_batch_audit.py`

- [ ] **Step 3.1: Implement TSV/CSV query rendering**

  Create `/Users/raymond/projects/dora-orchestrator/orchestrator/source_slicing.py`:

  ```python
  """Restricted TSV/CSV slicing for Execution Packet source context."""

  from __future__ import annotations

  import csv
  import re
  from dataclasses import dataclass
  from pathlib import Path
  from typing import Any

  from .source_context import SOURCE_FILTER_OPS, SourceQuery, SourceTable, hash_file


  @dataclass(frozen=True)
  class SliceResult:
      ok: bool
      code: str
      query_id: str
      output_path: Path | None
      row_count: int
      columns: tuple[str, ...]
      sha256: str = ""
      message: str = ""
  ```

  ```python
  def render_query_slice(
      *,
      table: SourceTable,
      query: SourceQuery,
      context: dict[str, Any],
      output_path: Path,
      write_output: bool = True,
  ) -> SliceResult:
      table_path = Path(table.path)
      if not table_path.is_file():
          return SliceResult(False, "source_table_not_found", query.id, None, 0, query.columns, message=f"table not found: {table.path}")
      if table.format not in {"tsv", "csv"}:
          return SliceResult(False, "source_table_format", query.id, None, 0, query.columns, message=f"unsupported format: {table.format}")
      delimiter = "\t" if table.format == "tsv" else ","
      with table_path.open("r", encoding="utf-8", newline="") as handle:
          reader = csv.DictReader(handle, delimiter=delimiter)
          fieldnames = tuple(reader.fieldnames or ())
          if not fieldnames:
              return SliceResult(False, "source_table_header", query.id, None, 0, (), message="table has no header")
          selected_columns = query.columns or fieldnames
          missing_columns = [column for column in selected_columns if column not in fieldnames]
          if missing_columns:
              return SliceResult(False, "source_query_column", query.id, None, 0, selected_columns, message=", ".join(missing_columns))
          try:
              rows = [row for row in reader if _matches_filters(row, query.filters, context)]
          except ValueError as exc:
              return SliceResult(False, "source_query_filter", query.id, None, 0, selected_columns, message=str(exc))
      if query.required and not rows:
          return SliceResult(False, "source_query_empty", query.id, None, 0, selected_columns, message="required query returned no rows")
      if len(rows) > query.max_rows:
          return SliceResult(False, "source_query_too_many_rows", query.id, None, len(rows), selected_columns, message=f"{len(rows)} > {query.max_rows}")
      if not write_output:
          return SliceResult(True, "ok", query.id, None, len(rows), selected_columns)
      output_path.parent.mkdir(parents=True, exist_ok=True)
      with output_path.open("w", encoding="utf-8", newline="") as handle:
          writer = csv.DictWriter(handle, fieldnames=list(selected_columns), delimiter=delimiter, lineterminator="\n")
          writer.writeheader()
          for row in rows:
              writer.writerow({column: row.get(column, "") for column in selected_columns})
      return SliceResult(True, "ok", query.id, output_path, len(rows), selected_columns, sha256=hash_file(output_path))
  ```

  ```python
  def _matches_filters(row: dict[str, str], filters: tuple[dict[str, object], ...], context: dict[str, Any]) -> bool:
      for item in filters:
          column = str(item.get("column") or "")
          op = str(item.get("op") or "")
          if column not in row:
              raise ValueError(f"filter column not found: {column}")
          if op not in SOURCE_FILTER_OPS:
              raise ValueError(f"unsupported filter op: {op}")
          expected = _filter_value(item, context)
          actual = row.get(column, "")
          if op == "equals" and actual != str(expected):
              return False
          if op == "contains" and str(expected) not in actual:
              return False
          if op == "regex" and re.search(str(expected), actual) is None:
              return False
          if op == "in" and actual not in {str(value) for value in _as_list(expected)}:
              return False
      return True


  def _filter_value(item: dict[str, object], context: dict[str, Any]) -> object:
      if "value" in item:
          return item["value"]
      path = str(item.get("value_from") or "")
      if not path:
          raise ValueError("filter requires value or value_from")
      current: object = context
      for part in path.split("."):
          if not isinstance(current, dict) or part not in current:
              raise ValueError(f"value_from not found: {path}")
          current = current[part]
      return current


  def _as_list(value: object) -> list[object]:
      if isinstance(value, list):
          return value
      if isinstance(value, tuple):
          return list(value)
      return [value]
  ```

- [ ] **Step 3.2: Wire source audit into `audit_task_issue_batch()`**

  In `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_audit.py`, import:

  ```python
  from .source_context import resolve_repo_path, source_queries_from_batch, source_tables_from_batch
  from .source_slicing import render_query_slice
  ```

  In `audit_task_issue_batch`, after each per-task `_audit_source_paths` call, add:

  ```python
      _audit_source_tables_and_queries(batch, task, findings)
  ```

  Add:

  ```python
  def _audit_source_tables_and_queries(batch: TaskIssueBatch, task: TaskIssueDraft, findings: list[AuditFinding]) -> None:
      try:
          tables = source_tables_from_batch(batch)
          queries = source_queries_from_batch(batch)
      except ValueError as exc:
          findings.append(AuditFinding(code="source_context", message=str(exc), path=str(batch.batch_doc.path)))
          return
      table_by_id = {table.id: table for table in tables}
      for table in tables:
          if not table.id:
              findings.append(AuditFinding(code="source_table", message="source table id is required", path=str(batch.batch_doc.path)))
          if table.format not in {"tsv", "csv"}:
              findings.append(AuditFinding(code="source_table_format", message=f"unsupported source table format: {table.format}", path=str(batch.batch_doc.path)))
          resolved = resolve_repo_path(batch.repo_root, table.path)
          if table.required and not resolved.is_file():
              findings.append(AuditFinding(code="source_table_not_found", message=f"source table path does not exist: {table.path}", path=str(batch.batch_doc.path)))
      for query in queries:
          if query.table not in table_by_id:
              findings.append(AuditFinding(code="source_query_table", message=f"source query references unknown table: {query.table}", path=str(batch.batch_doc.path)))
              continue
          resolved_table = table_by_id[query.table]
          table = type(resolved_table)(
              id=resolved_table.id,
              path=str(resolve_repo_path(batch.repo_root, resolved_table.path)),
              format=resolved_table.format,
              key_columns=resolved_table.key_columns,
              required=resolved_table.required,
              sha256=resolved_table.sha256,
          )
          result = render_query_slice(
              table=table,
              query=query,
              context={"task": dict(task.metadata), "issue": {"external_id": task.task_id}},
              output_path=batch.path / ".audit" / task.task_id / f"{query.id}.{table.format}",
              write_output=False,
          )
          if not result.ok:
              findings.append(AuditFinding(code=result.code, message=f"{query.id}: {result.message}", path=str(task.path)))
  ```

- [ ] **Step 3.3: Include tables/query definitions in approval hash**

  In `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_hash.py`, import:

  ```python
  import json
  from .source_context import resolve_repo_path, source_queries_from_batch, source_tables_from_batch
  ```

  Before returning the digest, add:

  ```python
      for table in source_tables_from_batch(batch):
          digest.update(json.dumps(table.to_issue_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"))
          _hash_file(digest, resolve_repo_path(batch.repo_root, table.path))
      for query in source_queries_from_batch(batch):
          digest.update(json.dumps(query.to_issue_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"))
  ```

- [ ] **Step 3.4: Run focused tests**

  ```bash
  pytest tests/test_source_slicing.py tests/test_batch_audit.py -k "source_table or source_query" -q
  pytest tests/test_batch_submit.py::BatchSubmitTest::test_rejects_batch_changed_after_approval -q
  ```

  Expected after implementation: all selected tests pass.

## Task 4: Submit And Plane Live Metadata Round Trip

**Files:**
- Modify: `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_submit.py`
- Modify: `/Users/raymond/projects/dora-orchestrator/orchestrator/plane_live.py`
- Test: `/Users/raymond/projects/dora-orchestrator/tests/test_batch_submit.py`
- Test: `/Users/raymond/projects/dora-orchestrator/tests/test_live_plane.py`

- [ ] **Step 4.1: Build task execution packet payload during submit**

  In `/Users/raymond/projects/dora-orchestrator/orchestrator/batch_submit.py`, import:

  ```python
  from .source_context import (
      EXECUTION_PACKET_VERSION,
      source_docs_for_task,
      source_queries_from_batch,
      source_tables_from_batch,
  )
  ```

  Inside the `for task in batch.tasks:` loop, before `plane_client.upsert_issue`, compute:

  ```python
          source_docs = [doc.to_issue_dict(batch.repo_root) for doc in source_docs_for_task(batch, task)]
          source_tables = [table.to_issue_dict() for table in source_tables_from_batch(batch)]
          source_queries = [query.to_issue_dict() for query in source_queries_from_batch(batch)]
          execution_packet_hash = compute_batch_hash(batch.path, repo_root=repo_root)
  ```

  Add these fields to the task issue payload:

  ```python
                  "execution_packet_version": EXECUTION_PACKET_VERSION,
                  "execution_packet_hash": execution_packet_hash,
                  "source_docs": source_docs,
                  "source_tables": source_tables,
                  "source_queries": source_queries,
  ```

- [ ] **Step 4.2: Add hidden JSON metadata block in live Plane markdown**

  In `/Users/raymond/projects/dora-orchestrator/orchestrator/plane_live.py`, add constants near `_issue_markdown`:

  ```python
  _DORA_METADATA_START = "<!-- dora:metadata"
  _DORA_METADATA_END = "dora:metadata -->"
  _DORA_METADATA_KEYS = [
      "execution_packet_version",
      "execution_packet_hash",
      "source_docs",
      "source_tables",
      "source_queries",
      "verification_commands",
  ]
  ```

  Add helpers:

  ```python
  def _metadata_payload(payload: dict[str, Any]) -> dict[str, Any]:
      return {key: payload[key] for key in _DORA_METADATA_KEYS if key in payload}


  def _append_metadata_block(markdown: str, metadata: dict[str, Any]) -> str:
      if not metadata:
          return markdown
      encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
      return f"{markdown.rstrip()}\n\n{_DORA_METADATA_START}\n{encoded}\n{_DORA_METADATA_END}\n"


  def _extract_metadata_block(description_html: str) -> dict[str, Any]:
      text = html.unescape(description_html)
      if "<pre>" in text and "</pre>" in text:
          text = text.split("<pre>", 1)[1].split("</pre>", 1)[0]
      start = text.find(_DORA_METADATA_START)
      if start < 0:
          return {}
      json_start = text.find("\n", start)
      end = text.find(_DORA_METADATA_END, json_start)
      if json_start < 0 or end < 0:
          return {}
      raw = text[json_start:end].strip()
      try:
          value = json.loads(raw)
      except json.JSONDecodeError:
          return {}
      return value if isinstance(value, dict) else {}
  ```

  At the end of `_issue_markdown`, before return:

  ```python
      markdown = "\n".join(lines).strip() + "\n"
      return _append_metadata_block(markdown, _metadata_payload(payload))
  ```

  In `_adapt_issue`, after progress metadata extraction:

  ```python
      adapted.update(_extract_metadata_block(description_html))
  ```

- [ ] **Step 4.3: Preserve verification commands in live Plane**

  With the metadata block above, `verification_commands` no longer depends on YAML scalar/list extraction. Add an assertion in the live test that `verification_commands` survives.

- [ ] **Step 4.4: Run submit/live tests**

  ```bash
  pytest tests/test_batch_submit.py::BatchSubmitTest::test_submits_execution_packet_source_context_to_task_issue -q
  pytest tests/test_live_plane.py -k execution_packet_metadata -q
  ```

  Expected after implementation: both pass.

## Task 5: Bundle Generation And Prompt Injection

**Files:**
- Create: `/Users/raymond/projects/dora-orchestrator/orchestrator/source_bundle.py`
- Modify: `/Users/raymond/projects/dora-orchestrator/orchestrator/run_ready_task.py`
- Test: `/Users/raymond/projects/dora-orchestrator/tests/test_source_bundle.py`
- Test: `/Users/raymond/projects/dora-orchestrator/tests/test_run_ready_batch_task.py`

- [ ] **Step 5.1: Implement source bundle generation**

  Create `/Users/raymond/projects/dora-orchestrator/orchestrator/source_bundle.py`:

  ```python
  """Generate per-task source bundles for executor runs."""

  from __future__ import annotations

  import json
  from dataclasses import dataclass
  from pathlib import Path
  from typing import Mapping

  from .source_context import EXECUTION_PACKET_VERSION, SourceQuery, SourceTable, hash_file, resolve_repo_path
  from .source_slicing import SliceResult, render_query_slice


  @dataclass(frozen=True)
  class SourceBundleResult:
      ok: bool
      bundle_root: Path
      bundle_path: Path
      manifest_path: Path
      required_read_paths: tuple[Path, ...]
      slice_results: tuple[SliceResult, ...]
      message: str = ""
  ```

  ```python
  def create_source_bundle(*, issue: Mapping[str, object], worktree_root: Path) -> SourceBundleResult:
      batch_id = str(issue.get("batch_id") or _batch_id_from_external_id(str(issue.get("external_id") or "")) or "unknown-batch")
      task_key = str(issue.get("external_id") or issue.get("key") or "unknown-task")
      bundle_root = worktree_root / ".dora" / "source-bundles" / batch_id / task_key
      bundle_path = bundle_root / "source-bundle.md"
      manifest_path = bundle_root / "manifest.json"
      slice_dir = bundle_root / "slices"
      source_docs = [item for item in issue.get("source_docs") or [] if isinstance(item, dict)]
      source_tables = [item for item in issue.get("source_tables") or [] if isinstance(item, dict)]
      source_queries = [item for item in issue.get("source_queries") or [] if isinstance(item, dict)]
      required_paths: list[Path] = []
      missing: list[str] = []
      for doc in source_docs:
          if not bool(doc.get("required", True)):
              continue
          path = resolve_repo_path(worktree_root, str(doc.get("path") or ""))
          if not path.is_file():
              missing.append(str(path))
          required_paths.append(path)
      table_by_id = {
          str(item.get("id")): SourceTable(
              id=str(item.get("id") or ""),
              path=str(resolve_repo_path(worktree_root, str(item.get("path") or ""))),
              format=str(item.get("format") or "tsv"),
              key_columns=tuple(str(value) for value in item.get("key_columns") or []),
              required=bool(item.get("required", True)),
              sha256=str(item.get("sha256") or ""),
          )
          for item in source_tables
      }
      slice_results: list[SliceResult] = []
      for item in source_queries:
          query = SourceQuery(
              id=str(item.get("id") or ""),
              table=str(item.get("table") or ""),
              required=bool(item.get("required", True)),
              filters=tuple(dict(value) for value in item.get("filters") or [] if isinstance(value, dict)),
              columns=tuple(str(value) for value in item.get("columns") or []),
              max_rows=int(item.get("max_rows") or 200),
          )
          table = table_by_id.get(query.table)
          if table is None:
              missing.append(f"query {query.id} table {query.table}")
              continue
          result = render_query_slice(
              table=table,
              query=query,
              context={"task": dict(issue), "issue": dict(issue)},
              output_path=slice_dir / f"{query.id}.{table.format}",
          )
          slice_results.append(result)
          if not result.ok and query.required:
              missing.append(f"{query.id}: {result.message}")
          if result.ok and result.output_path is not None and query.required:
              required_paths.append(result.output_path)
      required_paths.insert(0, bundle_path)
      if missing:
          return SourceBundleResult(False, bundle_root, bundle_path, manifest_path, tuple(required_paths), tuple(slice_results), message="; ".join(missing))
      bundle_root.mkdir(parents=True, exist_ok=True)
      manifest = {
          "execution_packet_version": EXECUTION_PACKET_VERSION,
          "batch_id": batch_id,
          "task_key": task_key,
          "source_docs": source_docs,
          "source_tables": source_tables,
          "source_queries": source_queries,
          "slices": [
              {"query_id": result.query_id, "path": str(result.output_path), "rows": result.row_count, "sha256": result.sha256}
              for result in slice_results
          ],
          "required_read_paths": [str(path) for path in required_paths],
      }
      manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
      bundle_path.write_text(_render_bundle_markdown(manifest, manifest_path), encoding="utf-8")
      return SourceBundleResult(True, bundle_root, bundle_path, manifest_path, tuple(required_paths), tuple(slice_results), message="ok")
  ```

  ```python
  def _render_bundle_markdown(manifest: dict[str, object], manifest_path: Path) -> str:
      lines = [
          "# Source Bundle",
          "",
          f"- Execution packet version: {manifest['execution_packet_version']}",
          f"- Batch: {manifest['batch_id']}",
          f"- Task: {manifest['task_key']}",
          f"- Manifest: {manifest_path}",
          "",
          "## Required Reading",
          "",
      ]
      lines.extend(f"- {path}" for path in manifest["required_read_paths"])
      lines.extend(["", "## Generated Slices", ""])
      for item in manifest["slices"]:
          lines.append(f"- {item['query_id']}: {item['path']} ({item['rows']} rows, {item['sha256']})")
      return "\n".join(lines) + "\n"


  def _batch_id_from_external_id(external_id: str) -> str:
      parts = external_id.split("-")
      return parts[2] if len(parts) >= 4 else ""
  ```

- [ ] **Step 5.2: Add source bundle prompt section**

  In `/Users/raymond/projects/dora-orchestrator/orchestrator/run_ready_task.py`, import:

  ```python
  from .source_bundle import SourceBundleResult, create_source_bundle
  from .source_context import EXECUTION_PACKET_VERSION
  ```

  Add helper:

  ```python
  def _render_source_context_prompt(source_bundle: SourceBundleResult) -> str:
      required = "\n".join(f"- {path}" for path in source_bundle.required_read_paths)
      slices = "\n".join(
          f"- {result.query_id}: {result.output_path} ({result.row_count} rows)"
          for result in source_bundle.slice_results
          if result.output_path is not None
      )
      return (
          "## Source Context Contract\n\n"
          "Before editing code, read every required path below with file-reading tools. "
          "The orchestrator verifies read evidence from the executor event log before delivery.\n\n"
          f"Required reads:\n{required}\n\n"
          f"Generated slices:\n{slices}\n"
      )
  ```

  Modify the prompt write path:

  ```python
          source_bundle = create_source_bundle(issue=claimed, worktree_root=repo_root)
          if not source_bundle.ok:
              raise RuntimeError(f"source bundle generation failed after claim: {source_bundle.message}")
          if delivery is not None and delivery.enable_commit:
              prompt_text = _render_executor_prompt(
                  claimed,
                  batch_id=_extract_batch_id(external_id),
                  branch=branch,
                  worktree_path=repo_root,
              )
          else:
              prompt_text = _render_batch_prompt(claimed)
          prompt_text = prompt_text.rstrip() + "\n\n" + _render_source_context_prompt(source_bundle) + "\n"
  ```

  This also fixes the current prompt path bug by passing `repo_root` instead of `delivery.worktree_path`.

- [ ] **Step 5.3: Preflight legacy and missing source context before claim**

  In `_execute_one_task`, after `picked` and before `claim_issue`, add:

  ```python
      if issue.get("execution_packet_version") != EXECUTION_PACKET_VERSION:
          if hasattr(plane_client, "add_label"):
              plane_client.add_label(config.project_slug, external_id, "dora:source-context-missing")
          plane_client.add_comment(
              config.project_slug,
              external_id,
              "Execution Packet v1 is missing. Regenerate/resubmit the batch so source docs, source tables, source queries, and verification commands are attached.",
              marker="dora-loop:source-context",
          )
          plane_client.release_issue(config.project_slug, external_id, "Needs Input")
          return {"outcome": "source_context_missing", "state": "Needs Input", "run_id": run_id, "external_id": external_id}
  ```

  Then generate a preflight bundle against `config.target_repo` before claim:

  ```python
      preflight_bundle = create_source_bundle(issue=issue, worktree_root=config.target_repo)
      if not preflight_bundle.ok:
          if hasattr(plane_client, "add_label"):
              plane_client.add_label(config.project_slug, external_id, "dora:source-context-missing")
          plane_client.add_comment(
              config.project_slug,
              external_id,
              f"Source context preflight failed: {preflight_bundle.message}",
              marker="dora-loop:source-context",
          )
          plane_client.release_issue(config.project_slug, external_id, "Needs Input")
          return {"outcome": "source_context_missing", "state": "Needs Input", "run_id": run_id, "external_id": external_id}
  ```

  After worktree creation, regenerate the bundle in the actual `repo_root` used by the executor.

- [ ] **Step 5.4: Run bundle and prompt tests**

  ```bash
  pytest tests/test_source_bundle.py tests/test_run_ready_batch_task.py -k "source_bundle or source_context" -q
  ```

  Expected after implementation: source bundle tests pass and prompt contains required paths.

## Task 6: Source Evidence Gate Before Delivery

**Files:**
- Create: `/Users/raymond/projects/dora-orchestrator/orchestrator/source_evidence.py`
- Modify: `/Users/raymond/projects/dora-orchestrator/orchestrator/run_ready_task.py`
- Test: `/Users/raymond/projects/dora-orchestrator/tests/test_source_evidence.py`
- Test: `/Users/raymond/projects/dora-orchestrator/tests/test_run_ready_batch_task.py`

- [ ] **Step 6.1: Implement JSONL evidence parser**

  Create `/Users/raymond/projects/dora-orchestrator/orchestrator/source_evidence.py`:

  ```python
  """Evaluate source-read evidence from executor JSONL event logs."""

  from __future__ import annotations

  import json
  import re
  import shlex
  from dataclasses import dataclass
  from pathlib import Path
  from typing import Any, Iterable, Mapping


  @dataclass(frozen=True)
  class SourceEvidenceResult:
      ok: bool
      required_paths: tuple[Path, ...]
      observed_paths: tuple[Path, ...]
      missing_paths: tuple[Path, ...]
      message: str
  ```

  ```python
  def evaluate_source_evidence_from_event_path(
      event_path: Path,
      *,
      worktree_root: Path,
      required_paths: Iterable[Path],
  ) -> SourceEvidenceResult:
      events: list[Mapping[str, Any]] = []
      if event_path.exists():
          for line in event_path.read_text(encoding="utf-8").splitlines():
              if not line.strip().startswith("{"):
                  continue
              try:
                  event = json.loads(line)
              except json.JSONDecodeError:
                  continue
              if isinstance(event, dict):
                  events.append(event)
      return evaluate_source_evidence(events=events, worktree_root=worktree_root, required_paths=required_paths)
  ```

  ```python
  def evaluate_source_evidence(
      *,
      events: Iterable[Mapping[str, Any]],
      worktree_root: Path,
      required_paths: Iterable[Path],
  ) -> SourceEvidenceResult:
      root = worktree_root.resolve()
      required = tuple(_normalize_path(path, root) for path in required_paths)
      observed_set: set[Path] = set()
      for event in events:
          for raw_path in _paths_from_event(event):
              observed_set.add(_normalize_path(Path(raw_path), root))
      observed = tuple(sorted(observed_set, key=str))
      missing = tuple(path for path in required if path not in observed)
      return SourceEvidenceResult(
          ok=not missing,
          required_paths=required,
          observed_paths=observed,
          missing_paths=missing,
          message="source read evidence complete" if not missing else "missing source read evidence: " + ", ".join(str(path) for path in missing),
      )
  ```

  ```python
  def _paths_from_event(event: Mapping[str, Any]) -> list[str]:
      return _paths_from_claude_event(event) + _paths_from_codex_event(event)


  def _paths_from_claude_event(event: Mapping[str, Any]) -> list[str]:
      paths: list[str] = []
      message = event.get("message")
      if not isinstance(message, Mapping):
          return paths
      content = message.get("content")
      if not isinstance(content, list):
          return paths
      for block in content:
          if not isinstance(block, Mapping) or block.get("type") != "tool_use":
              continue
          name = str(block.get("name") or "")
          tool_input = block.get("input")
          if not isinstance(tool_input, Mapping):
              continue
          if name in {"Read", "Grep", "Glob"}:
              for key in ("file_path", "path"):
                  value = tool_input.get(key)
                  if isinstance(value, str):
                      paths.append(value)
          if name == "Bash":
              command = tool_input.get("command")
              if isinstance(command, str):
                  paths.extend(_paths_from_shell_command(command))
      return paths
  ```

  ```python
  def _paths_from_codex_event(event: Mapping[str, Any]) -> list[str]:
      item = event.get("item")
      if not isinstance(item, Mapping):
          return []
      command = item.get("command")
      if isinstance(command, str):
          return _paths_from_shell_command(command)
      if isinstance(command, list):
          return [token for token in command if isinstance(token, str) and _looks_like_path(token)]
      return []
  ```

  ```python
  def _paths_from_shell_command(command: str) -> list[str]:
      try:
          tokens = shlex.split(command)
      except ValueError:
          tokens = command.split()
      paths = [token for token in tokens if _looks_like_path(token)]
      paths.extend(re.findall(r"['\"]([^'\"]+\\.dora/source-bundles/[^'\"]+)['\"]", command))
      return paths


  def _looks_like_path(value: str) -> bool:
      return value.startswith("/") or value.startswith("./") or value.startswith("../") or ".dora/source-bundles/" in value or value.startswith("docs/")


  def _normalize_path(path: Path, root: Path) -> Path:
      return path.resolve() if path.is_absolute() else (root / path).resolve()
  ```

- [ ] **Step 6.2: Gate release/delivery on evidence**

  In `/Users/raymond/projects/dora-orchestrator/orchestrator/run_ready_task.py`, import:

  ```python
  from .source_evidence import evaluate_source_evidence_from_event_path
  ```

  After verification and before outcome selection, add:

  ```python
          source_evidence = evaluate_source_evidence_from_event_path(
              artifacts.event_path,
              worktree_root=repo_root,
              required_paths=source_bundle.required_read_paths,
          )
          if on_progress:
              on_progress("source_evidence", {"external_id": external_id, "pass": source_evidence.ok})
  ```

  In outcome selection, source evidence failure overrides successful agent completion:

  ```python
          if not source_evidence.ok:
              outcome = "source_evidence_missing"
          elif strict_progress and result_signal is not None:
              outcome = _progress_controlled_outcome(result.outcome, bool(verification["pass"]), result_signal)
          elif result.outcome == "agent_done" and not verification["pass"]:
              outcome = "agent_unverified"
          else:
              outcome = result.outcome
  ```

  In terminal state selection, add:

  ```python
          if outcome == "source_evidence_missing":
              terminal_state = "Needs Input"
          elif outcome == "agent_done":
              terminal_state = "Done"
  ```

  Add label/comment:

  ```python
          if outcome == "source_evidence_missing" and hasattr(plane_client, "add_label"):
              plane_client.add_label(config.project_slug, external_id, "dora:source-evidence-missing")
          if outcome == "source_evidence_missing":
              plane_client.add_comment(
                  config.project_slug,
                  external_id,
                  "Source evidence failed after executor run.\n\nMissing reads:\n" + "\n".join(f"- {path}" for path in source_evidence.missing_paths),
                  marker="dora-loop:source-evidence",
              )
  ```

  Include `source_evidence` in `publish_run_report`:

  ```python
                  "source_evidence": {
                      "pass": source_evidence.ok,
                      "missing_paths": [str(path) for path in source_evidence.missing_paths],
                      "observed_paths": [str(path) for path in source_evidence.observed_paths],
                  },
  ```

  In `run_delivery`, pass combined verification:

  ```python
              verification_pass=bool(verification["pass"]) and source_evidence.ok,
  ```

- [ ] **Step 6.3: Run evidence tests**

  ```bash
  pytest tests/test_source_evidence.py -q
  pytest tests/test_run_ready_batch_task.py -k source_evidence -q
  ```

  Expected after implementation: evidence tests pass and delivery uses combined verification.

## Task 7: Operator Visibility

**Files:**
- Modify: `/Users/raymond/projects/dora-orchestrator/orchestrator/status.py`
- Modify: `/Users/raymond/projects/dora-orchestrator/orchestrator/query_issues.py`
- Test: existing CLI tests if present, otherwise add focused unit tests around JSON output helpers.

- [ ] **Step 7.1: Add source context derivation helper**

  Add this helper to both CLI files or to a small shared module if duplication becomes more than two call sites:

  ```python
  def _source_context_state(issue: dict[str, object]) -> str:
      labels = set(issue.get("labels") or [])
      if "dora:source-context-missing" in labels:
          return "missing"
      if "dora:source-evidence-missing" in labels:
          return "evidence_missing"
      if issue.get("execution_packet_version") == 1:
          return "packet_v1"
      return "legacy"
  ```

- [ ] **Step 7.2: Include `source_context` in query output**

  In `/Users/raymond/projects/dora-orchestrator/orchestrator/query_issues.py`, add:

  ```python
              "source_context": _source_context_state(i),
  ```

  Update stderr row formatting:

  ```python
              print(
                  f"    {i['external_id']:32s}  {i['state']:12s}  {i['module']:14s}  {i['source_context']:16s}  {name}",
                  file=sys.stderr,
              )
  ```

- [ ] **Step 7.3: Include blocked source context in status output**

  In `/Users/raymond/projects/dora-orchestrator/orchestrator/status.py`, add:

  ```python
              "source_context": _source_context_state(i),
  ```

  Add stderr detail:

  ```python
              print(f"      source_context: {bi['source_context']}", file=sys.stderr)
  ```

## Task 8: Full Verification And Commit Sequence

**Files:** all touched files.

- [ ] **Step 8.1: Run focused tests**

  ```bash
  pytest tests/test_batch_loader.py -q
  pytest tests/test_source_slicing.py tests/test_source_evidence.py -q
  pytest tests/test_batch_audit.py -k "source_table or source_query" -q
  pytest tests/test_batch_submit.py::BatchSubmitTest::test_submits_execution_packet_source_context_to_task_issue -q
  pytest tests/test_live_plane.py -k execution_packet_metadata -q
  pytest tests/test_run_ready_batch_task.py -k "source_context or source_evidence" -q
  ```

- [ ] **Step 8.2: Run broader regression**

  ```bash
  pytest -q
  python -m compileall orchestrator tests
  git diff --check
  ```

- [ ] **Step 8.3: Inspect generated source bundle manually from test artifact**

  ```bash
  find /Users/raymond/projects/dora-orchestrator -path "*/.dora/source-bundles/*" -type f | sort | head -20
  ```

  Confirm `source-bundle.md`, `manifest.json`, and generated `slices/current_task_row.tsv` exist. Confirm full raw TSV content is not copied into the prompt.

- [ ] **Step 8.4: Commit in small slices**

  Stage only files touched for each slice:

  ```bash
  git add orchestrator/source_context.py orchestrator/batch_loader.py tests/test_batch_loader.py
  git commit -m "feat(batch): parse execution packet source context"
  ```

  ```bash
  git add orchestrator/source_slicing.py orchestrator/batch_audit.py orchestrator/batch_hash.py tests/test_source_slicing.py tests/test_batch_audit.py
  git commit -m "feat(batch): audit source tables and slices"
  ```

  ```bash
  git add orchestrator/batch_submit.py orchestrator/plane_live.py tests/test_batch_submit.py tests/test_live_plane.py
  git commit -m "feat(plane): persist execution packet metadata"
  ```

  ```bash
  git add orchestrator/source_bundle.py orchestrator/source_evidence.py orchestrator/run_ready_task.py tests/test_source_bundle.py tests/test_source_evidence.py tests/test_run_ready_batch_task.py
  git commit -m "feat(executor): gate runs on source context evidence"
  ```

  ```bash
  git add orchestrator/status.py orchestrator/query_issues.py
  git commit -m "feat(status): show source context state"
  ```

## Done Criteria

- Nested `source_tables` and `source_queries` parse from `batch.md`.
- Existing task-level `source_pages`, `source_docs`, and `source_summaries` are normalized into issue metadata with hashes.
- Audit fails for missing required source docs/tables, invalid table/query definitions, empty required query results, and row count overflow.
- Approval hash changes when a source table file or source query definition changes.
- Submitted task issues include `execution_packet_version`, `execution_packet_hash`, `source_docs`, `source_tables`, `source_queries`, and `verification_commands`.
- Live Plane issue markdown round-trips nested source metadata and `verification_commands`.
- `run_ready_batch_task` blocks legacy unfinished issues without Execution Packet v1 before claim.
- `run_ready_batch_task` generates `.dora/source-bundles/<batch>/<task>/source-bundle.md`, `manifest.json`, and required TSV/CSV slices.
- Executor prompt lists exact required read paths and generated slice paths.
- Missing source context moves the issue to `Needs Input` and labels it `dora:source-context-missing`.
- Missing read evidence after execution moves the issue to `Needs Input`, labels it `dora:source-evidence-missing`, and prevents delivery/auto-merge.
- Query/status output exposes `source_context` as `packet_v1`, `missing`, `evidence_missing`, or `legacy`.
- `pytest -q`, `python -m compileall orchestrator tests`, and `git diff --check` pass, or failures are documented as unrelated pre-existing failures.

## Self-Review Checklist

- [ ] Source docs and large TSV/CSV files are not embedded into the prompt.
- [ ] Executor receives exact bundle/doc/slice paths before editing.
- [ ] Source context is validated before claim.
- [ ] Source read evidence is validated after executor run and before delivery.
- [ ] `verification_commands` are preserved through live Plane.
- [ ] Legacy unfinished tasks are hard-blocked until regenerated/resubmitted.
- [ ] No new runtime dependency is introduced.
- [ ] Every commit stages only source-packet changes, preserving existing dirty worktree changes.
