import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


VE = _load_module("venue_enrichment", ROOT / "src" / "venue_enrichment.py")


def _paper(arxiv_id="2608.01706", **overrides):
    base = {
        "id": arxiv_id,
        "source": "arxiv",
        "title": "A Test Paper",
        "authors": ["Alice", "Bob"],
    }
    base.update(overrides)
    return base


def _config(enabled=True):
    return {"arxiv_paper_setting": {"venue_enrichment": {"enabled": enabled}}}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, exc=None):
        self.status_code = status_code
        self._payload = payload
        self._exc = exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class VenueEnrichmentTest(unittest.TestCase):
    def test_disabled_returns_unchanged_without_network(self):
        paper = _paper()
        with patch.object(VE.requests, "get", side_effect=AssertionError("should not call")) as mock:
            out = VE.enrich_venue(paper, _config(enabled=False))
        self.assertIs(out, paper)
        self.assertNotIn("venue", out)
        mock.assert_not_called()

    def test_missing_arxiv_id_returns_unchanged(self):
        paper = _paper("")
        with patch.object(VE.requests, "get", side_effect=AssertionError("should not call")) as mock:
            out = VE.enrich_venue(paper, _config(enabled=True))
        self.assertIs(out, paper)
        mock.assert_not_called()

    def test_hit_from_publication_venue(self):
        payload = {
            "title": "A Test Paper",
            "venue": "CVPR 2026",
            "publicationVenue": {"name": "2026 IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)"},
            "journal": None,
            "year": 2026,
            "publicationTypes": ["Conference"],
            "externalIds": {"DOI": "10.1109/CVPR2026.01234", "ArXiv": "2608.01706"},
        }
        with patch.object(VE.requests, "get", return_value=FakeResponse(200, payload)) as mock:
            out = VE.enrich_venue(_paper(), _config(enabled=True))
        self.assertEqual(out["venue"], "2026 IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)")
        self.assertEqual(out["authoritative_url"], "https://doi.org/10.1109/CVPR2026.01234")
        self.assertEqual(out["source"], "arxiv")  # source 保持不动
        self.assertEqual(out["id"], "2608.01706")  # canonical 身份不变

    def test_journal_fallback_when_no_pub_venue(self):
        payload = {
            "title": "A Test Paper",
            "publicationVenue": None,
            "journal": {"name": "Nature Machine Intelligence"},
            "venue": "",
            "externalIds": {"DOI": "10.1038/s42256-026-00000-0"},
        }
        with patch.object(VE.requests, "get", return_value=FakeResponse(200, payload)):
            out = VE.enrich_venue(_paper(), _config(enabled=True))
        self.assertEqual(out["venue"], "Nature Machine Intelligence")
        self.assertEqual(out["authoritative_url"], "https://doi.org/10.1038/s42256-026-00000-0")

    def test_openreview_url_fallback(self):
        payload = {
            "publicationVenue": {"name": "NeurIPS 2026"},
            "externalIds": {"OpenReview": "AbC123", "ArXiv": "2608.01706"},
        }
        with patch.object(VE.requests, "get", return_value=FakeResponse(200, payload)):
            out = VE.enrich_venue(_paper(), _config(enabled=True))
        self.assertEqual(out["venue"], "NeurIPS 2026")
        self.assertEqual(out["authoritative_url"], "https://openreview.net/forum?id=AbC123")

    def test_not_found_returns_unchanged(self):
        with patch.object(VE.requests, "get", return_value=FakeResponse(404, None)):
            out = VE.enrich_venue(_paper(), _config(enabled=True))
        self.assertNotIn("venue", out)
        self.assertNotIn("authoritative_url", out)
        self.assertEqual(out["source"], "arxiv")

    def test_network_error_silently_degrades(self):
        with patch.object(VE.requests, "get", return_value=FakeResponse(200, None, exc=ConnectionError("boom"))):
            out = VE.enrich_venue(_paper(), _config(enabled=True))
        self.assertNotIn("venue", out)
        self.assertEqual(out["source"], "arxiv")

    def test_idempotent_second_call_skips_network(self):
        payload = {
            "publicationVenue": {"name": "CVPR 2026"},
            "externalIds": {"DOI": "10.1/abc"},
        }
        paper = _paper()
        with patch.object(VE.requests, "get", return_value=FakeResponse(200, payload)):
            VE.enrich_venue(paper, _config(enabled=True))
        self.assertIn("venue", paper)
        # 已补充过 venue，第二次调用不应再发网络请求
        with patch.object(VE.requests, "get", side_effect=AssertionError("should not call")) as mock:
            VE.enrich_venue(paper, _config(enabled=True))
        mock.assert_not_called()

    def test_config_none_disables(self):
        paper = _paper()
        with patch.object(VE.requests, "get", side_effect=AssertionError("should not call")) as mock:
            out = VE.enrich_venue(paper, None)
        self.assertIs(out, paper)
        mock.assert_not_called()

    def test_invalid_arxiv_id_pattern_skipped(self):
        paper = _paper("not-an-arxiv-id")
        with patch.object(VE.requests, "get", side_effect=AssertionError("should not call")) as mock:
            VE.enrich_venue(paper, _config(enabled=True))
        mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()