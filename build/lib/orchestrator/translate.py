"""English -> Chinese translation helpers for ``orchestrator publish-docs``.

Three pure functions, all called from ``publish_docs.py``:

* :func:`is_english` — heuristic: CJK char ratio below 30% -> English.
* :func:`translate_to_chinese` — shells out to ``codex exec``; raises
  :class:`TranslationError` on any failure mode (codex non-zero, empty
  output, result still English, length wildly off).
* :func:`demote_headings` — code-fence-aware regex sweep:
  ``# x`` -> ``### x``, ``## x`` -> ``#### x``; h3+ untouched; content
  inside ``` ... ``` fences left alone.
"""

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

__all__ = ["TranslationError", "is_english", "translate_to_chinese", "demote_headings"]


class TranslationError(RuntimeError):
    """Raised when the codex translation pass cannot be trusted."""


_CJK_RANGES = (
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0x3000, 0x303F),   # CJK symbols & punctuation (、。「」 etc.)
    (0xFF00, 0xFFEF),   # Fullwidth forms (，；：（） etc.)
)

_ENGLISH_THRESHOLD = 0.30
# Any file with at least this many CJK characters in absolute count is
# considered "Chinese enough" regardless of ratio. Covers the common
# bilingual case: a long markdown doc with substantial Chinese prose buried
# under code blocks, English path references, link targets, and ASCII
# structural noise that pushes the ratio below 30%. Without this, every
# subsequent publish re-confirms via codex (one ~30-60s call per file).
_CJK_ABSOLUTE_FLOOR = 500


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _cjk_count(text: str) -> int:
    return sum(1 for c in text if _is_cjk(c))


def is_english(text: str) -> bool:
    """Return True when *text* needs codex translation.

    Two-stage heuristic:
    1. If absolute CJK count >= :data:`_CJK_ABSOLUTE_FLOOR` (500), the file
       is "Chinese-enough" regardless of ratio — even bilingual docs
       saturated with English markdown structure pass here.
    2. Otherwise, fall back to ratio: CJK / non-whitespace < 30% -> English.

    Empty input is treated as English (translation is a no-op anyway).
    NOT appropriate for validating translator OUTPUT — see
    ``_seems_translated`` for that.
    """
    cjk = _cjk_count(text)
    if cjk >= _CJK_ABSOLUTE_FLOOR:
        return False
    meaningful = [c for c in text if not c.isspace()]
    if not meaningful:
        return True
    return (cjk / len(meaningful)) < _ENGLISH_THRESHOLD


_TRANSLATION_MIN_NEW_CJK = 20


def _seems_translated(src: str, dst: str) -> bool:
    """Did codex actually add Chinese content?

    Counts raw CJK characters in both texts; passes when the destination
    has at least :data:`_TRANSLATION_MIN_NEW_CJK` more CJK chars than the
    source. Robust to ratio-skewed cases like docs/dora/index.md, where
    only ~20% of the body translates (prose) and 80% stays ASCII (links,
    paths, code, heading hashes).
    """
    return _cjk_count(dst) >= _cjk_count(src) + _TRANSLATION_MIN_NEW_CJK


_TRANSLATE_PROMPT = """你是一个翻译工具。把下面这份英文 markdown 翻译成中文。

硬约束：
1. 严格保留所有 markdown 结构：heading 级别、code fence、列表、图片 / 链接、front-matter。
2. front-matter 块（开头 --- 到结束 ---）内的所有内容（键、字符串值）原样透传，不翻译。
3. code fence 内的代码原样透传，不翻译注释。
4. 行内代码 `xxx` 原样透传。
5. 只输出翻译后的 markdown 本体；不输出任何说明、注释、code fence 包裹。

原文：

"""


