"""CLI for showing project issue status and blocked tasks."""

import argparse
import json
import sys

from .config import load_config
from .plane_backends import create_plane_client
from .project_resolver import resolve_project_config
from .source_visibility import classify_source_context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show project issue status.")
    parser.add_argument("--repo", default=None, help="target repository root")
    parser.add_argument("--project", default=None, help="project slug for registry lookup")
    parser.add_argument("--show-blocked", action="store_true", help="show blocked task details")
    args = parser.parse_args(argv)

    resolved = resolve_project_config(
        repo=args.repo,
        project=args.project,
    )

    config = load_config()
    client = create_plane_client(config, resolved_project=resolved)

    state_counts = client.state_counts(resolved.project_slug)
    total = sum(state_counts.values())

    blocked = client.blocked_issues(resolved.project_slug)
    blocked_issues = [
        {
            "external_id": i.get("external_id", ""),
            "name": i.get("name", ""),
            "depends_on": list(i.get("depends_on") or []),
            "priority": i.get("priority", ""),
            "module": i.get("module", ""),
            "source_context": classify_source_context(i),
        }
        for i in blocked
    ]

    result = {
        "status": "OK",
        "repo_root": str(resolved.repo_root),
        "project_slug": resolved.project_slug,
        "project_title": resolved.project_title,
        "total_issues": total,
        "state_summary": state_counts,
        "blocked_count": len(blocked_issues),
        "blocked_issues": blocked_issues if args.show_blocked else [],
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))

    print(f"\nProject: {resolved.project_slug} ({resolved.repo_root})", file=sys.stderr)
    if total:
        print(f"  Issues: {total} total", file=sys.stderr)
        for state, count in sorted(state_counts.items()):
            flag = "  [!] " if state == "Blocked" else "     "
            print(f"{flag}{state}: {count}", file=sys.stderr)

    if blocked_issues:
        print(f"\n  Blocked tasks ({len(blocked_issues)}):", file=sys.stderr)
        for bi in blocked_issues:
            deps = ", ".join(bi["depends_on"]) if bi["depends_on"] else "(none)"
            print(f"    {bi['external_id']}", file=sys.stderr)
            print(f"      name: {bi['name'][:80]}", file=sys.stderr)
            print(f"      depends_on: [{deps}]", file=sys.stderr)
            print(f"      source_context: {bi['source_context']}", file=sys.stderr)

        if not args.show_blocked:
            print("\n  Use --show-blocked for full JSON details.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
