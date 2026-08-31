from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


PROVIDERS: dict[str, dict[str, Any]] = {
    "llm.primary": {
        "label": "统一 LLM",
        "kind": "llm",
        "description": "负责飞书指令理解，并供日报精筛、论文总结、综述写作、查引用和小红书共用。",
        "fields": ["enabled", "base_url", "model", "api_key"],
        "secret_fields": ["api_key"],
    },
    "embedding.paper": {
        "label": "论文 Embedding",
        "kind": "embedding",
        "description": "把检索意图编码成向量，用于日报和综述的 Supabase 语义召回。",
        "fields": ["enabled", "base_url", "model", "api_key"],
        "secret_fields": ["api_key"],
    },
    "rerank.paper": {
        "label": "论文 Reranker",
        "kind": "rerank",
        "description": "对融合后的候选论文做专用相关性重排，再交给 LLM 精筛。",
        "fields": ["enabled", "base_url", "model", "api_key"],
        "secret_fields": ["api_key"],
    },
    "supabase.arxiv": {
        "label": "arXiv Supabase 论文池",
        "kind": "supabase",
        "description": "保存近期 arXiv 元数据和向量，提供 BM25 与向量召回。",
        "fields": ["enabled", "base_url", "anon_key", "papers_table", "bm25_rpc", "vector_rpc"],
        "secret_fields": ["anon_key"],
    },
    "web.exa": {
        "label": "Exa MCP 联网搜索",
        "kind": "exa_mcp",
        "description": "在执行任务前搜索近期概念、方法和 benchmark，用于富化用户需求。",
        "fields": ["enabled", "base_url"],
        "secret_fields": [],
    },
    "fetch.jina": {
        "label": "Jina Reader",
        "kind": "jina",
        "description": "把公开网页或 PDF 转换成适合 LLM 阅读的结构化文本。",
        "fields": ["enabled", "base_url"],
        "secret_fields": [],
    },
    "academic.deepxiv": {
        "label": "DeepXiv",
        "kind": "http",
        "description": "为领域综述补充外部语义检索、被引数和更新较新的论文。",
        "fields": ["enabled", "base_url", "api_key"],
        "secret_fields": ["api_key"],
    },
    "citation.scraperapi": {
        "label": "ScraperAPI",
        "kind": "scraperapi",
        "description": "帮助 CitationClaw 稳定抓取 Google Scholar 被引列表和学者主页。",
        "fields": ["enabled", "api_key"],
        "secret_fields": ["api_key"],
    },
    "citation.search_llm": {
        "label": "CitationClaw Search LLM",
        "kind": "llm",
        "description": "带联网搜索能力的模型，用于查找作者、机构和学术头衔；与统一轻量 LLM 分开配置。",
        "fields": ["enabled", "base_url", "model", "api_key"],
        "secret_fields": ["api_key"],
    },
    "citation.semantic_scholar": {
        "label": "Semantic Scholar",
        "kind": "semantic_scholar",
        "description": "补充引用关系、论文和作者元数据，并提高 PDF 定位成功率。",
        "fields": ["enabled", "api_key"],
        "secret_fields": ["api_key"],
    },
    "citation.openalex": {
        "label": "OpenAlex",
        "kind": "openalex",
        "description": "补充作者、机构和论文元数据；邮箱用于礼貌池请求，不是密钥。",
        "fields": ["enabled", "email"],
        "secret_fields": [],
    },
    "citation.wos": {
        "label": "Web of Science",
        "kind": "wos",
        "description": "可选的结构化作者与论文信息来源，优先级高于通用网页抽取。",
        "fields": ["enabled", "api_key"],
        "secret_fields": ["api_key"],
    },
    "document.mineru": {
        "label": "MinerU Cloud（备用）",
        "kind": "configured_only",
        "description": "仅作为 CitationClaw 大文件解析备用；论文日报主路线使用本地 Docling。",
        "fields": ["enabled", "base_url", "api_key"],
        "secret_fields": ["api_key"],
    },
}


