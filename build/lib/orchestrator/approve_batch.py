"""CLI for approving audited TaskIssueDraft batches."""

import argparse
import json
import sys
from pathlib import Path

from .batch_approval import approve_task_issue_batch
from .project_resolver import list_available_batches, resolve_project_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approve a Dora TaskIssueDraft batch.")
    parser.add_argument("--batch", default=None, help="batch id (e.g. 20260501A)")
    parser.add_argument("--repo", default=None, help="target repository root")
    parser.add_argument("--project", default=None, help="project slug for registry lookup")
    parser.add_argument("--approved-by", required=True, help="approval principal")
    parser.add_argument("--approved-at", default=None, help="approval timestamp")
    args = parser.parse_args(argv)

    resolved = resolve_project_config(
        batch_id=args.batch,
        repo=args.repo,
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

    approval = approve_task_issue_batch(
        resolved.batch_path,
        repo_root=resolved.repo_root,
        approved_by=args.approved_by,
        approved_at=args.approved_at,
    )
    output = json.dumps(approval, ensure_ascii=False, sort_keys=True)
    print(output)
    print(f"\napproved {approval['batch_id']} with {len(approval['approved_task_ids'])} task(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
