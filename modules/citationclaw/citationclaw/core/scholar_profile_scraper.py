import asyncio
import requests
import time
from urllib.parse import urlparse, parse_qs
from typing import Optional, List, Callable
from bs4 import BeautifulSoup


class ScholarProfileScraper:
    def __init__(self, api_keys: list, log_callback: Callable,
                 retry_max_attempts: int = 3, retry_intervals: str = "5,10,20",
                 s2_api_key: str = ""):
        self.api_keys = api_keys
        self.log_callback = log_callback
        self.retry_max_attempts = max(1, retry_max_attempts)
        self.retry_intervals = self._parse_intervals(retry_intervals)
        self._key_idx = 0
        self._s2_api_key = s2_api_key

    @staticmethod
    def _parse_intervals(intervals_str: str) -> list:
        try:
            parts = [float(s.strip()) for s in intervals_str.split(',') if s.strip()]
            return parts if parts else [5.0]
        except (ValueError, AttributeError):
            return [5.0]

    def _get_retry_wait(self, attempt: int) -> float:
        if attempt < len(self.retry_intervals):
            return self.retry_intervals[attempt]
        return self.retry_intervals[-1]

    @staticmethod
    def extract_user_id(profile_url: str) -> str:
        parsed = urlparse(profile_url)
        qs = parse_qs(parsed.query)
        if 'user' not in qs or not qs['user']:
            raise ValueError(f"无法从 URL 中解析 user 参数: {profile_url}")
        return qs['user'][0]

    async def _scraper_fetch(self, url: str) -> Optional[str]:
        """Fetch a page via ScraperAPI, non-blocking via asyncio.to_thread."""
        if not self.api_keys:
            return None
        for attempt in range(self.retry_max_attempts):
            key_idx = self._key_idx % len(self.api_keys)
            api_key = self.api_keys[key_idx]
            # Always advance key index, even on failure
            self._key_idx = (self._key_idx + 1) % len(self.api_keys)
            try:
                payload = {'api_key': api_key, 'url': url}
                r = await asyncio.to_thread(
                    requests.get, 'https://api.scraperapi.com/',
                    params=payload, timeout=90
                )
                if r.status_code == 200:
                    return r.text
                else:
                    self.log_callback(f"[ScholarProfile] 请求失败(尝试 {attempt+1}/{self.retry_max_attempts}), 状态码: {r.status_code}")
                    # 401/403 = key invalid/quota exhausted — no point retrying
                    if r.status_code in (401, 403):
                        self.log_callback(f"[ScholarProfile] API Key 无效或额度耗尽，停止重试")
                        return None
                    if attempt < self.retry_max_attempts - 1:
                        wait = self._get_retry_wait(attempt)
                        self.log_callback(f"[ScholarProfile] 等待 {wait:.0f}s 后重试...")
                        await asyncio.sleep(wait)
            except Exception as e:
                self.log_callback(f"[ScholarProfile] 请求错误(尝试 {attempt+1}/{self.retry_max_attempts}): {e}")
                if attempt < self.retry_max_attempts - 1:
                    wait = self._get_retry_wait(attempt)
                    await asyncio.sleep(wait)
        return None

    # Synchronous fallback for callers without an event loop
    def _scraper_fetch_sync(self, url: str) -> Optional[str]:
        for attempt in range(self.retry_max_attempts):
            key_idx = self._key_idx % len(self.api_keys)
            api_key = self.api_keys[key_idx]
            # Always advance key index, even on failure
            self._key_idx = (self._key_idx + 1) % len(self.api_keys)
            try:
                payload = {'api_key': api_key, 'url': url}
                r = requests.get('https://api.scraperapi.com/', params=payload, timeout=90)
                if r.status_code == 200:
                    return r.text
                else:
                    self.log_callback(f"[ScholarProfile] 请求失败(尝试 {attempt+1}/{self.retry_max_attempts}), 状态码: {r.status_code}")
                    if r.status_code in (401, 403):
                        self.log_callback(f"[ScholarProfile] API Key 无效或额度耗尽，停止重试")
                        return None
                    if attempt < self.retry_max_attempts - 1:
                        wait = self._get_retry_wait(attempt)
                        self.log_callback(f"[ScholarProfile] 等待 {wait:.0f}s 后重试...")
                        time.sleep(wait)
            except Exception as e:
                self.log_callback(f"[ScholarProfile] 请求错误(尝试 {attempt+1}/{self.retry_max_attempts}): {e}")
                if attempt < self.retry_max_attempts - 1:
                    wait = self._get_retry_wait(attempt)
                    time.sleep(wait)
        return None

    @staticmethod
    def parse_paper_rows(html: str) -> List[dict]:
        """Parse a Google Scholar profile HTML page into a paper list.

        Shared by the live ScraperAPI path and the local HTML-upload path.
        Returns [{title, year, citations}] (caller sorts by citations desc).
        """
        soup = BeautifulSoup(html, 'html.parser')
        papers = []
        for row in soup.select('tr.gsc_a_tr'):
            title_el = row.select_one('a.gsc_a_at')
            title = title_el.get_text(strip=True) if title_el else ''
            if not title:
                continue

            cite_el = row.select_one('a.gsc_a_ac')
            cite_text = cite_el.get_text(strip=True) if cite_el else ''
            try:
                citations = int(cite_text.replace(',', ''))
            except (ValueError, AttributeError):
                citations = 0

            year_el = row.select_one('span.gsc_a_h')
            year_text = year_el.get_text(strip=True) if year_el else ''
            try:
                year = int(year_text)
            except (ValueError, AttributeError):
                year = None

            papers.append({'title': title, 'year': year, 'citations': citations})
        return papers

    @staticmethod
    def parse_html(html: str) -> List[dict]:
        """Parse a locally-saved Google Scholar profile HTML page.

        Returns [{title, year, citations}] sorted by citations desc — the same
        shape as ``fetch_all_papers``. Used by the HTML-upload entry point so
        users can supply a profile page they saved themselves (no ScraperAPI
        needed).
        """
        papers = ScholarProfileScraper.parse_paper_rows(html)
        papers.sort(key=lambda p: p['citations'], reverse=True)
        return papers

    async def fetch_all_papers(self, profile_url: str) -> List[dict]:
        user_id = self.extract_user_id(profile_url)
        base = "https://scholar.google.com/citations"
        all_papers = []
        cstart = 0

        if self.api_keys:
            self.log_callback(f"[ScholarProfile] 开始爬取 user={user_id} 的论文列表")
            while True:
                url = f"{base}?user={user_id}&sortby=citations&cstart={cstart}&pagesize=100"
                self.log_callback(f"[ScholarProfile] 获取第 {cstart//100 + 1} 页 (cstart={cstart})")
                html = await self._scraper_fetch(url)
                if not html:
                    self.log_callback(f"[ScholarProfile] 获取页面失败，停止分页")
                    break
                batch = self.parse_paper_rows(html)
                self.log_callback(f"[ScholarProfile] 本页解析到 {len(batch)} 篇论文")
                all_papers.extend(batch)
                if len(batch) < 100:
                    break
                cstart += 100
        else:
            self.log_callback("[ScholarProfile] 未配置 ScraperAPI，直接使用 Semantic Scholar")

        all_papers.sort(key=lambda p: p['citations'], reverse=True)
        self.log_callback(f"[ScholarProfile] 共爬取到 {len(all_papers)} 篇论文")

        # ScraperAPI 不存在或失败时走 Semantic Scholar。S2 的公开 API 无需
        # Key 也可使用（1 req/s）；Key 仅用于提高限额。
        if not all_papers:
            all_papers = await self._s2_fallback(profile_url)
            if all_papers:
                self.log_callback(f"[ScholarProfile] S2 兜底获取到 {len(all_papers)} 篇论文")

        return all_papers

    async def _s2_fallback(self, profile_url: str) -> List[dict]:
        """Fallback to Semantic Scholar when ScraperAPI fails.

        Since S2 cannot use a Google Scholar user_id, we look for the author
        name embedded in the GS profile page. If ScraperAPI is down we can't
        fetch the page, so we ask the caller to pass the author name via the
        profile_url as a query parameter: ...?user=XXX&name=Author+Name
        """
        from citationclaw.core.s2_client import S2Client

        parsed = urlparse(profile_url)
        qs = parse_qs(parsed.query)
        name = (qs.get('name') or [None])[0]
        if not name:
            self.log_callback("[ScholarProfile] S2 兜底需要作者姓名，请在 URL 中加 &name=作者姓名")
            return []

        self.log_callback(f"[ScholarProfile] S2 兜底：按作者名搜索 '{name}'")
        try:
            s2 = S2Client(api_key=self._s2_api_key or None)
            try:
                papers = await s2.get_author_papers_by_name(name, max_papers=2000)
            finally:
                await s2.close()
        except Exception as e:
            self.log_callback(f"[ScholarProfile] S2 兜底失败: {e}")
            return []

        if not papers:
            self.log_callback(f"[ScholarProfile] S2 未找到作者 '{name}' 的论文")
        return papers
