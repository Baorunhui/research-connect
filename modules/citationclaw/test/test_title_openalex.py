"""Title lookup must use the OpenAlex citing client, not a Scholar citation URL."""
import asyncio
import sys
import os

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from citationclaw.core.openalex_citing import OpenAlexCitingFetcher, titles_equivalent

from citationclaw.app.config_manager import AppConfig
from citationclaw.app.log_manager import LogManager
from citationclaw.app.task_executor import TaskExecutor
import citationclaw.app.task_executor as task_executor_mod
import citationclaw.core.openalex_citing as openalex_citing
import citationclaw.core.url_finder as url_finder


TITLE = (
    "AG-Pose: Instance-Adaptive and Geometric-Aware Keypoint Learning "
    "for Category-Level 6D Object Pose Estimation"
)


class _BoomFinder:
    def __init__(self, *args, **kwargs):
        raise AssertionError("title lookup must not search for a citation URL")


class _Honor:
    def stats(self):
        return {"available": True, "total": 1}

    def candidates_many(self, names):
        return {}

    def match(self, name, affiliation="", email="", *, prepared=None):
        if name == "Wei Wang" and "Los Angeles" in (affiliation or ""):
            return {
                "hits": [{
                    "name": "Wei Wang", "name_zh": "", "honor": "ACM Fellow",
                    "source": "test", "basis": "单位", "matched_value": affiliation,
                    "listed_affiliation": "University of California, Los Angeles",
                }],
                "name_only": 0,
            }
        return {"hits": [], "name_only": 0}


class _Kaggle:
    available = False


