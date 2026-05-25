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
    metadata = _parse_simple_yaml(lines[1:end], path, start_line=2)
    body = "\n".join(lines[end + 1 :]).strip() + "\n"
    return metadata, body


def _parse_simple_yaml(lines: list[str], path: Path, *, start_line: int = 1) -> dict[str, object]:
    normalized = [
        (start_line + index, len(raw) - len(raw.lstrip(" ")), raw.strip())
        for index, raw in enumerate(lines)
        if raw.strip()
    ]
    data: dict[str, object] = {}
    index = 0
    while index < len(normalized):
        line_no, indent, text = normalized[index]
        if indent != 0:
            raise ValueError(f"unsupported nested YAML in {path}: line {line_no}: {text}")
        if ":" not in text:
            raise ValueError(f"invalid YAML line in {path}: line {line_no}: {text}")
        key, raw_value = text.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if value:
            data[key] = _parse_scalar(value)
            index += 1
            continue
        block, index = _parse_yaml_block(
            normalized,
            index + 1,
            parent_indent=0,
            path=path,
            key=key,
        )
        data[key] = block
    return data


def _parse_yaml_block(
    lines: list[tuple[int, int, str]],
    index: int,
    *,
    parent_indent: int,
    path: Path,
    key: str,
) -> tuple[object, int]:
    if index >= len(lines) or lines[index][1] <= parent_indent:
        return [], index
    line_no, indent, text = lines[index]
    if key == "source_tables":
        return _parse_yaml_mapping_list(
            lines,
            index,
            indent=indent,
            path=path,
            bool_scalar_keys={"required"},
            nested_scalar_lists={"key_columns"},
            nested_mapping_lists=set(),
        )
    if key == "source_queries":
        return _parse_yaml_mapping_list(
            lines,
            index,
            indent=indent,
            path=path,
            bool_scalar_keys={"required"},
            nested_scalar_lists={"columns"},
            nested_mapping_lists={"filters"},
        )
    if text.startswith("- "):
        return _parse_yaml_scalar_list(lines, index, indent=indent, path=path)
    raise ValueError(f"unsupported nested YAML in {path}: line {line_no}: {text}")


def _parse_yaml_scalar_list(
    lines: list[tuple[int, int, str]],
    index: int,
    *,
    indent: int,
    path: Path,
) -> tuple[list[object], int]:
    out: list[object] = []
    while index < len(lines):
        line_no, current_indent, text = lines[index]
        if current_indent < indent:
            break
        if current_indent != indent or not text.startswith("- "):
            raise ValueError(f"unsupported nested YAML in {path}: line {line_no}: {text}")
        item_text = text[2:].strip()
        if not item_text:
            raise ValueError(f"unsupported nested YAML in {path}: line {line_no}: {text}")
        out.append(_parse_scalar(item_text))
        index += 1
    return out, index


def _parse_yaml_mapping_list(
    lines: list[tuple[int, int, str]],
    index: int,
    *,
    indent: int,
    path: Path,
    bool_scalar_keys: set[str],
    nested_scalar_lists: set[str],
    nested_mapping_lists: set[str],
) -> tuple[list[object], int]:
    out: list[object] = []
    while index < len(lines):
        line_no, current_indent, text = lines[index]
        if current_indent < indent:
            break
        if current_indent != indent or not text.startswith("- "):
            raise ValueError(f"unsupported nested YAML in {path}: line {line_no}: {text}")
        item_text = text[2:].strip()
        if not item_text:
            item, index = _parse_yaml_mapping_item(
                lines,
                index + 1,
                indent=indent + 2,
                path=path,
                bool_scalar_keys=bool_scalar_keys,
                nested_scalar_lists=nested_scalar_lists,
                nested_mapping_lists=nested_mapping_lists,
            )
            out.append(item)
            continue
        if ":" not in item_text:
            raise ValueError(f"invalid YAML line in {path}: line {line_no}: {text}")
        key, raw_value = item_text.split(":", 1)
        key = key.strip()
        item = {
            key: _parse_scalar(raw_value.strip(), parse_bool=key in bool_scalar_keys)
            if raw_value.strip()
            else []
        }
        index += 1
        item, index = _parse_yaml_mapping_item(
            lines,
            index,
            indent=indent + 2,
            path=path,
            bool_scalar_keys=bool_scalar_keys,
            nested_scalar_lists=nested_scalar_lists,
            nested_mapping_lists=nested_mapping_lists,
            item=item,
        )
        out.append(item)
    return out, index


def _parse_yaml_mapping_item(
    lines: list[tuple[int, int, str]],
    index: int,
    *,
    indent: int,
    path: Path,
    bool_scalar_keys: set[str],
    nested_scalar_lists: set[str],
    nested_mapping_lists: set[str],
    item: dict[str, object] | None = None,
) -> tuple[dict[str, object], int]:
    out = {} if item is None else item
    while index < len(lines):
        line_no, current_indent, text = lines[index]
        if current_indent < indent:
            break
        if current_indent != indent:
            raise ValueError(f"unsupported nested YAML in {path}: line {line_no}: {text}")
        if text.startswith("- "):
            break
        if ":" not in text:
            raise ValueError(f"invalid YAML line in {path}: line {line_no}: {text}")
        key, raw_value = text.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if value:
            out[key] = _parse_scalar(value, parse_bool=key in bool_scalar_keys)
            index += 1
        elif key in nested_scalar_lists:
            nested, index = _parse_yaml_scalar_list(lines, index + 1, indent=indent + 2, path=path)
            out[key] = nested
        elif key in nested_mapping_lists:
            nested, index = _parse_yaml_mapping_list(
                lines,
                index + 1,
                indent=indent + 2,
                path=path,
                bool_scalar_keys=set(),
                nested_scalar_lists=set(),
                nested_mapping_lists=set(),
            )
            out[key] = nested
        else:
            raise ValueError(f"unsupported nested YAML in {path}: line {line_no}: {text}")
    return out, index


def _parse_scalar(value: str, *, parse_bool: bool = False) -> object:
    if value == "[]":
        return []
    if parse_bool and value in {"true", "True"}:
        return True
    if parse_bool and value in {"false", "False"}:
        return False
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
