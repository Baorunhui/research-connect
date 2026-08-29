from __future__ import annotations

import re

from .schemas import CardPlan, CardPlanItem, SocialContentRequest


DENSITY_RULES = {
    "cover": {"min_points": 3, "min_chars": 42, "max_points": 5, "max_chars": 180},
    "image_cover": {"min_points": 1, "min_chars": 18, "max_points": 2, "max_chars": 90, "asset": True},
    "thesis": {"min_points": 1, "min_chars": 28, "max_points": 2, "max_chars": 130},
    "quote": {"min_points": 1, "min_chars": 24, "max_points": 2, "max_chars": 110},
    "list": {"min_points": 4, "min_chars": 64, "max_points": 6, "max_chars": 210},
    "body": {"min_points": 3, "min_chars": 90, "max_points": 4, "max_chars": 260},
    "pipeline": {"min_points": 3, "min_chars": 60, "max_points": 5, "max_chars": 210},
    "ending": {"min_points": 4, "min_chars": 64, "max_points": 6, "max_chars": 220},
    "media": {"min_points": 2, "min_chars": 42, "max_points": 3, "max_chars": 160, "asset": True},
    "evidence": {"min_points": 2, "min_chars": 42, "max_points": 4, "max_chars": 180, "asset": True},
}


def evaluate_card_density(request: SocialContentRequest, card_plan: CardPlan) -> list[str]:
    findings = []
    for card in card_plan.cards:
        recipe = card.layout_recipe or density_recipe_for(card)
        rule = DENSITY_RULES.get(recipe, DENSITY_RULES["body"])
        points = density_points(request, card, int(rule["max_points"]))
        total_chars = density_chars(card, points)
        point_count = len(points)
        bullet_count = len([bullet for bullet in card.bullets if str(bullet).strip()])

        if rule.get("asset") and not card.asset_ids:
            findings.append(f"内容密度警告：P{card.page:02d} `{recipe}` 需要图片资产，否则页面会显得空。")
            continue
        if point_count < int(rule["min_points"]) or total_chars < int(rule["min_chars"]):
            findings.append(
                f"内容密度警告：P{card.page:02d} `{recipe}` 偏空，"
                f"当前 {point_count} 个有效要点/{total_chars} 字，"
                f"建议至少 {rule['min_points']} 个要点/{rule['min_chars']} 字。"
            )
        if bullet_count > int(rule["max_points"]):
            findings.append(
                f"内容密度警告：P{card.page:02d} `{recipe}` 要点超出显示上限，"
                f"当前 {bullet_count} 条，模板最多稳定展示 {rule['max_points']} 条，建议删减或拆页。"
            )
        if total_chars > int(rule["max_chars"]):
            findings.append(
                f"内容密度警告：P{card.page:02d} `{recipe}` 偏挤，"
                f"当前 {max(point_count, bullet_count)} 个有效要点/{total_chars} 字，建议拆页或压缩到 "
                f"{rule['max_points']} 个要点/{rule['max_chars']} 字以内。"
            )
    return findings


def density_recipe_for(card: CardPlanItem) -> str:
    if card.asset_ids:
        return "media"
    if card.role == "cover":
        return "cover"
    if card.role == "method":
        return "pipeline"
    if card.role in {"problem", "question", "thesis"}:
        return "thesis"
    if card.role == "cta":
        return "ending"
    if card.role in {"result", "value", "audience", "info"}:
        return "list"
    return "body"


def density_points(request: SocialContentRequest, card: CardPlanItem, limit: int) -> list[str]:
    candidates = [
        *card.bullets,
        card.subtitle,
        request.goal.takeaway,
        request.source.summary,
    ]
    values = []
    for item in candidates:
        text = " ".join(str(item or "").split())
        if text and text not in values:
            values.append(text)
        if len(values) >= limit:
            break
    return values


def density_chars(card: CardPlanItem, points: list[str]) -> int:
    text = "".join([card.headline, *points])
    return len(re.sub(r"\s+", "", text))
