from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


# Public demo services published by daily-paper-reader. These credentials are
# intentionally public client credentials, not Supabase service-role secrets.
# Users may replace them with a compatible private service in /config.
PUBLIC_ZWWEN_API_KEY = "26932a86d772001af60cbd9d2c162bfda3a90e094f797f3d6806f6077478b27a"
PUBLIC_ARXIV_SUPABASE_URL = "https://lyucdwgefyfbmaiopjbk.supabase.co"
PUBLIC_ARXIV_SUPABASE_KEY = "sb_publishable_lX-oi64Uxyd7SIVv3_w2Uw_MTOojeKq"


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
        "description": "把检索意图编码成向量，用于日报和综述的 Supabase 语义召回；Demo 已预置上游公开服务。",
        "fields": ["enabled", "base_url", "model", "api_key"],
        "secret_fields": ["api_key"],
    },
    "rerank.paper": {
        "label": "论文 Reranker",
        "kind": "rerank",
        "description": "对融合后的候选论文做专用相关性重排，再交给 LLM 精筛；Demo 已预置上游公开服务。",
        "fields": ["enabled", "base_url", "model", "api_key"],
        "secret_fields": ["api_key"],
    },
    "supabase.arxiv": {
        "label": "arXiv Supabase 近期论文池",
        "kind": "supabase",
        "description": "保存近期 arXiv 元数据和向量，提供 BM25 与向量召回；Demo 已预置上游公开只读论文池。",
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
        "description": "论文、作者和引用关系主来源；默认启用并可匿名访问，填写 API Key 可获得更稳定的配额。",
        "fields": ["enabled", "api_key"],
        "secret_fields": ["api_key"],
        "credential_optional": True,
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
        "id": "connect", "title": "飞书机器人", "subtitle": "从自然语言到固定工具调用",
        "steps": [
            {"label": "理解指令", "description": "识别任务、补全参数并决定固定工具。", "providers": ["llm.primary"]},
            {"label": "联网富化", "description": "主题含糊或依赖近期信息时搜索相关概念。", "providers": ["web.exa"], "optional": True},
            {"label": "读取网页", "description": "把选中的网页转换为便于模型阅读的文本。", "providers": ["fetch.jina"], "optional": True},
            {"label": "调用工具", "description": "调用日报、总结、综述、小红书等受控工具。", "local": True},
        ],
    },
    {
        "id": "daily", "title": "论文日报", "subtitle": "召回、排序、精筛与逐篇阅读",
        "steps": [
            {"label": "查询规划", "description": "把研究需求扩展成关键词与语义查询。", "providers": ["llm.primary"]},
            {"parallel": [
                {"label": "BM25 召回", "description": "从近期论文池按关键词召回候选。", "sources": ["arxiv"]},
                {"label": "语义召回", "description": "编码查询向量并从论文池寻找语义近邻。", "providers": ["embedding.paper"], "sources": ["arxiv"]},
            ], "parallel_label": "并行双路"},
            {"label": "RRF 融合", "description": "在本机融合关键词与向量两路排名。", "local": True},
            {"label": "专用重排", "description": "用论文 Reranker 重排融合候选。", "providers": ["rerank.paper"]},
            {"label": "LLM 精筛", "description": "批量相关性评分并生成中英文摘要。", "providers": ["llm.primary"]},
            {"label": "阅读与成文", "description": "读取正文、用 Docling 处理图表并生成日报页面。", "providers": ["llm.primary", "fetch.jina"], "sources": ["arxiv_direct"]},
        ],
    },
    {
        "id": "summary", "title": "单篇论文总结", "subtitle": "链接或 PDF 到结构化论文页",
        "steps": [
            {"label": "识别论文", "description": "解析 arXiv 链接、网页地址或用户上传的 PDF。", "sources": ["arxiv_direct"], "local": True},
            {"label": "提取正文", "description": "优先 Jina 读取公开页面，失败时本机解析 PDF。", "providers": ["fetch.jina"], "optional": True},
            {"label": "Docling 图表", "description": "在本机提取 PDF 结构、图片和表格。", "local": True},
            {"label": "总结与写作", "description": "生成速览、精读内容与图表解释。", "providers": ["llm.primary"]},
            {"label": "写入论文页", "description": "保存 Markdown 并注册到原版论文站点。", "local": True},
        ],
    },
    {
        "id": "survey", "title": "领域综述", "subtitle": "多源召回到带引用的领域报告",
        "steps": [
            {"label": "查询规划", "description": "定义任务范式并生成英文检索查询组。", "providers": ["llm.primary"]},
            {"label": "多源召回", "description": "合并近期论文池、DeepXiv 与可选 Kaggle 历史快照。", "sources": ["arxiv"], "optional_sources": ["deepxiv", "kaggle"]},
            {"label": "语义粗排", "description": "对候选编码并缩小到可重排规模。", "providers": ["embedding.paper"]},
            {"label": "专用重排", "description": "精选进入逐篇结构化分析的论文。", "providers": ["rerank.paper"]},
            {"label": "全文深读", "description": "下载核心论文并提取正文，Jina 失败时本机解析。", "providers": ["fetch.jina"], "sources": ["arxiv_direct"], "optional": True},
            {"label": "聚类与写作", "description": "逐簇分析、生成大纲、分节写作并审校。", "providers": ["llm.primary"]},
            {"label": "生成综述页", "description": "校验引用、保存报告并注册侧栏。", "local": True},
        ],
    },
    {
        "id": "citation", "title": "查引用 CitationClaw", "subtitle": "引用关系、作者信息与综合报告",
        "steps": [
            {"label": "确定目标", "description": "接收论文、作者主页或上传的结果文件。", "local": True},
            {"label": "论文与引用", "description": "获取作者论文、施引文献和引用关系。", "sources": ["semantic_scholar"]},
            {"label": "作者元数据", "description": "从 OpenAlex 补充作者、机构与论文信息。", "sources": ["openalex"], "optional": True},
            {"label": "权威元数据", "description": "用 Web of Science 补充结构化信息。", "sources": ["wos"], "optional": True},
            {"label": "联网查证", "description": "搜索作者头衔、机构和外部证据。", "providers": ["citation.search_llm"], "optional": True},
            {"label": "解析全文", "description": "本地解析失败时使用 MinerU Cloud。", "providers": ["document.mineru"], "fallback": True, "optional": True},
            {"label": "分析与报告", "description": "分析引用语境并生成可视化报告。", "providers": ["llm.primary"]},
        ],
    },
    {
        "id": "xhs", "title": "小红书贴文", "subtitle": "轻量文案与页面渲染",
        "steps": [
            {"label": "理解素材", "description": "提取主题、受众、目标与页面风格。", "providers": ["llm.primary"]},
            {"label": "生成文案", "description": "生成标题、正文和多页卡片结构。", "providers": ["llm.primary"]},
            {"label": "本地渲染", "description": "用浏览器在本机渲染并导出图片。", "local": True},
        ],
    },
]


