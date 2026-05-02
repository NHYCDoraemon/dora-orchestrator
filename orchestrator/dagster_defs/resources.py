"""Dagster resource placeholders for Dora orchestration."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoConfig:
    root: Path
    default_executor: str = "noop"

