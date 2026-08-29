# xhs_agent

`xhs_agent` 是一个面向科研场景的小红书内容包生成模块。它接收上游 `paper_agent` / 数据层给出的结构化材料，调用学校 API 平台上的大模型生成文案、卡片策划和 QA，再把卡片渲染成 `1080x1440` PNG。

当前 MVP 不自动上传小红书，只输出可人工发布的内容包。

## Data Flow

```text
structured request JSON
        |
        v
Brief Agent
归纳事实、可写观点、风险边界
        |
        v
Writer Agent
生成小红书 title/body/tags
        |
        v
Card Planner Agent
生成每页卡片 role/layout_recipe/asset_ids
        |
        v
Local Postprocess
修正页数、标题、链接话术、图片引用、layout recipe
        |
        v
QA Agent + Local QA
检查事实风险、平台话术、内容密度、人审项
        |
        v
Renderer
HTML/CSS + Chrome screenshot -> PNG cards
        |
        v
output package
```

## Input

CLI 和 FastAPI 都接收同一个请求格式：`SocialContentRequest`。

最小输入：

```json
{
  "schema_version": "xhs_agent.request.v1",
  "request_id": "demo-paper-001",
  "intent": "paper_promo",
  "mode": "generate_package",
  "source": {
    "kind": "paper",
    "title": "StructBench: A Structured Benchmark for Reliable Scientific Agents",
    "summary": "该工作提出一个面向科研 agent 的结构化评测基准。",
    "materials": [
      {
        "id": "M1",
        "type": "problem",
        "text": "现有科研 agent 评测往往只看单点任务。"
      }
    ],
    "links": [
      {
        "type": "paper",
        "url": "https://arxiv.org/abs/2601.00001"
      }
    ],
    "entities": {
      "authors": ["Demo Author"],
      "venue": "ICLR 2026",
      "lab": "USTC Agent Lab"
    }
  }
}
```

常用字段：

- `intent`: `paper_promo`、`daily_paper`、`lab_recruit`、`project_promo`
- `audience`: 目标读者、阅读上下文、读者关心的问题
- `goal`: 希望读者带走什么、做什么
- `source.kind`: `paper`、`lab`、`project` 等上游材料类型
- `source.materials`: 上游结构化事实，模型只能基于这里和 `summary/entities/links/assets` 写
- `source.links`: 论文、代码、联系方式等链接。MVP 只输出到内容包，不自动发布
- `source.entities`: 作者、会议、实验室等结构化元信息
- `requirements.card_count`: 默认 5，范围 1-8
- `requirements.style`: 风格提示
- `constraints.must_include`: 必须提到的点
- `constraints.must_avoid`: 禁止越界表达

## Image Assets

图片通过 `source.assets` 输入，再由卡片计划里的 `asset_ids` 引用。renderer 会把本地或远程图片复制/下载到输出包的 `assets/` 目录。

```json
{
  "source": {
    "assets": [
      {
        "id": "method_fig",
        "type": "image",
        "uri": "fixtures/assets/scientific_method.png",
        "label": "论文方法图示例",
        "caption": "方法图示例，发布前替换为论文原图",
        "kind": "method_figure",
        "fit": "contain",
        "source_url": "https://example.com/source"
      }
    ]
  }
}
```

图片字段：

- `id`: 图片资产唯一 ID
- `uri`: 本地路径、`file://` 或 `http(s)` URL
- `kind`: `method_figure`、`result_chart`、`screenshot`、`photo`、`diagram`、`other`
- `fit`: `contain` 或 `cover`。论文方法图/结果图建议 `contain`，照片可用 `cover`
- `caption`: 图注，会显示在图片卡中
- `source_url`: 图片来源或原始链接，用于人审

支持的图片型 layout recipe：

- `image_cover`: 图片主导封面，适合实验室照片/强视觉图
- `media`: 单图 + 三个解释点，适合论文方法图
- `evidence`: 图片作为证据主体，适合方法图、结果图、截图

如果 planner 漏掉方法图，postprocess 会尽量把 `method_figure` / `diagram` 自动挂到 `method` 页。

## Card Plan Format

模型生成的卡片计划格式：

```json
{
  "cards": [
    {
      "page": 1,
      "role": "cover",
      "layout_recipe": "cover",
      "headline": "一张图讲清论文方法",
      "subtitle": "可为空",
      "bullets": ["别再只看摘要了", "看懂方法图才是关键"],
      "asset_ids": [],
      "visual_hint": "首页大标题"
    }
  ]
}
```

`layout_recipe` 控制单页结构，不控制视觉主题。当前支持：

- `cover`: 首页大标题
- `thesis`: 核心问题/观点页
- `list`: 编号清单页
- `body`: 段落解释页
- `pipeline`: 方法/流程页
- `quote`: 金句/结论页
- `ending`: 结尾/行动页
- `image_cover`: 图片封面
- `media`: 单图解释页
- `evidence`: 图片证据页

