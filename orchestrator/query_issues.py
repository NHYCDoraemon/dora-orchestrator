"""CLI for querying Plane issues by state, module, and batch."""

import argparse
import json
import sys

from .config import load_config
from .plane_backends import create_plane_client
from .project_resolver import resolve_project_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query Plane issues by state, module, or batch.")
    parser.add_argument("--repo", default=None, help="target repository root")
    parser.add_argument("--project", default=None, help="project slug for registry lookup")
    parser.add_argument(
        "--state",
        action="append",
        default=None,
        help="filter by state name (repeatable). Example: --state Todo --state Backlog",
    )
    parser.add_argument(
        "--module",
        action="append",
        default=None,
        help="filter by module name (repeatable). Example: --module implementation",
    )
    parser.add_argument(
        "--batch",
        default=None,
        help="filter by batch id (the YYYYMMDDA segment of external_id). Example: --batch 20260505A",
    )
    parser.add_argument(
        "--include-root-epic",
        action="store_true",
        help="include root_epic issues (default: only task issues)",
    )
    args = parser.parse_args(argv)

    resolved = resolve_project_config(repo=args.repo, project=args.project)
    config = load_config()
    client = create_plane_client(config)

    issues = client.query_issues(
        resolved.project_slug,
        states=args.state,
        modules=args.module,
        batch=args.batch,
        include_root_epic=args.include_root_epic,
    )

    output_issues = [
        {
            "external_id": i.get("external_id", ""),
            "name": i.get("name", ""),
            "state": i.get("state", ""),
            "module": i.get("module", ""),
            "priority": i.get("priority", ""),
            "depends_on": list(i.get("depends_on") or []),
            "assignee": i.get("assignee"),
        }
        for i in issues
    ]

    result = {
        "status": "OK",
        "repo_root": str(resolved.repo_root),
        "project_slug": resolved.project_slug,
        "project_title": resolved.project_title,
        "filters": {
            "states": args.state,
            "modules": args.module,
            "batch": args.batch,
            "include_root_epic": args.include_root_epic,
        },
        "match_count": len(output_issues),
        "issues": output_issues,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))

    print(
        f"\nProject: {resolved.project_slug} ({resolved.repo_root})",
        file=sys.stderr,
    )
    filter_parts = []
    if args.state:
        filter_parts.append(f"state in [{', '.join(args.state)}]")
    if args.module:
        filter_parts.append(f"module in [{', '.join(args.module)}]")
    if args.batch:
        filter_parts.append(f"batch = {args.batch}")
    if filter_parts:
        print(f"  Filters: {' AND '.join(filter_parts)}", file=sys.stderr)
    else:
        print("  Filters: (none)", file=sys.stderr)

    if output_issues:
        print(f"  Matches: {len(output_issues)}", file=sys.stderr)
        for i in output_issues:
            name = (i["name"] or "")[:60]
            print(
                f"    {i['external_id']:32s}  {i['state']:12s}  {i['module']:14s}  {name}",
                file=sys.stderr,
            )
    else:
        print("  Matches: 0", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