PIPELINES = [
    {
        "id": "connect",
        "title": "飞书机器人与指令理解",
        "steps": [
            ["llm.primary", "理解用户意图并决定调用哪个固定工具"],
            ["web.exa", "遇到近期或含糊主题时联网搜索"],
            ["fetch.jina", "读取搜索结果中选中的网页正文"],
        ],
    },
    {
        "id": "daily",
        "title": "论文日报",
        "steps": [
            ["llm.primary", "扩充检索意图和关键词"],
            ["supabase.arxiv", "执行 BM25 关键词召回"],
            ["embedding.paper", "编码查询并执行向量语义召回"],
            ["rerank.paper", "对 RRF 候选池做相关性重排"],
            ["llm.primary", "精筛、摘要和日报写作"],
            ["fetch.jina", "按需读取论文结构化正文"],
        ],
    },
    {
        "id": "summary",
        "title": "单篇论文总结",
        "steps": [
            ["fetch.jina", "尝试读取论文网页或 PDF 文本"],
            ["llm.primary", "生成速览、精读内容和图表解释"],
        ],
        "note": "arXiv 元数据/PDF 免费直连；图表提取使用本地 Docling，不需要 API Key。",
    },
    {
        "id": "survey",
        "title": "领域综述",
        "steps": [
            ["llm.primary", "规划英文查询并定义任务范式"],
            ["supabase.arxiv", "召回时间窗内候选论文"],
            ["embedding.paper", "执行语义召回"],
            ["academic.deepxiv", "可选补充新论文和被引信息"],
            ["rerank.paper", "精选最终逐篇分析的论文"],
            ["llm.primary", "逐篇抽取、聚类、写作和审校"],
        ],
        "note": "Kaggle 本地大索引已默认关闭，不进入 demo 部署。",
    },
    {
        "id": "citation",
        "title": "查引用 CitationClaw",
        "steps": [
            ["citation.scraperapi", "抓取 Google Scholar 被引列表"],
            ["citation.search_llm", "联网搜索作者、机构与学术头衔"],
            ["citation.semantic_scholar", "补充引用、作者和 PDF 元数据"],
            ["citation.openalex", "补充作者及机构信息"],
            ["citation.wos", "可选结构化权威元数据"],
            ["llm.primary", "作者分析、引用语境总结和报告生成"],
            ["document.mineru", "可选的大文件解析备用路线"],
        ],
    },
    {
        "id": "xhs",
        "title": "小红书贴文",
        "steps": [["llm.primary", "生成页面结构、文案和排版数据"]],
    },
]


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "connect.providers.v1",
    "providers": {
        "llm.primary": {"enabled": True, "base_url": "", "model": "", "api_key": ""},
        "embedding.paper": {
            "enabled": True,
            "base_url": "https://zwwen.online/embed",
            "model": "BAAI/bge-small-en-v1.5",
            "api_key": "",
        },
        "rerank.paper": {
            "enabled": True,
            "base_url": "https://zwwen.online/rerank",
            "model": "Qwen/Qwen3-Reranker-0.6B",
            "api_key": "",
        },
        "supabase.arxiv": {
            "enabled": True,
            "base_url": "",
            "anon_key": "",
            "papers_table": "arxiv_papers",
            "bm25_rpc": "match_arxiv_papers_bm25",
            "vector_rpc": "match_arxiv_papers_exact",
        },
        "web.exa": {
            "enabled": True,
            "base_url": "https://mcp.exa.ai/mcp?tools=web_search_exa",
        },
        "fetch.jina": {"enabled": True, "base_url": "https://r.jina.ai/"},
        "academic.deepxiv": {
            "enabled": False,
            "base_url": "https://data.rag.ac.cn",
            "api_key": "",
        },
        "citation.scraperapi": {"enabled": False, "api_key": ""},
        "citation.search_llm": {
            "enabled": False,
            "base_url": "https://api.gpt.ge/v1/",
            "model": "gemini-3-flash-preview-search",
            "api_key": "",
        },
        "citation.semantic_scholar": {"enabled": False, "api_key": ""},
        "citation.openalex": {"enabled": True, "email": ""},
        "citation.wos": {"enabled": False, "api_key": ""},
        "document.mineru": {
            "enabled": False,
            "base_url": "https://mineru.net",
            "api_key": "",
        },
    },
    "paper_sources": {
        "arxiv": {"enabled": True},
        "deepxiv": {"enabled": False},
        "kaggle": {"enabled": False},
    },
    "modules": {
        "daily-paper": {
            "local": {
                "schedule": {"enabled": False, "time": "03:00"},
                "rerank": {"profile": "public-zwwen-rerank"},
                "recall": {"mode": "hybrid"},
            },
            "subscriptions": {"intent_profiles": []},
            "recommend_setting": {
                "deep_dive_base": 5,
                "quick_skim_base": 10,
                "deep_dive_unlimited": False,
            },
            "source_backends": {},
            "supabase": {},
        },
        "citationclaw": {},
    },
    "runtime_defaults": {"daily-paper": {}, "citationclaw": {}},
}


