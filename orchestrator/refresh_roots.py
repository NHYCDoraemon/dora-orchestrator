"""CLI for rolling up root_epic state from current children.

Useful when:
- Children completed before the auto roll-up was wired up.
- A child state was edited manually in Plane and the root drifted.
"""

import argparse
import json
import sys

from .config import load_config
from .plane_backends import create_plane_client
from .project_resolver import resolve_project_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh root_epic states from their children.")
    parser.add_argument("--repo", default=None, help="target repository root")
    parser.add_argument("--project", default=None, help="project slug for registry lookup")
    parser.add_argument("--json", action="store_true", help="emit JSON summary on stdout")
    args = parser.parse_args(argv)

    resolved = resolve_project_config(repo=args.repo, project=args.project)
    config = load_config()
    client = create_plane_client(config)

    refresh = getattr(client, "_refresh_root_epics", None)
    if refresh is None:
        print(f"backend {type(client).__name__} has no _refresh_root_epics", file=sys.stderr)
        return 2
    refresh(resolved.project_slug)

    summary = {"project": resolved.project_slug, "ok": True}
    if args.json:
        print(json.dumps(summary))
    else:
        print(f"refreshed root states for project={resolved.project_slug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
