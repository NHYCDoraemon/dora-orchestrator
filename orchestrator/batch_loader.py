"""Load TaskIssueDraft batch directories from markdown files."""

import re
from pathlib import Path

from .batch_models import MarkdownDocument, SECTION_TITLE_ALIASES, TaskIssueBatch, TaskIssueDraft


SECTION_RE = re.compile(r"^# (?P<title>.+?)\s*$", re.MULTILINE)


def load_task_issue_batch(batch_dir: Path, *, repo_root: Path | None = None) -> TaskIssueBatch:
    batch_dir = batch_dir.expanduser().resolve()
    root = repo_root.expanduser().resolve() if repo_root else _infer_repo_root(batch_dir)
    batch_doc = _read_markdown_document(batch_dir / "batch.md")
    program_page = _read_markdown_document(batch_dir / "program-page.md", require_frontmatter=False)
    tasks_dir = batch_dir / "tasks"
    if not tasks_dir.exists():
        raise FileNotFoundError(f"missing tasks directory: {tasks_dir}")
    tasks = [
        _read_task_issue_draft(path)
        for path in sorted(tasks_dir.glob("*.md"))
    ]
    if not tasks:
        raise ValueError(f"batch has no task drafts: {tasks_dir}")
    return TaskIssueBatch(
        path=batch_dir,
        batch_doc=batch_doc,
        program_page=program_page,
        tasks=tasks,
        repo_root=root,
    )


def _infer_repo_root(batch_dir: Path) -> Path:
    marker = f"{Path('docs') / 'dora' / 'batches'}"
    raw = str(batch_dir)
    if marker in raw:
        return Path(raw.split(marker, 1)[0]).resolve()
    return batch_dir.parent


def _read_task_issue_draft(path: Path) -> TaskIssueDraft:
    doc = _read_markdown_document(path)
    return TaskIssueDraft(
        path=doc.path,
        metadata=doc.metadata,
        body=doc.body,
        sections=_split_sections(doc.body),
    )


def _read_markdown_document(path: Path, *, require_frontmatter: bool = True) -> MarkdownDocument:
    if not path.exists():
        raise FileNotFoundError(f"missing markdown document: {path}")
    text = path.read_text(encoding="utf-8")
    if text.splitlines()[0:1] == ["---"]:
        metadata, body = _parse_frontmatter(text, path)
    elif require_frontmatter:
        raise ValueError(f"missing YAML frontmatter: {path}")
    else:
        metadata, body = {}, text.strip() + "\n"
    return MarkdownDocument(path=path.resolve(), metadata=metadata, body=body)


def _parse_frontmatter(text: str, path: Path) -> tuple[dict[str, object], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"missing YAML frontmatter: {path}")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError(f"unterminated YAML frontmatter: {path}") from exc
    metadata = _parse_simple_yaml(lines[1:end], path)
    body = "\n".join(lines[end + 1 :]).strip() + "\n"
    return metadata, body


def _parse_simple_yaml(lines: list[str], path: Path) -> dict[str, object]:
    data: dict[str, object] = {}
    current_key = ""
    for raw in lines:
        if not raw.strip():
            continue
        if raw.startswith("  - "):
            if not current_key:
                raise ValueError(f"list item without key in {path}: {raw}")
            value = data.setdefault(current_key, [])
            if not isinstance(value, list):
                raise ValueError(f"key is not a list in {path}: {current_key}")
            value.append(_parse_scalar(raw[4:].strip()))
            continue
        if raw.startswith(" "):
            raise ValueError(f"unsupported nested YAML in {path}: {raw}")
        if ":" not in raw:
            raise ValueError(f"invalid YAML line in {path}: {raw}")
        key, raw_value = raw.split(":", 1)
        current_key = key.strip()
        value = raw_value.strip()
        if value == "":
            data[current_key] = []
        else:
            data[current_key] = _parse_scalar(value)
    return data


def _parse_scalar(value: str) -> object:
    if value == "[]":
        return []
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value.isdigit():
        return int(value)
    return value


def _split_sections(body: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        raw_title = match.group("title").strip()
        title = SECTION_TITLE_ALIASES.get(raw_title, raw_title)
        sections[title] = body[start:end].strip()
    return sections