def catalog_payload() -> dict[str, Any]:
    providers = []
    for provider_id, definition in PROVIDERS.items():
        providers.append({"id": provider_id, **definition})
    return {"providers": providers, "pipelines": PIPELINES}


def merged_defaults(config: Mapping[str, Any] | None) -> dict[str, Any]:
    return _deep_merge(DEFAULT_CONFIG, dict(config or {}), preserve_blank_secrets=False)


def public_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    full = merged_defaults(config)
    result = json.loads(json.dumps(full, ensure_ascii=False))
    providers = result.get("providers") or {}
    original = full.get("providers") or {}
    for provider_id, definition in PROVIDERS.items():
        item = providers.get(provider_id) if isinstance(providers.get(provider_id), dict) else {}
        source = original.get(provider_id) if isinstance(original.get(provider_id), dict) else {}
        secret_fields = definition.get("secret_fields") or []
        item["configured"] = all(bool(str(source.get(name) or "").strip()) for name in secret_fields)
        for name in secret_fields:
            item[name] = ""
        providers[provider_id] = item
    result["providers"] = providers
    return result


def merge_public_update(current: Mapping[str, Any] | None, update: Mapping[str, Any]) -> dict[str, Any]:
    return _deep_merge(merged_defaults(current), dict(update), preserve_blank_secrets=True)


def installation_suffix(site_id: str) -> str:
    for prefix in ("connect-config-", "daily-paper-", "citationclaw-", "xhs-agent-"):
        if site_id.startswith(prefix):
            return site_id[len(prefix):]
    return site_id


def canonical_site_id(site_id: str) -> str:
    return f"connect-config-{installation_suffix(site_id)}"


def daily_public_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project the installation config into Daily Paper's native schema."""
    full = merged_defaults(config)
    module = ((full.get("modules") or {}).get("daily-paper") or {})
    result = json.loads(json.dumps(module, ensure_ascii=False))
    local = result.setdefault("local", {})
    llm = ((full.get("providers") or {}).get("llm.primary") or {})
    local["chat"] = {
        "base_url": llm.get("base_url", ""),
        "model": llm.get("model", ""),
        "api_key": llm.get("api_key", ""),
        "api_key_configured": bool(str(llm.get("api_key") or "").strip()),
    }
    # The unified provider is the source of truth. Project it into Daily
    # Paper's native source_backends schema so the original UI and pipeline
    # see the same Supabase configuration.
    supabase = ((full.get("providers") or {}).get("supabase.arxiv") or {})
    source_backends = result.setdefault("source_backends", {})
    existing_arxiv = (
        source_backends.get("arxiv")
        if isinstance(source_backends.get("arxiv"), dict)
        else {}
    )
    source_backends["arxiv"] = {
        **existing_arxiv,
        "enabled": supabase.get("enabled", True) is not False,
        "url": supabase.get("base_url", ""),
        "anon_key": supabase.get("anon_key", ""),
        "anon_key_configured": bool(str(supabase.get("anon_key") or "").strip()),
        "papers_table": supabase.get("papers_table", "arxiv_papers"),
        "use_bm25_rpc": True,
        "bm25_rpc": supabase.get("bm25_rpc", "match_arxiv_papers_bm25"),
        "use_vector_rpc": True,
        "vector_rpc": supabase.get("vector_rpc", "match_arxiv_papers_exact"),
        "vector_rpc_exact": supabase.get("vector_rpc", "match_arxiv_papers_exact"),
    }
    return result


