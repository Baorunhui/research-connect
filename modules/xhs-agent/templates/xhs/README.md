# XHS Template Library

小红书卡片模板库，只收 `1080x1440` 图文卡相关模板。

## Layout

- `native/`: 本项目自研、可直接填充的 HTML seed。
- `converted/`: 从非 HTML 路线转写来的 HTML seed，比如 Canvas 配置、React 组件、prompt spec。
- `third_party/`: 第三方原始模板或参考文件，按许可证分目录保存。
- `_licenses/`: 第三方许可证副本。
- `manifest.json`: 模板索引、来源、许可证和建议用途。

## Recipe Pack

当前 `ready` 模板按 Guizang 路线组织：一个视觉 pack 内含多个 layout recipe，而不是同一张单页模板重复 N 次。

支持的 recipe：

- `cover`: 首页大标题。
- `thesis`: 核心问题/观点页。
- `list`: 编号清单页。
- `body`: 段落解释页。
- `pipeline`: 方法/流程页。
- `quote`: 金句/结论页。
- `ending`: 结尾/人工确认/行动页。
- `image_cover`: 图片主导封面页，适合强视觉照片/海报。
- `media`: 单张图 + 简短解释，适合论文方法图、截图、实验图。
- `evidence`: 图片作为证据主体，适合方法图/结果图加核对要点。

`CardPlanItem.layout_recipe` 可以显式指定页型；缺省时 pipeline 会按 `role`、页码、`visual_hint` 和 `asset_ids` 自动补齐。图片页通过 `CardPlanItem.asset_ids` 引用 `source.assets` 中的图片 id；renderer 会把本地或远程图片复制到输出包的 `assets/` 目录。

运行时代码在 `src/xhs_agent/recipe_packs.py`。每个 pack 都有独立 DOM 片段和 CSS，不再只是单一 HTML seed 换色。

## License Boundary

`third_party/agpl/guizang-social-card-skill/` 来自 `op7418/guizang-social-card-skill`，许可证是 AGPL-3.0。它保留在模板库中作参考和显式第三方模板，不作为默认 renderer 依赖。比赛/产品化时如果要直接使用或修改这套模板，需要确认 AGPL 的源代码开放义务，或取得商业授权。

`third_party/mit/` 与 `third_party/apache/` 中的模板可作为更宽松的参考来源，但正式集成时仍应保留 attribution。

## Current Recommendation

科研内容 MVP 默认优先使用：

- `native/research-editorial.html`: 论文宣传、每日论文、深度解释。
- `native/research-swiss.html`: 科研工具、引用追踪、方法/流程页。
- `converted/open-design-morandi-carousel.html`: 更接近小红书知识卡语气的柔和款。

Guizang 原始模板最强，但也最重，适合后续做高级 HTML renderer profile。
