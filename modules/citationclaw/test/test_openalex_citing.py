import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import httpx

from citationclaw.core.openalex_citing import (
    CITING_SELECT, OpenAlexCitingFetcher, compact_work, verify_with_kaggle,
)

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


TARGET = {"id": "https://openalex.org/W100", "display_name": "Attention Is All You Need",
          "publication_year": 2017, "doi": None, "ids": {}, "cited_by_count": 450}


def _citing(i):
    return {
        "id": f"https://openalex.org/W{i}", "display_name": f"Citing {i}", "publication_year": 2020,
        "doi": "https://doi.org/10.48550/arxiv.2001.0000" + str(i % 10) if i % 2 else None,
        "ids": {"openalex": f"https://openalex.org/W{i}"},
        "authorships": [{
            "author": {"id": "A1", "display_name": "Wei Wang"},
            "institutions": [{"display_name": "University of California, Los Angeles", "country_code": "US"}],
            "raw_affiliation_strings": ["UCLA, wwang@cs.ucla.edu"],
        }],
        "primary_location": {"pdf_url": "https://example.org/x.pdf"},
    }


def _transport(seen, rate_limit_once=True):
    pages = {"*": (0, "c1"), "c1": (200, "c2"), "c2": (400, None)}
    state = {"limited": not rate_limit_once}

    def handler(request: httpx.Request):
        seen.append(request)
        q = request.url.params
        if "title.search" in q.get("filter", ""):
            return httpx.Response(200, json={"results": [TARGET]})
        if q.get("filter", "").startswith("cites:"):
            if not state["limited"]:
                state["limited"] = True
                return httpx.Response(429, headers={"Retry-After": "0"})
            start, nxt = pages[q["cursor"]]
            n = 200 if nxt else 50
            return httpx.Response(200, json={"meta": {"count": 450, "next_cursor": nxt},
                                             "results": [_citing(start + j) for j in range(n)]})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _fetcher(tmp_path, seen, **kw):
    client = httpx.AsyncClient(transport=_transport(seen, **kw))
    return OpenAlexCitingFetcher(tmp_path, email="t@example.org", client=client, route="direct",
                                 rate=1000, log=lambda _m: None)


def test_compact_work_keeps_light_metadata_only():
    rec = compact_work(_citing(1))
    assert rec["id"] == "W1" and rec["arxiv_id"] == "2001.00001"
    assert rec["authors"] == [{"name": "Wei Wang", "affiliation": "University of California, Los Angeles",
                               "email": "wwang@cs.ucla.edu"}]
    assert "pdf" not in str(rec).lower()


def test_cursor_paging_fetches_everything_and_never_asks_for_pdf(tmp_path):
    seen = []

    async def run():
        f = _fetcher(tmp_path, seen)
        res = await f.resolve("Attention is all you need", year=2017)
        got = await f.fetch_citing([w["id"] for w in res["works"]], res["cited_by_count"])
        await f.close()
        return res, got, f

    res, got, f = _run(run())
    assert [w["id"] for w in res["works"]] == ["W100"]
    assert got["fetched"] == 450 and got["complete"]
    cites = [r for r in seen if r.url.params.get("filter", "").startswith("cites:")]
    assert [r.url.params["cursor"] for r in cites] == ["*", "*", "c1", "c2"]  # 429 retried
    for r in seen:
        assert r.url.host == "api.openalex.org" and r.url.path.startswith("/works")
        assert "pdf" not in str(r.url).lower()
        assert r.url.params.get("mailto") == "t@example.org"
    assert all(r.url.params["select"] == CITING_SELECT and r.url.params["per-page"] == "200" for r in cites)
    assert f.retries == 1


def test_citing_cache_is_reused(tmp_path):
    seen = []

    async def run():
        f = _fetcher(tmp_path, seen, rate_limit_once=False)
        await f.fetch_citing(["W100"], 450)
        n = len(seen)
        again = await f.fetch_citing(["W100"], 450)
        res2 = await f.resolve("Attention Is All You Need")
        res3 = await f.resolve("Attention Is All You Need")
        await f.close()
        return n, again, res2, res3

    n, again, res2, res3 = _run(run())
    assert n == 3 and again["fetched"] == 450 and again["cached_parts"] == 1
    assert res3["cached"] and not res2["cached"]


def test_refuses_pdf_or_non_works_urls(tmp_path):
    f = _fetcher(tmp_path, [])
    for url, params in [("https://example.org/paper.pdf", {}),
                        ("https://api.openalex.org/works", {"select": "id,primary_location"}),
                        ("https://api.openalex.org/works/W1/pdf", {})]:
        try:
            _run(f._get(url, params))
        except ValueError:
            continue
        raise AssertionError(f"should refuse {url}")


class _FakeKaggle:
    def lookup(self, title="", arxiv_id="", doi=""):
        if arxiv_id == "2001.00001":
            return {"arxiv_id": "2001.00001", "title": "Citing 1", "year": 2020, "authors": ["Wei Wang"]}
        return None


def test_verify_with_kaggle_fills_bibliography_without_touching_affiliations():
    recs = [compact_work(_citing(1)), compact_work(_citing(2)),
            {"id": "W9", "title": "", "year": None, "doi": "", "arxiv_id": "2001.00001", "authors": []}]
    assert verify_with_kaggle(recs, _FakeKaggle()) == 2
    assert recs[0]["verified_by"] == "kaggle_arxiv"
    assert recs[0]["authors"][0]["affiliation"].startswith("University of California")
    assert "verified_by" not in recs[1]
    assert recs[2]["title"] == "Citing 1" and recs[2]["authors"][0]["name"] == "Wei Wang"


def test_large_targets_split_by_year_and_quarter(tmp_path):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        q = request.url.params
        if q.get("group_by") == "publication_year":
            return httpx.Response(200, json={"group_by": [{"key": "2020", "count": 30},
                                                          {"key": "2021", "count": 5}]})
        filt = q["filter"]
        n = 5 if "publication_year:2021" in filt else (10 if "from_publication_date:2020-01" in filt else 0)
        base = 1000 if "2021" in filt else 0
        return httpx.Response(200, json={"meta": {"next_cursor": None},
                                         "results": [_citing(base + j) for j in range(n)]})

    async def run():
        f = OpenAlexCitingFetcher(tmp_path, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                  route="direct", rate=1000, split_threshold=10, log=lambda _m: None)
        got = await f.fetch_citing(["W100"], 35)
        n = len(seen)
        again = await f.fetch_citing(["W100"], 35)
        await f.close()
        assert len(seen) == n and again["cached_parts"] == 5 and again["fetched"] == 15
        return got

    got = _run(run())
    assert got["partitions"] == 5 and got["complete"] and got["fetched"] == 15
    filters = sorted(r.url.params["filter"] for r in seen if "group_by" not in r.url.params)
    assert "cites:W100,publication_year:2021" in filters
    assert sum("from_publication_date:2020-" in f for f in filters) == 4