def merge_daily_update(config: Mapping[str, Any] | None, update: Mapping[str, Any]) -> dict[str, Any]:
    full = merged_defaults(config)
    patch = json.loads(json.dumps(dict(update), ensure_ascii=False))
    local = patch.get("local") if isinstance(patch.get("local"), dict) else {}
    chat = local.pop("chat", None) if isinstance(local, dict) else None
    if isinstance(chat, dict):
        full = merge_public_update(full, {"providers": {"llm.primary": {
            "base_url": chat.get("base_url", ""), "model": chat.get("model", ""),
            "api_key": chat.get("api_key", ""), "enabled": True,
        }}})
    source_backends = patch.get("source_backends") if isinstance(patch.get("source_backends"), dict) else {}
    arxiv = source_backends.get("arxiv") if isinstance(source_backends.get("arxiv"), dict) else None
    if isinstance(arxiv, dict):
        full = merge_public_update(full, {"providers": {"supabase.arxiv": {
            "enabled": arxiv.get("enabled", True),
            "base_url": arxiv.get("url", ""),
            "anon_key": arxiv.get("anon_key", ""),
            "papers_table": arxiv.get("papers_table", "arxiv_papers"),
            "bm25_rpc": arxiv.get("bm25_rpc", "match_arxiv_papers_bm25"),
            "vector_rpc": arxiv.get("vector_rpc") or arxiv.get("vector_rpc_exact", "match_arxiv_papers_exact"),
        }}})
        # Do not retain a second authoritative copy under modules.daily-paper.
        source_backends = dict(source_backends)
        source_backends.pop("arxiv", None)
        patch["source_backends"] = source_backends
    if isinstance(patch.get("local"), dict):
        patch["local"] = local
    return merge_public_update(full, {"modules": {"daily-paper": patch}})


def citation_public_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project shared LLM into CitationClaw's light model fields."""
    full = merged_defaults(config)
    result = json.loads(json.dumps(((full.get("modules") or {}).get("citationclaw") or {}), ensure_ascii=False))
    llm = ((full.get("providers") or {}).get("llm.primary") or {})
    providers = full.get("providers") or {}
    scraper = providers.get("citation.scraperapi") or {}
    search_llm = providers.get("citation.search_llm") or {}
    result.update({
        "light_api_key": llm.get("api_key", ""),
        "light_base_url": llm.get("base_url", ""),
        "dashboard_model": llm.get("model", ""),
        "scraper_api_keys": [scraper.get("api_key")] if scraper.get("api_key") else [],
        "openai_api_key": search_llm.get("api_key", ""),
        "openai_base_url": search_llm.get("base_url", ""),
        "openai_model": search_llm.get("model", ""),
        "s2_api_key": (providers.get("citation.semantic_scholar") or {}).get("api_key", ""),
        "openalex_email": (providers.get("citation.openalex") or {}).get("email", ""),
        "wos_api_key": (providers.get("citation.wos") or {}).get("api_key", ""),
        "mineru_api_token": (providers.get("document.mineru") or {}).get("api_key", ""),
    })
    result["_configured_secrets"] = {
        "light_api_key": bool(str(llm.get("api_key") or "").strip()),
        "openai_api_key": bool(str(result.get("openai_api_key") or "").strip()),
        "scraper_api_keys": bool(result.get("scraper_api_keys")),
    }
    result["_runtime_defaults"] = ((full.get("runtime_defaults") or {}).get("citationclaw") or {})
    return result


