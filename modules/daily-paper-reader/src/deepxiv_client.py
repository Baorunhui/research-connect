"""DeepXiv 薄 REST 客户端（零新依赖，requests 已在 requirements.txt）。

DeepXiv 是面向 agent 的学术数据服务，当前主力稳定源为 arXiv 预印本
（PMC/bioRxiv/medRxiv 逐步接入中）。本模块只封装综述召回需要的三个端点：
- GET /arxiv/?type=retrieve  语义检索（Bearer token，日期窗 between）
- GET /arxiv/?type=brief     按 arXiv id 取元数据（tldr/被引数等）
- GET /arxiv/?type=raw       按 arXiv id 取全文 markdown

资源：API 文档 https://data.rag.ac.cn/api/docs ；
Token 管理 https://data.rag.ac.cn/token-lookup ；服务监控 https://data.rag.ac.cn/status 。
免费额度：自动注册 token 1000 次/天（无需注册）；网页注册 10000 次/天。

降级约定：本模块只抛 DeepXivError，调用方（survey_pipeline）负责 catch 后
warn + 跳过该召回路，综述继续（镜像 rerank_papers 的降级模式）。
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import requests

DEFAULT_BASE_URL = "https://data.rag.ac.cn"
_REGISTER_DOCS_URL = "https://data.rag.ac.cn/api/docs"
_TOKEN_LOOKUP_URL = "https://data.rag.ac.cn/token-lookup"


class DeepXivError(RuntimeError):
    """DeepXiv 请求失败（带 HTTP status_code，便于调用方区分 401/429/5xx）。"""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def resolve_base_url() -> str:
    return (os.getenv("DEEPXIV_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL


def resolve_token() -> str:
    return (os.getenv("DEEPXIV_TOKEN") or "").strip()


def is_deepxiv_available() -> tuple[bool, str]:
    """探测 token 配置。返回 (可用与否, 说明)。"""
    token = resolve_token()
    if token:
        return True, ""
    return False, (
        "未配置 DEEPXIV_TOKEN，DeepXiv 外部召回已跳过（不影响本地库与引文直取）。"
        f"免费 token 获取：{_TOKEN_LOOKUP_URL}，配置方式见 {_REGISTER_DOCS_URL}"
    )


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _normalize_result_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """把 DeepXiv 检索结果条目归一为 survey_pipeline 的论文 dict 契约。

    paper_id 统一剥离 arXiv 版本号（v1/v2...），避免同一论文多版本重复入池。
    """
    import re

    raw_id = str(item.get("arxiv_id") or "").strip()
    match = re.match(r"^(\d{4}\.\d{4,5})(v\d+)?$", raw_id)
    if match:
        arxiv_id, version = match.group(1), match.group(2) or ""
    else:
        arxiv_id, version = raw_id, ""
    authors = item.get("authors")
    if isinstance(authors, list):
        author_names = [str(a.get("name") or "") for a in authors if isinstance(a, dict) and a.get("name")]
    else:
        author_names = [str(a) for a in (authors or [])]
    published = str(item.get("date") or "").strip()
    link = str(item.get("url") or "").strip() or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "")
    return {
        "paper_id": arxiv_id,
        "title": str(item.get("title") or "").strip(),
        "abstract": str(item.get("abstract") or item.get("tldr") or "").strip(),
        "authors": author_names,
        "published": published[:10],
        "link": link,
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "",
        "source": "deepxiv",
        "citation_count": int(item.get("citation_count") or 0),
        "deepxiv_score": round(float(item.get("score") or 0.0), 6),
        "arxiv_version": version,
    }


class DeepXivClient:
    def __init__(self, token: Optional[str] = None, *, timeout: Optional[int] = None) -> None:
        self.token = (token or resolve_token()).strip()
        if not self.token:
            raise DeepXivError(
                "缺少 DEEPXIV_TOKEN。免费 token（1000 次/天）可在 "
                f"{_TOKEN_LOOKUP_URL} 获取后写入 .env 的 DEEPXIV_TOKEN=。",
                status_code=401,
            )
        self.base_url = resolve_base_url()
        self.timeout = _env_int("DEEPXIV_TIMEOUT", 30)
        self.max_retries = _env_int("DEEPXIV_MAX_RETRIES", 3)
        self.session = requests.Session()
        # 直连（忽略环境/系统代理）：本地代理对国内可达的学术服务常成瓶颈甚至超时；
        # 需要走代理的部署可设 DPR_SURVEY_TRUST_ENV=1 恢复默认行为。
        if not (os.getenv("DPR_SURVEY_TRUST_ENV") or "").strip().lower() in ("1", "true", "yes", "on"):
            self.session.trust_env = False

    # ------------------------------------------------------------------ #
    # 内部请求：429/5xx 指数退避重试
    # ------------------------------------------------------------------ #

    def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/arxiv/"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        last_error: Optional[DeepXivError] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.get(url, headers=headers, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = DeepXivError(f"DeepXiv 网络请求失败：{exc}")
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 8))
                    continue
                raise last_error
            if resp.status_code < 500 and resp.status_code != 429:
                if resp.status_code >= 400:
                    raise DeepXivError(
                        f"DeepXiv 请求失败：HTTP {resp.status_code} {resp.text[:200]}",
                        status_code=resp.status_code,
                    )
                try:
                    return resp.json()
                except ValueError as exc:
                    raise DeepXivError(f"DeepXiv 响应不是合法 JSON：{exc}") from exc
            last_error = DeepXivError(
                f"DeepXiv 服务异常：HTTP {resp.status_code} {resp.text[:200]}",
                status_code=resp.status_code,
            )
            if attempt < self.max_retries:
                time.sleep(min(2**attempt, 8))
        raise last_error or DeepXivError("DeepXiv 请求失败")

    # ------------------------------------------------------------------ #
    # 公开能力
    # ------------------------------------------------------------------ #

    def search(
        self,
        query: str,
        *,
        top_k: int = 30,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        min_citation: Optional[int] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """语义检索。date_start/date_end 为 YYYY-MM-DD，两者同给才生效（between）。"""
        query = str(query or "").strip()
        if not query:
            return []
        if len(query) > 500:
            query = query[:500]
        params: Dict[str, Any] = {
            "type": "retrieve",
            "query": query,
            "top_k": max(1, min(int(top_k or 30), 100)),
            "offset": max(0, int(offset or 0)),
        }
        if date_start and date_end:
            params["date_search_type"] = "between"
            params["date_str"] = [str(date_start), str(date_end)]
        if min_citation:
            params["min_citation"] = int(min_citation)
        data = self._request(params)
        results = data.get("result") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        papers = [_normalize_result_item(item) for item in results if isinstance(item, dict)]
        return [p for p in papers if p["paper_id"] and p["title"]]

    def get_paper_meta(self, arxiv_id: str) -> Optional[Dict[str, Any]]:
        """按 arXiv id 取 brief 元数据（title/tldr/citations/publish_at）。"""
        arxiv_id = str(arxiv_id or "").strip()
        if not arxiv_id:
            return None
        data = self._request({"type": "brief", "arxiv_id": arxiv_id})
        if not isinstance(data, dict) or not data.get("arxiv_id"):
            return None
        return {
            "arxiv_id": str(data.get("arxiv_id") or arxiv_id),
            "title": str(data.get("title") or ""),
            "tldr": str(data.get("tldr") or ""),
            "abstract": str(data.get("abstract") or data.get("tldr") or ""),
            "published": str(data.get("publish_at") or "")[:10],
            "citation_count": int(data.get("citations") or 0),
        }

    def get_paper_markdown(self, arxiv_id: str) -> str:
        """按 arXiv id 取全文 markdown（种子论文与核心论文深读用）。

        网络层异常（超时/连接重置）统一包装成 DeepXivError，
        保证调用方的兜底链路（如种子的 arXiv Atom 回退）能接住。
        """
        arxiv_id = str(arxiv_id or "").strip()
        if not arxiv_id:
            return ""
        url = f"{self.base_url}/arxiv/"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        try:
            resp = self.session.get(url, headers=headers, params={"type": "raw", "arxiv_id": arxiv_id}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DeepXivError(f"DeepXiv 全文请求失败：{exc}") from exc
        if resp.status_code >= 400:
            raise DeepXivError(
                f"DeepXiv 全文获取失败：HTTP {resp.status_code} {resp.text[:200]}",
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise DeepXivError(f"DeepXiv 全文响应不是合法 JSON：{exc}") from exc
        # raw 端点返回结构兼容 {content|markdown|raw|...} 多种字段名
        for key in ("content", "markdown", "raw", "text", "data"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and value.strip():
                return value.strip()
        if isinstance(data, str) and data.strip():
            return data.strip()
        return ""
