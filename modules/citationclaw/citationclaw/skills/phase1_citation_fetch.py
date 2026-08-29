from __future__ import annotations

from pathlib import Path

from citationclaw.skills.base import SkillContext, SkillResult
from citationclaw.core.phase1_cache import Phase1Cache
from citationclaw.core.scraper import GoogleScholarScraper


class CitationFetchSkill:
    name = "phase1_citation_fetch"

    async def run(self, ctx: SkillContext, **kwargs) -> SkillResult:
        try:
            return await self._run_inner(ctx, **kwargs)
        except Exception as e:
            ctx.log(f"[Phase1] fatal error: {e}")
            raise

    async def _run_inner(self, ctx: SkillContext, **kwargs) -> SkillResult:
        config = ctx.config
        url: str = kwargs["url"]
        output_file = kwargs.get("output_file")
        probe_only: bool = kwargs.get("probe_only", False)
        start_page: int = kwargs.get("start_page", 0)
        sleep_seconds: int = kwargs.get("sleep_seconds", config.sleep_between_pages)
        enable_year_traverse: bool = kwargs.get("enable_year_traverse", config.enable_year_traverse)
        cost_tracker = kwargs.get("cost_tracker")

        # ── S2 fallback path: url is "s2:{paperId}" when ScraperAPI failed ──
        if url.startswith("s2:"):
            return await self._run_s2(ctx, url, config, output_file, probe_only)

        scraper = GoogleScholarScraper(
            api_keys=config.scraper_api_keys,
            log_callback=ctx.log,
            progress_callback=ctx.progress or (lambda _c, _t: None),
            debug_mode=config.debug_mode,
            premium=config.scraper_premium,
            ultra_premium=config.scraper_ultra_premium,
            retry_max_attempts=config.retry_max_attempts,
            retry_intervals=config.retry_intervals,
            session=config.scraper_session,
            no_filter=config.scholar_no_filter,
            geo_rotate=config.scraper_geo_rotate,
            dc_retry_max_attempts=config.dc_retry_max_attempts,
            cost_tracker=cost_tracker,
        )

        if probe_only:
            citation_count, estimated_pages = await scraper.detect_citation_count(url)
            return SkillResult(
                name=self.name,
                data={
                    "citation_count": citation_count,
                    "estimated_pages": estimated_pages,
                },
            )

        if output_file is None:
            raise ValueError("phase1_citation_fetch requires output_file when probe_only=False")

        out = Path(output_file)
        cache = Phase1Cache()

        # -- full cache hit: rebuild JSONL from cache, skip scraping --
        if cache.is_complete(url, require_year_traverse=enable_year_traverse):
            ctx.log(f"[Phase1 cache] full hit, skipping scrape: {url[:60]}...")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(cache.build_jsonl(url), encoding="utf-8")
            ctx.log(f"[Phase1 cache] reused {cache.paper_count(url)} papers")
            return SkillResult(name=self.name, data={"output_file": str(out), "from_cache": True})

        # -- page callback: write each page into cache --
        async def on_page(paper_dict: dict, year):
            await cache.add_papers(url, paper_dict, year=year)

        # -- year traverse: mark year complete --
        async def on_year_complete(year: int):
            await cache.mark_year_complete(url, year)

        await scraper.scrape(
            url=url,
            output_file=out,
            start_page=start_page,
            sleep_seconds=sleep_seconds,
            cancel_check=ctx.cancel_check,
            enable_year_traverse=enable_year_traverse,
            page_callback=on_page,
            year_complete_callback=on_year_complete,
            cached_years=set(
                int(y) for y, v in cache.cached_years(url).items()
                if v.get("complete")
            ) if enable_year_traverse else None,
        )

        # -- mark complete (only if not cancelled) --
        if not (ctx.cancel_check and ctx.cancel_check()):
            await cache.mark_complete(url)
            ctx.log(f"[Phase1 cache] saved {cache.paper_count(url)} papers")

        return SkillResult(name=self.name, data={"output_file": str(out), "from_cache": False})

    async def _run_s2(self, ctx: SkillContext, url: str, config, output_file, probe_only: bool) -> SkillResult:
        """S2 fallback path: fetch citing papers via Semantic Scholar API.

        url is "s2:{paperId}". Produces JSONL in the same format as the GS
        scraper path so downstream Phase 2/3 can consume it unchanged.
        """
        from citationclaw.core.s2_client import S2Client
        import json as _json
        from pathlib import Path

        s2_id = url.split("s2:", 1)[1]
        s2_key = getattr(config, "s2_api_key", "")

        ctx.log(f"[Phase1] S2 兜底模式: paperId={s2_id}")
        s2 = S2Client(api_key=s2_key or None)
        try:
            if probe_only:
                citation_count = await s2.get_paper_citation_count(s2_id)
                ctx.log(f"[Phase1] S2 probe: 目标论文被引 {citation_count} 次")
                return SkillResult(
                    name=self.name,
                    data={"citation_count": citation_count, "estimated_pages": 0},
                )

            if output_file is None:
                raise ValueError("phase1_citation_fetch requires output_file when probe_only=False")

            papers = await s2.get_paper_citations(s2_id, max_papers=5000)
            ctx.log(f"[Phase1] S2 获取到 {len(papers)} 篇施引文献")
        finally:
            await s2.close()

        if ctx.cancel_check and ctx.cancel_check():
            return SkillResult(name=self.name, data={"output_file": "", "from_cache": False})

        # Convert S2 paper dicts into Phase1 paper_dict format (paginated by 10)
        out = Path(output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        page_size = 10
        total_pages = (len(papers) + page_size - 1) // page_size or 1
        with open(out, "w", encoding="utf-8") as f:
            for page_idx in range(total_pages):
                batch = papers[page_idx * page_size:(page_idx + 1) * page_size]
                paper_dict = {}
                for i, p in enumerate(batch):
                    authors_raw = {}
                    for j, a in enumerate(p.get("authors", []) or []):
                        name = a.get("name", "")
                        if name:
                            authors_raw[f"author_{j}_{name}"] = a.get("s2_id", "") or ""
                    paper_dict[f"paper_{i}"] = {
                        "paper_link": f"https://www.semanticscholar.org/paper/{p.get('s2_id', '')}" if p.get("s2_id") else "",
                        "paper_title": p.get("title", ""),
                        "paper_year": p.get("year"),
                        "citation": str(p.get("cited_by_count", 0)),
                        "authors": authors_raw,
                        "gs_pdf_link": p.get("pdf_url", "") or "",
                        "gs_all_versions": "",
                    }
                record = {
                    f"page_{page_idx}": {
                        "paper_dict": paper_dict,
                        "next_page": "EMPTY" if page_idx == total_pages - 1 else f"s2_page_{page_idx + 1}",
                    }
                }
                f.write(_json.dumps(record, ensure_ascii=False) + "\n")

        ctx.log(f"[Phase1] S2 兜底写入 {len(papers)} 篇施引文献到 {out}")
        return SkillResult(name=self.name, data={"output_file": str(out), "from_cache": False})
