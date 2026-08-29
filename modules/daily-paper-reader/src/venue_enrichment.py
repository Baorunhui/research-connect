#!/usr/bin/env python
"""权威来源校验层（venue enrichment）。

在论文 `source` 保持 `arxiv`（PDF/图表提取链路依赖它）的前提下，
通过 Semantic Scholar 按 arXiv ID 反查权威出处（期刊/会议），
把结果增量写入论文 dict 的 `venue` / `venue_status` / `authoritative_url` 字段。

设计约束：
- 纯增量元数据，绝不改变 `source` / `paper_id`（canonical 身份）。
- 受 config 开关 `arxiv_paper_setting.venue_enrichment.enabled` 控制，默认关闭。
- 任何异常 / 未命中都静默降级，绝不改变 source、绝不中断流水线。
- 未命中只判「未找到权威记录」，不判「没投/被拒」。
"""

from __future__ import annotations

import datetime
import os
import re
import time
from typing import Any, Dict, Optional

import requests

USER_AGENT = "daily-paper-reader/1.0 (+https://github.com/ziwenhahaha/daily-paper-reader)"
DEFAULT_TIMEOUT = 20

# Semantic Scholar Graph API：单篇按 arXiv ID 查询。
# 文档：https://api.semanticscholar.org/api-docs/graph#tag/Paper-Data/operation/get_graph_get_paper
S2_PAPER_URL = "https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}"


def log(message: str) -> None:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}", flush=True)


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _headers() -> Dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _authoritative_url(external_ids: Any) -> str:
    """从 Semantic Scholar externalIds 挑选最权威的稳定链接。"""
    if not isinstance(external_ids, dict):
        return ""
    doi = _norm(external_ids.get("DOI"))
    if doi:
        return f"https://doi.org/{doi}"
    openreview = _norm(external_ids.get("OpenReview"))
    if openreview:
        return f"https://openreview.net/forum?id={openreview}"
    acl = _norm(external_ids.get("ACL"))
    if acl:
        return f"https://aclanthology.org/{acl}"
    return ""


def _resolve_venue_name(data: Dict[str, Any]) -> str:
    """依次从 publicationVenue / journal / venue 取权威出处名。"""
    pub_venue = data.get("publicationVenue")
    if isinstance(pub_venue, dict):
        name = _norm(pub_venue.get("name"))
        if name and name.lower() != "unknown":
            return name
    journal = data.get("journal")
    if isinstance(journal, dict):
        name = _norm(journal.get("name"))
        if name and name.lower() != "unknown":
            return name
    legacy = _norm(data.get("venue"))
    if legacy and legacy.lower() != "unknown":
        return legacy
    return ""


def enrich_venue(paper: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """为单篇论文补齐权威来源元数据（可开关、幂等、静默降级）。

    入参 paper 会被就地修改并返回同一 dict；未命中 / 异常 / 开关关时原样返回。
    """
    enabled = _is_enabled(config)
    if not enabled:
        return paper

    # canonical 身份是 arXiv ID（paper_id / id 均可）
    arxiv_id = _norm(paper.get("paper_id") or paper.get("id"))
    if not arxiv_id or not re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", arxiv_id):
        return paper

    # 已补充过则跳过（幂等，也避免重复网络请求）
    if _norm(paper.get("venue")):
        return paper

    data = _lookup_semantic_scholar(arxiv_id)
    if not data:
        return paper

    venue = _resolve_venue_name(data)
    authoritative_url = _authoritative_url(data.get("externalIds"))
    if not venue and not authoritative_url:
        return paper

    if venue:
        paper["venue"] = venue
    if authoritative_url:
        paper["authoritative_url"] = authoritative_url
    log(f"[venue] {arxiv_id} -> {venue or '/'} url={authoritative_url or '/'}")
    return paper


def _is_enabled(config: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(config, dict):
        return False
    setting = config.get("arxiv_paper_setting")
    if not isinstance(setting, dict):
        return False
    ve = setting.get("venue_enrichment")
    return isinstance(ve, dict) and bool(ve.get("enabled"))


def _lookup_semantic_scholar(arxiv_id: str) -> Optional[Dict[str, Any]]:
    """按 arXiv ID 查询 Semantic Scholar，命中返回 paper dict，否则 None。"""
    url = S2_PAPER_URL.format(arxiv_id=arxiv_id)
    params = {
        "fields": "title,venue,publicationVenue,journal,year,publicationTypes,externalIds",
    }
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            res = requests.get(
                url,
                params=params,
                headers=_headers(),
                timeout=DEFAULT_TIMEOUT,
            )
            if res.status_code == 429 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            if res.status_code in (403, 404, 429):
                _log_warn(arxiv_id, f"Semantic Scholar HTTP {res.status_code}")
                return None
            res.raise_for_status()
            data = res.json()
            if isinstance(data, dict):
                return data
            return None
        except Exception as exc:  # noqa: BLE001 —— 网络异常稳定地静默降级
            last_exc = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    if last_exc is not None:
        _log_warn(arxiv_id, f"Semantic Scholar lookup failed: {last_exc}")
    return None


def _log_warn(arxiv_id: str, message: str) -> None:
    log(f"[WARN] venue enrichment skip {arxiv_id}: {message}")


if __name__ == "__main__":
    import json
    import sys

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            paper = json.loads(line)
        except json.JSONDecodeError:
            continue
        print(json.dumps(enrich_venue(paper), ensure_ascii=False))