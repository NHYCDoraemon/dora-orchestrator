"""Provision project task graphs into the configured Plane backend."""

import argparse
import json
from dataclasses import asdict

from .config import OrchestratorConfig, load_config
from .plane_backends import create_plane_client
from .plane_provisioner import provision_project
from .spec_loader import load_project_spec
from .task_graph import build_task_graph


def provision_project_from_config(config: OrchestratorConfig) -> dict[str, int]:
    spec = load_project_spec(config.spec_path)
    graph = build_task_graph(spec)
    client = create_plane_client(config)
    return provision_project(client, graph)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision a Dora orchestration spec into Plane.")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args(argv)

    config = load_config()
    result = provision_project_from_config(config)
    if args.json:
        print(json.dumps({"config": asdict(config), "result": result}, default=str, sort_keys=True))
    else:
        print(
            "provisioned "
            f"{result['projects']} project(s), "
            f"{result['cycles']} cycle(s), "
            f"{result['modules']} module(s), "
            f"{result['issues']} issue(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