def merge_citation_update(config: Mapping[str, Any] | None, update: Mapping[str, Any]) -> dict[str, Any]:
    full = merged_defaults(config)
    patch = {k: v for k, v in dict(update).items() if not str(k).startswith("_")}
    light = {"api_key": patch.pop("light_api_key", ""),
             "base_url": patch.pop("light_base_url", ""),
             "model": patch.pop("dashboard_model", "")}
    full = merge_public_update(full, {"providers": {"llm.primary": {**light, "enabled": True}}})
    scraper_keys = patch.pop("scraper_api_keys", [])
    search_key = patch.pop("openai_api_key", "")
    search_base = patch.pop("openai_base_url", "")
    search_model = patch.pop("openai_model", "")
    provider_patch = {
        "citation.scraperapi": {
            "api_key": scraper_keys[0] if scraper_keys else "",
            **({"enabled": True} if scraper_keys else {}),
        },
        "citation.search_llm": {
            "api_key": search_key, "base_url": search_base, "model": search_model,
            **({"enabled": True} if search_key else {}),
        },
        "citation.semantic_scholar": {"api_key": patch.pop("s2_api_key", "")},
        "citation.openalex": {"email": patch.pop("openalex_email", ""), "enabled": True},
        "citation.wos": {"api_key": patch.pop("wos_api_key", "")},
        "document.mineru": {"api_key": patch.pop("mineru_api_token", "")},
    }
    full = merge_public_update(full, {"providers": provider_patch})
    return merge_public_update(full, {"modules": {"citationclaw": patch}})


def _deep_merge(base: dict[str, Any], update: dict[str, Any], *, preserve_blank_secrets: bool) -> dict[str, Any]:
    result = json.loads(json.dumps(base, ensure_ascii=False))
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value, preserve_blank_secrets=preserve_blank_secrets)
        elif preserve_blank_secrets and _secret_name(str(key)) and value in ("", []):
            if key not in result:
                result[key] = value
        else:
            result[key] = value
    return result


