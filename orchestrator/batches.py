"""CLI for listing available batches in a project."""

import argparse
import json
import sys

from .project_resolver import list_available_batches, resolve_project_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List available Dora TaskIssueDraft batches.")
    parser.add_argument("--repo", default=None, help="target repository root")
    parser.add_argument("--project", default=None, help="project slug for registry lookup")
    args = parser.parse_args(argv)

    resolved = resolve_project_config(
        repo=args.repo,
        project=args.project,
    )

    batches = list_available_batches(resolved.repo_root)
    result = {
        "status": "OK",
        "repo_root": str(resolved.repo_root),
        "project_slug": resolved.project_slug,
        "project_title": resolved.project_title,
        "available_batches": [
            {"batch_id": b.batch_id, "title": b.title, "task_count": b.task_count, "path": str(b.path)}
            for b in batches
        ],
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))

    if batches:
        print(f"\nAvailable batches ({resolved.repo_root}):", file=sys.stderr)
        for b in batches:
            print(f"  {b.batch_id:12s}  {b.title[:60]:60s}  {b.task_count} task(s)", file=sys.stderr)
    else:
        print(f"\nNo batches found under {resolved.repo_root / 'docs' / 'dora' / 'batches'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
