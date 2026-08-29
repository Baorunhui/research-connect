"""deepxiv_client 单元测试（全部 mock requests，不出网）。"""

import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location("deepxiv_client_mod", ROOT / "src" / "deepxiv_client.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["deepxiv_client_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def _ok_response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.text = ""
    resp.json = MagicMock(return_value=payload)
    return resp


class TokenResolutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_missing_token_raises_with_hint(self):
        with patch.dict("os.environ", {}, clear=False):
            self.mod.os.environ.pop("DEEPXIV_TOKEN", None)
            with self.assertRaises(self.mod.DeepXivError) as cm:
                self.mod.DeepXivClient()
            self.assertIn("DEEPXIV_TOKEN", str(cm.exception))

    def test_explicit_token_wins(self):
        client = self.mod.DeepXivClient(token="abc123")
        self.assertEqual(client.token, "abc123")


class SearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _client(self):
        return self.mod.DeepXivClient(token="t")

    def test_search_params_and_normalization(self):
        mod = self.mod
        client = self._client()
        captured = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            captured.update(url=url, headers=headers, params=params)
            return _ok_response(
                {
                    "status": "success",
                    "total_count": 1,
                    "result": [
                        {
                            "arxiv_id": "2302.01881v2",
                            "score": 0.83,
                            "title": "IKEA-Manual",
                            "tldr": "tldr text",
                            "abstract": "abs text",
                            "authors": [{"name": "A", "orgs": ["X"]}, {"name": "B"}],
                            "url": "https://arxiv.org/abs/2302.01881",
                            "date": "2023-02-04T00:00:00Z",
                            "citation_count": 33,
                            "categories": ["cs.CV"],
                        }
                    ],
                }
            )

        with patch.object(client.session, "get", side_effect=fake_get):
            papers = client.search("ikea assembly", top_k=5, date_start="2023-01-01", date_end="2026-08-28")

        self.assertIn("/arxiv/", captured["url"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer t")
        self.assertEqual(captured["params"]["type"], "retrieve")
        self.assertEqual(captured["params"]["top_k"], 5)
        self.assertEqual(captured["params"]["date_search_type"], "between")
        self.assertEqual(captured["params"]["date_str"], ["2023-01-01", "2026-08-28"])
        # 归一化：版本号剥离、authors 取 name、published 截日期
        self.assertEqual(len(papers), 1)
        p = papers[0]
        self.assertEqual(p["paper_id"], "2302.01881")
        self.assertEqual(p["arxiv_version"], "v2")
        self.assertEqual(p["authors"], ["A", "B"])
        self.assertEqual(p["published"], "2023-02-04")
        self.assertEqual(p["citation_count"], 33)
        self.assertEqual(p["pdf_url"], "https://arxiv.org/pdf/2302.01881")
        self.assertEqual(p["source"], "deepxiv")

    def test_http_error_raises_deepxiv_error(self):
        mod = self.mod
        client = self._client()
        with patch.object(client.session, "get", return_value=_ok_response({}, status=401)):
            with self.assertRaises(mod.DeepXivError) as cm:
                client.search("q")
            self.assertEqual(cm.exception.status_code, 401)

    def test_no_date_window_omits_between(self):
        client = self._client()
        captured = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            captured.update(params=params)
            return _ok_response({"result": []})

        with patch.object(client.session, "get", side_effect=fake_get):
            client.search("q", top_k=3)
        self.assertNotIn("date_search_type", captured["params"])


class PaperEndpointsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_get_paper_meta(self):
        client = self.mod.DeepXivClient(token="t")
        with patch.object(
            client.session,
            "get",
            return_value=_ok_response(
                {"arxiv_id": "2409.05591", "title": "MemoRAG", "tldr": "t", "publish_at": "2024-09-09T00:00:00Z", "citations": 100}
            ),
        ):
            meta = client.get_paper_meta("2409.05591")
        self.assertEqual(meta["title"], "MemoRAG")
        self.assertEqual(meta["citation_count"], 100)
        self.assertEqual(meta["published"], "2024-09-09")

    def test_get_paper_markdown_field_variants(self):
        client = self.mod.DeepXivClient(token="t")
        for key in ("content", "markdown", "raw"):
            with patch.object(client.session, "get", return_value=_ok_response({key: f"text-{key}"})):
                self.assertEqual(client.get_paper_markdown("1"), f"text-{key}")


class AvailabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_unavailable_without_token(self):
        with patch.dict("os.environ", {}, clear=False):
            self.mod.os.environ.pop("DEEPXIV_TOKEN", None)
            ok, reason = self.mod.is_deepxiv_available()
            self.assertFalse(ok)
            self.assertIn("DEEPXIV_TOKEN", reason)

    def test_available_with_token(self):
        with patch.dict("os.environ", {"DEEPXIV_TOKEN": "x"}, clear=False):
            ok, _ = self.mod.is_deepxiv_available()
            self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
