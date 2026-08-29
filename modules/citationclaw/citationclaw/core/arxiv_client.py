"""arXiv API client for preprint metadata and PDF links.

API docs: https://info.arxiv.org/help/api/
Free, rate limit: 3 req/s.
"""
import asyncio
import re
from typing import Optional, List
from urllib.parse import quote
from xml.etree import ElementTree as ET

from citationclaw.core.http_utils import make_async_client

BASE_URL = "https://export.arxiv.org/api"
ATOM_NS = "{http://www.w3.org/2005/Atom}"

class ArxivClient:
    def __init__(self):
        self._client = make_async_client(timeout=30.0
        )
        self._rate_delay = 0.34  # ~3 req/s

    async def search_paper(self, title: str) -> Optional[dict]:
        url = self._build_search_url(title)
        await asyncio.sleep(self._rate_delay)
        resp = await self._client.get(url)
        if resp.status_code != 200:
            return None
        entries = self._parse_feed(resp.text)
        if not entries:
            return None
        return entries[0]

    async def get_paper(self, arxiv_id: str) -> Optional[dict]:
        """Fetch a paper by exact arXiv ID (id_list=) — precise, no fuzzy match.

        Accepts versioned IDs (e.g. 2401.00001v2); normalizes internally.
        """
        clean_id = re.sub(r"v\d+$", "", (arxiv_id or "").strip())
        if not clean_id:
            return None
        url = f"{BASE_URL}/query?id_list={quote(clean_id)}"
        for attempt in range(3):
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
            entries = self._parse_feed(resp.text)
            if not entries:
                return None
            # id_list can return an "id_list error" entry with no authors
            first = entries[0]
            if not first.get("authors"):
                return None
            return first
        return None

    @staticmethod
    def _titles_match(query: str, result: str, threshold: float = 0.7) -> bool:
        """Check if result title is similar enough to query (word overlap ratio)."""
        q_words = set(re.sub(r'[^\w\s]', ' ', query.lower()).split())
        r_words = set(re.sub(r'[^\w\s]', ' ', result.lower()).split())
        if not q_words:
            return False
        overlap = len(q_words & r_words)
        return overlap / len(q_words) >= threshold

    def _build_search_url(self, title: str) -> str:
        clean_title = re.sub(r'[^\w\s]', ' ', title)
        return f"{BASE_URL}/query?search_query=ti:{quote(clean_title)}&max_results=1"

    def _parse_feed(self, xml_text: str) -> List[dict]:
        root = ET.fromstring(xml_text)
        entries = []
        for entry_el in root.findall(f"{ATOM_NS}entry"):
            entry = self._xml_entry_to_dict(entry_el)
            parsed = self._parse_entry(entry)
            if parsed:
                entries.append(parsed)
        return entries

    def _xml_entry_to_dict(self, entry_el) -> dict:
        """Convert XML entry element to a dict for parsing."""
        entry = {}
        # id
        id_el = entry_el.find(f"{ATOM_NS}id")
        entry["id"] = id_el.text if id_el is not None else ""
        # title
        title_el = entry_el.find(f"{ATOM_NS}title")
        entry["title"] = title_el.text.strip().replace("\n", " ") if title_el is not None else ""
        # summary
        summary_el = entry_el.find(f"{ATOM_NS}summary")
        entry["summary"] = summary_el.text.strip() if summary_el is not None else ""
        # published
        pub_el = entry_el.find(f"{ATOM_NS}published")
        entry["published"] = pub_el.text if pub_el is not None else ""
        # authors
        authors = []
        for author_el in entry_el.findall(f"{ATOM_NS}author"):
            name_el = author_el.find(f"{ATOM_NS}name")
            if name_el is not None:
                authors.append({"name": name_el.text})
        entry["authors"] = authors
        # links
        links = []
        for link_el in entry_el.findall(f"{ATOM_NS}link"):
            links.append({
                "href": link_el.get("href", ""),
                "type": link_el.get("type", ""),
            })
        entry["links"] = links
        return entry

    def _parse_entry(self, entry: dict) -> Optional[dict]:
        arxiv_id = self._extract_arxiv_id(entry.get("id", ""))
        if not arxiv_id:
            return None
        authors = [{"name": a.get("name", ""), "source": "arxiv"} for a in entry.get("authors", [])]
        pdf_url = ""
        for link in entry.get("links", []):
            if link.get("type") == "application/pdf" or "/pdf/" in link.get("href", ""):
                pdf_url = link["href"]
                break
        year_match = re.search(r'(\d{4})', entry.get("published", ""))
        return {
            "title": entry.get("title", ""),
            "arxiv_id": arxiv_id,
            "year": int(year_match.group(1)) if year_match else None,
            "abstract": entry.get("summary", ""),
            "authors": authors,
            "pdf_url": pdf_url,
            "source": "arxiv",
        }

    def _extract_arxiv_id(self, url: str) -> str:
        match = re.search(r'(\d{4}\.\d{4,5})', url)
        return match.group(1) if match else ""

    async def close(self):
        await self._client.aclose()