def _secret_name(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return any(part in normalized for part in ("api_key", "anon_key", "token", "secret", "password"))


def probe_provider(provider_id: str, config: Mapping[str, Any]) -> dict[str, Any]:
    definition = PROVIDERS.get(provider_id)
    if not definition:
        raise ValueError("未知 Provider")
    provider = ((config.get("providers") or {}).get(provider_id) or {})
    if not isinstance(provider, Mapping):
        raise ValueError("Provider 配置格式错误")
    if not bool(provider.get("enabled", True)):
        return {"ok": False, "status": "disabled", "message": "Provider 当前未启用"}
    started = time.monotonic()
    kind = str(definition.get("kind") or "")
    if kind == "configured_only":
        configured = all(bool(str(provider.get(name) or "").strip()) for name in definition.get("secret_fields") or [])
        return {
            "ok": configured,
            "status": "configured" if configured else "missing_credential",
            "message": "已保存凭据；demo 版暂不从公网服务器调用此 Provider" if configured else "缺少凭据",
        }
    try:
        if kind == "llm":
            base = _safe_public_url(str(provider.get("base_url") or ""))
            key = _required(provider, "api_key")
            response = _request_json(_join_openai(base, "models"), headers=_bearer(key), timeout=20)
            count = len(response.get("data") or []) if isinstance(response, dict) else 0
            message = f"模型列表连接成功，共返回 {count} 个模型"
        elif kind == "embedding":
            base = _safe_public_url(str(provider.get("base_url") or ""))
            endpoint = base if base.rstrip("/").endswith("/embed") else base.rstrip("/") + "/embed"
            response = _request_json(endpoint, method="POST", headers=_bearer(str(provider.get("api_key") or "")), payload={"texts": ["health check"]}, timeout=30)
            vectors = response.get("embeddings") if isinstance(response, dict) else None
            if not isinstance(vectors, list) or not vectors:
                raise ValueError("服务未返回 embeddings")
            message = "Embedding 测试向量生成成功"
        elif kind == "rerank":
            endpoint = _safe_public_url(str(provider.get("base_url") or ""))
            payload = {
                "query": "machine learning",
                "documents": ["machine learning paper", "cooking recipe"],
                "top_n": 1,
            }
            model = str(provider.get("model") or "").strip()
            if model:
                payload["model"] = model
            _request_json(endpoint, method="POST", headers=_bearer(str(provider.get("api_key") or "")), payload=payload, timeout=30)
            message = "Reranker 测试排序成功"
        elif kind == "supabase":
            base = _safe_public_url(str(provider.get("base_url") or ""))
            key = _required(provider, "anon_key")
            table = urllib.parse.quote(str(provider.get("papers_table") or "arxiv_papers"), safe="")
            _request_json(base.rstrip("/") + f"/rest/v1/{table}?select=*&limit=1", headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=20)
            message = "Supabase 论文表读取成功"
        elif kind == "semantic_scholar":
            key = str(provider.get("api_key") or "").strip()
            headers = {"x-api-key": key} if key else {}
            _request_json("https://api.semanticscholar.org/graph/v1/paper/ARXIV:1706.03762?fields=title", headers=headers, timeout=20)
            message = "Semantic Scholar 查询成功"
        elif kind == "openalex":
            email = str(provider.get("email") or "").strip()
            url = "https://api.openalex.org/works/W2741809807"
            if email:
                url += "?mailto=" + urllib.parse.quote(email)
            _request_json(url, timeout=20)
            message = "OpenAlex 查询成功"
        elif kind == "scraperapi":
            key = _required(provider, "api_key")
            _request_json("https://api.scraperapi.com/account?api_key=" + urllib.parse.quote(key), timeout=20)
            message = "ScraperAPI 账户接口连接成功"
        elif kind == "wos":
            key = _required(provider, "api_key")
            _request_json("https://api.clarivate.com/apis/wos-starter/v1/documents?q=TS%3Dmachine%20learning&limit=1", headers={"X-ApiKey": key}, timeout=20)
            message = "Web of Science 查询成功"
        elif kind == "exa_mcp":
            url = _safe_public_url(str(provider.get("base_url") or ""))
            body = json.dumps(
                {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "research-connect-probe", "version": "0.1"}},
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "User-Agent": "research-connect-config/0.1"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                if not response.read(512).strip():
                    raise ValueError("Exa MCP 返回空响应")
            message = "Exa MCP 握手成功"
        elif kind == "jina":
            base = _safe_public_url(str(provider.get("base_url") or ""))
            request = urllib.request.Request(
                base.rstrip("/") + "/https://example.com",
                headers={"Accept": "text/plain", "User-Agent": "research-connect-config/0.1"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                if not response.read(256).strip():
                    raise ValueError("Jina Reader 返回空内容")
            message = "Jina Reader 读取测试页面成功"
        else:
            url = _safe_public_url(str(provider.get("base_url") or ""))
            request = urllib.request.Request(url, headers={"User-Agent": "research-connect-config/0.1"})
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read(64)
            message = "服务地址可以访问"
        return {
            "ok": True,
            "status": "ready",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "message": message,
        }
    except (ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            detail = exc.read(300).decode("utf-8", errors="replace")
            message = f"HTTP {exc.code}: {detail or exc.reason}"
        else:
            message = str(exc)
        return {
            "ok": False,
            "status": "unreachable",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "message": message[:500],
        }


def _required(provider: Mapping[str, Any], name: str) -> str:
    value = str(provider.get(name) or "").strip()
    if not value:
        raise ValueError(f"缺少 {name}")
    return value


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


def _join_openai(base: str, suffix: str) -> str:
    root = base.rstrip("/")
    return root + "/" + suffix if root.endswith("/v1") else root + "/v1/" + suffix


def _safe_public_url(raw: str) -> str:
    text = raw.strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("探测地址必须是公网 HTTPS URL")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise ValueError(f"域名解析失败：{exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("拒绝探测内网、回环或保留地址")
    return text


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    payload: Mapping[str, Any] | None = None,
    timeout: int = 20,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Accept": "application/json", "User-Agent": "research-connect-config/0.1"}
    request_headers.update(dict(headers or {}))
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(2_000_000)
    return json.loads(raw.decode("utf-8", errors="replace") or "{}")
