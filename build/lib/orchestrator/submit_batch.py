"""CLI for submitting approved TaskIssueDraft batches."""

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .plane_backends import create_plane_client
from .batch_submit import submit_task_issue_batch
from .project_resolver import list_available_batches, resolve_project_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Submit an approved Dora TaskIssueDraft batch.")
    parser.add_argument("--batch", default=None, help="batch id (e.g. 20260501A)")
    parser.add_argument("--repo", default=None, help="target repository root")
    parser.add_argument("--project-slug", default=None, help="Plane project slug")
    parser.add_argument("--project-title", default=None, help="Plane project title")
    parser.add_argument("--project", default=None, help="project slug for registry lookup")
    args = parser.parse_args(argv)

    resolved = resolve_project_config(
        batch_id=args.batch,
        repo=args.repo,
        project_slug=args.project_slug,
        project_title=args.project_title,
        project=args.project,
    )

    # No batch specified — list available batches
    if args.batch is None:
        batches = list_available_batches(resolved.repo_root)
        result = {
            "status": "NO_BATCH",
            "available_batches": [
                {"batch_id": b.batch_id, "title": b.title, "task_count": b.task_count}
                for b in batches
            ],
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if batches:
            print(f"\nAvailable batches ({resolved.repo_root}):", file=sys.stderr)
            for b in batches:
                print(f"  {b.batch_id:12s}  {b.title[:60]:60s}  {b.task_count} task(s)", file=sys.stderr)
            print("\nSpecify --batch <id> to select one.", file=sys.stderr)
        else:
            print(f"\nNo batches found under {resolved.repo_root / 'docs' / 'dora' / 'batches'}", file=sys.stderr)
        return 1

    if resolved.batch_path is None:
        print(json.dumps({"status": "ERROR", "message": "batch path could not be resolved"}, ensure_ascii=False))
        return 1

    config = load_config()
    repo_root = resolved.repo_root
    client = create_plane_client(config, resolved_project=resolved)
    result = submit_task_issue_batch(
        resolved.batch_path,
        repo_root=repo_root,
        project_slug=resolved.project_slug,
        project_title=resolved.project_title,
        plane_client=client,
    )
    output = json.dumps(result, ensure_ascii=False, sort_keys=True)
    print(output)
    print(
        f"\nsubmitted {result['pages']} page(s), "
        f"{result['root_epic_issues']} root epic(s), "
        f"{result['task_issues']} task issue(s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
