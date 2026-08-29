import json
import asyncio
from pathlib import Path
from typing import Callable, Optional
from research_connect_core.llm import create_async_openai_client as AsyncOpenAI
import httpx
from citationclaw.core.author_cache import AuthorInfoCache
from citationclaw.core.web_search_compat import web_search_extra
from citationclaw.core.structured_author_fetcher import StructuredAuthorFetcher
from citationclaw.core.s2_client import S2Client
from citationclaw.core.llm_tool_loop import chat_with_search, _rate_limit
from citationclaw.core.scholar_db import ScholarDB


class AuthorSearcher:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        log_callback: Callable,
        progress_callback: Callable,
        prompt1: str = None,
        prompt2: str = None,
        enable_renowned_scholar: bool = False,
        renowned_scholar_model: str = "gemini-3-flash-preview-nothinking",
        renowned_scholar_prompt: str = None,
        enable_author_verification: bool = False,
        author_verify_model: str = "gemini-3-pro-preview-search",
        author_verify_prompt: str = None,
        debug_mode: bool = False,
        target_paper_authors: Optional[str] = None,
        author_cache: Optional[AuthorInfoCache] = None,
        cancel_event: Optional[asyncio.Event] = None,
        wos_api_key: str = "",
        s2_api_key: str = "",
        mineru_api_token: str = "",
        light_api_key: str = "",
        light_base_url: str = "",
        search_backend: str = "zhipu_native",
        scraper_api_keys: list = None,
    ):
        """
        作者学术信息搜索器

        Args:
            api_key: OpenAI兼容API Key
            base_url: API Base URL
            model: 模型名称
            log_callback: 日志回调函数
            progress_callback: 进度回调函数
            prompt1: 搜索作者列表的Prompt（可选，使用配置中的默认值）
            prompt2: 搜索作者详细信息的Prompt（可选，使用配置中的默认值）
            enable_renowned_scholar: 是否启用二次筛选重要学者
            renowned_scholar_model: 二次筛选使用的模型
            renowned_scholar_prompt: 二次筛选的Prompt
        """
        # 使用AsyncOpenAI客户端，配置适当的连接池限制
        # 根据API文档建议，支持高并发（最高2000）
        try:
            # 配置httpx连接池限制，避免TCP连接积累
            self._http_client = httpx.AsyncClient(
                trust_env=False,
                limits=httpx.Limits(
                    max_connections=100,      # 最大连接数
                    max_keepalive_connections=20,  # 保持活跃的连接数
                    keepalive_expiry=30.0     # 连接保持时间（秒）
                ),
                timeout=30.0
            )

            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                http_client=self._http_client,
                max_retries=2
            )
        except Exception as e:
            self._http_client = None
            log_callback(f"初始化AsyncOpenAI客户端失败: {e}")
            # 尝试不带自定义http_client的初始化（兼容某些API中转平台）
            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=30.0,
                max_retries=2,
                http_client=httpx.AsyncClient(trust_env=False, timeout=30.0),
            )

        # 轻量级客户端（非联网调用 offload 到免费 provider，如 USTC）
        # chat_fn/format_fn/self_citation 等不需联网的调用走此客户端以节省主 provider 费用
        self._light_http_client = None
        self.light_client = None
        if light_api_key and light_base_url:
            try:
                self._light_http_client = httpx.AsyncClient(
                    trust_env=False,
                    limits=httpx.Limits(
                        max_connections=50,
                        max_keepalive_connections=10,
                        keepalive_expiry=30.0,
                    ),
                    timeout=60.0,
                )
                self.light_client = AsyncOpenAI(
                    api_key=light_api_key,
                    base_url=light_base_url,
                    http_client=self._light_http_client,
                    max_retries=2,
                )
            except Exception as e:
                self._light_http_client = None
                self.light_client = None
                log_callback(f"⚠️ 轻量级客户端初始化失败（非联网调用将复用主客户端）: {e}")

        # 搜索后端：zhipu_native（智谱原生 web_search）或 ustc_function（USTC function-calling tool-loop）
        self.search_backend = search_backend
        self.scraper_api_keys = scraper_api_keys or []
        self._tool_s2_client: Optional[S2Client] = None
        if search_backend == "ustc_function" and self.light_client:
            try:
                self._tool_s2_client = S2Client(api_key=s2_api_key or None)
                if log_callback:
                    src = "S2+Google" if self.scraper_api_keys else "S2 only"
                    log_callback(f"🔧 搜索后端: USTC function-calling ({src})")
            except Exception as e:
                if log_callback:
                    log_callback(f"⚠️ tool-loop S2Client 初始化失败: {e}")

        self.model = model
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.debug_mode = debug_mode

        # 调试模式提示
        if self.debug_mode:
            self.log_callback("🐛 调试模式已启用（作者搜索）：将输出详细请求/响应信息")

        # 目标论文作者信息（名字+单位，用于LLM自引检测）
        self.target_paper_authors: Optional[str] = target_paper_authors

        # 作者信息持久化缓存
        self.author_cache: Optional[AuthorInfoCache] = author_cache
        self.cancel_event: Optional[asyncio.Event] = cancel_event

        # 结构化作者提取（WOS→S2→MinerU），有 wos_api_key 或 s2_api_key 时启用
        self.structured_fetcher: Optional[StructuredAuthorFetcher] = None
        if wos_api_key or s2_api_key:
            self.structured_fetcher = StructuredAuthorFetcher(
                wos_api_key=wos_api_key,
                s2_api_key=s2_api_key,
                mineru_api_token=mineru_api_token,
                openai_api_key=api_key,
                openai_base_url=base_url,
                model=model,
                log_callback=log_callback,
            )
            sources = []
            if wos_api_key:
                sources.append("WOS")
            if s2_api_key:
                sources.append("S2")
            log_callback(f"📋 结构化作者提取已启用：{'→'.join(sources)}→MinerU")
        else:
            log_callback("⚪ 结构化作者提取未启用（未配置 WOS/S2 API Key）")

        # 自引检测 Prompt（使用轻量级模型）
        self.self_citation_check_prompt = (
            "【任务】判断一篇施引论文是否为自引。\n"
            "【自引定义】若施引论文的任意一位作者，同时也是被引目标论文的作者，则视为自引。\n\n"
            "【被引目标论文的作者信息（含姓名和单位）】\n"
            "{target_info}\n\n"
            "【施引论文的作者信息】\n"
            "来源1（Google Scholar显示的作者）：\n{authors_with_profile}\n\n"
            "来源2（网络搜索获得的作者及单位）：\n{searched_affiliation}\n\n"
            "【注意】姓名可能有中英文差异、缩写差异，请结合单位信息综合判断。\n"
            "请直接回答：是（存在自引）或 否（不是自引）"
        )

        # 保存prompt模板
        self.prompt1 = prompt1 or "这是一篇论文。请你根据这个paper_link和paper_title，去搜索查阅这篇论文的作者列表，然后输出每个作者的名字及其对应的单位名称。"
        self.prompt2 = prompt2 or "这是一篇论文及作者列表。请你根据这篇论文、作者名字和作者单位，去搜索该每位作者的个人信息，输出每位作者的谷歌学术累积引用（如有）、重大学术头衔（比如是否IEEE/ACM/ACL等学术Fellow、中国科学院院士、中国工程院院士、国外院士如欧洲科学院院士、诺贝尔奖得主、图灵奖得主，国家杰青、长江学者、优青、在国外著名机构（例如google，deepmind，meta，openai）就业的人士，或在AI领域的国际知名人物），行政职位（如国内外知名大学的校长或院长）。"

        # 二次筛选配置
        self.enable_renowned_scholar = enable_renowned_scholar
        self.renowned_scholar_model = renowned_scholar_model

        self.renowned_scholar_prompt = renowned_scholar_prompt or (
            "以上是一篇论文的作者列表信息。\n"
            "### 任务指南：\n"
            "1. **高影响力判定 (is_high_impact)**：学术影响力大（院士、知名学会Fellow、国家杰青/长江/优青等）或企业界大佬（首席科学家、VP、负责人）。除此之外，其他级别的学者或教授一律不保留。\n"
            "2. **无重量级作者**：若作者信息明确说明无重量级作者，只需要输出'无任何重量级学者'。\n\n"
            "3. **有重量级作者**：若有重量级作者，只输出那些顶级大佬级别的学者，进一步总结每位重量级作者的元信息，包括姓名、机构、国家、职务、荣誉称号。每位重量级作者之间用 $$$分隔符$$$ 来隔开，输出格式参考如下：\n\n"
            "（输出格式参考）：\n"
            "$$$分隔符$$$\n"
            "重量级作者1\n"
            "姓名\n"
            "机构（当前最新任职单位）\n"
            "国家\n"
            "职务（在行政单位或著名研究机构的职务或职称）\n"
            "荣誉称号（所获得的学术头衔或国际重量级头衔）\n"
            "$$$分隔符$$$\n"
            "重量级作者2\n"
            "姓名\n"
            "机构（当前最新任职单位）\n"
            "国家\n"
            "职务（在行政单位或著名研究机构的职务或职称）\n"
            "荣誉称号（所获得的学术头衔或国际重量级头衔）\n"
            "直至所有的重量级作者都被记录下来。记住，无需任何前言后记。")

        self.renowned_scholar_formatoutput_prompt = (
            f"以上是一位重量级作者信息：\n"
            "### 任务指南：\n"
            "**JSON格式化输出**：以JSON格式输出每位重量级作者的元信息，包括姓名、机构、国家、职务、荣誉称号。\n\n"
            "### 输出格式参考（共5个键值对）：\n"
            " {\n"
            "    \"姓名\": \"姓名\",\n"
            "    \"机构\": \"当前最新任职单位\",\n"
            "    \"国家\": \"作者所在机构或单位的所在国家\",\n"
            "    \"职务\": \"作者所担任的职务\",\n"
            "    \"荣誉称号\":  \"作者所获取的重量级头衔\",\n"
            " }"
            "记住：不要任何前言后记。")

        # 作者信息校验配置
        self.enable_author_verification = enable_author_verification
        self.author_verify_model = author_verify_model
        self.author_verify_prompt = author_verify_prompt or (
            "这是一份已经整理好的作者学术信息列表。请你对列表中的每一位作者信息进行真实性校验。你需要执行以下任务：\n"
            "1. 针对每位作者，核查其姓名、所属单位、谷歌学术引用量、学术头衔、行政职位是否真实存在。\n"
            "2. 必须通过可靠公开来源进行核验（如Google Scholar、大学官网主页、DBLP、ORCID、ResearchGate、IEEE/ACM/ACL官方Fellow名单、科学院官网、诺奖或图灵奖官网等）。\n"
            "3. 对每条信息分别标注核验结果，格式为：\n"
            "   - 正确（Verified）：可被权威来源明确证实。\n"
            "   - 存疑（Uncertain）：存在部分证据但不充分或信息冲突。\n"
            "   - 错误（Incorrect）：无法找到可信来源或存在明显错误。\n"
            "4. 若发现错误或存疑，请给出修正后的准确信息（若能确定）。\n"
            "5. 对每条核验内容，必须给出对应的来源链接或来源名称。\n"
            "6. 最终输出结构化结果，包括：作者姓名、原始信息、核验结论、修正信息（如有）、核验来源。\n"
            "7. 若无法找到任何可信来源，请明确说明\"未检索到可信来源支持该信息\"，禁止基于推测补充信息。"
        )

        # 知名学者本地数据库
        self.scholar_db: ScholarDB | None = None
        try:
            db = ScholarDB()
            if db.count() > 0:
                self.scholar_db = db
                self.log_callback(f"📚 知名学者数据库已加载: {db.count()} 条记录")
            else:
                db.close()
        except Exception:
            pass

    import re as _re
    _name_patterns = [
        _re.compile(r'#{2,4}\s*\d*\.\s*\*\*([^*]{2,40})\*\*'),
        _re.compile(r'#{2,4}\s*\d*\.\s*([^\n（(]{2,40})[（(]'),
        _re.compile(r'\*\*([^*]{2,40})\*\*'),
    ]
    _cn_name_re = _re.compile(r'[（(]([\u4e00-\u9fff]{2,5})[）)]')
    _exclude_words = (
        '作者', '信息', '汇总', '论文', '单位', '方向', '影响力', '头衔',
        '职称', '引用', 'Paper', 'Link', '行政', '备注', '说明', '搜索',
        '根据', '整理', '确认', '所在', '谷歌', '学术', '未检索',
    )

    def _extract_author_names(self, text: str) -> list[str]:
        """Extract author names from LLM-generated author info text."""
        if not text:
            return []
        names: list[str] = []
        seen: set[str] = set()
        for pat in self._name_patterns:
            for m in pat.finditer(text):
                raw = m.group(1).strip().strip('*_—- ')
                if not raw or len(raw) < 2 or len(raw) > 40:
                    continue
                if raw in seen:
                    continue
                if any(w in raw for w in self._exclude_words):
                    continue
                seen.add(raw)
                names.append(raw)
                for cn in self._cn_name_re.finditer(raw):
                    cn_name = cn.group(1).strip()
                    if cn_name and cn_name not in seen and len(cn_name) >= 2:
                        seen.add(cn_name)
                        names.append(cn_name)
        for cn in self._cn_name_re.finditer(text):
            cn_name = cn.group(1).strip()
            if cn_name and cn_name not in seen and 2 <= len(cn_name) <= 5:
                if not any(w in cn_name for w in self._exclude_words):
                    seen.add(cn_name)
                    names.append(cn_name)
        return names

    def _lookup_scholar_db(self, names: list[str], context: str = "") -> list[dict]:
        """Look up author names in the local scholar DB.

        Args:
            names: extracted author names
            context: the full author info text (for affiliation cross-check)

        Returns a list of formatted scholar records:
          [{"序号": 1, "姓名": ..., "机构": ..., "国家": ..., "职务": ..., "荣誉称号": ...}]
        """
        if not self.scholar_db or not names:
            return []
        results: list[dict] = []
        seen_names: set[str] = set()
        for name in names:
            hit = self.scholar_db.lookup(name)
            if not hit:
                continue
            display_name = hit.get("name_en") or name
            if display_name in seen_names:
                continue
            if context and hit.get("affiliation"):
                affil = hit["affiliation"]
                short_affil = affil[:4] if len(affil) > 4 else affil
                if short_affil not in context and affil not in context:
                    continue
            elif context and not hit.get("affiliation"):
                continue
            seen_names.add(display_name)
            honors = hit.get("honors", [])
            honors_str = "、".join(honors) if honors else hit.get("title", "")
            results.append({
                "序号": len(results) + 1,
                "姓名": f"{name}（{hit.get('name_en', '')}）" if hit.get("name_en") and name != hit.get("name_en") else name,
                "机构": hit.get("affiliation", ""),
                "国家": hit.get("country", "中国"),
                "职务": hit.get("title", ""),
                "荣誉称号": honors_str,
            })
        return results

    async def close(self):
        """关闭底层 httpx 客户端，释放连接池资源。"""
        if self._http_client is not None:
            await self._http_client.aclose()
        if self._light_http_client is not None:
            await self._light_http_client.aclose()
        if self._tool_s2_client is not None:
            await self._tool_s2_client.close()
        if self.scholar_db is not None:
            self.scholar_db.close()

    # ── Unified LLM call with retry / backoff / quota handling ──────────
    async def _call_llm(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        response_format: dict | None = None,
        extra_body: dict | None = None,
        temperature: float = 0.1,
        max_retries: int = 5,
        log_prefix: str = "",
        debug_label: str = "LLM",
        use_light: bool = False,
    ) -> str:
        """Unified LLM call with retry, backoff, and quota handling.

        Returns the assistant message content on success, or ``'ERROR'`` after
        all retries are exhausted.
        """
        resolved_model = model or self.model
        quota_failures = 0

        for attempt in range(max_retries):
            try:
                if use_light:
                    await _rate_limit()
                if self.debug_mode:
                    self.log_callback(f"🔍 [DEBUG] 发送{debug_label}请求 (模型: {resolved_model})")
                    if messages:
                        self.log_callback(f"🔍 [DEBUG] 请求内容: {messages[-1]['content'][:200]}...")

                kwargs: dict = {
                    "model": resolved_model,
                    "messages": messages,
                }
                # Only pass temperature when not using json_object response_format
                # (some providers reject temperature with structured output)
                if response_format is None:
                    kwargs["temperature"] = temperature
                if response_format is not None:
                    kwargs["response_format"] = response_format
                if extra_body is not None:
                    if "web_search_options" in extra_body:
                        extra_body = web_search_extra(str(self.client.base_url))
                    kwargs["extra_body"] = extra_body

                active_client = self.light_client if (use_light and self.light_client) else self.client
                completion = await active_client.chat.completions.create(**kwargs)
                response = completion.choices[0].message.content

                if self.debug_mode:
                    self.log_callback(f"✅ [DEBUG] {debug_label}响应: {response[:200]}...")

                return response

            except Exception as e:
                error_msg = str(e).lower()

                if self.debug_mode:
                    self.log_callback(f"❌ [DEBUG] {debug_label}API异常: {type(e).__name__}: {e}")

                # ── Quota / rate-limit errors ────────────────────────────
                is_rate_limit = 'rate' in error_msg or 'limit' in error_msg or '429' in error_msg
                is_quota = 'quota' in error_msg or 'insufficient' in error_msg
                if is_rate_limit or is_quota:
                    quota_failures += 1
                    if is_quota and quota_failures >= 3:
                        # True quota exhaustion → cancel all
                        self.log_callback("❌ API配额耗尽，已停止重试。")
                        if self.cancel_event:
                            self.cancel_event.set()
                        return 'ERROR'
                    if self.cancel_event and self.cancel_event.is_set():
                        return 'ERROR'
                    # Rate limit → backoff and retry (don't cancel)
                    wait = min(10 * quota_failures, 60)  # 10s, 20s, 30s... max 60s
                    self.log_callback(f"⚠️ API限流，{wait}s后重试（第{quota_failures}次）...")
                    await asyncio.sleep(wait)
                    continue  # retry same attempt slot after wait

                # ── Other errors (including timeout) ─────────────────────
                is_timeout = 'timed out' in error_msg or 'timeout' in error_msg
                if attempt < max_retries - 1:
                    wait_time = min(2 ** attempt, 30)
                    if is_timeout:
                        self.log_callback(f"{log_prefix}⏰ 超时，{wait_time}s后重试({attempt + 1}/{max_retries})")
                    else:
                        self.log_callback(f"{log_prefix}⚠️ {debug_label}API错误: {e}，{wait_time}秒后重试 (第{attempt + 1}/{max_retries}次)，请耐心等待！")
                    await asyncio.sleep(wait_time)
                else:
                    self.log_callback(
                        f"{log_prefix}❌ {'请求超时，作者信息将留空' if is_timeout else f'{debug_label}API错误（已达最大重试次数）: {e}'}"
                    )
                    return 'ERROR'

        return 'ERROR'  # should not reach here, but safety fallback

    # ── Convenience wrappers (preserve original public signatures) ──────

    async def search_fn(self, query: str, retry_count: int = 0, max_retries: int = 5, log_prefix: str = "", quota_retry_count: int = 0) -> str:
        """调用搜索模型（启用web搜索）"""
        if self.search_backend == "ustc_function" and self.light_client:
            return await chat_with_search(
                self.light_client,
                self.renowned_scholar_model,
                messages=[{"role": "user", "content": query}],
                s2_client=self._tool_s2_client,
                scraper_keys=self.scraper_api_keys,
                log=self.log_callback,
            )
        return await self._call_llm(
            messages=[{"role": "user", "content": query}],
            extra_body={"web_search_options": {}},
            max_retries=max_retries,
            log_prefix=log_prefix,
            debug_label="搜索",
        )

    async def chat_fn(self, query: str, retry_count: int = 0, max_retries: int = 5, log_prefix: str = "", quota_retry_count: int = 0) -> str:
        """调用对话模型（不启用web搜索，用于二次筛选）"""
        return await self._call_llm(
            messages=[{"role": "user", "content": query}],
            model=self.renowned_scholar_model,
            max_retries=max_retries,
            log_prefix=log_prefix,
            debug_label="二次筛选",
            use_light=True,
        )

    async def format_fn(self, query: str, retry_count: int = 0, max_retries: int = 5, log_prefix: str = "", quota_retry_count: int = 0) -> str:
        """调用格式输出模型（不启用web搜索，用于输出JSON）"""
        return await self._call_llm(
            messages=[{"role": "user", "content": query}],
            model=self.renowned_scholar_model,
            response_format={"type": "json_object"},
            max_retries=max_retries,
            log_prefix=log_prefix,
            debug_label="格式化输出重量级学者",
            use_light=True,
        )

    async def verify_fn(self, query: str, retry_count: int = 0, max_retries: int = 5, log_prefix: str = "", quota_retry_count: int = 0) -> str:
        """调用校验模型（启用web搜索，用于作者信息真实性校验）"""
        if self.search_backend == "ustc_function" and self.light_client:
            return await chat_with_search(
                self.light_client,
                self.renowned_scholar_model,
                messages=[{"role": "user", "content": query}],
                s2_client=self._tool_s2_client,
                scraper_keys=self.scraper_api_keys,
                log=self.log_callback,
            )
        return await self._call_llm(
            messages=[{"role": "user", "content": query}],
            model=self.author_verify_model,
            extra_body={"web_search_options": {}},
            max_retries=max_retries,
            log_prefix=log_prefix,
            debug_label="作者校验",
        )

    async def _check_self_citation_llm(
        self,
        authors_with_profile: str,
        searched_affiliation: str,
        retry_count: int = 0,
        max_retries: int = 3,
        quota_retry_count: int = 0,
    ) -> bool:
        """用轻量级LLM判断施引论文是否为自引。

        综合三方信息：
        - 目标论文作者（名字+单位，已缓存）
        - Authors_with_Profile（Google Scholar抓取，可能不全）
        - Searched Author-Affiliation（网络搜索，更完整）
        """
        if not self.target_paper_authors:
            return False
        prompt = self.self_citation_check_prompt.format(
            target_info=self.target_paper_authors,
            authors_with_profile=authors_with_profile,
            searched_affiliation=searched_affiliation,
        )
        result = await self._call_llm(
            messages=[{"role": "user", "content": prompt}],
            model=self.renowned_scholar_model,
            temperature=0.0,
            max_retries=max_retries,
            debug_label="自引检测",
            use_light=True,
        )
        if result == 'ERROR':
            self.log_callback("⚠️ 自引检测LLM调用失败，默认视为非自引")
            return False
        return result.strip().startswith("是")

    async def _search_single_paper(
        self,
        page_id: str,
        paper_id: str,
        paper_content: dict,
        count: int,
        total_papers: int,
        semaphore: asyncio.Semaphore,
        citing_paper: str = "",
        completed_state: Optional[dict] = None,
    ) -> tuple:
        """
        搜索单篇论文的作者信息（并行任务单元）。
        优先从持久化缓存读取，未命中时调用 LLM 并将结果写入缓存。

        Returns:
            (count, record_dict) 元组
        """
        async with semaphore:
            if self.cancel_event and self.cancel_event.is_set():
                return (count, {})
            paper_title = paper_content['paper_title']
            paper_link  = paper_content['paper_link']
            log_prefix  = f"[{count}/{total_papers}] "

            record_dict = {
                'PageID': page_id,
                'PaperID': paper_id,
                'Paper_Title': paper_title,
                'Paper_Year': paper_content['paper_year'],
                'Paper_Link': paper_link,
                'Citations': paper_content['citation'],
                'Authors_with_Profile': str(paper_content['authors']),
            }

            # ── 结构化作者提取（WOS→S2→MinerU）─────────────────────────────
            if self.structured_fetcher:
                doi = paper_content.get('doi', '')
                pdf_path = paper_content.get('pdf_path', None)
                self.log_callback(f"  🔍 [结构化] 查询作者: {paper_title[:50]}...")
                try:
                    struct_authors, struct_source = await self.structured_fetcher.fetch(
                        paper_title, doi=doi, pdf_path=pdf_path
                    )
                    record_dict['Paper_Authors'] = struct_authors
                    record_dict['Paper_Authors_Source'] = struct_source
                    if struct_authors:
                        self.log_callback(
                            f"  📋 [{struct_source}] 找到 {len(struct_authors)} 位作者: {paper_title[:40]}..."
                        )
                    else:
                        self.log_callback(
                            f"  ⚪ [结构化] 未找到作者（WOS/S2 均无收录）: {paper_title[:40]}..."
                        )
                except Exception as exc:
                    self.log_callback(f"  ⚠️ 结构化作者提取失败: {exc}")
                    record_dict['Paper_Authors'] = []
                    record_dict['Paper_Authors_Source'] = ''

            # ── 查询缓存（取出已有字段供后续各步使用）────────────────────────
            cached = (await self.author_cache.get(paper_link, paper_title)) if self.author_cache else None

            # ── Step 1: 作者列表及单位 ────────────────────────────────────────
            if cached and cached.get('Searched Author-Affiliation'):
                response1 = cached['Searched Author-Affiliation']
                record_dict['Searched Author-Affiliation'] = response1
                record_dict['First_Author_Institution'] = cached.get('First_Author_Institution', '')
                record_dict['First_Author_Country'] = cached.get('First_Author_Country', '')
                self.log_callback(f"  💾 [缓存] 作者-单位: {paper_title[:40]}...")
            else:
                query1 = f'Paper_Link: {paper_link}, Paper_Title: {paper_title}.'
                query1 += '\n' + self.prompt1
                response1 = await self.search_fn(query1, log_prefix=log_prefix)
                record_dict['Searched Author-Affiliation'] = response1

                first_author_query = (
                    response1 + "\n\n"
                    "请从上述作者-机构列表中，提取**排在最前面的第一作者**的机构和国家。"
                    "以JSON格式输出（只输出JSON，无其他文字）：\n"
                    '{"first_author_institution": "机构全称", "first_author_country": "国家（中文）"}'
                )
                response_first = await self.format_fn(first_author_query, log_prefix=log_prefix)
                try:
                    fa = json.loads(response_first)
                    record_dict['First_Author_Institution'] = fa.get('first_author_institution', '')
                    record_dict['First_Author_Country'] = fa.get('first_author_country', '')
                except Exception:
                    record_dict['First_Author_Institution'] = ''
                    record_dict['First_Author_Country'] = ''

                # FIX: Don't cache ERROR sentinels
                if self.author_cache and response1 != 'ERROR':
                    await self.author_cache.update(paper_link, paper_title, {
                        'Searched Author-Affiliation': response1,
                        'First_Author_Institution': record_dict['First_Author_Institution'],
                        'First_Author_Country': record_dict['First_Author_Country'],
                    })

            # ── 自引检测（不缓存：依赖目标论文，每次运行需重新判断）──────────
            record_dict['Citing_Paper'] = citing_paper
            is_self_citation = await self._check_self_citation_llm(
                authors_with_profile=record_dict['Authors_with_Profile'],
                searched_affiliation=response1,
            )
            record_dict['Is_Self_Citation'] = is_self_citation
            if is_self_citation:
                self.log_callback(f"  ↩️ 自引：{paper_title[:50]}... 已标记，跳过知名学者筛选")

            # ── Step 2: 详细作者信息 ──────────────────────────────────────────
            if cached and cached.get('Searched Author Information'):
                response2 = cached['Searched Author Information']
                record_dict['Searched Author Information'] = response2
            else:
                query2 = f'Paper_Link: {paper_link}, Paper_Title: {paper_title}, Author-Affiliation: {response1}'
                query2 += '\n' + self.prompt2
                response2 = await self.search_fn(query2, log_prefix=log_prefix)
                record_dict['Searched Author Information'] = response2
                # FIX: Don't cache ERROR sentinels
                if self.author_cache and response2 != 'ERROR':
                    await self.author_cache.update(paper_link, paper_title, {
                        'Searched Author Information': response2,
                    })

            # ── Step 3: 作者信息校验（可选）──────────────────────────────────
            if self.enable_author_verification:
                if cached and cached.get('Author Verification'):
                    record_dict['Author Verification'] = cached['Author Verification']
                else:
                    query_verify = response2 + '\n\n' + self.author_verify_prompt
                    response_verify = await self.verify_fn(query_verify, log_prefix=log_prefix)
                    record_dict['Author Verification'] = response_verify
                    # FIX: Don't cache ERROR sentinels
                    if self.author_cache and response_verify != 'ERROR':
                        await self.author_cache.update(paper_link, paper_title, {
                            'Author Verification': response_verify,
                        })

            # ── Step 5: 知名学者筛选（可选，自引时跳过）─────────────────────
            if self.enable_renowned_scholar:
                if is_self_citation:
                    record_dict['Renowned Scholar'] = '自引，已跳过知名学者筛选'
                    record_dict['Formated Renowned Scholar'] = []
                elif cached and 'Formated Renowned Scholar' in cached:
                    record_dict['Renowned Scholar'] = cached.get('Renowned Scholar', '')
                    record_dict['Formated Renowned Scholar'] = cached['Formated Renowned Scholar']
                    self.log_callback(f"  💾 [缓存] 知名学者: {paper_title[:40]}...")
                else:
                    # ── 先查本地学者数据库 ──
                    db_scholars: list[dict] = []
                    if self.scholar_db:
                        author_names = self._extract_author_names(response2)
                        if author_names:
                            db_scholars = self._lookup_scholar_db(author_names, context=response2)
                            if db_scholars:
                                self.log_callback(
                                    f"  📚 [本地DB] 命中 {len(db_scholars)} 位知名学者: "
                                    f"{', '.join(s['姓名'] for s in db_scholars[:3])}"
                                )

                    # ── 再调 LLM 筛选（补充DB未覆盖的学者）──
                    query_filter = response2 + '\n\n' + self.renowned_scholar_prompt
                    response_filter = await self.chat_fn(query_filter, log_prefix=log_prefix)
                    record_dict['Renowned Scholar'] = response_filter
                    format_scholar_record = list(db_scholars)
                    scholar_count = len(db_scholars)
                    db_names = {s['姓名'].split('（')[0].strip() for s in db_scholars}
                    if "无任何重量级学者" not in response_filter:
                        if "$$$分隔符$$$" in response_filter:
                            scholars = response_filter.split("$$$分隔符$$$")
                            scholars = [s for s in scholars if s != '' and "无" not in s]
                            for scholar in scholars:
                                first_line = scholar.strip().split('\n')[0].strip()
                                first_name = first_line.split('（')[0].strip()
                                if first_name in db_names:
                                    continue
                                scholar_format_query = scholar + '\n\n' + self.renowned_scholar_formatoutput_prompt
                                response_scholar_format = await self.format_fn(scholar_format_query, log_prefix=log_prefix)
                                try:
                                    res = json.loads(response_scholar_format)
                                except Exception:
                                    res = ''
                                if isinstance(res, dict) and res.get('姓名', 'EMPTY') != 'EMPTY':
                                    llm_name = res.get('姓名', '').split('（')[0].strip()
                                    if llm_name in db_names:
                                        continue
                                    scholar_count += 1
                                    format_scholar_record.append({
                                        '序号': scholar_count,
                                        '姓名': res.get('姓名', ''),
                                        '机构': res.get('机构', ''),
                                        '国家': res.get('国家', ''),
                                        '职务': res.get('职务', ''),
                                        '荣誉称号': res.get('荣誉称号', ''),
                                    })
                    record_dict['Formated Renowned Scholar'] = format_scholar_record
                    # FIX: Don't cache ERROR sentinels
                    if self.author_cache and response_filter != 'ERROR':
                        await self.author_cache.update(paper_link, paper_title, {
                            'Renowned Scholar': response_filter,
                            'Formated Renowned Scholar': format_scholar_record,
                        })

            if completed_state is not None:
                async with completed_state["lock"]:
                    completed_state["n"] += 1
                    log_num = completed_state["n"]
            else:
                log_num = count
            self.log_callback(f"[{log_num}/{total_papers}] 搜索完成: {paper_title[:50]}...")
            # FIX: Update progress as each task completes (not in burst at end)
            if completed_state is not None:
                self.progress_callback(log_num, total_papers)
            return (count, record_dict)

    async def search(
        self,
        input_file: Path,
        output_file: Path,
        sleep_seconds: float = 0.5,
        parallel_workers: int = 1,
        cancel_check: Optional[Callable[[], bool]] = None,
        citing_paper: str = "",
    ):
        """
        搜索所有论文的作者信息

        Args:
            input_file: 输入JSONL文件(来自scraper)
            output_file: 输出JSONL文件
            sleep_seconds: 每条查询间隔(秒) - 并行模式下不使用
            parallel_workers: 并行处理数量(默认1为串行,>1为并行)
            cancel_check: 取消检查函数
        """
        # 读取引用列表
        with open(input_file, 'r', encoding='utf-8') as f:
            data = [json.loads(line) for line in f]

        # 统计总论文数并收集所有任务
        tasks_to_process = []
        count = 0
        for d in data:
            for page_id, page_content in d.items():
                paper_dict = page_content['paper_dict']
                for paper_id, paper_content in paper_dict.items():
                    count += 1
                    tasks_to_process.append({
                        'page_id': page_id,
                        'paper_id': paper_id,
                        'paper_content': paper_content,
                        'count': count
                    })

        total_papers = len(tasks_to_process)
        self.log_callback(f"共需要搜索 {total_papers} 篇论文的作者信息")

        if parallel_workers > 1:
            self.log_callback(f"使用并行模式，并发数: {parallel_workers}")
        else:
            self.log_callback(f"使用串行模式")

        # 确保输出目录存在
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # FIX: In serial mode, truncate output file first to avoid corruption
        # from leftover data of a previous run, then append per-record.
        if parallel_workers == 1:
            with open(output_file, 'w') as f:
                pass  # truncate

        # 创建信号量控制并发数
        semaphore = asyncio.Semaphore(parallel_workers)

        # 创建共享完成计数器（用于日志按完成顺序编号）
        completed_state = {"n": 0, "lock": asyncio.Lock()}

        if parallel_workers > 1:
            # ── 并行模式：增量保存，每完成一篇就写入 ──
            self.log_callback(f"使用并行模式，并发数: {parallel_workers}（增量保存）")
            output_file.parent.mkdir(parents=True, exist_ok=True)

            # Truncate file first
            with open(output_file, 'w', encoding='utf-8') as f:
                pass

            write_lock = asyncio.Lock()
            written_count = 0

            async def _run_and_save(task_info):
                nonlocal written_count
                result = await self._search_single_paper(
                    page_id=task_info['page_id'],
                    paper_id=task_info['paper_id'],
                    paper_content=task_info['paper_content'],
                    count=task_info['count'],
                    total_papers=total_papers,
                    semaphore=semaphore,
                    citing_paper=citing_paper,
                    completed_state=completed_state,
                )
                count_num, record_dict = result
                if record_dict:
                    async with write_lock:
                        with open(output_file, 'a', encoding='utf-8') as f:
                            f.write(json.dumps({count_num: record_dict}, ensure_ascii=False) + '\n')
                        written_count += 1
                return count_num

            # Create all tasks
            tasks = []
            for task_info in tasks_to_process:
                if cancel_check and cancel_check():
                    self.log_callback("任务已取消")
                    break
                tasks.append(asyncio.create_task(_run_and_save(task_info)))

            # Wait with incremental completion tracking
            # Scale timeout with paper count: 2h base + 30s per paper
            wait_timeout = max(7200, total_papers * 60)
            try:
                done, pending = await asyncio.wait(tasks, timeout=wait_timeout)
            except Exception:
                done, pending = set(), set(tasks)

            if pending:
                self.log_callback(f"⚠️ 超时（{wait_timeout//60}分钟），{len(pending)} 个任务未完成，正在取消...")
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

            # Report errors
            errors = [t for t in done if t.exception() is not None]
            if errors:
                self.log_callback(f"⚠️ 有 {len(errors)} 个任务失败")
                for i, t in enumerate(errors[:3]):
                    exc = t.exception()
                    self.log_callback(f"  错误 {i+1}: {type(exc).__name__}: {exc}")

            self.log_callback(f"✅ 已保存 {written_count}/{total_papers} 篇论文结果")

        else:
            # ── 串行模式 ──
            self.log_callback(f"使用串行模式")
            output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, 'w', encoding='utf-8') as f:
                pass

            for task_info in tasks_to_process:
                if cancel_check and cancel_check():
                    self.log_callback("任务已取消")
                    return

                result = await self._search_single_paper(
                    page_id=task_info['page_id'],
                    paper_id=task_info['paper_id'],
                    paper_content=task_info['paper_content'],
                    count=task_info['count'],
                    total_papers=total_papers,
                    semaphore=semaphore,
                    citing_paper=citing_paper,
                    completed_state=completed_state,
                )
                count_num, record_dict = result
                if record_dict:
                    with open(output_file, 'a', encoding='utf-8') as f:
                        f.write(json.dumps({count_num: record_dict}, ensure_ascii=False) + '\n')

                self.progress_callback(count_num, total_papers)
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)

        self.log_callback(f"✅ 作者信息搜索完成!共处理 {total_papers} 篇论文")
