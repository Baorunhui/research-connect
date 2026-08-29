from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from research_connect_core import runtime_from_env

from .llm import ModelConfig, USTCChatClient
from .package import write_package
from .pipeline import PipelineConfig, XHSPipeline
from .schemas import SocialContentRequest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xhs-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="Generate a Xiaohongshu content package.")
    generate.add_argument("input", type=Path, help="Path to request JSON.")
    generate.add_argument("--out", type=Path, default=Path("outputs"), help="Output root directory.")
    generate.add_argument("--offline", action="store_true", help="Use deterministic fake model output.")
    generate.add_argument("--api-key", default=None, help="USTC LLM API key. Defaults to USTC_LLM_API_KEY.")
    generate.add_argument("--base-url", default=None, help="Defaults to USTC_LLM_BASE_URL or school endpoint.")
    generate.add_argument("--brief-model", default=None)
    generate.add_argument("--writer-model", default=None)
    generate.add_argument("--card-model", default=None)
    generate.add_argument("--qa-model", default=None)
    generate.add_argument("--template-id", default=None, help="Renderer template id from templates/xhs/manifest.json.")
    generate.add_argument("--print-response", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate":
        response = generate(args)
        if args.print_response:
            print(response.model_dump_json(indent=2))
        else:
            print(response.data.output_dir if response.data else response.error)
        return 0 if response.status == "completed" else 1
    return 1


def generate(args: argparse.Namespace):
    request = SocialContentRequest.model_validate_json(args.input.read_text(encoding="utf-8"))
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
    config = PipelineConfig(models=models or None)
    with runtime_from_env("xhs-agent", "xhs.generate") as runtime:
        if args.offline:
            pipeline = XHSPipeline.offline()
            pipeline.config = config
            pipeline.runtime = runtime
        else:
            if args.api_key:
                os.environ["USTC_LLM_API_KEY"] = args.api_key
            if args.base_url:
                os.environ["USTC_LLM_BASE_URL"] = args.base_url
            pipeline = XHSPipeline(client=USTCChatClient(ModelConfig.from_env()), config=config, runtime=runtime)
        result = pipeline.run(request)
        package = write_package(result, args.out, template_id=args.template_id)
        runtime.emit(
            "job.artifact",
            "小红书素材包已生成",
            stage="artifacts",
            payload={"output_dir": str(package.data.output_dir) if package.data else ""},
        )
        return package


if __name__ == "__main__":
    raise SystemExit(main())
