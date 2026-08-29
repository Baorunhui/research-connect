from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from research_connect_core import StandaloneJobRuntime

from .density import evaluate_card_density
from .json_tools import extract_json_object
from .llm import ChatModel, FakeChatClient, USTCChatClient
from .prompts import brief_prompt, card_prompt, qa_prompt, writer_prompt
from .schemas import Brief, CardPlan, CardPlanItem, ImageAsset, NoteDraft, PipelineResult, QAReport, SocialContentRequest


DEFAULT_MODELS = {
    "brief": "deepseek-v4-pro",
    "writer": "deepseek-v4-pro",
    "card": "qwen3.6-chat",
    "qa": "deepseek-v4-pro",
}


@dataclass
class PipelineConfig:
    models: dict[str, str] | None = None
    max_retries: int = 1

    def model_for(self, step: str) -> str:
        return {**DEFAULT_MODELS, **(self.models or {})}[step]


class XHSPipeline:
    def __init__(self, client: ChatModel | None = None, config: PipelineConfig | None = None, runtime: StandaloneJobRuntime | None = None) -> None:
        self.client = client or USTCChatClient()
        self.config = config or PipelineConfig()
        self.runtime = runtime

    @classmethod
    def offline(cls) -> "XHSPipeline":
        return cls(client=FakeChatClient())

    def run(self, request: SocialContentRequest) -> PipelineResult:
        brief = self._call_step("brief", Brief, *brief_prompt(request), fallback=lambda: fallback_brief(request))
        note = self._call_step("writer", NoteDraft, *writer_prompt(request, brief), fallback=lambda: fallback_note(request, brief))
        note = fix_note(note, request)
        card_plan = self._call_step(
            "card",
            CardPlan,
            *card_prompt(request, brief, note),
            fallback=lambda: fallback_card_plan(request, note),
        )
        card_plan = fix_card_plan(card_plan, request.requirements.card_count, note, request)
        qa_report = self._call_step(
            "qa",
            QAReport,
            *qa_prompt(request, brief, note, card_plan),
            fallback=lambda: fallback_qa(request, brief, note),
        )
        qa_report = enrich_local_qa(request, note, card_plan, qa_report)
        return PipelineResult(request=request, brief=brief, note=note, card_plan=card_plan, qa_report=qa_report)

    def _call_step(self, step: str, schema: type[Any], system: str, user: str, fallback):
        if self.runtime:
            self.runtime.progress(f"小红书生成：{step}", stage=step)
        last_error: Exception | None = None
        for _ in range(self.config.max_retries + 1):
            try:
                raw = self.client.complete_json(
                    model=self.config.model_for(step),
                    system=system,
                    user=user,
                    max_tokens=2200,
                )
                return schema.model_validate(extract_json_object(raw))
            except (ValidationError, ValueError, RuntimeError) as exc:
                last_error = exc
        value = fallback()
        if last_error:
            if hasattr(value, "human_check"):
                value.human_check.append(f"{step} 模型输出异常，已使用降级结果：{last_error}")
            if hasattr(value, "needs_human_check"):
                value.needs_human_check.append(f"{step} 模型输出异常，已使用降级结果：{last_error}")
        return value


def fix_note(note: NoteDraft, request: SocialContentRequest | None = None) -> NoteDraft:
    title = choose_title(note)
    tags = [tag.strip().lstrip("#") for tag in note.tags if tag.strip()]
    body = re.sub(r"#\S+", "", note.body).strip()
    body = neutralize_platform_link_claims(body)
    if request is not None:
        body = normalize_link_placeholders(body, request)
    return note.model_copy(update={"selected_title": title, "body": body, "tags": tags[:12]})


def choose_title(note: NoteDraft) -> str:
    candidates = [note.selected_title, *note.title_candidates]
    for candidate in candidates:
        clean = normalize_title(candidate)
        if clean and len(clean) <= 20:
            return clean
    clean = normalize_title(note.selected_title)
    return clean[:20] if clean else "科研内容速览"


def normalize_title(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().strip("。！？!?"))


