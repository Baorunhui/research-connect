"""Function-calling tool-use loop for web search via USTC models.

USTC's litellm gateway only supports standard ``function`` tools (not
provider-native ``web_search``).  This module implements an agent tool-use
loop that gives free USTC models "pseudo web search" capability:

1. Send messages + ``search_web`` tool definition to the LLM
2. LLM returns ``tool_calls``
3. We execute ``search_web`` (S2 academic DB + Google fallback)
4. Feed results back as a ``tool`` message
5. LLM produces final answer

This avoids Zhipu web_search costs entirely for search_fn / verify_fn.
"""

import asyncio
import json
import re
import html
import time
from typing import Optional, Callable, List
from urllib.parse import quote

import httpx

from citationclaw.core.s2_client import S2Client


# ── 全局速率限制器 (USTC 限速 ~20 req/min) ──────────────────────────
_rate_lock = asyncio.Lock()
_rate_times: list[float] = []


async def _rate_limit(max_per_minute: int = 18):
    """Ensure no more than *max_per_minute* LLM calls in a rolling 60s window."""
    global _rate_times
    async with _rate_lock:
        now = time.monotonic()
        cutoff = now - 60.0
        _rate_times = [t for t in _rate_times if t > cutoff]
        if len(_rate_times) >= max_per_minute:
            wait = 60.0 - (now - _rate_times[0]) + 0.5
            if wait > 0:
                await asyncio.sleep(wait)
        now = time.monotonic()
        _rate_times.append(now)


SEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the web for real-time information. Returns relevant "
                "results from academic databases (Semantic Scholar) and web "
                "search (Google). Use this to find paper authors, author "
                "affiliations, citation counts, academic titles, etc. "
                "Extract the key search terms (paper title or author name) "
                "from the context and pass as the query."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query: a paper title, author name, or general query.",
                    }
                },
                "required": ["query"],
            },
        },
    }
]

_NOISE_PATTERNS = re.compile(
    r"^(Sign in|Settings|Privacy|Terms|Cookie|I agree|Skip to|Loading|Menu|"
    r"Search|Home|About|Help|Feedback|Learn more|See more|查看更多|登录|注册|"
    r"设置|隐私|条款|首页|关于|帮助|反馈)$",
    re.IGNORECASE,
)

# Strip raw XML-like tool-call blocks that some models leak into content
_RE_XML_BLOCK = re.compile(
    r"<(?:tool_call|think|reasoning)>.*?</(?:tool_call|think|reasoning)>",
    re.DOTALL,
)


def _extract_content(msg) -> str:
    """Extract clean content from an LLM response message.

    Handles reasoning models that put the actual answer in
    reasoning_content (when content is empty) and strips raw
    XML tool-call or think blocks that leak into content.
    """
    content = msg.content or ""
    if not content and hasattr(msg, "reasoning_content"):
        content = msg.reasoning_content or ""
    content = _RE_XML_BLOCK.sub("", content)
    return content.strip()


async def _google_search(
    query: str, scraper_keys: List[str], client: httpx.AsyncClient
) -> str:
    """Search Google via ScraperAPI and extract text snippets."""
    if not scraper_keys:
        return ""
    key = scraper_keys[0]
    gurl = f"https://www.google.com/search?q={quote(query)}&hl=zh-CN&num=5"
    try:
        resp = await client.get(
            "https://api.scraperapi.com/",
            params={"key": key, "url": gurl},
            timeout=25.0,
        )
        if resp.status_code != 200 or not resp.text:
            return ""
        text = resp.text
        chunks = re.findall(r">([^<]{40,400})<", text)
        seen = set()
        relevant = []
        for c in chunks:
            clean = html.unescape(c).strip()
            if len(clean) < 40 or clean in seen:
                continue
            if _NOISE_PATTERNS.match(clean):
                continue
            seen.add(clean)
            relevant.append(clean)
            if len(relevant) >= 8:
                break
        if relevant:
            return "[Google Web Search]\n" + "\n".join(
                f"- {c[:250]}" for c in relevant
            )
    except Exception:
        pass
    return ""


