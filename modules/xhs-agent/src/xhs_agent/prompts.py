from __future__ import annotations

import json

from .schemas import Brief, CardPlan, NoteDraft, SocialContentRequest


JSON_RULES = """只输出一个合法 JSON object，不要 Markdown，不要解释。
所有内容使用中文。不能编造论文结论、作者、实验数字、代码状态或录用信息。
不能声称“链接已放主页/评论区/私信”，因为 MVP 不自动发布，也不自动上传链接。
如果输入没有证据，就把它放到 human_check 或 risk_boundaries。"""


def request_json(request: SocialContentRequest) -> str:
    return request.model_dump_json(indent=2)


def brief_prompt(request: SocialContentRequest) -> tuple[str, str]:
    system = f"""You are the Brief Agent for xhs_agent.
你的任务是把上游结构化材料归一成小红书写作 brief，区分事实、可安全表达的判断和风险边界。
{JSON_RULES}
JSON schema:
{{
  "positioning": "一句话定位",
  "core_facts": ["可由输入直接支持的事实"],
  "safe_claims": ["保守可写的表达"],
  "risk_boundaries": ["不能越界写的点"],
  "human_check": ["发布前需要人确认的点"]
}}"""
    user = f"输入请求：\n{request_json(request)}"
    return system, user


def writer_prompt(request: SocialContentRequest, brief: Brief) -> tuple[str, str]:
    system = f"""You are the Writer Agent for xhs_agent.
生成小红书笔记文案，面向科研用户，专业但像真人分享，不要营销腔。
selected_title 必须 20 个中文字符以内；正文不要包含 #标签；tags 不要带 #。
如果要提到论文/代码链接，只能说“发布时可补充论文和代码链接”，不能说已经放在主页、评论区或私信。
{JSON_RULES}
JSON schema:
{{
  "title_candidates": ["3-5个标题"],
  "selected_title": "最终标题",
  "body": "小红书正文，分段自然",
  "tags": ["标签，不带#"]
}}"""
    payload = {"request": json.loads(request_json(request)), "brief": brief.model_dump()}
    return system, json.dumps(payload, ensure_ascii=False, indent=2)


def card_prompt(request: SocialContentRequest, brief: Brief, note: NoteDraft) -> tuple[str, str]:
    card_count = request.requirements.card_count
    system = f"""You are the Card Planner Agent for xhs_agent.
生成小红书图文卡片策划，固定 {card_count} 页，第一张是 cover。
每页 headline 短而清楚，bullets 每条不超过 22 个中文字符。
layout_recipe 控制本页排版，不是视觉风格。只能从以下值选择：
- cover: 首页大标题
- thesis: 核心问题/观点页
- list: 编号清单页
- body: 段落解释页
- pipeline: 方法/流程页
- quote: 金句/结论页
- ending: 结尾/人工确认/行动页
- image_cover: 图片主导封面页，只在输入 assets 有合适图片时使用
- media: 单张图 + 简短解释，适合方法图/实验图/截图
- evidence: 图片作为证据主体，适合方法图、结果图、代码截图
如果一页需要图片，只能通过 asset_ids 引用输入 request.source.assets 里的 id，不能编造图片 id。
方法图、结果图、截图优先用 media/evidence；不要把密集图安排成裁切封面。
{JSON_RULES}
JSON schema:
{{
  "cards": [
    {{
      "page": 1,
      "role": "cover|problem|method|result|audience|cta|info",
      "layout_recipe": "cover|thesis|list|body|pipeline|quote|ending|image_cover|media|evidence",
      "headline": "页标题",
      "subtitle": "可为空",
      "bullets": ["要点"],
      "asset_ids": ["可为空，只填输入已有 asset id"],
      "visual_hint": "给渲染器或设计师的视觉提示"
    }}
  ]
}}"""
    payload = {
        "request": json.loads(request_json(request)),
        "brief": brief.model_dump(),
        "note": note.model_dump(),
    }
    return system, json.dumps(payload, ensure_ascii=False, indent=2)


def qa_prompt(
    request: SocialContentRequest, brief: Brief, note: NoteDraft, card_plan: CardPlan
) -> tuple[str, str]:
    system = f"""You are the QA Agent for xhs_agent.
检查生成内容是否有事实风险、夸大表达、标题过长、标签混入正文等问题。
对科研内容尤其保守：没有证据的 SOTA、提升百分比、录用状态都算风险。
{JSON_RULES}
JSON schema:
{{
  "fact_risk": "low|medium|high",
  "style_risk": "low|medium|high",
  "unsupported_claims": ["没有输入支持的表达"],
  "needs_human_check": ["需要人确认的点"],
  "checks": ["通过或未通过的检查说明"]
}}"""
    payload = {
        "request": json.loads(request_json(request)),
        "brief": brief.model_dump(),
        "note": note.model_dump(),
        "card_plan": card_plan.model_dump(),
    }
    return system, json.dumps(payload, ensure_ascii=False, indent=2)