def neutralize_platform_link_claims(body: str) -> str:
    replacements = [
        (r"论文链接和代码仓库我都放在主页了[，,]?需要的同学自取[。.]?", "发布时可补充论文和代码链接，方便大家继续阅读。"),
        (r"链接(已经|已)?放(在)?(主页|评论区|评论|私信)(了)?[。.]?", "发布时可补充相关链接。"),
        (r"链接见(主页|评论区|评论|私信)[。.]?", "发布时可补充相关链接。"),
        (r"(主页|评论区|评论|私信)(里)?(有|见)链接[。.]?", "发布时可补充相关链接。"),
        (r"[^。.\n]*(链接|论文|代码)[^。.\n]*(主页|评论区|评论|私信)[^。.\n]*[。.]?", "发布时可补充相关链接。"),
    ]
    fixed = body
    for pattern, replacement in replacements:
        fixed = re.sub(pattern, replacement, fixed)
    fixed = re.sub(r"(发布时可补充[^。]*链接[^。]*。)[/／]?(主页|评论区|评论|私信)", r"\1", fixed)
    return fixed.strip()


def normalize_link_placeholders(text: str, request: SocialContentRequest) -> str:
    link_types = {link.type for link in request.source.links}
    if "论文和代码链接" not in text and "论文与代码链接" not in text:
        return text
    if {"paper", "code"} <= link_types:
        replacement = "论文和代码链接"
    elif "paper" in link_types:
        replacement = "论文链接"
    elif "code" in link_types:
        replacement = "代码仓库链接"
    elif "contact" in link_types:
        replacement = "联系方式和申请要求"
    else:
        replacement = "相关链接"
    return text.replace("论文和代码链接", replacement).replace("论文与代码链接", replacement)


def fix_card_plan(
    card_plan: CardPlan,
    card_count: int,
    note: NoteDraft,
    request: SocialContentRequest | None = None,
) -> CardPlan:
    cards = list(card_plan.cards[:card_count])
    while len(cards) < card_count:
        page = len(cards) + 1
        cards.append(
            CardPlanItem(
                page=page,
                role="info" if page < card_count else "cta",
                layout_recipe="list" if page < card_count else "ending",
                headline="继续看什么",
                subtitle="给科研人的阅读建议",
                bullets=["先看方法图", "再核对实验", "最后看代码"],
                visual_hint="清单",
            )
        )
    fixed = []
    for idx, card in enumerate(cards, start=1):
        headline = neutralize_platform_link_claims(card.headline)
        subtitle = neutralize_platform_link_claims(card.subtitle)
        bullets = [neutralize_platform_link_claims(bullet) for bullet in card.bullets]
        if request is not None:
            headline = normalize_link_placeholders(headline, request)
            subtitle = normalize_link_placeholders(subtitle, request)
            bullets = [normalize_link_placeholders(bullet, request) for bullet in bullets]
        asset_ids = normalize_asset_ids(card.asset_ids, request)
        layout_recipe = normalize_layout_recipe(
            card.layout_recipe,
            card.role,
            idx,
            card_count,
            card.visual_hint,
            asset_ids,
        )
        fixed.append(
            card.model_copy(
                update={
                    "page": idx,
                    "layout_recipe": layout_recipe,
                    "headline": headline,
                    "subtitle": subtitle,
                    "bullets": bullets,
                    "asset_ids": asset_ids,
                }
            )
        )
    if fixed:
        first_recipe = "image_cover" if fixed[0].layout_recipe == "image_cover" else "cover"
        fixed[0] = fixed[0].model_copy(
            update={
                "role": "cover",
                "layout_recipe": first_recipe,
                "headline": fixed[0].headline or note.selected_title,
            }
        )
    if request is not None:
        fixed = attach_unused_assets(fixed, request)
    return CardPlan(cards=fixed)


def normalize_layout_recipe(
    layout_recipe: str | None,
    role: str,
    page: int,
    card_count: int,
    visual_hint: str = "",
    asset_ids: list[str] | None = None,
) -> str:
    allowed = {
        "cover",
        "thesis",
        "list",
        "body",
        "pipeline",
        "quote",
        "ending",
        "image_cover",
        "media",
        "evidence",
    }
    if layout_recipe in allowed:
        return layout_recipe
    aliases = {
        "photo": "image_cover",
        "image": "media",
        "figure": "media",
        "screenshot": "evidence",
        "evidence_wall": "evidence",
        "evidence_feature": "evidence",
    }
    if layout_recipe in aliases:
        return aliases[layout_recipe]
    if page == 1 or role == "cover":
        return "cover"
    if page == card_count or role == "cta":
        return "ending"
    hint = visual_hint.lower()
    if asset_ids:
        if any(token in hint for token in ["证据", "结果图", "截图", "screenshot", "evidence", "chart"]):
            return "evidence"
        if role == "method" or any(token in hint for token in ["方法图", "图", "figure", "diagram"]):
            return "media"
        return "media"
    if role == "method" or any(token in hint for token in ["流程", "路线", "pipeline", "workflow"]):
        return "pipeline"
    if role in {"question", "thesis", "problem"}:
        return "thesis"
    if any(token in hint for token in ["金句", "引言", "quote"]):
        return "quote"
    if role in {"result", "value", "audience", "info"}:
        return "list"
    return "body"


