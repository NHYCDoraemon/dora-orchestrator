"""Unified CLI entry point: `orchestrator <subcommand> ...`."""

import argparse
import sys

from . import approve_batch, audit_batch, batches, provision, scaffold, status, submit_batch


SUBCOMMANDS = {
    "audit": audit_batch.main,
    "approve": approve_batch.main,
    "batches": batches.main,
    "provision": provision.main,
    "scaffold": scaffold.main,
    "status": status.main,
    "submit": submit_batch.main,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="Audit, approve, submit, and provision TaskIssueDraft batches.",
        add_help=True,
    )
    parser.add_argument("subcommand", choices=sorted(SUBCOMMANDS), help="subcommand to run")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="arguments forwarded to the subcommand")
    parsed = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return SUBCOMMANDS[parsed.subcommand](parsed.args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
