import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.publish_docs import (
    PAGE_TARGETS,
    build_marker,
    parse_marker,
    resolve_target,
    rewrite_cross_doc_links,
    strip_existing_marker,
)


class ResolveTargetTest(unittest.TestCase):
    def test_scaffolded_dora_paths_have_chinese_display_names(self):
        t = resolve_target("dora/product/vision.md")
        self.assertEqual(t.book_slug, "product")
        self.assertEqual(t.book_display, "产品")
        self.assertEqual(t.page_slug, "vision")
        self.assertEqual(t.page_display, "愿景")

    def test_dora_index_goes_to_overview_book(self):
        t = resolve_target("dora/index.md")
        self.assertEqual(t.book_slug, "overview")
        self.assertEqual(t.page_display, "索引")

    def test_root_doc_file_goes_to_project_overview(self):
        t = resolve_target("00-overview.md")
        self.assertEqual(t.book_slug, "_root_overview")
        self.assertEqual(t.book_display, "项目概览")
        self.assertEqual(t.page_slug, "00-overview")
        self.assertEqual(t.page_display, "00-overview")

    def test_top_level_dir_uses_chinese_book_name(self):
        t = resolve_target("decisions/0001-form-engine-separation.md")
        self.assertEqual(t.book_slug, "decisions")
        self.assertEqual(t.book_display, "架构决策")
        self.assertEqual(t.page_slug, "0001-form-engine-separation")
        self.assertEqual(t.page_display, "0001-form-engine-separation")

    def test_unmapped_top_level_dir_falls_back_to_english(self):
        t = resolve_target("brand-new/spec.md")
        self.assertEqual(t.book_slug, "brand-new")
        self.assertEqual(t.book_display, "brand-new")

    def test_deep_path_flattens_into_page_display(self):
        # docs/qa/2026-04-27-form-engine-acceptance.md → just 2 levels, no flatten
        t2 = resolve_target("qa/2026-04-27-form-engine-acceptance.md")
        self.assertEqual(t2.book_display, "质量保证")
        self.assertEqual(t2.page_display, "2026-04-27-form-engine-acceptance")

        # 3+ levels: middle dirs become breadcrumb prefix on the page display.
        t3 = resolve_target("guides/spike/2026/q2-plan.md")
        self.assertEqual(t3.book_display, "操作指南")
        self.assertEqual(t3.page_slug, "q2-plan")
        self.assertEqual(t3.page_display, "spike/2026/q2-plan")


class RewriteLinksTest(unittest.TestCase):
    def setUp(self):
        self.docs_root = Path("/repo/docs")
        self.url_map = {
            "architecture/overview.md":   "http://wiki/books/arch/page/overview",
            "decisions/0001-foo.md":      "http://wiki/books/adr/page/0001-foo",
            "dora/product/vision.md":     "http://wiki/books/product/page/vision",
            "qa/forms/acceptance.md":     "http://wiki/books/qa/page/forms-acceptance",
        }

    def _rewrite(self, body: str, current_rel: str) -> str:
        return rewrite_cross_doc_links(
            body, current_rel=current_rel, docs_root=self.docs_root, url_map=self.url_map,
        )

    def test_rewrites_relative_dot_slash(self):
        body = "See [overview](./overview.md) for context.\n"
        out = self._rewrite(body, "architecture/decisions.md")
        self.assertIn("[overview](http://wiki/books/arch/page/overview)", out)

    def test_rewrites_parent_relative(self):
        body = "See [架构概述](../architecture/overview.md).\n"
        out = self._rewrite(body, "decisions/0001-foo.md")
        self.assertIn("(http://wiki/books/arch/page/overview)", out)

    def test_preserves_fragment(self):
        body = "Jump to [section](./overview.md#section-2)\n"
        out = self._rewrite(body, "architecture/decisions.md")
        self.assertIn("(http://wiki/books/arch/page/overview#section-2)", out)

    def test_skips_external_urls(self):
        body = "See [Plane API](https://api.plane.so/docs).\n"
        out = self._rewrite(body, "architecture/decisions.md")
        self.assertEqual(body, out)

    def test_skips_unknown_md_targets(self):
        # docs/dora/operations/runbook.md exists in PAGE_TARGETS but not in
        # url_map for this test → leave alone (no false rewrite).
        body = "See [runbook](../operations/runbook.md).\n"
        out = self._rewrite(body, "dora/architecture/overview.md")
        self.assertEqual(body, out)

    def test_skips_image_syntax(self):
        body = "![diagram](./overview.md)\n"
        # Image with .md target makes no real sense, but we should not rewrite
        # any `![...]()` pattern to BookStack URL (those are images, separate
        # asset pipeline).
        out = self._rewrite(body, "architecture/decisions.md")
        self.assertEqual(body, out)

    def test_skips_inside_code_fence(self):
        body = (
            "Outside fence: [link](./overview.md)\n"
            "```\n"
            "[fake link](./overview.md)\n"
            "```\n"
            "After fence: [link2](./overview.md)\n"
        )
        out = self._rewrite(body, "architecture/decisions.md")
        # The fence-internal markdown sample stays as-is.
        self.assertIn("[fake link](./overview.md)", out)
        # The two outside-fence links got rewritten.
        self.assertEqual(out.count("http://wiki/books/arch/page/overview"), 2)