def normalize_asset_ids(asset_ids: list[str], request: SocialContentRequest | None = None) -> list[str]:
    if not asset_ids:
        return []
    allowed = {asset.id for asset in request.source.assets} if request is not None else None
    result = []
    for asset_id in asset_ids:
        clean = str(asset_id).strip()
        if not clean or clean in result:
            continue
        if allowed is not None and clean not in allowed:
            continue
        result.append(clean)
    return result[:4]


def attach_unused_assets(cards: list[CardPlanItem], request: SocialContentRequest) -> list[CardPlanItem]:
    if not cards or not request.source.assets:
        return cards
    fixed = list(cards)
    used = {asset_id for card in fixed for asset_id in card.asset_ids}
    for asset in request.source.assets:
        if asset.id in used:
            continue
        choice = choose_asset_target(fixed, asset, request)
        if choice is None:
            continue
        idx, recipe = choice
        card = fixed[idx]
        fixed[idx] = card.model_copy(
            update={
                "asset_ids": [*card.asset_ids, asset.id][:4],
                "layout_recipe": recipe,
            }
        )
        used.add(asset.id)
    return fixed


def choose_asset_target(
    cards: list[CardPlanItem], asset: ImageAsset, request: SocialContentRequest
) -> tuple[int, str] | None:
    if asset.kind == "photo" and request.intent.value == "lab_recruit" and not cards[0].asset_ids:
        return 0, "image_cover"
    if asset.kind in {"method_figure", "diagram"}:
        recipe = "media"
        preferred_roles = {"method"}
    elif asset.kind in {"result_chart", "screenshot"}:
        recipe = "evidence"
        preferred_roles = {"result", "value", "method"}
    elif asset.kind == "photo":
        return None
    else:
        recipe = "media"
        preferred_roles = {"method", "result", "info"}

    for idx, card in enumerate(cards):
        if card.role in preferred_roles and not card.asset_ids:
            if card.role == "cover" and recipe != "image_cover":
                continue
            return idx, recipe
    for idx, card in enumerate(cards[1:-1], start=1):
        if not card.asset_ids:
            return idx, recipe if recipe != "image_cover" else "media"
    return None


def enrich_local_qa(
    request: SocialContentRequest, note: NoteDraft, card_plan: CardPlan, qa_report: QAReport
) -> QAReport:
    checks = list(qa_report.checks)
    needs = list(qa_report.needs_human_check)
    unsupported = list(qa_report.unsupported_claims)
    fact_risk = qa_report.fact_risk
    style_risk = qa_report.style_risk

    if len(note.selected_title) > 20:
        checks.append("标题超过 20 字，建议人工缩短。")
        style_risk = "medium"
    else:
        checks.append("标题长度已约束到 20 字以内。")

    if "#" in note.body:
        checks.append("正文仍包含 # 标签。")
        style_risk = "medium"
    else:
        checks.append("正文和 tags 已分离。")

    material_text = "\n".join([request.source.title, request.source.summary, *[m.text for m in request.source.materials]])
    risky_patterns = ["SOTA", "state-of-the-art", "第一", "最强", "碾压", "%", "提升"]
    for pattern in risky_patterns:
        if pattern in note.body and pattern not in material_text:
            unsupported.append(f"正文出现可能需要证据的表达：{pattern}")
            fact_risk = "high" if pattern in {"SOTA", "第一", "最强", "碾压"} else "medium"

    generated_text = "\n".join(
        [
            note.body,
            *[
                "\n".join([card.headline, card.subtitle, *card.bullets])
                for card in card_plan.cards
            ],
        ]
    )
    link_claims = ["放在主页", "放主页", "见主页", "放在评论", "评论区有", "私信"]
    for claim in link_claims:
        if claim in generated_text:
            unsupported.append(f"正文包含未实际发生的平台动作：{claim}")
            fact_risk = "medium"

    if request.source.entities:
        labels = {"authors": "作者", "venue": "会议/期刊", "lab": "实验室"}
        for key in ("authors", "venue", "lab"):
            if key in request.source.entities:
                needs.append(f"确认{labels[key]}信息准确。")
    if len(card_plan.cards) != request.requirements.card_count:
        checks.append("卡片数量与需求不一致，已自动修正。")
    density_findings = evaluate_card_density(request, card_plan)
    if density_findings:
        checks.extend(density_findings)
        needs.append("检查内容密度提示：过空页面需要补充要点/图片，过挤页面建议拆页。")
        style_risk = "medium" if style_risk == "low" else style_risk
    else:
        checks.append("卡片内容密度已通过本地规则。")

    return qa_report.model_copy(
        update={
            "fact_risk": fact_risk,
            "style_risk": style_risk,
            "unsupported_claims": dedupe(unsupported),
            "needs_human_check": dedupe(needs),
            "checks": dedupe(checks),
        }
    )