视觉主题由 `--template-id` 指定，例如：

- `native.research-editorial`
- `native.research-swiss`
- `converted.open-design-morandi-carousel`
- `converted.xhs-textcard-pro-doc`
- `converted.rednote-tech`

## Output

一次生成会产出一个 package 目录：

```text
outputs_latest/
└── 20260811-paper_promo-demo-paper-images-001/
    ├── assets/
    │   ├── method_fig.png
    │   └── lab_photo.jpg
    ├── cards/
    │   ├── html/
    │   │   ├── template_id.txt
    │   │   └── xhs-03-method-media.html
    │   ├── xhs-01-cover.png
    │   ├── xhs-02-problem.png
    │   ├── xhs-03-method.png
    │   ├── xhs-04-method.png
    │   └── xhs-05-cta.png
    ├── metadata.json
    ├── note.md
    ├── qa_report.json
    ├── response.json
    └── xhs_payload.json
```

文件说明：

- `note.md`: 给人看的发布草稿，包含标题、正文、标签、链接和人审项
- `metadata.json`: 完整中间状态，包括原始 request、brief、card_plan、template_id
- `qa_report.json`: 模型 QA + 本地 QA。包含事实风险、风格风险、不支持断言、人审清单、内容密度检查
- `xhs_payload.json`: 未来发布适配器可直接消费的标题、正文、图片路径、标签
- `response.json`: FastAPI/CLI 返回体的完整落盘版
- `cards/*.png`: 小红书图文卡片，固定 `1080x1440`
- `cards/html/*.html`: 对应 PNG 的可调试 HTML
- `assets/*`: 输入图片资产的包内副本

`xhs_payload.json` 示例：

```json
{
  "title": "一张图讲清论文方法",
  "content": "小红书正文...",
  "images": [
    "/abs/path/cards/xhs-01-cover.png",
    "/abs/path/cards/xhs-02-problem.png"
  ],
  "tags": ["论文阅读", "科研方法"]
}
```

## API Response

CLI `--print-response` 和 FastAPI 返回同一个 `SocialContentResponse`：

```json
{
  "schema_version": "xhs_agent.response.v1",
  "request_id": "demo-paper-images-001",
  "status": "completed",
  "data": {
    "package_id": "20260811-paper_promo-demo-paper-images-001",
    "output_dir": "/abs/path/output",
    "artifacts": {
      "note_md": "/abs/path/note.md",
      "metadata_json": "/abs/path/metadata.json",
      "qa_report_json": "/abs/path/qa_report.json",
      "cards": ["/abs/path/cards/xhs-01-cover.png"]
    },
    "xhs_payload": {
      "title": "标题",
      "content": "正文",
      "images": ["/abs/path/cards/xhs-01-cover.png"],
      "tags": ["标签"]
    },
    "quality": {
      "fact_risk": "low",
      "style_risk": "medium",
      "needs_human_check": ["发布前需要人确认的点"]
    }
  },
  "next_actions": [
    {
      "action": "manual_publish",
      "description": "复制 title/content/tags，并上传 cards 图片到小红书。"
    }
  ],
  "error": null
}
```

## Local QA

除了大模型 QA，本地还会做确定性检查：

- 标题长度
- 正文是否混入 `#标签`
- 是否声称“链接已放主页/评论区/私信”
- 是否出现输入材料不支持的强断言
- 图片型页面是否缺图
- 内容密度是否过空、过挤、要点超出稳定展示上限

内容密度规则在 `src/xhs_agent/density.py`。

## Run

安装：

```bash
python -m venv .venv
.venv/bin/pip install -e .[dev]
```

离线生成：

```bash
XHS_AGENT_RENDERER=html-strict \
.venv/bin/python -m xhs_agent.cli generate \
  fixtures/demo_paper_with_images.json \
  --out outputs_latest \
  --offline \
  --template-id native.research-editorial \
  --print-response
```

真实学校 API 生成：

```bash
export USTC_LLM_API_KEY="..."
XHS_AGENT_RENDERER=html-strict \
.venv/bin/python -m xhs_agent.cli generate \
  fixtures/demo_paper_with_images.json \
  --out outputs_latest \
  --template-id native.research-editorial \
  --print-response
```

FastAPI：

```bash
.venv/bin/uvicorn xhs_agent.app:app --host 0.0.0.0 --port 8010
```

接口：

- `POST /v1/xhs/packages`
- `GET /v1/xhs/packages/{package_id}`

## Test

```bash
.venv/bin/python -m pytest -q
```

## Notes

- 模块不会自动上传小红书。
- 模型不能编造论文结论、作者、实验数字、代码状态或录用信息。
- 第三方 Guizang 模板保存在 `templates/xhs/third_party/agpl/` 作为参考，不作为默认 renderer 依赖。
- 当前默认模型配置在 `src/xhs_agent/pipeline.py`。实际运行通过学校 API 平台调用模型。