def translate_to_chinese(text: str, *, codex_bin: str = "codex") -> str | None:
    """Call ``codex exec`` to translate *text* from English to Chinese.

    Returns the translated markdown on success, or :obj:`None` when codex
    reasonably decides translation wasn't needed (returned content with
    no net CJK gain). The None signal lets the caller distinguish
    "file was actually bilingual / Chinese-enough" from a hard failure;
    it is not an error. Hard failures (codex non-zero, empty output,
    length wildly off) still raise :class:`TranslationError`.

    Reads the final agent message via codex's ``-o`` flag (more reliable
    than parsing the JSONL stream). Strips an outer ```markdown ... ```
    wrapper if codex added one.
    """
    if not text.strip():
        return text

    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as fh:
        out_path = Path(fh.name)

    try:
        try:
            proc = subprocess.run(
                [
                    codex_bin, "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--skip-git-repo-check",
                    "--color", "never",
                    "--json",
                    "-o", str(out_path),
                    "-",
                ],
                input=_TRANSLATE_PROMPT + text,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired as exc:
            # Re-raise as TranslationError so the caller's per-file skip path
            # handles it instead of aborting the whole run. The codex process
            # was killed by subprocess; nothing to clean up here.
            raise TranslationError(
                f"codex timed out after {exc.timeout}s (file too large or codex hung)"
            ) from exc
        except (OSError, FileNotFoundError) as exc:
            raise TranslationError(f"codex spawn failed: {exc}") from exc
        if proc.returncode != 0:
            raise TranslationError(
                f"codex exited {proc.returncode}: {proc.stderr[:500]}"
            )
        translated = out_path.read_text(encoding="utf-8")
    finally:
        out_path.unlink(missing_ok=True)

    translated = _strip_markdown_fence_wrap(translated).rstrip() + "\n"

    if not translated.strip():
        raise TranslationError("codex returned empty translation")
    if not _seems_translated(text, translated):
        # Codex returned ~the same content. Most likely the source was
        # bilingual or already substantially Chinese and is_english() was
        # a false positive against the 30% threshold. Signal "no-op" and
        # let the caller proceed with the original text.
        return None
    src_len = len(text)
    dst_len = len(translated)
    if src_len > 100 and (dst_len < src_len * 0.3 or dst_len > src_len * 3.0):
        raise TranslationError(
            f"translation length suspect: src={src_len} dst={dst_len} (ratio out of [0.3, 3.0])"
        )
    return translated


_FENCE_WRAP_RE = re.compile(
    r"\A\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*\Z",
    re.DOTALL,
)


def _strip_markdown_fence_wrap(text: str) -> str:
    """If the whole reply is wrapped in ```markdown ... ```, unwrap it once."""
    m = _FENCE_WRAP_RE.match(text)
    return m.group(1) if m else text


def demote_headings(text: str) -> str:
    """Demote h1 -> h3 and h2 -> h4 outside of fenced code blocks.

    A fenced block starts on a line whose first non-whitespace token is
    three or more backticks, and ends on the matching closing fence.
    Inside such a block, leading ``#`` characters are left alone.
    """
    out_lines: list[str] = []
    in_fence = False
    fence_marker: str | None = None
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            ticks = stripped[: len(stripped) - len(stripped.lstrip("`"))]
            if not in_fence:
                in_fence = True
                fence_marker = ticks
            elif fence_marker and stripped.startswith(fence_marker):
                in_fence = False
                fence_marker = None
            out_lines.append(line)
            continue
        if not in_fence:
            # ## must be checked before # so we don't accidentally double-demote.
            # Both branches prepend two '#': h1 (1#) -> h3 (3#), h2 (2#) -> h4 (4#).
            if line.startswith("## ") or line.rstrip("\n") == "##":
                line = "##" + line
            elif line.startswith("# ") or line.rstrip("\n") == "#":
                line = "##" + line
        out_lines.append(line)
    return "".join(out_lines)


_H12_RE = re.compile(r"^(#{1,2})(?:\s|$)")


def find_h1_or_h2(text: str) -> list[tuple[int, str]]:
    """Return list of (line_number, line) where h1/h2 appears outside fences.

    Used as the lint pass after :func:`demote_headings` — in a correct
    pipeline this should always return [].
    """
    findings: list[tuple[int, str]] = []
    in_fence = False
    fence_marker: str | None = None
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            ticks = stripped[: len(stripped) - len(stripped.lstrip("`"))]
            if not in_fence:
                in_fence = True
                fence_marker = ticks
            elif fence_marker and stripped.startswith(fence_marker):
                in_fence = False
                fence_marker = None
            continue
        if in_fence:
            continue
        if _H12_RE.match(line):
            findings.append((idx, line.rstrip("\n")))
    return findings


def file_fingerprint(path: Path) -> str:
    """Short content hash for cache filenames / debug output."""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:12]
