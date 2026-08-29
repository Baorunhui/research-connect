from __future__ import annotations

import json
import os
import tempfile
import uuid
import sys
from pathlib import Path
from typing import Any, Mapping

from connect_hub.config import ProviderSettings
from connect_hub.tools.base import ToolContext, ToolDefinition


def xhs_generate_tool(
    *,
    agent_dir: Path,
    provider: ProviderSettings,
    output_dir: Path,
    timeout_seconds: int,
    offline: bool,
) -> ToolDefinition:
    resolved_agent_dir = agent_dir.resolve()
    python_path = Path(sys.executable).resolve()

    def handler(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        request_id = f"feishu-{uuid.uuid4().hex[:12]}"
        materials = [
            {
                "id": f"M{index}",
                "type": "fact",
                "text": text,
                "confidence": "medium",
            }
            for index, text in enumerate(arguments.get("materials") or [], start=1)
            if str(text).strip()
        ]
        links = [
            {"type": "source", "url": url}
            for url in (arguments.get("links") or [])
            if str(url).strip()
        ]
        request = {
            "schema_version": "xhs_agent.request.v1",
            "request_id": request_id,
            "intent": arguments["intent"],
            "mode": "generate_package",
            "audience": {
                "who": arguments.get("audience") or "相关方向学生和研究者",
                "context": "在小红书中快速理解并判断是否值得继续阅读",
                "question": "这项内容和我有什么关系",
            },
            "goal": {
                "takeaway": arguments.get("purpose") or "读者能快速复述核心价值",
                "action": "收藏并根据来源链接继续阅读",
            },
            "source": {
                "kind": arguments.get("source_kind") or "project",
                "title": arguments["title"],
                "summary": arguments["summary"],
                "materials": materials,
                "links": links,
                "entities": {},
            },
            "requirements": {
                "platform": "xiaohongshu",
                "deliverables": ["note", "carousel"],
                "card_count": arguments.get("card_count", 5),
                "style": arguments.get("style") or "专业但像真人科研分享",
                "publish": False,
            },
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            {
                "USTC_LLM_API_KEY": provider.api_key,
                "USTC_LLM_BASE_URL": provider.base_url.removesuffix("/v1"),
                "USTC_LLM_TIMEOUT": str(provider.timeout_seconds),
                "XHS_AGENT_RENDERER": "html-strict",
                "CONNECT_JOB_ID": context.job_id,
                "CONNECT_EMIT_EVENTS": "1",
            }
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        ) as request_file:
            json.dump(request, request_file, ensure_ascii=False)
            request_path = Path(request_file.name)
        command = [
            str(python_path),
            "-m",
            "xhs_agent.cli",
            "generate",
            str(request_path),
            "--out",
            str(output_dir.resolve()),
            "--template-id",
            "native.research-editorial",
            "--print-response",
        ]
        if offline:
            command.append("--offline")
        try:
            completed = context.run_process(
                command,
                cwd=resolved_agent_dir,
                env=env,
                capture_output=True,
                timeout=timeout_seconds,
            )
        finally:
            request_path.unlink(missing_ok=True)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout)[-3000:]
            raise RuntimeError(f"xhs_agent exited {completed.returncode}: {detail}")
        response = json.loads(completed.stdout)
        data = response.get("data") or {}
        payload = data.get("xhs_payload") or {}
        artifacts = data.get("artifacts") or {}
        result = {
            "status": response.get("status"),
            "package_id": data.get("package_id"),
            "output_dir": data.get("output_dir"),
            "title": payload.get("title"),
            "content": payload.get("content"),
            "tags": payload.get("tags") or [],
            "images": payload.get("images") or artifacts.get("cards") or [],
            "needs_human_check": ((data.get("quality") or {}).get("needs_human_check") or []),
            "note_md": artifacts.get("note_md"),
        }
        note_md = str(result.get("note_md") or "").strip()
        if note_md:
            context.record_artifact(kind="file", path=note_md)
        return result

    return ToolDefinition(
        name="generate_xhs_package",
        description=(
            "根据用户明确提供的论文、项目、实验室或日报材料生成小红书文案和卡片图片包。"
            "不会自动发布。这是耗时业务工具，只有用户明确要求生成小红书内容包、且主题或材料"
            "足以支撑内容时才能调用；普通改写或问答不要调用。公开事实材料不足且联网可用时，"
            "应先搜索再决定是否追问或生成。用户未指定时默认5页和专业但像真人科研分享的风格。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["paper_promo", "daily_paper", "lab_recruit", "project_promo"],
                },
                "source_kind": {"type": "string"},
                "title": {"type": "string", "minLength": 1},
                "summary": {"type": "string", "minLength": 1},
                "materials": {"type": "array", "items": {"type": "string"}},
                "links": {"type": "array", "items": {"type": "string"}},
                "card_count": {"type": "integer", "minimum": 1, "maximum": 8},
                "style": {"type": "string"},
                "audience": {"type": "string"},
                "purpose": {"type": "string"},
            },
            "required": ["intent", "title", "summary"],
            "additionalProperties": False,
        },
        handler=handler,
        timeout_seconds=timeout_seconds + 10,
        progress_message="已开始生成小红书文案和卡片，通常需要1～3分钟，完成后会把图片发到当前会话。",
        module_name="xhs-agent",
        module_version="0.1.0",
        job_type="generate_xhs_package",
        kind="business",
    )
