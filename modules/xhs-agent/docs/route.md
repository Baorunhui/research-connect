# Xiaohongshu Agent Route

## Route

Use:

```text
Standalone core + optional skill/MCP wrappers
```

Runtime should not depend on Claude Code. Claude Code is a development helper and optional wrapper target.

Current repo follows this route. The deployed module is ordinary Python code that calls the school API directly. A Claude Code skill, MCP adapter, or Feishu adapter can be added later without changing the core pipeline.

## Boundary

This module does not parse raw PDFs. It receives structured input from:

- `paper_agent`
- citation/data layer
- manual Feishu input

It owns:

- brief normalization
- Xiaohongshu writing
- carousel planning
- HTML-to-PNG rendering
- output packaging
- QA checks

It does not own:

- PDF parsing
- citation retrieval
- code analysis
- automatic upload in MVP

## Request Schema

```json
{
  "schema_version": "xhs_agent.request.v1",
  "request_id": "optional-id",
  "intent": "paper_promo",
  "mode": "generate_package",
  "audience": {
    "who": "AI研究生/青椒/博士生",
    "context": "刷小红书时快速理解一个科研工作",
    "question": "这项工作解决了什么问题，值不值得点开看"
  },
  "goal": {
    "takeaway": "读者能用一句话复述这项工作的价值",
    "action": "收藏/转发/点击论文或代码链接"
  },
  "source": {
    "kind": "paper",
    "title": "论文标题",
    "summary": "结构化摘要，不一定是原始 abstract",
    "materials": [
      {
        "id": "M1",
        "type": "contribution",
        "text": "核心贡献"
      }
    ],
    "links": [
      {
        "type": "paper",
        "url": "https://arxiv.org/..."
      }
    ],
    "entities": {
      "authors": ["..."],
      "venue": "ICLR 2026",
      "lab": "..."
    }
  },
  "requirements": {
    "platform": "xiaohongshu",
    "deliverables": ["note", "carousel"],
    "card_count": 5,
    "style": "专业但像真人科研分享",
    "publish": false
  },
  "constraints": {
    "must_include": [],
    "must_avoid": ["夸大SOTA", "编造实验结论"]
  }
}
```

MVP intents:

- `paper_promo`
- `daily_paper`
- `lab_recruit`
- `project_promo`

## Response Schema

```json
{
  "schema_version": "xhs_agent.response.v1",
  "request_id": "optional-id",
  "status": "completed",
  "data": {
    "package_id": "20260809-paper-promo-xxx",
    "output_dir": "outputs/20260809-paper-promo-xxx",
    "artifacts": {
      "note_md": "outputs/.../note.md",
      "metadata_json": "outputs/.../metadata.json",
      "qa_report_json": "outputs/.../qa_report.json",
      "cards": [
        "outputs/.../cards/xhs-01-cover.png"
      ]
    },
    "xhs_payload": {
      "title": "20字以内小红书标题",
      "content": "正文，不含#标签",
      "images": ["绝对路径1"],
      "tags": ["AI论文", "大模型", "科研日常"]
    },
    "quality": {
      "fact_risk": "low",
      "style_risk": "low",
      "needs_human_check": ["确认实验提升数字"]
    }
  },
  "next_actions": [
    {
      "action": "manual_publish",
      "description": "复制 title/content/tags，并上传 cards 图片"
    }
  ]
}
```

## Agent Steps

1. Brief Agent: normalize source into audience, goal, facts, inferences, and risk boundaries.
2. Writer Agent: generate title candidates, selected title, body, and tags.
3. Card Planner Agent: generate 1 cover plus 4-6 content page plans.
4. Renderer: default path renders deterministic `1080x1440` PNG cards from HTML/CSS with Playwright + system Chrome. Pillow remains as a fallback renderer.
5. QA Agent: check fact risk, unsupported claims, title length, tag separation, and human-check items.

Renderer notes after source-code survey:

- `guizang-social-card-skill` uses `Noto Serif SC`, `Noto Sans SC`, Inter, mono fonts, and separates layout into cover, ledger, bento/grid, pull-quote, closing recipes.
- `xhs-web-app` uses an Inter + `Noto Sans SC` web stack.
- `wewrite` themes use system CJK fallbacks such as `PingFang SC`, `Hiragino Sans GB`, and `Microsoft YaHei`.
- Browser renderer uses CSS font stacks such as `Noto Serif CJK SC`, `Noto Sans CJK SC`, `PingFang SC`, and `Microsoft YaHei UI`.
- Pillow fallback must specify the SC face inside Noto CJK TTC files. On this Linux host, `NotoSansCJK-*.ttc` and `NotoSerifCJK-*.ttc` index `0` is JP, while index `2` is SC. Loading the TTC without index can produce non-standard simplified Chinese glyph variants.

Current renderer recipes:

- `cover`: editorial magazine cover with issue header, large title, subtitle, and numbered teasers
- `ledger`: full-height numbered rows for problems, audience, info, and dense checklists
- `bento`: 2x2 module layout for method/result/value pages
- `closing`: CTA page with anchored checklist and human-confirmation band
- `pullquote`: sparse thesis/question page for future card roles

Theme selection is package-level rather than page-level:

- daily paper: indigo research palette
- lab recruit: forest/academic palette
- project promo: Swiss blue utility palette
- paper promo fallback: warm paper editorial palette

Local post-processing currently enforces:

- selected title <= 20 Chinese characters
- tags do not include `#`
- body does not contain hashtag tags
- body/cards do not claim links are already in homepage, comments, or private messages
- card count matches request

## API

```text
POST /v1/xhs/packages
GET  /v1/xhs/packages/{package_id}
```

CLI:

```bash
.venv/bin/python -m xhs_agent.cli generate fixtures/paper_promo.json --out outputs --offline --print-response
.venv/bin/python -m xhs_agent.cli generate fixtures/paper_promo.json --out outputs_api --print-response
```

## Development Order

Completed:

1. Pydantic schemas
2. deterministic fake pipeline
3. OpenAI-compatible LLM client
4. prompts
5. brief generation
6. writing generation
7. card planning
8. HTML/CSS to PNG rendering with Pillow fallback
9. QA checks and local guardrails
10. FastAPI
11. CLI
12. fixtures for paper promotion and lab recruitment

Next:

1. add daily-paper and project-promo fixtures
2. add Feishu `cc_connect` adapter
3. add optional style presets for paper promotion, daily paper, and lab recruitment
4. split HTML templates into separate files if CSS grows too large
5. add optional skill/MCP wrapper for Claude Code demos

## Model Choice

Quality-first default:

- brief: `deepseek-v4-pro`
- writer: `deepseek-v4-pro`
- card planner: `qwen3.6-chat`
- QA: `deepseek-v4-pro`

Demo-speed alternative:

- brief: `qwen3.6-chat`
- writer: `deepseek-v4-flash`
- card planner: `qwen3.6-chat`
- QA: `deepseek-v4-flash`

Avoid depending on `glm-chat` for the MVP because the earlier smoke test returned server errors. `glm-5.2` can be tested later for Chinese style, but keep the JSON guardrails because it may return reasoning without final JSON if token limits are tight.
