"""Git delivery policy for Dora orchestration.

Git records deliverables. Dagster and Plane record orchestration state.
"""

FORBIDDEN_ORCHESTRATION_PREFIXES = (
    "chore(progress): claim lock",
    "chore(progress): release lock",
    "chore(progress): reset lock",
    "chore(progress): heartbeat",
)


def is_forbidden_orchestration_commit(message: str) -> bool:
    normalized = " ".join(message.strip().split())
    return any(normalized.startswith(prefix) for prefix in FORBIDDEN_ORCHESTRATION_PREFIXES)

