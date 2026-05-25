import tempfile
import unittest
from pathlib import Path

from orchestrator.batch_loader import load_task_issue_batch


class BatchLoaderTest(unittest.TestCase):
    def test_loads_batch_and_task_issue_packets(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)

            batch = load_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(batch.batch_id, "20260501A")
            self.assertEqual(batch.program_prefix, "CTX")
            self.assertEqual(batch.program_page.path, (batch_dir / "program-page.md").resolve())
            self.assertEqual(len(batch.tasks), 1)
            self.assertEqual(batch.tasks[0].task_id, "DORA-CTX-20260501A-T01")
            self.assertEqual(batch.tasks[0].sections["Scope"], "允许修改：`internal/cognition/context/*`。")

    def test_loads_batch_source_tables_and_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)

            batch = load_task_issue_batch(batch_dir, repo_root=repo)

            table = batch.batch_doc.metadata["source_tables"][0]
            self.assertEqual(table["id"], "progress_ledger")
            self.assertEqual(table["key_columns"], ["row_id"])
            self.assertIs(table["required"], True)
            query = batch.batch_doc.metadata["source_queries"][0]
            self.assertEqual(query["id"], "current_task_row")
            self.assertIs(query["required"], True)
            self.assertEqual(query["filters"][0]["value_from"], "task.row_id")
            self.assertEqual(query["columns"], ["row_id", "frontend_surface", "backend_contract", "acceptance_signal"])
            self.assertEqual(query["max_rows"], 10)

    def test_preserves_legacy_boolean_scalar_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)
            batch_path = batch_dir / "batch.md"
            batch_path.write_text(
                batch_path.read_text(encoding="utf-8").replace(
                    "created_at: 2026-05-01T21:30:00+08:00\n",
                    "created_at: 2026-05-01T21:30:00+08:00\n"
                    "feature_flag: true\n",
                ),
                encoding="utf-8",
            )

            batch = load_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(batch.batch_doc.metadata["feature_flag"], "true")

    def test_rejects_unknown_source_query_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
            batch_path = batch_dir / "batch.md"
            batch_path.write_text(
                batch_path.read_text(encoding="utf-8").replace(
                    "    max_rows: 10\n",
                    "    max_row: 10\n",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, r"unknown source metadata key.*line 29: max_row"):
                load_task_issue_batch(batch_dir, repo_root=repo)

    def test_rejects_unknown_source_table_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
            batch_path = batch_dir / "batch.md"
            batch_path.write_text(
                batch_path.read_text(encoding="utf-8").replace(
                    "    required: true\nsource_queries:\n",
                    "    requiredd: false\nsource_queries:\n",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, r"unknown source metadata key.*line 15: requiredd"):
                load_task_issue_batch(batch_dir, repo_root=repo)

    def test_rejects_unsupported_nested_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)
            batch_path = batch_dir / "batch.md"
            batch_path.write_text(
                batch_path.read_text(encoding="utf-8").replace(
                    "created_at: 2026-05-01T21:30:00+08:00\n",
                    "created_at: 2026-05-01T21:30:00+08:00\n"
                    "unsupported:\n"
                    "  nested:\n"
                    "    key: value\n",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, r"unsupported nested YAML.*line 10: nested:"):
                load_task_issue_batch(batch_dir, repo_root=repo)


def create_batch(repo: Path, *, with_source_table: bool = False, with_task_row_id: bool = False) -> Path:
    batch_dir = repo / "docs" / "dora" / "batches" / "20260501A"
    tasks_dir = batch_dir / "tasks"
    tasks_dir.mkdir(parents=True)
    (repo / "docs" / "summaries").mkdir(parents=True)
    (repo / "docs" / "summaries" / "S1.5-P3-05.md").write_text("# Prior work\n", encoding="utf-8")
    (repo / "docs" / "design.md").write_text("# Design\n", encoding="utf-8")
    if with_source_table:
        (repo / "docs" / "progress").mkdir(parents=True, exist_ok=True)
        (repo / "docs" / "progress" / "ledger.tsv").write_text(
            "row_id\tfrontend_surface\tbackend_contract\tacceptance_signal\n"
            "F97-036\tsrc/pages/forms/list/FormListPage.tsx\tGET /api/v1/forms\t真实接口响应驱动页面。\n",
            encoding="utf-8",
        )
    source_context_frontmatter = (
        """source_tables:
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
"""
        if with_source_table
        else ""
    )
    (batch_dir / "batch.md").write_text(
        f"""---
batch_id: 20260501A
program_id: dora-context-assembly
program_prefix: CTX
title: Dora 上下文装配第四阶段
status: draft
created_by: raymond
created_at: 2026-05-01T21:30:00+08:00
{source_context_frontmatter}---

# 批次说明

建设上下文装配加固批次，确保开发任务可审计、可执行、可验收。
""",
        encoding="utf-8",
    )
    (batch_dir / "program-page.md").write_text(
        """# 计划说明

本计划负责上下文装配质量、任务拆解和交付验收。
""",
        encoding="utf-8",
    )
    task_row_frontmatter = "row_id: F97-036\n" if with_task_row_id else ""
    (tasks_dir / "DORA-CTX-20260501A-T01.md").write_text(
        f"""---
task_id: DORA-CTX-20260501A-T01
id_scheme: batch-native
legacy_refs:
  - S1.5-P4-01
title: CLI 上下文检查能力
batch_id: 20260501A
program_id: dora-context-assembly
program_prefix: CTX
sequence: 1
cycle: S1.5 Phase 4
module: implementation
priority: P1
depends_on: []
depends_on_legacy:
  - S1.5-P3-05
source_pages:
  - ../program-page.md
source_docs:
  - docs/design.md
source_summaries:
  - docs/summaries/S1.5-P3-05.md
source_commits: []
{task_row_frontmatter}---

# 任务概要

为上下文装配增加 CLI 检查入口，便于开发人员快速确认上下文内容和来源。

# 开发背景

以关联设计文档和历史总结为输入，保持现有上下文装配边界不变。

# 范围

允许修改：`internal/cognition/context/*`。

# 非目标

不修改记忆持久化逻辑。

# 实现要求

阅读上下文装配包，补充聚焦测试，并保持最小变更。

# 验收标准

`go test ./internal/cognition/...` 执行通过。

# 验证要求

执行任务中列出的 L1 和 L2 检查，并记录结果。

# 停止条件

如果设计文档与 ADR 约束冲突，停止实现并记录阻塞。

# 执行器提示契约

编辑前阅读本任务、关联来源和 git 历史，严格按任务范围执行。
""",
        encoding="utf-8",
    )
    return batch_dir
