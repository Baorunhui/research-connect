from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from xhs_agent.llm import ModelConfig, USTCChatClient
from xhs_agent.package import write_package
from xhs_agent.pipeline import PipelineConfig, XHSPipeline
from xhs_agent.renderer import template_manifest
from xhs_agent.schemas import SocialContentRequest


DEFAULT_FIXTURES = [
    Path("fixtures/demo_daily_paper_multimodal.json"),
    Path("fixtures/demo_lab_recruit_agent.json"),
    Path("fixtures/demo_project_promo_citationclaw.json"),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate real-API XHS packages with every ready template.")
    parser.add_argument("--out", type=Path, default=Path("outputs_template_showcase"))
    parser.add_argument("--fixture", type=Path, action="append", dest="fixtures")
    parser.add_argument("--template-id", action="append", dest="template_ids")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--brief-model", default=None)
    parser.add_argument("--writer-model", default=None)
    parser.add_argument("--card-model", default=None)
    parser.add_argument("--qa-model", default=None)
    return parser


def ready_template_ids() -> list[str]:
    return [
        entry["id"]
        for entry in template_manifest()["templates"]
        if entry.get("status") == "ready"
    ]


def main() -> int:
    args = build_parser().parse_args()
    if args.api_key:
        os.environ["USTC_LLM_API_KEY"] = args.api_key
    if args.base_url:
        os.environ["USTC_LLM_BASE_URL"] = args.base_url

    models = {
        key: value
        for key, value in {
            "brief": args.brief_model,
            "writer": args.writer_model,
            "card": args.card_model,
            "qa": args.qa_model,
        }.items()
        if value
    }
    pipeline = XHSPipeline(
        client=USTCChatClient(ModelConfig.from_env()),
        config=PipelineConfig(models=models or None),
    )

    fixtures = args.fixtures or DEFAULT_FIXTURES
    template_ids = args.template_ids or ready_template_ids()
    args.out.mkdir(parents=True, exist_ok=True)

    records = []
    for fixture in fixtures:
        request = SocialContentRequest.model_validate_json(fixture.read_text(encoding="utf-8"))
        print(f"[api] {fixture}")
        result = pipeline.run(request)
        for template_id in template_ids:
            template_out = args.out / template_id.replace(".", "__")
            print(f"[render] {fixture.stem} -> {template_id}")
            response = write_package(result, template_out, template_id=template_id)
            records.append(
                {
                    "fixture": str(fixture),
                    "request_id": request.request_id,
                    "template_id": template_id,
                    "status": response.status,
                    "output_dir": response.data.output_dir if response.data else None,
                    "cards": response.data.artifacts.cards if response.data else [],
                }
            )

    (args.out / "index.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[done] {args.out / 'index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
