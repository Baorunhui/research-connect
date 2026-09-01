"""Semantic Scholar API client for academic metadata.

API docs: https://api.semanticscholar.org/
Free tier: 1 req/s without key, higher with API key.
Unique fields: h_index, influentialCitationCount.
"""
import asyncio
from typing import Optional, List
from urllib.parse import quote

from citationclaw.core.http_utils import make_async_client

BASE_URL = "https://api.semanticscholar.org/graph/v1"

class S2Client:
    def __init__(self, api_key: Optional[str] = None):
        self._client = make_async_client(timeout=30.0)
        self._has_key = bool(api_key)
        if api_key:
            self._client.headers["x-api-key"] = api_key
            # New keys start at roughly 1 request/second unless Semantic
            # Scholar explicitly grants a higher quota.  Staying within that
            # default is slower than optimistic concurrency, but avoids turning
            # every throttled response into an apparent "paper not found".
            self._rate_delay = 1.05
            self._sem = asyncio.Semaphore(1)
        else:
            self._rate_delay = 1.1  # Free tier: 1 req/s
            self._sem = asyncio.Semaphore(1)

    async def search_paper(self, title: str) -> Optional[dict]:
        url = self._build_search_url(title)
        for attempt in range(3):
            async with self._sem:
                await asyncio.sleep(self._rate_delay)
                try:
                    resp = await self._client.get(url)
                except Exception:
                    return None
            if resp.status_code == 429:
                # Rate limited — back off and retry
                await asyncio.sleep(3 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return None
            data = resp.json()
            results = data.get("data", [])
            if not results:
                return None
            return self._parse_paper(results[0])
        return None  # All retries exhausted

    async def get_paper_by_arxiv_id(self, arxiv_id: str) -> Optional[dict]:
        """Resolve a paper precisely by arXiv ID via /paper/ArXiv:{id}.

        Far more reliable than fuzzy title search when the arxiv_id is known
        (e.g. from the local arXiv title→author DB). Returns the same parsed
        shape as search_paper.
        """
        import re as _re
        clean_id = _re.sub(r"v\d+$", "", (arxiv_id or "").strip())
        if not clean_id:
            return None
        fields = ("title,year,authors,citationCount,influentialCitationCount,"
                  "externalIds,isOpenAccess,openAccessPdf,venue,publicationVenue,journal")
        url = f"{BASE_URL}/paper/ArXiv:{quote(clean_id)}?fields={fields}"
        for attempt in range(3):
            async with self._sem:
                await asyncio.sleep(self._rate_delay)
                try:
                    resp = await self._client.get(url)
                except Exception:
                    return None
            if resp.status_code == 429:
                await asyncio.sleep(3 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return None
            try:
                data = resp.json()
            except Exception:
                return None
            if not data or data.get("paperId") is None:
                return None
            return self._parse_paper(data)
        return None

    @staticmethod
    def _titles_match(query: str, result: str, threshold: float = 0.45) -> bool:
        """Check title similarity by word overlap.

        Threshold lowered from 0.7 to 0.45 (PaperRadar uses no validation at all).
        Handles: Chinese titles, abbreviations, minor variations.
        """
        import re as _re
        _stop = {'a', 'an', 'the', 'of', 'in', 'on', 'for', 'and', 'or', 'to',
                 'with', 'by', 'is', 'are', 'from', 'at', 'as', 'its', 'via', 'using'}

        # If query contains Chinese chars, S2 may return English translation
        # → accept if any significant word matches (very lenient for cross-language)
        has_cjk = any('\u4e00' <= c <= '\u9fff' for c in query)
        if has_cjk:
            # Extract any English/pinyin words from both
            q_eng = set(_re.findall(r'[a-zA-Z]{3,}', query.lower()))
            r_eng = set(_re.findall(r'[a-zA-Z]{3,}', result.lower()))
            if q_eng and r_eng:
                return len(q_eng & r_eng) >= 1  # Any shared English word
            return True  # Can't compare → accept (let user verify)

        q_words = set(_re.sub(r'[^\w\s]', ' ', query.lower()).split()) - _stop
        r_words = set(_re.sub(r'[^\w\s]', ' ', result.lower()).split()) - _stop
        if not q_words:
            return True
        if len(q_words) <= 3:
            return len(q_words & r_words) >= 1
        return len(q_words & r_words) / len(q_words) >= threshold

    async def search_by_url(self, paper_url: str) -> Optional[dict]:
        """Search S2 by external URL (paper_link from GS).

        S2 supports: /paper/URL:{encoded_url}?fields=...
        This works for IEEE, arXiv, ACM, Springer etc. URLs.
        """
        if not paper_url:
            return None
        fields = "title,year,authors,citationCount,influentialCitationCount,externalIds,openAccessPdf,venue,publicationVenue,journal"
        encoded = quote(paper_url, safe='')
        url = f"{BASE_URL}/paper/URL:{encoded}?fields={fields}"
        async with self._sem:
            await asyncio.sleep(self._rate_delay)
            try:
                resp = await self._client.get(url)
            except Exception:
                return None
        if resp.status_code != 200:
            return None
        try:
            return self._parse_paper(resp.json())
        except Exception:
            return None

    async def search_author(self, name: str, limit: int = 5) -> Optional[List[dict]]:
        """Search S2 authors by name. Returns list of author dicts."""
        fields = "name,hIndex,citationCount,affiliations"
        url = f"{BASE_URL}/author/search?query={quote(name)}&limit={limit}&fields={fields}"
        for attempt in range(4):
            async with self._sem:
                await asyncio.sleep(self._rate_delay)
                try:
                    resp = await self._client.get(url)
                except Exception:
                    return None
            if resp.status_code == 429:
                await asyncio.sleep(min(5 * (attempt + 1), 20))
                continue
            if resp.status_code != 200:
                return None
            data = resp.json()
            results = data.get("data", [])
            if not results:
                return None
            return [self._parse_author(a) for a in results]
        return None

    async def get_author(self, author_id: str) -> Optional[dict]:
        url = f"{BASE_URL}/author/{author_id}?fields=name,hIndex,citationCount,affiliations"
        async with self._sem:
            await asyncio.sleep(self._rate_delay)
            resp = await self._client.get(url)
        if resp.status_code != 200:
            return None
        return self._parse_author(resp.json())

    async def get_paper_citation_count(self, paper_id: str) -> int:
        """Fetch only the citation count for a paper (lightweight, no paging)."""
        url = f"{BASE_URL}/paper/{paper_id}?fields=citationCount"
        for attempt in range(3):
            async with self._sem:
                await asyncio.sleep(self._rate_delay)
                try:
                    resp = await self._client.get(url)
                except Exception:
                    return 0
            if resp.status_code == 429:
                await asyncio.sleep(3 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return 0
            try:
                return int(resp.json().get("citationCount", 0) or 0)
            except Exception:
                return 0
        return 0

    async def get_author_papers(self, author_id: str, max_papers: int = 2000) -> List[dict]:
        """Fetch an author's papers from S2, paginated.

        Returns list of {title, year, citations} sorted by citations desc.
        """
        papers: List[dict] = []
        offset = 0
        limit = 100
        # Keep paperId and target authors here.  The scholar-profile pipeline
        # can then walk citations directly instead of throwing this identity
        # away and performing a second fuzzy title search for every paper.
        fields = "paperId,title,year,citationCount,authors,externalIds"
        while offset < max_papers:
            url = (f"{BASE_URL}/author/{author_id}/papers"
                   f"?fields={fields}&limit={limit}&offset={offset}")
            resp = None
            for attempt in range(3):
                async with self._sem:
                    await asyncio.sleep(self._rate_delay)
                    try:
                        resp = await self._client.get(url)
                    except Exception:
                        return papers
                if resp.status_code == 429:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                break
            if resp is None or resp.status_code != 200:
                return papers
            try:
                data = resp.json()
            except Exception:
                return papers
            batch = data.get("data", []) or []
            for p in batch:
                try:
                    y = p.get("year")
                    year = int(y) if y else None
                except (ValueError, TypeError):
                    year = None
                parsed = self._parse_paper(p)
                papers.append({
                    "title": p.get("title", "") or "",
                    "year": year,
                    "citations": p.get("citationCount", 0) or 0,
                    "s2_id": parsed.get("s2_id", ""),
                    "authors": parsed.get("authors", []),
                    "arxiv_id": parsed.get("arxiv_id", ""),
                })
            if len(batch) < limit:
                break
            offset += limit
        papers.sort(key=lambda p: p["citations"], reverse=True)
        return papers

    async def get_author_papers_by_name(self, name: str, max_papers: int = 2000) -> Optional[List[dict]]:
        """Search an author by name on S2 and return their papers.

        S2 often splits one real author into multiple author IDs. To mitigate
        this, we merge papers from ALL candidates whose name matches exactly
        (case-insensitive), sorted by citation_count so the most prolific
        candidate is fetched first. Returns None if no author found.
        """
        authors = await self.search_author(name, limit=20)
        if not authors:
            return None
        target = name.strip().lower()
        # Exact name matches first (case-insensitive), then near matches
        exact = [a for a in authors if a.get("name", "").strip().lower() == target]
        candidates = exact or authors
        # Sort by citation_count desc so we fetch the most prolific first
        candidates.sort(key=lambda a: (a.get("citation_count", 0) or 0,
                                        a.get("h_index", 0) or 0), reverse=True)
        # Cap at 5 candidates to avoid excessive API calls under rate limits
        candidates = candidates[:5]

        seen_ids: set = set()
        merged: List[dict] = []
        for a in candidates:
            if len(merged) >= max_papers:
                break
            aid = a.get("s2_id")
            if not aid:
                continue
            papers = await self.get_author_papers(aid, max_papers=max_papers - len(merged))
            for p in papers:
                key = p.get("title", "").strip().lower()
                if key and key not in seen_ids:
                    seen_ids.add(key)
                    merged.append(p)
        merged.sort(key=lambda p: p.get("citations", 0), reverse=True)
        return merged

    async def get_paper_citations(self, paper_id: str, max_papers: int = 5000) -> List[dict]:
        """Fetch citing papers of a paper from S2, paginated.

        Returns list of paper dicts (parsed via _parse_paper) for each citing
        paper, sorted by citationCount desc.
        """
        papers: List[dict] = []
        offset = 0
        limit = 100
        fields = ("title,year,authors,citationCount,influentialCitationCount,"
                  "externalIds,openAccessPdf,venue,publicationVenue,journal")
        while offset < max_papers:
            url = (f"{BASE_URL}/paper/{paper_id}/citations"
                   f"?fields={fields}&limit={limit}&offset={offset}")
            resp = None
            for attempt in range(3):
                async with self._sem:
                    await asyncio.sleep(self._rate_delay)
                    try:
                        resp = await self._client.get(url)
                    except Exception:
                        return papers
                if resp.status_code == 429:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                break
            if resp is None or resp.status_code != 200:
                return papers
            try:
                data = resp.json()
            except Exception:
                return papers
            batch = data.get("data", []) or []
            for entry in batch:
                cp = entry.get("citingPaper", {}) or {}
                if not cp.get("paperId"):
                    continue
                parsed = self._parse_paper(cp)
                if parsed.get("title"):
                    papers.append(parsed)
            if len(batch) < limit:
                break
            offset += limit
        papers.sort(key=lambda p: p.get("cited_by_count", 0), reverse=True)
        return papers

    def _build_search_url(self, title: str) -> str:
        # NOTE: Do NOT include authors.affiliations — it causes S2 to return empty author names!
        # Affiliations are supplemented later from OpenAlex or PDF extraction.
        fields = "title,year,authors,citationCount,influentialCitationCount,externalIds,isOpenAccess,openAccessPdf,venue,publicationVenue,journal"
        return f"{BASE_URL}/paper/search?query={quote(title)}&limit=1&fields={fields}"

    def _parse_paper(self, paper: dict) -> dict:
        authors = []
        for author in paper.get("authors", []):
            # Don't request authors.affiliations — it breaks author name!
            # Affiliations supplemented from OpenAlex or PDF later.
            authors.append({
                "name": author.get("name", ""),
                "s2_id": author.get("authorId", ""),
                "affiliation": "",
            })
        ext_ids = paper.get("externalIds", {}) or {}
        pdf_info = paper.get("openAccessPdf") or {}
        venue = (paper.get("venue", "")
                 or (paper.get("publicationVenue") or {}).get("name", "")
                 or (paper.get("journal") or {}).get("name", ""))

        # PDF URL fallback chain (PaperRadar-style: construct at metadata stage)
        arxiv_id = ext_ids.get("ArXiv", "")
        doi = ext_ids.get("DOI", "")
        pdf_url = pdf_info.get("url", "")
        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        if not pdf_url and doi:
            pdf_url = f"https://doi.org/{doi}"

        return {
            "title": paper.get("title", ""),
            "year": paper.get("year"),
            "doi": doi,
            "arxiv_id": arxiv_id,
            "cited_by_count": paper.get("citationCount", 0),
            "influential_citation_count": paper.get("influentialCitationCount", 0),
            "s2_id": paper.get("paperId", ""),
            "authors": authors,
            "pdf_url": pdf_url,
            "venue": venue,
            "_external_ids": ext_ids,
            "source": "s2",
        }

    def _parse_author(self, author: dict) -> dict:
        affiliations = author.get("affiliations", [])
        return {
            "name": author.get("name", ""),
            "s2_id": author.get("authorId", ""),
            "h_index": author.get("hIndex", 0),
            "citation_count": author.get("citationCount", 0),
            "affiliation": affiliations[0] if affiliations else "",
            "source": "s2",
        }

    async def close(self):
        await self._client.aclose()
