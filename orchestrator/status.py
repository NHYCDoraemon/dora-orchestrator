"""CLI for showing project issue status and blocked tasks."""

import argparse
import json
import sys

from .config import load_config
from .plane_backends import create_plane_client
from .project_resolver import resolve_project_config


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
    client = create_plane_client(config)

    # Build status summary from plane state
    state_counts: dict[str, int] = {}
    blocked_issues: list[dict] = []
    total = 0

    if hasattr(client, "issues") and isinstance(client.issues, dict):
        for (slug, external_id), issue in client.issues.items():
            if slug != resolved.project_slug:
                continue
            total += 1
            state = issue.get("state", "Unknown")
            state_counts[state] = state_counts.get(state, 0) + 1
            if state == "Blocked":
                blocked_issues.append({
                    "external_id": external_id,
                    "name": issue.get("name", ""),
                    "depends_on": issue.get("depends_on", []),
                    "priority": issue.get("priority", ""),
                    "module": issue.get("module", ""),
                })
    else:
        # For live Plane backend, use the blocked_issues method
        blocked = client.blocked_issues(resolved.project_slug)
        blocked_issues = [
            {
                "external_id": i.get("external_id", ""),
                "name": i.get("name", ""),
                "depends_on": i.get("depends_on", []),
                "priority": i.get("priority", ""),
                "module": i.get("module", ""),
            }
            for i in blocked
        ]
        for i in blocked_issues:
            state_counts["Blocked"] = state_counts.get("Blocked", 0) + 1

    result = {
        "status": "OK",
        "repo_root": str(resolved.repo_root),
        "project_slug": resolved.project_slug,
        "project_title": resolved.project_title,
        "total_issues": total if total else len(blocked_issues),
        "state_summary": state_counts,
        "blocked_count": len(blocked_issues),
        "blocked_issues": blocked_issues if args.show_blocked else [],
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))

    print(f"\nProject: {resolved.project_slug} ({resolved.repo_root})", file=sys.stderr)
    if total or blocked_issues:
        print(f"  Issues: {total if total else len(blocked_issues)} total", file=sys.stderr)
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

        if not args.show_blocked:
            print("\n  Use --show-blocked for full JSON details.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
