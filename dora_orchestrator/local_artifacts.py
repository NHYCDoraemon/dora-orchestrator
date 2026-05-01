"""Local artifact paths for Dora orchestration runs."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunArtifacts:
    root: Path
    prompt_path: Path
    event_path: Path
    summary_path: Path
    verify_path: Path


def create_run_artifacts(repo_root: Path, run_id: str) -> RunArtifacts:
    root = (repo_root / ".dora" / "loop-runs" / run_id).resolve()
    repo = repo_root.resolve()
    if repo not in root.parents:
        raise ValueError(f"run artifact path escapes repo root: {root}")
    root.mkdir(parents=True, exist_ok=True)
    return RunArtifacts(
        root=root,
        prompt_path=root / "prompt.md",
        event_path=root / "events.ndjson",
        summary_path=root / "summary.md",
        verify_path=root / "verify.txt",
    )