def fallback_brief(request: SocialContentRequest) -> Brief:
    facts = [request.source.title, request.source.summary]
    facts.extend(material.text for material in request.source.materials[:6])
    return Brief(
        positioning=f"面向{request.audience.who}的{request.intent.value}小红书内容。",
        core_facts=[item for item in facts if item],
        safe_claims=["可以基于输入材料做保守解读。"],
        risk_boundaries=request.constraints.must_avoid,
        human_check=["模型降级生成：发布前人工核对关键事实。"],
    )


def fallback_note(request: SocialContentRequest, brief: Brief) -> NoteDraft:
    title = request.source.title[:20] or "科研内容速览"
    bullets = "\n".join(f"- {fact}" for fact in brief.core_facts[:4])
    body = f"{request.goal.takeaway}\n\n核心信息：\n{bullets}\n\n适合：{request.audience.who}\n\n发布前建议核对链接和实验数字。"
    return NoteDraft(
        title_candidates=[title, "科研人快速看懂", "这项工作值得看吗"],
        selected_title=title,
        body=body,
        tags=default_tags(request),
    )


def fallback_card_plan(request: SocialContentRequest, note: NoteDraft) -> CardPlan:
    roles = ["cover", "problem", "method", "value", "cta", "info", "info", "info"]
    cards = []
    lines = [line.strip("- ") for line in note.body.splitlines() if line.strip()][:10]
    primary_asset_id = request.source.assets[0].id if request.source.assets else None
    for page in range(1, request.requirements.card_count + 1):
        asset_ids = [primary_asset_id] if primary_asset_id and roles[page - 1] in {"method", "value"} else []
        cards.append(
            CardPlanItem(
                page=page,
                role=roles[page - 1],
                layout_recipe=normalize_layout_recipe(
                    None,
                    roles[page - 1],
                    page,
                    request.requirements.card_count,
                    "方法图" if asset_ids else "",
                    asset_ids,
                ),
                headline=note.selected_title if page == 1 else ["问题背景", "方法亮点", "适合谁看", "下一步"][min(page - 2, 3)],
                subtitle=request.source.title if page == 1 else "",
                bullets=lines[(page - 1) * 2 : (page - 1) * 2 + 3] or ["发布前人工核对事实"],
                asset_ids=asset_ids,
                visual_hint="科研笔记卡片",
            )
        )
    return CardPlan(cards=cards)


def fallback_qa(request: SocialContentRequest, brief: Brief, note: NoteDraft) -> QAReport:
    return QAReport(
        fact_risk="medium",
        style_risk="medium",
        unsupported_claims=[],
        needs_human_check=dedupe([*brief.human_check, "人工确认最终文案是否符合课题组口径。"]),
        checks=["使用本地 QA 降级结果。"],
    )


def default_tags(request: SocialContentRequest) -> list[str]:
    base = {
        "paper_promo": ["AI论文", "论文精读", "科研日常"],
        "daily_paper": ["每日论文", "论文速递", "AI研究"],
        "lab_recruit": ["实验室招生", "保研考研", "科研生活"],
        "project_promo": ["开源项目", "科研工具", "AI应用"],
    }[request.intent.value]
    kind = request.source.kind
    if kind and kind not in base:
        base.append(kind)
    return base


def dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        clean = item.strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result