class MarkerTest(unittest.TestCase):
    def test_build_and_parse_round_trip(self):
        marker = build_marker("process-engine", "product/vision.md", "abc1234")
        parsed = parse_marker(marker)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["project"], "process-engine")
        self.assertEqual(parsed["src"], "product/vision.md")

    def test_parse_no_marker(self):
        self.assertIsNone(parse_marker("just some markdown content\n"))

    def test_strip_existing_marker_idempotent(self):
        marker = build_marker("p", "a.md", "sha")
        body = "### hello\n\nstuff\n\n" + marker + "\n"
        stripped = strip_existing_marker(body)
        self.assertNotIn("managed-by", stripped)
        # Stripping again is a no-op.
        self.assertEqual(strip_existing_marker(stripped), stripped)


# ── flow-level tests with full mock isolation ───────────────────────


def _fake_client():
    client = MagicMock()
    client.settings = MagicMock(base_url="http://bookstack.test")
    client.upsert_shelf.return_value = {"id": 1, "slug": "smoke", "name": "Smoke", "books": []}
    # upsert_book returns sequential ids per call
    book_counter = {"n": 100}
    def _upsert_book(*, shelf_id, name, description=""):
        book_counter["n"] += 1
        # Hash-like auto-slug for Chinese names, matching real BookStack behavior.
        return {"id": book_counter["n"], "slug": f"b{book_counter['n']:x}", "name": name}
    client.upsert_book.side_effect = _upsert_book
    client.upsert_page.side_effect = lambda *, book_id, name, markdown: {
        "id": 5000 + book_id, "slug": f"p{5000 + book_id:x}", "name": name, "book_id": book_id,
    }
    client.list_pages_in_book.return_value = []
    return client


class PublishDocsFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "docs" / "dora" / "product").mkdir(parents=True)
        (self.repo / "docs" / "dora" / "product" / "vision.md").write_text(
            "### 项目愿景\n\n这是一份已是中文的愿景文档。\n", encoding="utf-8"
        )
        (self.repo / "docs" / "dora" / "product" / "requirements.md").write_text(
            "# Requirements\n\nThis is an English doc that must be translated.\n", encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_registry(self):
        from orchestrator.dagster_defs.project_config import ProjectConfig
        cfg = ProjectConfig(
            slug="smoke",
            title="Smoke",
            repo_root=self.repo,
            plane_project_id="",
            plane_workspace_slug="",
            default_executor="codex",
        )
        return patch("orchestrator.publish_docs.load_project_configs", return_value=[cfg])

    def test_dry_run_no_codex_no_bookstack(self):
        """--dry-run + --no-translate on a mixed repo skips english files cleanly."""
        with self._patch_registry(), \
             patch("orchestrator.publish_docs.BookStackClient") as Client, \
             patch("orchestrator.publish_docs.translate_to_chinese") as Trans:
            from orchestrator.publish_docs import publish_docs
            # Use --no-translate path on the english file → abort
            with self.assertRaises(SystemExit) as ctx:
                publish_docs("smoke", dry_run=True, no_translate=True)
            self.assertEqual(ctx.exception.code, 2)
            Client.assert_not_called()
            Trans.assert_not_called()

    def test_translates_then_writes_back(self):
        """English file → translate_to_chinese called → file is overwritten."""
        with self._patch_registry(), \
             patch("orchestrator.publish_docs.BookStackSettings.from_env",
                   return_value=MagicMock()), \
             patch("orchestrator.publish_docs.BookStackClient") as ClientCls, \
             patch("orchestrator.publish_docs.translate_to_chinese",
                   return_value="### 需求\n\n这是翻译后的内容。\n") as Trans:
            ClientCls.return_value = _fake_client()
            from orchestrator.publish_docs import publish_docs
            result = publish_docs("smoke")
        # English file translated once
        self.assertEqual(Trans.call_count, 1)
        # File on disk now Chinese
        req_text = (self.repo / "docs" / "dora" / "product" / "requirements.md").read_text(encoding="utf-8")
        self.assertIn("需求", req_text)
        # Both files published
        self.assertEqual(len(result["published"]), 2)
        self.assertIn("requirements.md", result["translated"][0])

    def test_no_publish_writes_files_but_no_upsert(self):
        with self._patch_registry(), \
             patch("orchestrator.publish_docs.BookStackClient") as ClientCls, \
             patch("orchestrator.publish_docs.translate_to_chinese",
                   return_value="### 需求\n\n这是翻译后的内容。\n"):
            ClientCls.return_value = _fake_client()
            from orchestrator.publish_docs import publish_docs
            result = publish_docs("smoke", no_publish=True)
        ClientCls.assert_not_called()
        self.assertEqual(result["published"], [])
        self.assertEqual(len(result["skipped"]), 2)

    def test_second_run_skips_translation(self):
        """File written back in run 1 → run 2 detects Chinese, no codex call."""
        with self._patch_registry(), \
             patch("orchestrator.publish_docs.BookStackSettings.from_env",
                   return_value=MagicMock()), \
             patch("orchestrator.publish_docs.BookStackClient") as ClientCls, \
             patch("orchestrator.publish_docs.translate_to_chinese",
                   return_value="### 需求\n\n这是翻译后的内容。\n") as Trans:
            ClientCls.return_value = _fake_client()
            from orchestrator.publish_docs import publish_docs
            publish_docs("smoke")  # run 1: translates 1 file
            publish_docs("smoke")  # run 2: should translate 0
        # translate_to_chinese was called exactly once across the two runs.
        self.assertEqual(Trans.call_count, 1)


if __name__ == "__main__":
    unittest.main()
