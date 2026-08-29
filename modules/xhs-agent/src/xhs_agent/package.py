from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from .renderer import render_cards
from .schemas import (
    Artifacts,
    NextAction,
    PipelineResult,
    ResponseData,
    ResponseQuality,
    SocialContentResponse,
    XHSPayload,
    as_abs,
)


def write_package(
    result: PipelineResult,
    out_root: Path,
    template_id: str | None = None,
) -> SocialContentResponse:
    request = result.request
    package_id = make_package_id(request.intent.value, request.request_id)
    output_dir = out_root / package_id
    cards_dir = output_dir / "cards"
    output_dir.mkdir(parents=True, exist_ok=True)

    card_paths = render_cards(request, result.card_plan, cards_dir, template_id=template_id)

    note_md = output_dir / "note.md"
    metadata_json = output_dir / "metadata.json"
    qa_report_json = output_dir / "qa_report.json"
    xhs_payload_json = output_dir / "xhs_payload.json"
    response_json = output_dir / "response.json"

    note_md.write_text(render_note_md(result), encoding="utf-8")
    metadata_json.write_text(
        json.dumps(
            {
                "request": request.model_dump(mode="json"),
                "brief": result.brief.model_dump(mode="json"),
                "card_plan": result.card_plan.model_dump(mode="json"),
                "template_id": template_id or "default",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    qa_report_json.write_text(result.qa_report.model_dump_json(indent=2), encoding="utf-8")

    payload = XHSPayload(
        title=result.note.selected_title,
        content=result.note.body,
        images=[as_abs(path) for path in card_paths],
        tags=result.note.tags,
    )
    xhs_payload_json.write_text(payload.model_dump_json(indent=2), encoding="utf-8")

    response = SocialContentResponse(
        request_id=request.request_id,
        status="completed",
        data=ResponseData(
            package_id=package_id,
            output_dir=as_abs(output_dir),
            artifacts=Artifacts(
                note_md=as_abs(note_md),
                metadata_json=as_abs(metadata_json),
                qa_report_json=as_abs(qa_report_json),
                cards=[as_abs(path) for path in card_paths],
            ),
            xhs_payload=payload,
            quality=ResponseQuality(
                fact_risk=result.qa_report.fact_risk,
                style_risk=result.qa_report.style_risk,
                needs_human_check=result.qa_report.needs_human_check,
            ),
        ),
        next_actions=[
            NextAction(
                action="manual_publish",
                description="复制 title/content/tags，并上传 cards 图片到小红书。",
            )
        ],
    )
    response_json.write_text(response.model_dump_json(indent=2), encoding="utf-8")
    return response


def render_note_md(result: PipelineResult) -> str:
    tags = " ".join(f"#{tag}" for tag in result.note.tags)
    links = "\n".join(
        f"- {link.label or link.type}: {link.url}" for link in result.request.source.links
    )
    qa = "\n".join(f"- {item}" for item in result.qa_report.needs_human_check)
    return (
        f"# {result.note.selected_title}\n\n"
        f"{result.note.body}\n\n"
        f"{tags}\n\n"
        "## Links\n\n"
        f"{links or '- 暂无'}\n\n"
        "## Human Check\n\n"
        f"{qa or '- 暂无'}\n"
    )


def make_package_id(intent: str, request_id: str | None) -> str:
    date = datetime.now().strftime("%Y%m%d")
    suffix = slug(request_id) if request_id else uuid.uuid4().hex[:8]
    return f"{date}-{slug(intent)}-{suffix}"


def slug(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-").lower()
    return clean[:48] or "xhs"