async def _bing_search(
    query: str, client: httpx.AsyncClient
) -> str:
    """Search Bing via direct scraping (free, no API key needed).

    Works in China where Google is blocked.  Extracts result titles
    and snippets from Bing HTML response.
    """
    burl = f"https://cn.bing.com/search?q={quote(query)}&count=10"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = await client.get(
            burl, headers=headers, timeout=12.0, follow_redirects=True,
        )
        if resp.status_code != 200 or not resp.text:
            return ""
        text = resp.text

        # Extract result snippets from Bing HTML
        snippets = []
        seen = set()

        # Method 1: Extract from b_algo result blocks (title + snippet)
        # Bing structure: <li class="b_algo">...<h2><a>title</a></h2>...<p>snippet</p>...</li>
        algo_blocks = re.findall(
            r'class="b_algo"[^>]*>(.*?)(?=class="b_algo"|$)',
            text, re.DOTALL,
        )
        for block in algo_blocks:
            # Extract snippet from <p> tags within this block
            ptags = re.findall(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
            for p in ptags:
                clean = re.sub(r"<[^>]+>", "", p)
                clean = html.unescape(clean).strip()
                if len(clean) >= 30 and clean not in seen:
                    if not _NOISE_PATTERNS.match(clean):
                        # Skip JavaScript code
                        if "function" not in clean[:20] and "var " not in clean[:10]:
                            seen.add(clean)
                            snippets.append(clean[:300])

        # Method 2: Fallback - extract from all <p> and text chunks
        if not snippets:
            chunks = re.findall(r">([^<]{30,400})<", text)
            for c in chunks:
                clean = html.unescape(c).strip()
                if len(clean) < 30 or clean in seen:
                    continue
                if _NOISE_PATTERNS.match(clean):
                    continue
                # Skip JavaScript, CSS, and code noise
                if any(clean.startswith(x) for x in [
                    "function", "var ", "if ", "for ", "//", "/*",
                    "Skip to", "Accessibility",
                ]):
                    continue
                seen.add(clean)
                snippets.append(clean[:300])
                if len(snippets) >= 8:
                    break

        if snippets:
            return "[Bing Web Search]\n" + "\n".join(
                f"- {s}" for s in snippets[:8]
            )
    except Exception:
        pass
    return ""


async def execute_web_search(
    query: str,
    s2_client: Optional[S2Client] = None,
    scraper_keys: Optional[List[str]] = None,
    google_client: Optional[httpx.AsyncClient] = None,
    log: Optional[Callable] = None,
) -> str:
    """Execute a web search using S2 (academic) + Google (general) fallback.

    Returns formatted text results suitable for feeding back to an LLM.
    """
    results = []

    # 1. Try S2 paper search (query might be a paper title)
    if s2_client:
        try:
            paper = await s2_client.search_paper(query)
            if paper:
                authors_list = ", ".join(
                    a["name"] for a in paper.get("authors", [])
                )
                venue = paper.get("venue", "")
                info = (
                    f"[Semantic Scholar · 论文]\n"
                    f"标题: {paper.get('title', '')}\n"
                    f"年份: {paper.get('year', '')}\n"
                    f"作者: {authors_list}\n"
                    f"引用数: {paper.get('cited_by_count', 0)}\n"
                )
                if venue:
                    info += f"发表: {venue}\n"
                results.append(info)
        except Exception as e:
            if log:
                log(f"  [tool-loop] S2 paper search error: {e}")

    # 2. Try S2 author search (query might be an author name, or paper search empty)
    if s2_client:
        try:
            authors = await s2_client.search_author(query, limit=5)
            if authors:
                lines = ["[Semantic Scholar · 作者搜索]"]
                for a in authors[:5]:
                    affil = a.get("affiliation", "") or "未知"
                    lines.append(
                        f"- {a['name']} (h-index: {a.get('h_index', 0)}, "
                        f"引用: {a.get('citation_count', 0)}, 机构: {affil})"
                    )
                results.append("\n".join(lines))
        except Exception as e:
            if log:
                log(f"  [tool-loop] S2 author search error: {e}")

    # 3. Fallback: Google via ScraperAPI, then Bing (free, no key)
    if not results:
        gc = google_client or httpx.AsyncClient(timeout=25.0)
        tried_google = False
        if scraper_keys:
            tried_google = True
            try:
                google_result = await _google_search(query, scraper_keys, gc)
                if google_result:
                    results.append(google_result)
            except Exception as e:
                if log:
                    log(f"  [tool-loop] Google search error: {e}")

        # Bing fallback: when no ScraperAPI keys, or Google returned nothing
        if not results:
            try:
                bing_result = await _bing_search(query, gc)
                if bing_result:
                    results.append(bing_result)
            except Exception as e:
                if log:
                    log(f"  [tool-loop] Bing search error: {e}")

    return (
        "\n\n".join(results)
        if results
        else f"未找到与「{query}」相关的搜索结果"
    )


# ── 全局共享 httpx client (避免每次 chat_with_search 新建连接) ─────
_shared_http_client: Optional[httpx.AsyncClient] = None
_shared_http_lock = asyncio.Lock()


async def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        async with _shared_http_lock:
            if _shared_http_client is None or _shared_http_client.is_closed:
                _shared_http_client = httpx.AsyncClient(
                    trust_env=False, timeout=25.0,
                    limits=httpx.Limits(
                        max_connections=20,
                        max_keepalive_connections=5,
                        keepalive_expiry=30.0,
                    ),
                )
    return _shared_http_client


async def _call_llm_with_retry(
    client, model, msgs, log, max_tokens, tools=None, **kwargs,
):
    """Call LLM with 429-aware retry (exponential backoff)."""
    max_retries = 4
    for attempt in range(max_retries + 1):
        await _rate_limit()
        try:
            params = dict(model=model, messages=msgs, max_tokens=max_tokens, **kwargs)
            if tools:
                params["tools"] = tools
                params["tool_choice"] = "auto"
            resp = await client.chat.completions.create(**params)
            return resp
        except Exception as e:
            err_str = str(e)
            is_429 = "429" in err_str or "rate" in err_str.lower() or "throttl" in err_str.lower()
            if is_429 and attempt < max_retries:
                wait = (2 ** attempt) * 5  # 5s, 10s, 20s, 40s
                if log:
                    log(f"  [tool-loop] 429 限流，{wait}s 后重试 ({attempt+1}/{max_retries})")
                await asyncio.sleep(wait)
                continue
            raise
    raise RuntimeError("LLM call failed after max retries")


async def chat_with_search(
    client,  # AsyncOpenAI
    model: str,
    messages: list[dict],
    s2_client: Optional[S2Client] = None,
    scraper_keys: Optional[List[str]] = None,
    max_tool_rounds: int = 3,
    log: Optional[Callable] = None,
    max_tokens: int = 4000,
    overall_timeout: float = 300.0,
    **kwargs,
) -> str:
    """Run a chat completion with function-calling web search loop.

    Sends messages with ``search_web`` tool.  If the LLM calls
    ``search_web``, executes the search and feeds results back, up to
    *max_tool_rounds* times.  Returns the final assistant message content,
    or ``'ERROR'`` on failure.

    *overall_timeout* caps the total wall-clock time for all rounds.
    """
    google_client = await _get_shared_http_client()
    try:
        return await asyncio.wait_for(
            _chat_with_search_inner(
                client, model, messages, s2_client, scraper_keys,
                max_tool_rounds, log, max_tokens, google_client, **kwargs,
            ),
            timeout=overall_timeout,
        )
    except asyncio.TimeoutError:
        if log:
            log(f"  [tool-loop] 超时 ({overall_timeout}s)，返回已有信息")
        return "ERROR"
    except Exception as e:
        if log:
            log(f"  [tool-loop] error: {e}")
        return "ERROR"


async def _chat_with_search_inner(
    client, model, messages, s2_client, scraper_keys,
    max_tool_rounds, log, max_tokens, google_client, **kwargs,
) -> str:
    """Inner loop (no timeout wrapper, called by chat_with_search)."""
    # Add system instruction to guide the model
    sys_msg = {
        "role": "system",
        "content": (
            "You have access to a search_web tool. Use it to find information, "
            "then provide a definitive answer. NEVER respond with phrases like "
            "'let me try' or 'let me search' without calling the tool. "
            "If you have enough information from search results, provide your "
            "final answer directly. If you cannot find information, state what "
            "you know and what is unknown."
        ),
    }
    msgs = [sys_msg] + list(messages)
    for round_idx in range(max_tool_rounds):
        resp = await _call_llm_with_retry(
            client, model, msgs, log, max_tokens,
            tools=SEARCH_TOOLS, **kwargs,
        )
        msg = resp.choices[0].message

        # No tool calls -> check if it's a real answer or "let me try..."
        if not msg.tool_calls:
            content = _extract_content(msg)
            # Check for non-answer patterns
            content_lower = content.lower().strip()
            is_non_answer = any(
                content_lower.startswith(p)
                for p in [
                    "let me try",
                    "let me search",
                    "i need to",
                    "i should",
                    "the search",
                    "the results aren't",
                    "the searches didn't",
                ]
            )
            if is_non_answer and round_idx < max_tool_rounds - 1:
                # Force a final answer by adding instruction
                msgs.append({
                    "role": "assistant",
                    "content": content,
                })
                msgs.append({
                    "role": "user",
                    "content": (
                        "请根据以上搜索结果，直接给出最终答案。"
                        "不要建议进一步搜索，根据已有信息提供最佳回答。"
                        "如果搜索结果不足，请说明哪些信息已知、哪些未知。"
                    ),
                })
                continue
            return content

        # Append assistant message with tool_calls
        msgs.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        # Execute each tool call
        for tc in msg.tool_calls:
            if tc.function.name == "search_web":
                try:
                    args = json.loads(tc.function.arguments)
                    query = args.get("query", "")
                except Exception:
                    query = ""
                if log:
                    log(f"  [tool-loop] search_web: {query[:80]}")
                result = await execute_web_search(
                    query,
                    s2_client,
                    scraper_keys,
                    google_client,
                    log,
                )
                if log:
                    log(f"  [tool-loop] result ({len(result)} chars)")
            else:
                result = f"未知工具: {tc.function.name}"

            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": result,
                }
            )

    # Max rounds reached -> get final response without tools
    msgs.append({
        "role": "user",
        "content": (
            "请根据以上所有搜索结果，给出最终的完整答案。"
            "不要再建议搜索，直接整理已有信息。"
        ),
    })
    resp = await _call_llm_with_retry(
        client, model, msgs, log, max_tokens, **kwargs,
    )
    return _extract_content(resp.choices[0].message)
