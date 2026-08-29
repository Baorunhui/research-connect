from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol

from research_connect_core.llm import LLMProvider, RetryPolicy, UnifiedLLM


class ChatModel(Protocol):
    def complete_json(self, *, model: str, system: str, user: str, max_tokens: int = 1600) -> str:
        ...


@dataclass(frozen=True)
class ModelConfig:
    base_url: str = "https://api.llm.ustc.edu.cn"
    api_key: str | None = None
    timeout: int = 90
    max_concurrency: int = 4

    @classmethod
    def from_env(cls) -> "ModelConfig":
        return cls(
            base_url=os.getenv("USTC_LLM_BASE_URL", "https://api.llm.ustc.edu.cn").rstrip("/"),
            api_key=os.getenv("USTC_LLM_API_KEY"),
            timeout=int(os.getenv("USTC_LLM_TIMEOUT", "90")),
            max_concurrency=int(os.getenv("LLM_MAX_CONCURRENCY", "4")),
        )


class USTCChatClient:
    def __init__(self, config: ModelConfig | None = None) -> None:
        self.config = config or ModelConfig.from_env()
        if not self.config.api_key:
            raise ValueError("USTC_LLM_API_KEY is required for USTCChatClient.")
        self.client = UnifiedLLM(
            [
                LLMProvider(
                    name="ustc",
                    base_url=self.config.base_url,
                    api_key=self.config.api_key,
                    model="per-request",
                    timeout_seconds=self.config.timeout,
                    max_concurrency=self.config.max_concurrency,
                    retry=RetryPolicy(max_attempts=4),
                )
            ]
        )

    def complete_json(self, *, model: str, system: str, user: str, max_tokens: int = 1600) -> str:
        result = self.client.complete(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        content = result.content
        if content:
            return content

        try:
            reasoning = getattr(result.raw.choices[0].message, "reasoning_content", None)
        except (AttributeError, IndexError, TypeError):
            reasoning = None
        if reasoning:
            raise RuntimeError(
                "Model returned reasoning_content without JSON content. "
                "Try deepseek-v4-pro/qwen3.6-chat or raise max_tokens."
            )
        raise RuntimeError(f"Model returned empty content from {result.provider}/{result.model}")


class FakeChatClient:
    """Deterministic model stub used by tests and offline demos."""

    def complete_json(self, *, model: str, system: str, user: str, max_tokens: int = 1600) -> str:
        if "Brief Agent" in system:
            return json.dumps(
                {
                    "positioning": "把结构化科研材料转成适合小红书快速理解的内容 brief。",
                    "core_facts": ["输入材料包含标题、摘要、亮点和链接。", "输出不自动发布，只生成可人工发布的素材包。"],
                    "safe_claims": ["该工作有明确方法亮点。", "适合科研用户收藏后继续阅读。"],
                    "risk_boundaries": ["没有来源的实验数字不能写成确定结论。"],
                    "human_check": ["发布前确认作者、单位、venue 和实验数字。"],
                },
                ensure_ascii=False,
            )
        if "Writer Agent" in system:
            return json.dumps(
                {
                    "title_candidates": ["这篇论文解决了什么问题", "科研人快速看懂这个工作", "一篇值得收藏的AI论文"],
                    "selected_title": "科研人快速看懂这个工作",
                    "body": "今天这篇工作适合想快速抓住技术路线的同学。\n\n核心看点：它围绕真实科研场景中的一个关键问题，给出更清晰的方法设计和实验验证。\n\n建议先看方法图，再对照实验部分判断是否适合自己的课题。",
                    "tags": ["AI论文", "科研日常", "论文精读", "机器学习"],
                },
                ensure_ascii=False,
            )
        if "Card Planner Agent" in system:
            return json.dumps(
                {
                    "cards": [
                        {
                            "page": 1,
                            "role": "cover",
                            "layout_recipe": "cover",
                            "headline": "科研人快速看懂这个工作",
                            "subtitle": "一组卡片抓住问题、方法和价值",
                            "bullets": ["论文速览", "方法亮点", "适合收藏"],
                            "visual_hint": "学术笔记感封面",
                        },
                        {
                            "page": 2,
                            "role": "problem",
                            "layout_recipe": "thesis",
                            "headline": "它想解决什么问题",
                            "subtitle": "先把任务背景讲清楚",
                            "bullets": ["问题来自输入摘要和贡献点", "避免扩大到论文没有证明的场景"],
                            "visual_hint": "问题拆解图",
                        },
                        {
                            "page": 3,
                            "role": "method",
                            "layout_recipe": "pipeline",
                            "headline": "方法亮点",
                            "subtitle": "用三句话说明路线",
                            "bullets": ["结构更清晰", "实验验证更完整", "代码链接可继续检查"],
                            "visual_hint": "流程图",
                        },
                        {
                            "page": 4,
                            "role": "value",
                            "layout_recipe": "list",
                            "headline": "适合谁看",
                            "subtitle": "给科研用户的阅读建议",
                            "bullets": ["相关方向研究生", "准备复现实验的人", "找选题灵感的青椒"],
                            "visual_hint": "读者画像",
                        },
                        {
                            "page": 5,
                            "role": "cta",
                            "layout_recipe": "ending",
                            "headline": "阅读顺序",
                            "subtitle": "先图后实验，再看代码",
                            "bullets": ["收藏后读方法图", "核对实验设置", "再判断是否复现"],
                            "visual_hint": "清单",
                        },
                    ]
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "fact_risk": "medium",
                "style_risk": "low",
                "unsupported_claims": [],
                "needs_human_check": ["确认正式发布前所有链接可访问。"],
                "checks": ["标题长度合规", "标签已去掉井号", "未自动发布"],
            },
            ensure_ascii=False,
        )
