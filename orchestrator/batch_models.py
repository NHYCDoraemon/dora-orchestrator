"""Models for TaskIssueDraft batch submission."""

from dataclasses import dataclass, field
from pathlib import Path


REQUIRED_ISSUE_PACKET_SECTIONS = [
    "Task Summary",
    "Development Context",
    "Scope",
    "Non-goals",
    "Implementation Detail",
    "Acceptance",
    "Verification",
    "Stop Conditions",
    "Executor Prompt Contract",
]

SECTION_DISPLAY_TITLES = {
    "Task Summary": "任务概要",
    "Development Context": "开发背景",
    "Scope": "范围",
    "Non-goals": "非目标",
    "Implementation Detail": "实现要求",
    "Acceptance": "验收标准",
    "Verification": "验证要求",
    "Stop Conditions": "停止条件",
    "Executor Prompt Contract": "执行器提示契约",
}

SECTION_TITLE_ALIASES = {
    **{title: title for title in REQUIRED_ISSUE_PACKET_SECTIONS},
    **{display: canonical for canonical, display in SECTION_DISPLAY_TITLES.items()},
}

FIXED_MODULE_TAXONOMY = {
    "product",
    "architecture",
    "planning",
    "implementation",
    "verification",
    "operations",
    "governance",
}

PROGRESS_METADATA_FIELDS = [
    "progress_schema",
    "progress_task_id",
    "row_id",
    "source_scope",
    "task_kind",
    "route_path",
    "backend_contract",
    "frontend_surface",
    "requirement_signal",
    "development_work",
    "data_lineage",
    "acceptance_signal",
    "verification_signal",
    "ledger_update_rule",
    "no_go_signal",
]


@dataclass(frozen=True)
class MarkdownDocument:
    path: Path
    metadata: dict[str, object]
    body: str


@dataclass(frozen=True)
class TaskIssueDraft:
    path: Path
    metadata: dict[str, object]
    body: str
    sections: dict[str, str]

    @property
    def task_id(self) -> str:
        return str(self.metadata.get("task_id", ""))

    @property
    def title(self) -> str:
        return str(self.metadata.get("title", ""))

    @property
    def cycle(self) -> str:
        return str(self.metadata.get("cycle", ""))

    @property
    def module(self) -> str:
        return str(self.metadata.get("module", ""))

    @property
    def priority(self) -> str:
        return str(self.metadata.get("priority", ""))


@dataclass(frozen=True)
class TaskIssueBatch:
    path: Path
    batch_doc: MarkdownDocument
    program_page: MarkdownDocument
    tasks: list[TaskIssueDraft]
    repo_root: Path

    @property
    def batch_id(self) -> str:
        return str(self.batch_doc.metadata.get("batch_id", ""))

    @property
    def program_id(self) -> str:
        return str(self.batch_doc.metadata.get("program_id", ""))

    @property
    def program_prefix(self) -> str:
        return str(self.batch_doc.metadata.get("program_prefix", ""))

    @property
    def title(self) -> str:
        return str(self.batch_doc.metadata.get("title", ""))


@dataclass(frozen=True)
class AuditFinding:
    code: str
    message: str
    path: str = ""
    severity: str = "error"


@dataclass(frozen=True)
class BatchAuditResult:
    status: str
    batch_id: str
    task_count: int
    planned_cycle_creates: list[str] = field(default_factory=list)
    findings: list[AuditFinding] = field(default_factory=list)
