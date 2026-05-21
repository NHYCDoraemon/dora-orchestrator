import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.translate import (
    TranslationError,
    demote_headings,
    find_h1_or_h2,
    is_english,
    translate_to_chinese,
)


class IsEnglishTest(unittest.TestCase):
    def test_pure_english(self):
        self.assertTrue(is_english("hello world how are you"))

    def test_pure_chinese(self):
        self.assertFalse(is_english("你好世界今天怎么样"))

    def test_mostly_english_with_a_few_chinese_terms(self):
        # ~95% ASCII
        self.assertTrue(is_english("ABCDEFGHIJ KLMNOPQRST UVWXYZ abc 中"))

    def test_mostly_chinese_with_a_few_english_terms(self):
        self.assertFalse(is_english("这是关于 API 设计的说明文档"))

    def test_empty(self):
        self.assertTrue(is_english(""))
        self.assertTrue(is_english("   \n\t  "))

    def test_threshold_just_above(self):
        # 30 cjk + 60 ascii non-space (~33% cjk) → still Chinese (>= threshold)
        text = "中" * 30 + "a" * 60
        self.assertFalse(is_english(text))

    def test_threshold_just_below(self):
        # 20 cjk + 80 ascii non-space (20% cjk) → English
        text = "中" * 20 + "a" * 80
        self.assertTrue(is_english(text))

    def test_cjk_absolute_floor_overrides_low_ratio(self):
        # 600 CJK + 10_000 ASCII (~6% ratio, well below 30%) — but absolute
        # CJK count >= 500, so the doc is clearly bilingual-substantive and
        # we should NOT call codex on it again.
        text = "中" * 600 + "a" * 10000
        self.assertFalse(is_english(text))


class DemoteHeadingsTest(unittest.TestCase):
    def test_h1_to_h3(self):
        self.assertEqual(demote_headings("# Title\n"), "### Title\n")

    def test_h2_to_h4(self):
        self.assertEqual(demote_headings("## Section\n"), "#### Section\n")

    def test_h3_untouched(self):
        self.assertEqual(demote_headings("### Sub\n"), "### Sub\n")

    def test_h4_untouched(self):
        self.assertEqual(demote_headings("#### Detail\n"), "#### Detail\n")

    def test_inside_code_fence_untouched(self):
        text = "Foo\n\n```\n# not a heading\n## also not\n```\n\n# Real\n"
        out = demote_headings(text)
        self.assertIn("# not a heading", out)
        self.assertIn("## also not", out)
        self.assertIn("### Real", out)
        self.assertNotIn("##### Real", out)

    def test_nested_fences_with_different_tick_counts(self):
        text = "````\n# inside outer\n```\nstill inside\n```\n# also inside\n````\n# Outside\n"
        out = demote_headings(text)
        self.assertIn("# inside outer", out)
        self.assertIn("# also inside", out)
        self.assertIn("### Outside", out)

    def test_bare_hash_lines(self):
        # Bare `#` or `##` with nothing else is unusual but should still demote.
        self.assertEqual(demote_headings("#\n"), "###\n")
        self.assertEqual(demote_headings("##\n"), "####\n")


class FindH12Test(unittest.TestCase):
    def test_after_demote_is_clean(self):
        text = "### a\n#### b\n##### c\n"
        self.assertEqual(find_h1_or_h2(text), [])

    def test_picks_up_violation(self):
        text = "### a\n# bad\n## also bad\n### ok\n"
        findings = find_h1_or_h2(text)
        self.assertEqual([(2, "# bad"), (3, "## also bad")], findings)

    def test_ignores_h12_inside_fence(self):
        text = "### ok\n```\n# in fence\n```\n"
        self.assertEqual(find_h1_or_h2(text), [])


class TranslateToChineseMockedTest(unittest.TestCase):
    def _mock_run(self, payload_text: str, *, rc: int = 0, stderr: str = ""):
        """Patch subprocess.run so codex returns *payload_text* via -o file."""
        def runner(cmd, *args, **kwargs):
            # The -o path is the last "-o", "<path>" pair in cmd; locate it.
            o_idx = cmd.index("-o")
            out_path = Path(cmd[o_idx + 1])
            out_path.write_text(payload_text, encoding="utf-8")
            return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout="", stderr=stderr)
        return patch("orchestrator.translate.subprocess.run", side_effect=runner)

    def test_happy_path(self):
        with self._mock_run("这是翻译后的中文文档示例，足够多的中文字符以满足校验阈值。\n"):
            out = translate_to_chinese("This is an English doc.")
        self.assertIn("中文", out)

    def test_strips_outer_markdown_fence_wrap(self):
        wrapped = "```markdown\n这是翻译后的中文文档示例，足够多的中文字符以满足校验阈值。\n```\n"
        with self._mock_run(wrapped):
            out = translate_to_chinese("This is an English doc.")
        self.assertNotIn("```", out)
        self.assertIn("中文", out)

    def test_empty_input_returns_input(self):
        # Should never even call codex for an empty doc.
        with patch("orchestrator.translate.subprocess.run") as m:
            self.assertEqual(translate_to_chinese("   \n  "), "   \n  ")
            m.assert_not_called()

    def test_codex_nonzero_raises(self):
        with self._mock_run("", rc=2, stderr="codex auth required"):
            with self.assertRaises(TranslationError) as ctx:
                translate_to_chinese("hello")
        self.assertIn("codex exited 2", str(ctx.exception))

    def test_subprocess_timeout_becomes_translation_error(self):
        import subprocess as _sp
        def raiser(*a, **kw):
            raise _sp.TimeoutExpired(cmd=a[0], timeout=600)
        with patch("orchestrator.translate.subprocess.run", side_effect=raiser):
            with self.assertRaises(TranslationError) as ctx:
                translate_to_chinese("hello world")
        self.assertIn("timed out", str(ctx.exception))

    def test_empty_output_raises(self):
        with self._mock_run(""):
            with self.assertRaises(TranslationError):
                translate_to_chinese("hello world")

    def test_no_progress_returns_none(self):
        # codex returned ~input → 0 CJK added → caller signal: "no-op, skip".
        with self._mock_run("Still English here, not translated."):
            self.assertIsNone(translate_to_chinese("hello world"))

    def test_length_too_short_raises(self):
        src = "a long English paragraph " * 50  # ~1250 chars
        with self._mock_run("中" * 20):  # tiny Chinese reply, ratio < 0.3
            with self.assertRaises(TranslationError) as ctx:
                translate_to_chinese(src)
        self.assertIn("length suspect", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