PAPER_SOURCES = [
    {"id": "arxiv", "label": "arXiv Supabase 近期论文池", "description": "日报与综述的 BM25、向量召回主数据源。", "provider": "supabase.arxiv"},
    {"id": "arxiv_direct", "label": "arXiv 官方接口 / 在线 PDF", "description": "免密钥联网访问 arXiv API 和公开 PDF；下载后会缓存在本机，也支持用户另行上传本地 PDF。", "always_ready": True},
    {"id": "deepxiv", "label": "DeepXiv 补充源", "description": "综述可选的新论文、语义召回和被引信息。", "provider": "academic.deepxiv"},
    {"id": "semantic_scholar", "label": "Semantic Scholar", "description": "CitationClaw 的论文、作者和引用关系主来源。", "provider": "citation.semantic_scholar"},
    {"id": "openalex", "label": "OpenAlex", "description": "免密钥补充作者、机构和论文元数据。", "provider": "citation.openalex"},
    {"id": "wos", "label": "Web of Science", "description": "可选的结构化权威论文数据源。", "provider": "citation.wos"},
    {"id": "kaggle", "label": "Kaggle arXiv 本地快照", "description": "约 4GB 的历史论文索引，Demo 默认不部署。", "disabled_hint": True},
]


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "connect.providers.v1",
    "providers": {
        "llm.primary": {"enabled": True, "base_url": "", "model": "", "api_key": ""},
        "embedding.paper": {
            "enabled": True,
            "base_url": "https://zwwen.online/embed",
            "model": "BAAI/bge-small-en-v1.5",
            "api_key": PUBLIC_ZWWEN_API_KEY,
        },
        "rerank.paper": {
            "enabled": True,
            "base_url": "https://zwwen.online/rerank",
            "model": "Qwen/Qwen3-Reranker-0.6B",
            "api_key": PUBLIC_ZWWEN_API_KEY,
        },
        "supabase.arxiv": {
            "enabled": True,
            "base_url": PUBLIC_ARXIV_SUPABASE_URL,
            "anon_key": PUBLIC_ARXIV_SUPABASE_KEY,
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
        "citation.search_llm": {
            "enabled": False,
            "base_url": "https://api.gpt.ge/v1/",
            "model": "gemini-3-flash-preview-search",
            "api_key": "",
        },
        "citation.semantic_scholar": {"enabled": True, "api_key": ""},
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
    return {"providers": providers, "paper_sources": PAPER_SOURCES, "pipelines": PIPELINES}


def merged_defaults(config: Mapping[str, Any] | None) -> dict[str, Any]:
    supplied = json.loads(json.dumps(dict(config or {}), ensure_ascii=False))
    _migrate_public_service_blanks(supplied)
    return _deep_merge(DEFAULT_CONFIG, supplied, preserve_blank_secrets=False)


def _migrate_public_service_blanks(config: dict[str, Any]) -> None:
    """Upgrade old demo configs without overwriting custom provider services."""
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return
    for provider_id, public_url, secret_name in (
        ("embedding.paper", "https://zwwen.online/embed", "api_key"),
        ("rerank.paper", "https://zwwen.online/rerank", "api_key"),
        ("supabase.arxiv", PUBLIC_ARXIV_SUPABASE_URL, "anon_key"),
    ):
        provider = providers.get(provider_id)
        if not isinstance(provider, dict):
            continue
        configured_url = str(provider.get("base_url") or "").strip().rstrip("/")
        if configured_url and configured_url != public_url.rstrip("/"):
            continue
        if not configured_url:
            provider.pop("base_url", None)
        if not str(provider.get(secret_name) or "").strip():
            provider.pop(secret_name, None)


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
        "base_url": _active_value(llm, "base_url"),
        "model": _active_value(llm, "model"),
        "api_key": _active_value(llm, "api_key"),
        "api_key_configured": bool(_active_value(llm, "api_key")),
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
        llm_patch = _nonblank_provider_fields({
            "base_url": chat.get("base_url", ""), "model": chat.get("model", ""),
            "api_key": chat.get("api_key", ""),
        })
        if llm_patch:
            full = merge_public_update(full, {"providers": {"llm.primary": {
                **llm_patch, "enabled": True,
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
    s2 = providers.get("citation.semantic_scholar") or {}
    openalex = providers.get("citation.openalex") or {}
    wos = providers.get("citation.wos") or {}
    mineru = providers.get("document.mineru") or {}
    result.update({
        "light_api_key": _active_value(llm, "api_key"),
        "light_base_url": _active_value(llm, "base_url"),
        "dashboard_model": _active_value(llm, "model"),
        "scraper_api_keys": [_active_value(scraper, "api_key")] if _active_value(scraper, "api_key") else [],
        "openai_api_key": _active_value(search_llm, "api_key"),
        "openai_base_url": _active_value(search_llm, "base_url"),
        "openai_model": _active_value(search_llm, "model"),
        "s2_api_key": _active_value(s2, "api_key"),
        "openalex_email": _active_value(openalex, "email"),
        "wos_api_key": _active_value(wos, "api_key"),
        "mineru_api_token": _active_value(mineru, "api_key"),
    })
    result["_configured_secrets"] = {
        "light_api_key": bool(_active_value(llm, "api_key")),
        "openai_api_key": bool(str(result.get("openai_api_key") or "").strip()),
        "scraper_api_keys": bool(result.get("scraper_api_keys")),
        "s2_api_key": bool(_active_value(s2, "api_key")),
        "wos_api_key": bool(_active_value(wos, "api_key")),
        "mineru_api_token": bool(_active_value(mineru, "api_key")),
    }
    result["_runtime_defaults"] = ((full.get("runtime_defaults") or {}).get("citationclaw") or {})
    return result


def merge_citation_update(config: Mapping[str, Any] | None, update: Mapping[str, Any]) -> dict[str, Any]:
    full = merged_defaults(config)
    patch = {k: v for k, v in dict(update).items() if not str(k).startswith("_")}
    light = _nonblank_provider_fields({
        "api_key": patch.pop("light_api_key", ""),
        "base_url": patch.pop("light_base_url", ""),
        "model": patch.pop("dashboard_model", ""),
    })
    if light:
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
            **_nonblank_provider_fields({"api_key": search_key, "base_url": search_base, "model": search_model}),
            **({"enabled": True} if search_key else {}),
        },
        "citation.semantic_scholar": {
            "api_key": (s2_key := patch.pop("s2_api_key", "")),
            **({"enabled": True} if s2_key else {}),
        },
        "citation.openalex": {
            **_nonblank_provider_fields({"email": (openalex_email := patch.pop("openalex_email", ""))}),
            **({"enabled": True} if openalex_email else {}),
        },
        "citation.wos": {
            "api_key": (wos_key := patch.pop("wos_api_key", "")),
            **({"enabled": True} if wos_key else {}),
        },
        "document.mineru": {
            "api_key": (mineru_key := patch.pop("mineru_api_token", "")),
            **({"enabled": True} if mineru_key else {}),
        },
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


def _active_value(provider: Mapping[str, Any], key: str) -> str:
    if not isinstance(provider, Mapping) or provider.get("enabled", True) is False:
        return ""
    return str(provider.get(key) or "").strip()


def _nonblank_provider_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    """Original module forms use blank as 'keep existing' for provider fields."""
    return {key: value for key, value in values.items() if str(value or "").strip()}


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
            models = sorted({
                str(item.get("id") or "").strip()
                for item in ((response.get("data") or []) if isinstance(response, dict) else [])
                if isinstance(item, Mapping) and str(item.get("id") or "").strip()
            })
            count = len(models)
            if not models:
                raise ValueError("端点返回了空模型列表")
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
            base = _safe_public_url(
                str(provider.get("base_url") or ""), trusted_hosts={"r.jina.ai"}
            )
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
        result = {
            "ok": True,
            "status": "ready",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "message": message,
        }
        if kind == "llm":
            result["models"] = models
        return result
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


def _safe_public_url(raw: str, *, trusted_hosts: set[str] | None = None) -> str:
    text = raw.strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("探测地址必须是公网 HTTPS URL")
    if parsed.hostname.lower() in {host.lower() for host in (trusted_hosts or set())}:
        return text
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
