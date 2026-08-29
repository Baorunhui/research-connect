from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Intent(str, Enum):
    paper_promo = "paper_promo"
    daily_paper = "daily_paper"
    lab_recruit = "lab_recruit"
    project_promo = "project_promo"


class Material(BaseModel):
    id: str
    type: str
    text: str
    source_ids: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "medium"


class Link(BaseModel):
    type: str
    url: str
    label: str | None = None


class ImageAsset(BaseModel):
    id: str
    type: Literal["image"] = "image"
    uri: str
    label: str | None = None
    caption: str | None = None
    kind: Literal[
        "method_figure",
        "result_chart",
        "screenshot",
        "photo",
        "diagram",
        "other",
    ] = "other"
    fit: Literal["contain", "cover"] = "contain"
    object_position: str = "center center"
    source_url: str | None = None


class Audience(BaseModel):
    who: str = "AI研究生/青椒/博士生"
    context: str = "刷小红书时快速理解科研信息"
    question: str = "这件事和我有什么关系"


class Goal(BaseModel):
    takeaway: str = "读者能快速复述核心价值"
    action: str = "收藏/转发/点击链接"


class Source(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: str
    title: str
    summary: str
    materials: list[Material] = Field(default_factory=list)
    assets: list[ImageAsset] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)


class Requirements(BaseModel):
    platform: Literal["xiaohongshu"] = "xiaohongshu"
    deliverables: list[Literal["note", "carousel"]] = Field(
        default_factory=lambda: ["note", "carousel"]
    )
    card_count: int = Field(default=5, ge=1, le=8)
    style: str = "专业但像真人科研分享"
    publish: bool = False


class Constraints(BaseModel):
    must_include: list[str] = Field(default_factory=list)
    must_avoid: list[str] = Field(default_factory=lambda: ["夸大结论", "编造事实"])


class SocialContentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["xhs_agent.request.v1"] = "xhs_agent.request.v1"
    request_id: str | None = None
    intent: Intent
    mode: Literal["generate_package"] = "generate_package"
    audience: Audience = Field(default_factory=Audience)
    goal: Goal = Field(default_factory=Goal)
    source: Source
    requirements: Requirements = Field(default_factory=Requirements)
    constraints: Constraints = Field(default_factory=Constraints)

    @field_validator("request_id")
    @classmethod
    def blank_request_id_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class Brief(BaseModel):
    positioning: str
    core_facts: list[str] = Field(default_factory=list)
    safe_claims: list[str] = Field(default_factory=list)
    risk_boundaries: list[str] = Field(default_factory=list)
    human_check: list[str] = Field(default_factory=list)


class NoteDraft(BaseModel):
    title_candidates: list[str] = Field(default_factory=list)
    selected_title: str
    body: str
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str]) -> list[str]:
        normalized = []
        for tag in tags:
            clean = tag.strip().lstrip("#")
            if clean and clean not in normalized:
                normalized.append(clean)
        return normalized[:12]


class CardPlanItem(BaseModel):
    page: int
    role: str
    layout_recipe: str | None = None
    headline: str
    subtitle: str = ""
    bullets: list[str] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list)
    visual_hint: str = ""


class CardPlan(BaseModel):
    cards: list[CardPlanItem]


class QAReport(BaseModel):
    fact_risk: Literal["low", "medium", "high"] = "medium"
    style_risk: Literal["low", "medium", "high"] = "medium"
    unsupported_claims: list[str] = Field(default_factory=list)
    needs_human_check: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


class Artifacts(BaseModel):
    note_md: str
    metadata_json: str
    qa_report_json: str
    cards: list[str]


class XHSPayload(BaseModel):
    title: str
    content: str
    images: list[str]
    tags: list[str]


class ResponseQuality(BaseModel):
    fact_risk: Literal["low", "medium", "high"]
    style_risk: Literal["low", "medium", "high"]
    needs_human_check: list[str] = Field(default_factory=list)


class ResponseData(BaseModel):
    package_id: str
    output_dir: str
    artifacts: Artifacts
    xhs_payload: XHSPayload
    quality: ResponseQuality


class NextAction(BaseModel):
    action: str
    description: str


class SocialContentResponse(BaseModel):
    schema_version: Literal["xhs_agent.response.v1"] = "xhs_agent.response.v1"
    request_id: str | None = None
    status: Literal["completed", "action_required", "failed"]
    data: ResponseData | None = None
    next_actions: list[NextAction] = Field(default_factory=list)
    error: str | None = None


class PipelineResult(BaseModel):
    request: SocialContentRequest
    brief: Brief
    note: NoteDraft
    card_plan: CardPlan
    qa_report: QAReport


def as_abs(path: Path) -> str:
    return str(path.resolve())
