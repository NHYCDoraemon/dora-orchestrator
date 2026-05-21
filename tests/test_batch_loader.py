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


def create_batch(repo: Path) -> Path:
    batch_dir = repo / "docs" / "dora" / "batches" / "20260501A"
    tasks_dir = batch_dir / "tasks"
    tasks_dir.mkdir(parents=True)
    (repo / "docs" / "summaries").mkdir(parents=True)
    (repo / "docs" / "summaries" / "S1.5-P3-05.md").write_text("# Prior work\n", encoding="utf-8")
    (repo / "docs" / "design.md").write_text("# Design\n", encoding="utf-8")
    (batch_dir / "batch.md").write_text(
        """---
batch_id: 20260501A
program_id: dora-context-assembly
program_prefix: CTX
title: Dora 上下文装配第四阶段
status: draft
created_by: raymond
created_at: 2026-05-01T21:30:00+08:00
---

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
    (tasks_dir / "DORA-CTX-20260501A-T01.md").write_text(
        """---
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
---

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
