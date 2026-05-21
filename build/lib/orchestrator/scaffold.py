"""Standard project scaffold for Dora-orchestrated repositories."""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .project_resolver import write_project_registry


STANDARD_MODULES = [
    {
        "id": "product",
        "name": "Product",
        "purpose": "Business intent, product goals, user value, glossary, and requirements.",
    },
    {
        "id": "architecture",
        "name": "Architecture",
        "purpose": "System design, ADR anchors, technical constraints, and integration boundaries.",
    },
    {
        "id": "planning",
        "name": "Planning",
        "purpose": "Roadmap, milestones, decomposition, dependencies, and sequencing decisions.",
    },
    {
        "id": "implementation",
        "name": "Implementation",
        "purpose": "Executable implementation plans, task breakdowns, and engineering handoff details.",
    },
    {
        "id": "verification",
        "name": "Verification",
        "purpose": "Acceptance criteria, test strategy, quality gates, and release readiness.",
    },
    {
        "id": "operations",
        "name": "Operations",
        "purpose": "Runbooks, deployment notes, recovery paths, and operational checks.",
    },
    {
        "id": "governance",
        "name": "Governance",
        "purpose": "Security, compliance, permissions, audit requirements, and policy decisions.",
    },
]


@dataclass(frozen=True)
class ScaffoldResult:
    created: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)


def scaffold_project(repo_root: Path, *, project_slug: str, title: str) -> ScaffoldResult:
    repo = repo_root.resolve()
    created: list[Path] = []
    skipped: list[Path] = []

    files = {
        repo / ".dora" / "project.json": _project_json(project_slug, title),
        repo / "docs" / "dora" / "index.md": _index_md(title),
        repo / "docs" / "dora" / "modules.md": _modules_md(),
        repo / "docs" / "dora" / "product" / "vision.md": _doc_template(title, "Product Vision"),
        repo / "docs" / "dora" / "product" / "requirements.md": _doc_template(title, "Requirements"),
        repo / "docs" / "dora" / "product" / "glossary.md": _doc_template(title, "Glossary"),
        repo / "docs" / "dora" / "architecture" / "overview.md": _doc_template(title, "Architecture Overview"),
        repo / "docs" / "dora" / "architecture" / "decisions.md": _doc_template(title, "Decision Index"),
        repo / "docs" / "dora" / "planning" / "roadmap.md": _doc_template(title, "Roadmap"),
        repo / "docs" / "dora" / "planning" / "implementation.md": _doc_template(title, "Implementation Plan"),
        repo / "docs" / "dora" / "quality" / "acceptance.md": _doc_template(title, "Acceptance Strategy"),
        repo / "docs" / "dora" / "quality" / "risk-register.md": _doc_template(title, "Risk Register"),
        repo / "docs" / "dora" / "operations" / "runbook.md": _doc_template(title, "Operations Runbook"),
        repo / "docs" / "dora" / "governance" / "policy.md": _doc_template(title, "Governance Policy"),
    }

    for path, content in files.items():
        if path.exists():
            skipped.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(path)

    return ScaffoldResult(created=created, skipped=skipped)


def _project_json(project_slug: str, title: str) -> str:
    payload = {
        "project_slug": project_slug,
        "title": title,
        "module_taxonomy": [module["id"] for module in STANDARD_MODULES],
        "doc_root": "docs/dora",
        "orchestration_spec": ".dora/orchestration.json",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _index_md(title: str) -> str:
    module_lines = "\n".join(f"- [{module['name']}](#{module['id']})" for module in STANDARD_MODULES)
    return f"""# {title} Dora Index

This is the canonical project index consumed by dora-orchestrator.

## Standard Modules

{module_lines}

## Document Slots

- Product: `docs/dora/product/`
- Architecture: `docs/dora/architecture/`
- Planning: `docs/dora/planning/`
- Verification: `docs/dora/quality/`
- Operations: `docs/dora/operations/`
- Governance: `docs/dora/governance/`

Business-specific concepts should be captured inside these slots. Do not create project-specific Plane modules for every business domain; use issue labels, task metadata, or document anchors instead.
"""


def _modules_md() -> str:
    sections = []
    for module in STANDARD_MODULES:
        sections.append(f"## {module['id']}\n\n- Name: {module['name']}\n- Purpose: {module['purpose']}\n")
    return "# Dora Standard Modules\n\n" + "\n".join(sections)


def _doc_template(project_title: str, title: str) -> str:
    return f"""# {title}

Project: {project_title}

## Current Understanding

Write the current agreed understanding here.

## Evidence

- Link to source discussions, ADRs, specs, issues, or implementation notes.

## Open Questions

- List unresolved questions that block planning or execution.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a standard Dora project scaffold.")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--project-slug", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args(argv)

    result = scaffold_project(args.repo, project_slug=args.project_slug, title=args.title)
    reg_path = write_project_registry(args.project_slug, args.title, args.repo.resolve())
    print(f"created={len(result.created)} skipped={len(result.skipped)} registry={reg_path}", file=sys.stderr)
    output = {
        "status": "OK",
        "created": [str(p) for p in result.created],
        "skipped": [str(p) for p in result.skipped],
        "registry": str(reg_path),
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    for path in result.created:
        print(f"created {path}", file=sys.stderr)
    for path in result.skipped:
        print(f"skipped {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