class _Fetcher:
    mode = "hit"

    def __init__(self, *args, **kwargs):
        self.resolved = []
        self.fetched = []
        self.requests = 2
        self.retries = 0
        self.route = "direct"
        self.fetched_records = 0

    async def resolve(self, title, doi="", arxiv_id="", year=None):
        self.resolved.append(title)
        if self.mode == "miss":
            return {"works": [], "cited_by_count": 0, "resolved_by": ""}
        if self.mode == "zero":
            return {
                "works": [{"id": "W100", "title": title, "year": 2024,
                           "cited_by_count": 0, "authors": []}],
                "cited_by_count": 0, "resolved_by": "title",
            }
        if "preprint" in title.lower():
            return {
                "works": [{"id": "W200", "title": title, "year": 2024,
                           "cited_by_count": 1, "authors": []}],
                "cited_by_count": 1, "resolved_by": "title",
            }
        authors = [] if self.mode == "no-authors" else ["Ada Lovelace"]
        return {
            "works": [{"id": "W100", "title": title, "year": 2024,
                       "cited_by_count": 2, "authors": authors}],
            "cited_by_count": 2, "resolved_by": "title",
        }

    async def fetch_citing(self, work_ids, expected=0, label=""):
        self.fetched.append(list(work_ids))
        records = [
            {"id": "C1", "title": "A citing paper", "year": 2025, "doi": "", "arxiv_id": "",
             "authors": [{"name": "Wei Wang",
                          "affiliation": "University of California, Los Angeles", "email": ""}]},
            {"id": "C2", "title": "Author follow-up", "year": 2025, "doi": "", "arxiv_id": "",
             "authors": [{"name": "Ada Lovelace", "affiliation": "X", "email": ""}]},
        ]
        if self.mode == "zero":
            records = []
        self.fetched_records += len(records)
        return {"records": records, "fetched": len(records), "expected": expected,
                "complete": True, "cached_parts": 0}

    async def close(self):
        return None


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _executor(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(task_executor_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(url_finder, "PaperURLFinder", _BoomFinder)
    monkeypatch.setattr("citationclaw.core.honor_list.get_honor_list", lambda: _Honor())
    monkeypatch.setattr("citationclaw.core.kaggle_arxiv_meta.get_kaggle_meta", lambda: _Kaggle())
    _Fetcher.mode = mode
    created = []

    class _Tracking(_Fetcher):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(openalex_citing, "OpenAlexCitingFetcher", _Tracking)
    logs = LogManager()
    executor = TaskExecutor(logs, config_manager=None)
    return executor, logs, created


def _messages(logs):
    return "\n".join(item["message"] for item in logs.logs)


def test_title_task_uses_openalex_and_does_not_skip_missing_citation_url(tmp_path, monkeypatch):
    executor, logs, created = _executor(tmp_path, monkeypatch, "hit")
    result = _run(executor.execute_for_titles_openalex(
        [{"title": TITLE, "aliases": ["AG-Pose preprint"]}],
        AppConfig(),
        "agpose",
    ))
    text = _messages(logs)
    assert created and created[0].resolved[0] == TITLE
    assert "AG-Pose preprint" in created[0].resolved
    assert created[0].fetched and "W100" in created[0].fetched[0] and "W200" in created[0].fetched[0]
    assert "未找到引用链接" not in text
    assert "Phase 1 未爬取" not in text
    assert "OpenAlex 找到了这篇论文" in text
    assert result["mode"] == "openalex_citing_honor_match"
    assert result["coverage"]["citing_records_fetched"] == 2
    assert result["coverage"]["self_citations_skipped"] == 1
    assert result["coverage"]["unique_citing_works"] == 1
    assert result["coverage"]["honor_citers"] == 1


def test_title_task_without_author_names_keeps_self_cites(tmp_path, monkeypatch):
    executor, logs, created = _executor(tmp_path, monkeypatch, "no-authors")
    result = _run(executor.execute_for_titles_openalex(
        [{"title": TITLE, "aliases": []}],
        AppConfig(),
        "agpose",
    ))
    assert created[0].fetched
    assert "未找到引用链接" not in _messages(logs)
    assert result["coverage"]["self_citations_skipped"] == 0
    assert result["coverage"]["unique_citing_works"] == 2


def test_missing_openalex_work_does_not_mention_citation_url(tmp_path, monkeypatch):
    executor, logs, created = _executor(tmp_path, monkeypatch, "miss")
    result = _run(executor.execute_for_titles_openalex(
        [{"title": TITLE, "aliases": []}],
        AppConfig(),
        "agpose",
    ))
    text = _messages(logs)
    assert result is None
    assert created and created[0].fetched == []
    assert "OpenAlex 没有这篇论文" in text
    assert "未找到引用链接" not in text
    assert "Phase 1" not in text
    assert executor._terminal_status == "no_results"
    assert "OpenAlex 没有这篇论文" in executor._terminal_message


def test_zero_citations_are_reported(tmp_path, monkeypatch):
    executor, logs, created = _executor(tmp_path, monkeypatch, "zero")
    result = _run(executor.execute_for_titles_openalex(
        [{"title": TITLE, "aliases": []}],
        AppConfig(),
        "agpose",
    ))
    text = _messages(logs)
    assert "被引为 0" in text
    assert "未找到引用链接" not in text
    assert result["coverage"]["openalex_cited_by_total"] == 0
    assert result["coverage"]["citing_records_fetched"] == 0
    assert created[0].fetched


def test_resolve_accepts_title_when_openalex_drops_a_short_prefix(tmp_path):
    query = (
        "AG-Pose: Instance-Adaptive and Geometric-Aware Keypoint Learning "
        "for Category-Level 6D Object Pose Estimation"
    )
    kept = "Instance-Adaptive and Geometric-Aware Keypoint Learning for Category-Level 6D Object Pose Estimation"
    assert titles_equivalent(query, kept)
    assert not titles_equivalent(query, "KeyPose: Category-Level 6D Object Pose Estimation with Self-Adaptive Keypoints")

    def handler(request: httpx.Request):
        q = request.url.params
        if "title.search" in q.get("filter", ""):
            return httpx.Response(200, json={"results": []})
        if q.get("search"):
            assert "pdf" not in q.get("select", "")
            return httpx.Response(200, json={"results": [
                {"id": "https://openalex.org/W4402716422", "display_name": kept,
                 "publication_year": 2024, "cited_by_count": 36, "doi": None, "ids": {}},
                {"id": "https://openalex.org/W0", "display_name": kept,
                 "publication_year": 2024, "cited_by_count": 0, "doi": None, "ids": {}},
                {"id": "https://openalex.org/W4409366236",
                 "display_name": "KeyPose: Category-Level 6D Object Pose Estimation with Self-Adaptive Keypoints",
                 "publication_year": 2024, "cited_by_count": 1, "doi": None, "ids": {}},
            ]})
        return httpx.Response(404)

    async def run():
        f = OpenAlexCitingFetcher(
            tmp_path, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            route="direct", rate=1000, log=lambda _m: None,
        )
        got = await f.resolve(query)
        await f.close()
        return got

    got = _run(run())
    assert [w["id"] for w in got["works"]] == ["W4402716422"]
    assert got["cited_by_count"] == 36 and got["resolved_by"] == "title"
