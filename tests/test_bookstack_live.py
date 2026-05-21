import io
import json
import unittest
from unittest.mock import patch

from orchestrator.bookstack_live import (
    BookStackClient,
    BookStackSettings,
    _load_bookstack_env,
)


class FromEnvTest(unittest.TestCase):
    def setUp(self):
        # Force the dotenv loader to read nothing from disk so the user's real
        # ~/.dora/bookstack.env doesn't leak into the test.
        self._patch = patch(
            "orchestrator.bookstack_live._parse_env_file",
            return_value={},
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_missing_required_raises(self):
        with self.assertRaises(ValueError) as ctx:
            BookStackSettings.from_env({})
        self.assertIn("missing BookStack env var", str(ctx.exception))

    def test_full_env_loads(self):
        env = {
            "BOOKSTACK_API_URL": "https://wiki.example.com/",
            "BOOKSTACK_TOKEN_ID": "tid",
            "BOOKSTACK_TOKEN_SECRET": "tsecret",
        }
        s = BookStackSettings.from_env(env)
        self.assertEqual(s.base_url, "https://wiki.example.com")  # trailing / stripped
        self.assertEqual(s.token_id, "tid")
        self.assertEqual(s.token_secret, "tsecret")
        self.assertGreater(s.throttle, 0)

    def test_load_env_filters_empty_overrides(self):
        # Empty string in env should NOT override (parity with plane_live).
        env = {
            "BOOKSTACK_API_URL": "https://wiki.example.com",
            "BOOKSTACK_TOKEN_ID": "",
            "BOOKSTACK_TOKEN_SECRET": "secret-from-env",
        }
        merged = _load_bookstack_env(env)
        # The empty TOKEN_ID is dropped and dotenv is patched to empty → absent.
        self.assertNotIn("BOOKSTACK_TOKEN_ID", merged)
        self.assertEqual(merged["BOOKSTACK_TOKEN_SECRET"], "secret-from-env")


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode("utf-8") if self._payload is not None else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ClientUpsertTest(unittest.TestCase):
    def setUp(self):
        self.settings = BookStackSettings(
            base_url="https://wiki.example.com",
            token_id="tid",
            token_secret="tsecret",
            throttle=0.0,
        )
        self.client = BookStackClient(self.settings)

    def _request_log(self, calls):
        """Walk the call list and surface (method, path, payload)."""
        log = []
        for c in calls:
            req = c.args[0]
            method = req.get_method()
            path = req.full_url.replace(self.settings.base_url, "")
            body = req.data.decode("utf-8") if req.data else None
            log.append((method, path, json.loads(body) if body else None))
        return log

    def test_upsert_shelf_creates_when_absent(self):
        responses = [
            _FakeResponse({"data": []}),                                  # GET search → none
            _FakeResponse({"id": 7, "slug": "process-engine", "name": "PE"}),  # POST create
        ]
        with patch("orchestrator.bookstack_live.urllib.request.urlopen", side_effect=responses) as mock:
            out = self.client.upsert_shelf(name="PE", slug="process-engine")
        self.assertEqual(out["id"], 7)
        log = self._request_log(mock.call_args_list)
        self.assertEqual(log[0][0], "GET")
        self.assertIn("/api/shelves", log[0][1])
        self.assertEqual(log[1][0], "POST")
        self.assertEqual(log[1][2]["slug"], "process-engine")

    def test_upsert_shelf_updates_when_present(self):
        responses = [
            _FakeResponse({"data": [{"id": 9, "slug": "process-engine", "name": "old"}]}),
            _FakeResponse({"id": 9, "slug": "process-engine", "name": "PE new"}),
        ]
        with patch("orchestrator.bookstack_live.urllib.request.urlopen", side_effect=responses) as mock:
            out = self.client.upsert_shelf(name="PE new", slug="process-engine")
        self.assertEqual(out["id"], 9)
        log = self._request_log(mock.call_args_list)
        self.assertEqual(log[1][0], "PUT")
        self.assertEqual(log[1][1], "/api/shelves/9")

    def test_upsert_page_lookup_by_name_then_post(self):
        responses = [
            _FakeResponse({"data": []}),                              # list_pages_in_book → none
            _FakeResponse({"id": 42, "slug": "vinmm", "name": "愿景"}),  # POST
        ]
        with patch("orchestrator.bookstack_live.urllib.request.urlopen", side_effect=responses) as mock:
            out = self.client.upsert_page(book_id=5, name="愿景", markdown="### Hi\n")
        self.assertEqual(out["id"], 42)
        log = self._request_log(mock.call_args_list)
        # GET filters by book_id only (we resolve by name client-side).
        self.assertIn("filter%5Bbook_id%5D=5", log[0][1])
        # POST payload has no slug (we let BookStack auto-generate it).
        self.assertEqual(log[1][2]["markdown"], "### Hi\n")
        self.assertEqual(log[1][2]["book_id"], 5)
        self.assertEqual(log[1][2]["name"], "愿景")
        self.assertNotIn("slug", log[1][2])

    def test_upsert_page_finds_existing_by_name(self):
        responses = [
            _FakeResponse({"data": [
                {"id": 99, "name": "愿景", "slug": "vinmm"},
                {"id": 100, "name": "其他", "slug": "other"},
            ]}),
            _FakeResponse({"id": 99, "name": "愿景", "slug": "vinmm"}),
        ]
        with patch("orchestrator.bookstack_live.urllib.request.urlopen", side_effect=responses) as mock:
            out = self.client.upsert_page(book_id=5, name="愿景", markdown="### Hi\n")
        self.assertEqual(out["id"], 99)
        log = self._request_log(mock.call_args_list)
        self.assertEqual(log[1][0], "PUT")
        self.assertEqual(log[1][1], "/api/pages/99")

    def test_upsert_book_scopes_lookup_to_shelf(self):
        responses = [
            _FakeResponse({"id": 35, "name": "Process Engine", "books": [
                {"id": 12, "name": "产品"},
                {"id": 13, "name": "架构"},
            ]}),
            _FakeResponse({"id": 13, "name": "架构"}),  # PUT update existing
        ]
        with patch("orchestrator.bookstack_live.urllib.request.urlopen", side_effect=responses) as mock:
            out = self.client.upsert_book(shelf_id=35, name="架构")
        self.assertEqual(out["id"], 13)
        log = self._request_log(mock.call_args_list)
        self.assertEqual(log[0][0], "GET")
        self.assertEqual(log[0][1], "/api/shelves/35")
        self.assertEqual(log[1][0], "PUT")
        self.assertEqual(log[1][1], "/api/books/13")

    def test_upsert_book_creates_and_attaches(self):
        responses = [
            _FakeResponse({"id": 35, "name": "Process Engine", "books": []}),  # GET shelf
            _FakeResponse({"id": 99, "name": "产品"}),                          # POST book
            _FakeResponse({"id": 35, "name": "Process Engine", "books": []}),  # GET shelf (attach)
            _FakeResponse({"id": 35}),                                          # PUT shelf attach
        ]
        with patch("orchestrator.bookstack_live.urllib.request.urlopen", side_effect=responses) as mock:
            out = self.client.upsert_book(shelf_id=35, name="产品")
        self.assertEqual(out["id"], 99)
        log = self._request_log(mock.call_args_list)
        # POST book has no slug (we let BookStack auto-generate it).
        self.assertEqual(log[1][0], "POST")
        self.assertEqual(log[1][1], "/api/books")
        self.assertNotIn("slug", log[1][2])
        # Attach step: PUT /api/shelves/35 with books=[99].
        self.assertEqual(log[3][0], "PUT")
        self.assertEqual(log[3][1], "/api/shelves/35")
        self.assertEqual(log[3][2]["books"], [99])

    def test_auth_header_attached(self):
        with patch("orchestrator.bookstack_live.urllib.request.urlopen",
                   return_value=_FakeResponse({"data": []})) as mock:
            self.client._find_by_slug("/api/shelves", "x")
        req = mock.call_args.args[0]
        self.assertEqual(
            req.headers["Authorization"],
            "Token tid:tsecret",
        )


if __name__ == "__main__":
    unittest.main()
