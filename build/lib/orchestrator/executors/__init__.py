"""Executor registry."""

from .noop import NoopExecutor


def get_executor(name: str):
    if name == "noop":
        return NoopExecutor()
    if name == "codex":
        from .codex import CodexExecutor

        return CodexExecutor()
    if name == "claude":
        from .claude import ClaudeExecutor

        return ClaudeExecutor()
    raise ValueError(f"unsupported executor: {name}")
