from __future__ import annotations

import html
import re
from collections.abc import Callable
from typing import Any

from .schemas import CardPlanItem, SocialContentRequest


RecipeRenderer = Callable[[SocialContentRequest, CardPlanItem, int], str]


def recipe_pack_choice(card: CardPlanItem, entry: dict) -> str:
    recipes = entry.get("recipes", {})
    default = entry.get("default_recipe", "body")
    desired = card.layout_recipe or role_to_recipe(card.role)
    aliases = {"bento": "list", "closing": "ending", "pullquote": "quote"}
    desired = aliases.get(desired, desired)
    if desired in recipes:
        return desired
    return default if default in recipes else next(iter(recipes), "body")


def render_recipe_pack_card(
    request: SocialContentRequest,
    card: CardPlanItem,
    total: int,
    entry: dict,
    recipe: str,
    asset_map: dict[str, dict[str, Any]] | None = None,
) -> str:
    pack = str(entry.get("pack", entry["id"].split(".")[-1]))
    renderers = PACK_RENDERERS.get(pack, PACK_RENDERERS["research-editorial"])
    if recipe in {"image_cover", "media", "evidence"}:
        body = render_image_recipe(request, card, total, pack, recipe, asset_map or {})
    else:
        renderer = renderers.get(recipe, renderers["body"])
        body = renderer(request, card, total)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=1080, initial-scale=1">
  <title>{esc(card.headline)}</title>
  <style>{base_css()}{PACK_CSS.get(pack, PACK_CSS["research-editorial"])}</style>
</head>
<body>
  <section class="card pack-{safe_class(pack)} recipe-{safe_class(recipe)}">
    {body}
  </section>
</body>
</html>
"""


def role_to_recipe(role: str) -> str:
    if role == "cover":
        return "cover"
    if role == "method":
        return "pipeline"
    if role in {"problem", "question", "thesis"}:
        return "thesis"
    if role == "cta":
        return "ending"
    if role in {"result", "value", "audience", "info"}:
        return "list"
    return "body"


def base_css() -> str:
    return """
    @page { size: 1080px 1440px; margin: 0; }
    * { box-sizing: border-box; }
    html, body { margin: 0; width: 1080px; height: 1440px; background: #111; }
    body {
      font-family: "Inter", "Noto Sans CJK SC", "Noto Sans SC", -apple-system, "PingFang SC", sans-serif;
      -webkit-font-smoothing: antialiased;
      text-rendering: geometricPrecision;
    }
    .card { width: 1080px; height: 1440px; position: relative; overflow: hidden; isolation: isolate; }
    .meta {
      position: absolute; z-index: 5; left: var(--x); right: var(--x); top: var(--top);
      display: flex; justify-content: space-between; align-items: center;
      color: var(--muted); font: 22px "Noto Sans Mono CJK SC", ui-monospace, monospace;
    }
    .footer {
      position: absolute; z-index: 5; left: var(--x); right: var(--x); bottom: 48px;
      color: var(--muted); font-size: 24px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    h1, h2, h3, p { margin: 0; }
    .image-recipe {
      position:absolute; z-index:2; left:var(--x); right:var(--x); top:176px; bottom:126px;
      display:grid; gap:28px; color:inherit;
    }
    .image-recipe h1 { font-size:70px; line-height:1.12; font-weight:900; letter-spacing:0; }
    .image-recipe .lead { color:var(--image-muted, #666); font-size:30px; line-height:1.36; }
    .asset-frame {
      position:relative; overflow:hidden; background:var(--image-panel, rgba(255,255,255,.72));
      border:var(--image-border, 2px solid rgba(0,0,0,.14)); box-shadow:var(--image-shadow, none);
    }
    .asset-frame img {
      width:100%; height:calc(100% - 54px); display:block; object-fit:var(--fit, contain);
      object-position:var(--pos, center center);
    }
    .asset-frame .missing {
      height:100%; display:flex; align-items:center; justify-content:center;
      color:var(--image-muted, #666); font-size:26px; font-weight:800;
    }
    .asset-caption {
      position:absolute; left:22px; right:22px; bottom:14px;
      color:var(--image-muted, #666); font-size:22px; line-height:1.35;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    }
    .media-main { grid-template-rows:auto auto 1fr; }
    .media-main .asset-frame { height:600px; padding:22px; }
    .media-main .asset-points { display:grid; grid-template-columns:repeat(3, 1fr); gap:16px; }
    .media-main .asset-points article {
      min-height:128px; padding:22px; background:var(--image-chip, rgba(255,255,255,.68));
      border:var(--image-hair, 1px solid rgba(0,0,0,.1));
    }
    .media-main .asset-points span {
      display:block; color:var(--image-accent, #222); font:800 20px "Noto Sans Mono CJK SC", monospace;
      margin-bottom:18px;
    }
    .media-main .asset-points p { font-size:25px; line-height:1.28; font-weight:800; }
    .evidence-main { grid-template-rows:auto 1fr auto; }
    .evidence-main .asset-frame { height:720px; padding:26px; }
    .evidence-main .evidence-strip { display:grid; gap:14px; }
    .evidence-main .evidence-row {
      display:grid; grid-template-columns:58px 1fr; align-items:start; min-height:72px;
      border-top:var(--image-hair, 1px solid rgba(0,0,0,.1)); padding-top:16px;
    }
    .evidence-main .evidence-row span { color:var(--image-accent, #222); font:800 20px "Noto Sans Mono CJK SC", monospace; }
    .evidence-main .evidence-row p { font-size:26px; line-height:1.32; font-weight:800; }
    .image-cover-main { position:absolute; inset:0; z-index:2; color:var(--image-cover-text, inherit); }
    .cover-asset { position:absolute; inset:0; width:100%; height:100%; border:0; padding:0; background:#111; }
    .cover-asset img { height:100%; }
    .cover-asset::after {
      content:""; position:absolute; inset:0;
      background:linear-gradient(180deg, rgba(0,0,0,.18), rgba(0,0,0,.12) 42%, rgba(0,0,0,.72));
    }
    .recipe-image-cover .footer {
      color:rgba(255,255,255,.78); border-top:1px solid rgba(255,255,255,.28); padding-top:24px;
    }
    .image-cover-main .copy {
      position:absolute; left:var(--x); right:var(--x); bottom:150px; z-index:3;
      color:white; text-shadow:0 2px 24px rgba(0,0,0,.48);
    }
    .image-cover-main .copy .kicker {
      color:rgba(255,255,255,.78); font:800 22px "Noto Sans Mono CJK SC", monospace; margin-bottom:26px;
    }
    .image-cover-main h1 { font-size:86px; line-height:1.08; font-weight:950; letter-spacing:0; max-width:900px; }
    .image-cover-main .copy p:not(.kicker) { margin-top:24px; max-width:820px; font-size:32px; line-height:1.36; }
    """


def meta(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    return f"""
    <header class="meta">
      <span>{esc(intent_label(request.intent.value))} / {esc(role_label(card.role))}</span>
      <span>{card.page:02d}/{total:02d}</span>
    </header>"""


def footer(request: SocialContentRequest) -> str:
    return f'<footer class="footer">{esc(footer_text(request))}</footer>'


def render_image_recipe(
    request: SocialContentRequest,
    card: CardPlanItem,
    total: int,
    pack: str,
    recipe: str,
    asset_map: dict[str, dict[str, Any]],
) -> str:
    asset = primary_asset(card, asset_map)
    if recipe == "image_cover":
        return image_cover_recipe(request, card, total, pack, asset)
    if recipe == "evidence":
        return evidence_recipe(request, card, total, pack, asset)
    return media_recipe(request, card, total, pack, asset)


def media_recipe(
    request: SocialContentRequest,
    card: CardPlanItem,
    total: int,
    pack: str,
    asset: dict[str, Any] | None,
) -> str:
    items = "".join(
        f'<article><span>{idx:02d}</span><p>{esc(item)}</p></article>'
        for idx, item in enumerate(points(request, card, 3), 1)
    )
    return f"""
    {pack_background(pack)}{meta(request, card, total)}
    <main class="image-recipe media-main">
      <section>
        <h1>{esc(card.headline)}</h1>
        <p class="lead">{esc(card.subtitle or asset_label(asset) or request.goal.takeaway)}</p>
      </section>
      {asset_figure(asset, force_fit="contain")}
      <section class="asset-points">{items}</section>
    </main>{footer(request)}"""


def evidence_recipe(
    request: SocialContentRequest,
    card: CardPlanItem,
    total: int,
    pack: str,
    asset: dict[str, Any] | None,
) -> str:
    rows = "".join(
        f'<div class="evidence-row"><span>{idx:02d}</span><p>{esc(item)}</p></div>'
        for idx, item in enumerate(points(request, card, 3), 1)
    )
    return f"""
    {pack_background(pack)}{meta(request, card, total)}
    <main class="image-recipe evidence-main">
      <section>
        <h1>{esc(card.headline)}</h1>
        <p class="lead">{esc(card.subtitle or "把图当证据看，而不是只看标题")}</p>
      </section>
      {asset_figure(asset, force_fit="contain")}
      <section class="evidence-strip">{rows}</section>
    </main>{footer(request)}"""


def image_cover_recipe(
    request: SocialContentRequest,
    card: CardPlanItem,
    total: int,
    pack: str,
    asset: dict[str, Any] | None,
) -> str:
    return f"""
    {asset_figure(asset, force_fit="cover", cover=True)}
    <main class="image-cover-main">
      <section class="copy">
        <p class="kicker">{esc(source_marker(request))} / {card.page:02d}/{total:02d}</p>
        <h1>{esc(card.headline)}</h1>
        <p>{esc(card.subtitle or request.goal.takeaway)}</p>
      </section>
    </main>{footer(request)}"""


def primary_asset(card: CardPlanItem, asset_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for asset_id in card.asset_ids:
        if asset_id in asset_map:
            return asset_map[asset_id]
    return None


def asset_figure(
    asset: dict[str, Any] | None,
    force_fit: str | None = None,
    cover: bool = False,
) -> str:
    cls = "asset-frame cover-asset" if cover else "asset-frame"
    if not asset:
        return f'<figure class="{cls}"><div class="missing">图片素材未找到，发布前补图</div></figure>'
    fit = force_fit or str(asset.get("fit") or "contain")
    position = str(asset.get("object_position") or "center center")
    caption = asset_caption(asset)
    caption_html = "" if cover or not caption else f'<figcaption class="asset-caption">{esc(caption)}</figcaption>'
    return f"""
      <figure class="{cls}" style="--fit:{esc(fit)}; --pos:{esc(position)};">
        <img src="{esc(asset["src"])}" alt="{esc(asset_label(asset) or caption or "image asset")}" />
        {caption_html}
      </figure>"""


def asset_label(asset: dict[str, Any] | None) -> str:
    if not asset:
        return ""
    return str(asset.get("label") or asset.get("caption") or asset.get("kind") or "")


def asset_caption(asset: dict[str, Any]) -> str:
    parts = [str(asset.get("caption") or asset.get("label") or "").strip()]
    source = str(asset.get("source_url") or "").strip()
    if source:
        parts.append(source)
    return " | ".join(part for part in parts if part)


def pack_background(pack: str) -> str:
    if pack == "research-editorial":
        return '<div class="paper-grain"></div>'
    if pack == "research-swiss":
        return '<div class="sw-bar"></div>'
    if pack == "morandi-carousel":
        return '<div class="soft-orb"></div>'
    if pack == "pro-doc":
        return '<div class="doc-top"></div>'
    if pack == "rednote-tech":
        return '<div class="tech-grid"></div><div class="tech-glow"></div>'
    return ""


def points(request: SocialContentRequest, card: CardPlanItem, limit: int = 6) -> list[str]:
    candidates = [
        *card.bullets,
        card.subtitle,
        request.goal.takeaway,
        request.source.summary,
        "发布前人工核对关键事实",
    ]
    values = []
    for item in candidates:
        text = " ".join(str(item or "").split())
        if text and text not in values:
            values.append(text)
    return values[:limit]


def split_bullet(text: str) -> tuple[str, str]:
    for sep in ["：", ":"]:
        if sep in text:
            first, rest = text.split(sep, 1)
            return first.strip() + sep, rest.strip()
    return text, ""


def step_item(idx: int, item: str, cls: str = "step") -> str:
    first, rest = split_bullet(item)
    rest_html = f"<p>{esc(rest)}</p>" if rest else ""
    return f'<article class="{cls}"><span>{idx:02d}</span><div><h2>{esc(first)}</h2>{rest_html}</div></article>'


def list_item(idx: int, item: str, cls: str = "row") -> str:
    return f'<div class="{cls}"><span>{idx:02d}</span><p>{esc(item)}</p></div>'


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def safe_class(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-") or "default"


def intent_label(intent: str) -> str:
    labels = {
        "paper_promo": "Paper Promo",
        "daily_paper": "Daily Paper",
        "lab_recruit": "Lab Recruit",
        "project_promo": "Project",
    }
    return labels.get(intent, intent)


def role_label(role: str) -> str:
    labels = {
        "cover": "封面",
        "problem": "问题",
        "method": "方法",
        "result": "结果",
        "audience": "适合谁",
        "value": "价值",
        "cta": "行动",
        "info": "信息",
    }
    return labels.get(role, role[:8])


def source_marker(request: SocialContentRequest) -> str:
    venue = request.source.entities.get("venue")
    lab = request.source.entities.get("lab")
    if venue:
        return str(venue)
    if lab:
        return str(lab)
    return request.source.kind


def footer_text(request: SocialContentRequest) -> str:
    venue = request.source.entities.get("venue")
    lab = request.source.entities.get("lab")
    parts = [part for part in [str(venue) if venue else "", str(lab) if lab else ""] if part]
    return " | ".join(parts) if parts else "xhs_agent 科研内容包"


def ed_cover(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    items = "".join(list_item(idx, item, "issue-item") for idx, item in enumerate(points(request, card, 4), 1))
    return f"""
    <div class="paper-grain"></div>{meta(request, card, total)}
    <main class="ed ed-cover">
      <p class="kicker">{esc(source_marker(request))}</p>
      <h1>{esc(card.headline)}</h1>
      <p class="lead">{esc(card.subtitle)}</p>
      <section class="issue-strip">{items}</section>
    </main>{footer(request)}"""


def ed_thesis(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    lead = card.bullets[0] if card.bullets else card.subtitle
    return f"""
    <div class="paper-grain"></div>{meta(request, card, total)}
    <main class="ed ed-thesis">
      <p class="kicker">Thesis</p>
      <h1>{esc(card.headline)}</h1>
      <p class="lead">{esc(lead)}</p>
      <aside>{esc(card.subtitle or request.goal.takeaway)}</aside>
    </main>{footer(request)}"""


def ed_list(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    rows = "".join(list_item(idx, item, "ledger-row") for idx, item in enumerate(points(request, card, 6), 1))
    return f"""
    <div class="paper-grain"></div>{meta(request, card, total)}
    <main class="ed ed-list">
      <h1>{esc(card.headline)}</h1>
      <p class="lead">{esc(card.subtitle)}</p>
      <section class="ledger">{rows}</section>
    </main>{footer(request)}"""


def ed_body(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    paras = "".join(f"<p>{esc(item)}</p>" for item in points(request, card, 3))
    return f"""
    <div class="paper-grain"></div>{meta(request, card, total)}
    <main class="ed ed-body">
      <section><p class="kicker">{esc(role_label(card.role))}</p><h1>{esc(card.headline)}</h1></section>
      <article>{paras}</article>
      <aside>{esc(card.subtitle or request.goal.takeaway)}</aside>
    </main>{footer(request)}"""


def ed_pipeline(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    steps = "".join(step_item(idx, item) for idx, item in enumerate(points(request, card, 5), 1))
    return f"""
    <div class="paper-grain"></div>{meta(request, card, total)}
    <main class="ed ed-pipeline">
      <h1>{esc(card.headline)}</h1>
      <p class="lead">{esc(card.subtitle)}</p>
      <section class="pipeline-v">{steps}</section>
    </main>{footer(request)}"""


def ed_quote(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    quote = card.bullets[0] if card.bullets else card.subtitle
    return f"""
    <div class="paper-grain"></div>{meta(request, card, total)}
    <main class="ed ed-quote">
      <p class="mark">“</p>
      <h1>{esc(card.headline)}</h1>
      <p>{esc(quote)}</p>
      <small>{esc(source_marker(request))}</small>
    </main>{footer(request)}"""


def ed_ending(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    rows = "".join(list_item(idx, item, "ledger-row") for idx, item in enumerate(points(request, card, 5), 1))
    return f"""
    <div class="paper-grain"></div>{meta(request, card, total)}
    <main class="ed ed-ending">
      <h1>{esc(card.headline)}</h1>
      <p class="lead">{esc(card.subtitle)}</p>
      <section class="ledger compact">{rows}</section>
      <div class="confirm">发布前人工确认事实、链接和口径</div>
    </main>{footer(request)}"""


def sw_cover(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    items = "".join(f"<b>{esc(item)}</b>" for item in points(request, card, 4))
    return f"""
    <div class="sw-bar"></div>{meta(request, card, total)}
    <main class="sw sw-cover"><p>{esc(source_marker(request))}</p><h1>{esc(card.headline)}</h1><h2>{esc(card.subtitle)}</h2><section>{items}</section></main>{footer(request)}"""


def sw_thesis(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    return f"""
    <div class="sw-bar"></div>{meta(request, card, total)}
    <main class="sw sw-thesis"><p>Statement</p><h1>{esc(card.headline)}</h1><h2>{esc(card.bullets[0] if card.bullets else card.subtitle)}</h2></main>{footer(request)}"""


def sw_list(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    cells = "".join(list_item(idx, item, "cell") for idx, item in enumerate(points(request, card, 6), 1))
    return f"""
    <div class="sw-bar"></div>{meta(request, card, total)}
    <main class="sw sw-list"><h1>{esc(card.headline)}</h1><p>{esc(card.subtitle)}</p><section>{cells}</section></main>{footer(request)}"""


def sw_body(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    paras = "".join(f"<p>{esc(item)}</p>" for item in points(request, card, 3))
    return f"""
    <div class="sw-bar"></div>{meta(request, card, total)}
    <main class="sw sw-body"><h1>{esc(card.headline)}</h1><article>{paras}</article><aside>{esc(card.subtitle or request.goal.takeaway)}</aside></main>{footer(request)}"""


def sw_pipeline(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    steps = "".join(step_item(idx, item, "tower-step") for idx, item in enumerate(points(request, card, 5), 1))
    return f"""
    <div class="sw-bar"></div>{meta(request, card, total)}
    <main class="sw sw-pipeline"><h1>{esc(card.headline)}</h1><p>{esc(card.subtitle)}</p><section>{steps}</section></main>{footer(request)}"""


def sw_quote(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    return f"""
    <div class="sw-bar"></div>{meta(request, card, total)}
    <main class="sw sw-quote"><p>Quote</p><h1>{esc(card.headline)}</h1><h2>{esc(card.bullets[0] if card.bullets else card.subtitle)}</h2></main>{footer(request)}"""


def sw_ending(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    rows = "".join(list_item(idx, item, "release-row") for idx, item in enumerate(points(request, card, 5), 1))
    return f"""
    <div class="sw-bar"></div>{meta(request, card, total)}
    <main class="sw sw-ending"><h1>{esc(card.headline)}</h1><section>{rows}</section><div>人工确认后发布</div></main>{footer(request)}"""


def soft_cover(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    chips = "".join(f"<span>{esc(item)}</span>" for item in points(request, card, 4))
    return f"""
    <div class="soft-orb"></div>{meta(request, card, total)}
    <main class="soft soft-cover"><span>{esc(source_marker(request))}</span><h1>{esc(card.headline)}</h1><p>{esc(card.subtitle)}</p><section>{chips}</section></main>{footer(request)}"""


def soft_thesis(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    return f"""
    <div class="soft-orb"></div>{meta(request, card, total)}
    <main class="soft soft-thesis"><span>{esc(role_label(card.role))}</span><h1>{esc(card.headline)}</h1><p>{esc(card.bullets[0] if card.bullets else card.subtitle)}</p></main>{footer(request)}"""


def soft_list(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    rows = "".join(list_item(idx, item, "soft-row") for idx, item in enumerate(points(request, card, 6), 1))
    return f"""
    <div class="soft-orb"></div>{meta(request, card, total)}
    <main class="soft soft-list"><h1>{esc(card.headline)}</h1><p>{esc(card.subtitle)}</p><section>{rows}</section></main>{footer(request)}"""


def soft_body(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    paras = "".join(f"<p>{esc(item)}</p>" for item in points(request, card, 3))
    return f"""
    <div class="soft-orb"></div>{meta(request, card, total)}
    <main class="soft soft-body"><h1>{esc(card.headline)}</h1><article>{paras}</article><aside>{esc(card.subtitle or request.goal.takeaway)}</aside></main>{footer(request)}"""


def soft_pipeline(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    steps = "".join(step_item(idx, item, "soft-step") for idx, item in enumerate(points(request, card, 5), 1))
    return f"""
    <div class="soft-orb"></div>{meta(request, card, total)}
    <main class="soft soft-pipeline"><h1>{esc(card.headline)}</h1><p>{esc(card.subtitle)}</p><section>{steps}</section></main>{footer(request)}"""


def soft_quote(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    return f"""
    <div class="soft-orb"></div>{meta(request, card, total)}
    <main class="soft soft-quote"><b>“</b><h1>{esc(card.headline)}</h1><p>{esc(card.bullets[0] if card.bullets else card.subtitle)}</p></main>{footer(request)}"""


def soft_ending(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    rows = "".join(f"<li>{esc(item)}</li>" for item in points(request, card, 5))
    return f"""
    <div class="soft-orb"></div>{meta(request, card, total)}
    <main class="soft soft-ending"><h1>{esc(card.headline)}</h1><p>{esc(card.subtitle)}</p><ol>{rows}</ol><div>发布前人工确认</div></main>{footer(request)}"""


def doc_cover(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    rows = "".join(list_item(idx, item, "doc-row") for idx, item in enumerate(points(request, card, 4), 1))
    return f"""
    <div class="doc-top"></div>{meta(request, card, total)}
    <main class="doc doc-cover"><code>{esc(source_marker(request))}</code><h1>{esc(card.headline)}</h1><p>{esc(card.subtitle)}</p><section>{rows}</section></main>{footer(request)}"""


def doc_thesis(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    return f"""
    <div class="doc-top"></div>{meta(request, card, total)}
    <main class="doc doc-thesis"><code>ASSERTION</code><h1>{esc(card.headline)}</h1><pre>{esc(card.bullets[0] if card.bullets else card.subtitle)}</pre></main>{footer(request)}"""


def doc_list(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    rows = "".join(list_item(idx, item, "doc-row") for idx, item in enumerate(points(request, card, 6), 1))
    return f"""
    <div class="doc-top"></div>{meta(request, card, total)}
    <main class="doc doc-list"><h1>{esc(card.headline)}</h1><p>{esc(card.subtitle)}</p><section>{rows}</section></main>{footer(request)}"""


def doc_body(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    paras = "".join(f"<p>{esc(item)}</p>" for item in points(request, card, 3))
    return f"""
    <div class="doc-top"></div>{meta(request, card, total)}
    <main class="doc doc-body"><h1>{esc(card.headline)}</h1><article>{paras}</article><aside>{esc(card.subtitle or request.goal.takeaway)}</aside></main>{footer(request)}"""


def doc_pipeline(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    steps = "".join(step_item(idx, item, "doc-step") for idx, item in enumerate(points(request, card, 5), 1))
    return f"""
    <div class="doc-top"></div>{meta(request, card, total)}
    <main class="doc doc-pipeline"><h1>{esc(card.headline)}</h1><p>{esc(card.subtitle)}</p><section>{steps}</section></main>{footer(request)}"""


def doc_quote(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    return f"""
    <div class="doc-top"></div>{meta(request, card, total)}
    <main class="doc doc-quote"><code>NOTE</code><h1>{esc(card.headline)}</h1><p>{esc(card.bullets[0] if card.bullets else card.subtitle)}</p></main>{footer(request)}"""


def doc_ending(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    rows = "".join(list_item(idx, item, "doc-check") for idx, item in enumerate(points(request, card, 5), 1))
    return f"""
    <div class="doc-top"></div>{meta(request, card, total)}
    <main class="doc doc-ending"><h1>{esc(card.headline)}</h1><section>{rows}</section><div>READY FOR HUMAN REVIEW</div></main>{footer(request)}"""


def tech_cover(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    items = "".join(f"<span>{esc(item)}</span>" for item in points(request, card, 4))
    return f"""
    <div class="tech-grid"></div><div class="tech-glow"></div>{meta(request, card, total)}
    <main class="tech tech-cover"><code>{esc(source_marker(request))}</code><h1>{esc(card.headline)}</h1><p>{esc(card.subtitle)}</p><section>{items}</section></main>{footer(request)}"""


def tech_thesis(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    return f"""
    <div class="tech-grid"></div><div class="tech-glow"></div>{meta(request, card, total)}
    <main class="tech tech-thesis"><code>QUESTION_NODE</code><h1>{esc(card.headline)}</h1><p>{esc(card.bullets[0] if card.bullets else card.subtitle)}</p></main>{footer(request)}"""


def tech_list(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    rows = "".join(list_item(idx, item, "node") for idx, item in enumerate(points(request, card, 6), 1))
    return f"""
    <div class="tech-grid"></div><div class="tech-glow"></div>{meta(request, card, total)}
    <main class="tech tech-list"><h1>{esc(card.headline)}</h1><p>{esc(card.subtitle)}</p><section>{rows}</section></main>{footer(request)}"""


def tech_body(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    paras = "".join(f"<p>{esc(item)}</p>" for item in points(request, card, 3))
    return f"""
    <div class="tech-grid"></div><div class="tech-glow"></div>{meta(request, card, total)}
    <main class="tech tech-body"><h1>{esc(card.headline)}</h1><article>{paras}</article><aside>{esc(card.subtitle or request.goal.takeaway)}</aside></main>{footer(request)}"""


def tech_pipeline(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    steps = "".join(step_item(idx, item, "node-step") for idx, item in enumerate(points(request, card, 5), 1))
    return f"""
    <div class="tech-grid"></div><div class="tech-glow"></div>{meta(request, card, total)}
    <main class="tech tech-pipeline"><h1>{esc(card.headline)}</h1><p>{esc(card.subtitle)}</p><section>{steps}</section></main>{footer(request)}"""


def tech_quote(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    return f"""
    <div class="tech-grid"></div><div class="tech-glow"></div>{meta(request, card, total)}
    <main class="tech tech-quote"><code>TAKEAWAY</code><h1>{esc(card.headline)}</h1><p>{esc(card.bullets[0] if card.bullets else card.subtitle)}</p></main>{footer(request)}"""


def tech_ending(request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    rows = "".join(list_item(idx, item, "node") for idx, item in enumerate(points(request, card, 5), 1))
    return f"""
    <div class="tech-grid"></div><div class="tech-glow"></div>{meta(request, card, total)}
    <main class="tech tech-ending"><h1>{esc(card.headline)}</h1><section>{rows}</section><div>HUMAN_CHECK_REQUIRED</div></main>{footer(request)}"""


PACK_RENDERERS: dict[str, dict[str, RecipeRenderer]] = {
    "research-editorial": {
        "cover": ed_cover,
        "thesis": ed_thesis,
        "list": ed_list,
        "body": ed_body,
        "pipeline": ed_pipeline,
        "quote": ed_quote,
        "ending": ed_ending,
    },
    "research-swiss": {
        "cover": sw_cover,
        "thesis": sw_thesis,
        "list": sw_list,
        "body": sw_body,
        "pipeline": sw_pipeline,
        "quote": sw_quote,
        "ending": sw_ending,
    },
    "morandi-carousel": {
        "cover": soft_cover,
        "thesis": soft_thesis,
        "list": soft_list,
        "body": soft_body,
        "pipeline": soft_pipeline,
        "quote": soft_quote,
        "ending": soft_ending,
    },
    "pro-doc": {
        "cover": doc_cover,
        "thesis": doc_thesis,
        "list": doc_list,
        "body": doc_body,
        "pipeline": doc_pipeline,
        "quote": doc_quote,
        "ending": doc_ending,
    },
    "rednote-tech": {
        "cover": tech_cover,
        "thesis": tech_thesis,
        "list": tech_list,
        "body": tech_body,
        "pipeline": tech_pipeline,
        "quote": tech_quote,
        "ending": tech_ending,
    },
}


PACK_CSS = {
    "research-editorial": """
    .pack-research-editorial { --x:72px; --top:84px; --image-panel:#faf8f2; --image-border:2px solid #c8bfb1; --image-hair:1.5px solid #c8bfb1; --image-chip:#ebe6da; --image-muted:#6b6257; --image-accent:#2e5e51; background:#f3f0e8; color:#111; }
    .pack-research-editorial .paper-grain { position:absolute; inset:0; opacity:.22; background-image:radial-gradient(#d8d2c6 1px,transparent 1.5px); background-size:24px 24px; }
    .pack-research-editorial .meta { border-bottom:2px solid #c8bfb1; padding-bottom:28px; }
    .pack-research-editorial .footer { border-top:2px solid #c8bfb1; padding-top:24px; }
    .ed { position:absolute; z-index:2; left:72px; right:72px; top:176px; bottom:126px; }
    .ed .kicker { color:#6b6257; font:22px "Noto Sans Mono CJK SC",monospace; margin-bottom:26px; }
    .ed h1 { font-family:"Noto Serif CJK SC",serif; font-weight:700; letter-spacing:0; color:#111; }
    .pack-research-editorial .image-recipe h1 { font-family:"Noto Serif CJK SC",serif; font-weight:700; }
    .ed .lead { color:#2e5e51; font-size:34px; line-height:1.38; margin-top:18px; }
    .ed-cover h1 { font-size:88px; line-height:1.1; max-width:900px; margin-top:72px; }
    .issue-strip { position:absolute; left:0; right:0; bottom:116px; display:grid; gap:14px; }
    .issue-item { display:grid; grid-template-columns:62px 1fr; align-items:center; min-height:66px; border-top:1.5px solid #c8bfb1; }
    .issue-item span { color:#6b6257; font:22px "Noto Sans Mono CJK SC",monospace; }
    .issue-item p { font-size:28px; font-weight:700; line-height:1.28; }
    .ed-thesis { display:flex; flex-direction:column; justify-content:center; }
    .ed-thesis h1,.ed-quote h1 { font-size:104px; line-height:1.08; max-width:900px; }
    .ed-thesis aside { margin-top:72px; border-left:8px solid #2e5e51; padding-left:28px; color:#6b6257; font-size:30px; line-height:1.45; }
    .ed-list h1,.ed-pipeline h1,.ed-body h1,.ed-ending h1 { font-size:68px; line-height:1.12; }
    .ledger { margin-top:64px; border-top:2px solid #c8bfb1; }
    .ledger-row { min-height:112px; display:grid; grid-template-columns:72px 1fr; align-items:center; border-bottom:2px solid #c8bfb1; padding:20px 0; }
    .ledger-row span,.step span { color:#2e5e51; font:700 23px "Noto Sans Mono CJK SC",monospace; }
    .ledger-row p { font-size:34px; line-height:1.3; font-weight:700; }
    .ed-body { display:grid; grid-template-columns:1fr 1fr; gap:54px; align-content:start; padding-top:54px; }
    .ed-body article { border-left:2px solid #c8bfb1; padding-left:42px; display:grid; gap:28px; }
    .ed-body article p { font:34px/1.5 "Noto Serif CJK SC",serif; }
    .ed-body aside { grid-column:1/3; align-self:end; color:#6b6257; border-top:2px solid #c8bfb1; padding-top:26px; font-size:28px; }
    .pipeline-v { margin-top:62px; display:grid; gap:18px; }
    .step { display:grid; grid-template-columns:74px 1fr; min-height:112px; border-bottom:2px solid #c8bfb1; padding:18px 0; }
    .step:first-child { border-top:2px solid #c8bfb1; }
    .step h2 { font-size:34px; line-height:1.25; }
    .step p { color:#6b6257; font-size:26px; line-height:1.38; margin-top:8px; }
    .ed-quote { display:flex; flex-direction:column; justify-content:center; }
    .ed-quote .mark { height:90px; color:#2e5e51; font:180px Georgia,serif; line-height:.7; }
    .ed-quote p:not(.mark) { margin-top:42px; color:#6b6257; font-size:38px; line-height:1.45; }
    .ed-quote small { margin-top:70px; color:#2e5e51; font:700 22px "Noto Sans Mono CJK SC",monospace; }
    .ed-ending .confirm { position:absolute; left:0; right:0; bottom:84px; padding:28px 34px; background:#111; color:white; font-size:30px; font-weight:800; }
    """,
    "research-swiss": """
    .pack-research-swiss { --x:72px; --top:84px; --image-panel:#f0f0ee; --image-border:2px solid #d4d4d2; --image-hair:2px solid #d4d4d2; --image-chip:#f0f0ee; --image-muted:#737373; --image-accent:#002fa7; background:#fafaf8; color:#0a0a0a; }
    .sw-bar { position:absolute; inset:0 0 auto 0; height:28px; background:#002fa7; }
    .pack-research-swiss .meta { border-bottom:2px solid #d4d4d2; padding-bottom:28px; }
    .pack-research-swiss .footer { border-top:2px solid #d4d4d2; padding-top:24px; }
    .sw { position:absolute; z-index:2; left:72px; right:72px; top:176px; bottom:126px; }
    .sw h1 { font-weight:800; letter-spacing:0; color:#0a0a0a; }
    .pack-research-swiss .asset-frame,.pack-research-swiss .asset-points article { border-radius:0; }
    .sw-cover h1 { margin-top:72px; font-size:88px; line-height:1.08; max-width:900px; }
    .sw-cover h2,.sw p,.sw h2 { color:#002fa7; font-size:34px; line-height:1.35; margin-top:18px; }
    .sw-cover section { position:absolute; left:0; right:0; bottom:108px; display:grid; grid-template-columns:1fr 1fr; gap:24px; }
    .sw-cover b { background:#f0f0ee; border-left:8px solid #002fa7; padding:28px; font-size:28px; line-height:1.3; }
    .sw-thesis { display:flex; flex-direction:column; justify-content:center; }
    .sw-thesis p,.sw-quote p { color:#002fa7; font:700 22px "Noto Sans Mono CJK SC",monospace; }
    .sw-thesis h1,.sw-quote h1 { font-size:118px; line-height:1.04; font-weight:300; max-width:930px; }
    .sw-thesis h2,.sw-quote h2 { max-width:780px; font-size:34px; color:#737373; }
    .sw-list h1,.sw-pipeline h1,.sw-body h1,.sw-ending h1 { font-size:72px; line-height:1.1; }
    .sw-list section { margin-top:58px; display:grid; grid-template-columns:1fr 1fr; gap:24px; }
    .cell { min-height:168px; background:#f0f0ee; padding:26px 28px; display:block; }
    .cell span { color:#002fa7; font:700 22px "Noto Sans Mono CJK SC",monospace; }
    .cell p { margin-top:28px; color:#0a0a0a; font-size:32px; line-height:1.28; font-weight:800; }
    .sw-body { display:grid; grid-template-columns:320px 1fr; gap:52px; align-content:start; }
    .sw-body article { display:grid; gap:26px; }
    .sw-body article p { color:#0a0a0a; font-size:36px; line-height:1.42; }
    .sw-body aside { grid-column:1/3; border-left:8px solid #002fa7; padding:24px 30px; background:#f0f0ee; color:#737373; font-size:28px; }
    .sw-pipeline section { margin-top:54px; display:grid; gap:18px; border-left:8px solid #002fa7; padding-left:28px; }
    .tower-step { min-height:106px; display:grid; grid-template-columns:68px 1fr; background:#f0f0ee; padding:24px 28px; }
    .tower-step span { color:#002fa7; font:700 22px "Noto Sans Mono CJK SC",monospace; }
    .tower-step h2 { font-size:32px; line-height:1.24; }
    .tower-step p { color:#737373; font-size:24px; margin-top:8px; }
    .sw-quote { display:flex; flex-direction:column; justify-content:center; }
    .sw-ending section { margin-top:54px; border-top:2px solid #d4d4d2; }
    .release-row { display:grid; grid-template-columns:68px 1fr; min-height:92px; align-items:center; border-bottom:2px solid #d4d4d2; }
    .release-row span { color:#002fa7; font:700 22px "Noto Sans Mono CJK SC",monospace; }
    .release-row p { font-size:30px; font-weight:800; }
    .sw-ending div { position:absolute; left:0; right:0; bottom:92px; background:#002fa7; color:#fff; padding:28px 32px; font-size:30px; font-weight:800; }
    """,
    "morandi-carousel": """
    .pack-morandi-carousel { --x:76px; --top:86px; --image-panel:rgba(255,255,255,.74); --image-border:1px solid rgba(69,103,92,.16); --image-hair:1px solid rgba(69,103,92,.16); --image-chip:rgba(255,255,255,.68); --image-muted:#647872; --image-accent:#45675c; --image-shadow:0 18px 48px rgba(68,80,74,.08); background:linear-gradient(145deg,#f5eee8,#edf2ef 50%,#e8eef5); color:#23302c; }
    .soft-orb { position:absolute; right:-130px; bottom:150px; width:420px; height:420px; border-radius:50%; border:2px solid rgba(69,103,92,.12); }
    .pack-morandi-carousel .meta { color:#6d7f78; }
    .pack-morandi-carousel .footer { color:#7c8c86; }
    .soft { position:absolute; z-index:2; left:76px; right:76px; top:178px; bottom:126px; }
    .soft > span { display:inline-flex; height:48px; align-items:center; padding:0 24px; border-radius:999px; background:rgba(255,255,255,.72); color:#6d7f78; font-weight:800; }
    .soft h1 { color:#23302c; font-size:78px; line-height:1.12; font-weight:900; }
    .pack-morandi-carousel .asset-frame,.pack-morandi-carousel .asset-points article { border-radius:26px; }
    .soft-cover h1 { margin-top:72px; font-size:84px; max-width:900px; }
    .soft-cover p,.soft > p { color:#647872; font-size:34px; line-height:1.38; margin-top:22px; }
    .soft-cover section { position:absolute; left:0; right:0; bottom:104px; display:grid; gap:18px; }
    .soft-cover section span,.soft-row,.soft-step,.soft-body article p { background:rgba(255,255,255,.68); border-radius:26px; box-shadow:0 18px 48px rgba(68,80,74,.08); }
    .soft-cover section span { padding:24px 30px; font-size:30px; font-weight:800; }
    .soft-thesis { display:flex; flex-direction:column; justify-content:center; }
    .soft-thesis h1,.soft-quote h1 { font-size:96px; line-height:1.08; }
    .soft-thesis p,.soft-quote p { max-width:820px; color:#647872; font-size:38px; line-height:1.42; }
    .soft-list section,.soft-pipeline section { margin-top:54px; display:grid; gap:18px; }
    .soft-row { min-height:92px; display:grid; grid-template-columns:58px 1fr; align-items:center; padding:22px 30px; }
    .soft-row span,.soft-step span { color:#45675c; font:800 22px "Noto Sans Mono CJK SC",monospace; }
    .soft-row p { font-size:30px; line-height:1.3; font-weight:800; }
    .soft-step { min-height:108px; display:grid; grid-template-columns:58px 1fr; padding:24px 28px; }
    .soft-step h2 { font-size:32px; line-height:1.25; }
    .soft-step p { color:#647872; font-size:24px; margin-top:8px; }
    .soft-body article { margin-top:42px; display:grid; gap:22px; }
    .soft-body article p { padding:28px 32px; color:#23302c; font-size:34px; line-height:1.42; }
    .soft-body aside { position:absolute; left:0; right:0; bottom:112px; color:#647872; font-size:28px; line-height:1.4; }
    .soft-quote { display:flex; flex-direction:column; justify-content:center; }
    .soft-quote b { color:#45675c; font:180px Georgia,serif; line-height:.7; height:84px; }
    .soft-ending ol { margin:48px 0 0; padding-left:40px; display:grid; gap:18px; color:#23302c; font-size:30px; font-weight:800; }
    .soft-ending div { position:absolute; left:0; right:0; bottom:104px; border-radius:28px; padding:28px 34px; background:#45675c; color:white; font-size:30px; font-weight:900; }
    """,
    "pro-doc": """
    .pack-pro-doc { --x:76px; --top:82px; --image-panel:#fff; --image-border:1.5px solid #e5e7eb; --image-hair:1px solid #e5e7eb; --image-chip:#fff; --image-muted:#4b5563; --image-accent:#0066ff; background:#f9fafb; color:#111827; }
    .doc-top { position:absolute; inset:0 0 auto 0; height:16px; background:#0066ff; }
    .pack-pro-doc .meta { color:#6b7280; text-transform:uppercase; }
    .pack-pro-doc .footer { border-top:2px solid #e5e7eb; padding-top:24px; }
    .doc { position:absolute; z-index:2; left:76px; right:76px; top:176px; bottom:126px; }
    .doc code { display:inline-block; background:#0066ff; color:white; border-radius:999px; padding:10px 20px; font:800 20px "Noto Sans Mono CJK SC",monospace; }
    .doc h1 { color:#111827; font-size:72px; line-height:1.12; font-weight:900; }
    .pack-pro-doc .asset-frame,.pack-pro-doc .asset-points article { border-radius:10px; }
    .doc-cover h1 { margin-top:54px; font-size:78px; max-width:900px; }
    .doc-cover p,.doc > p { color:#4b5563; font-size:32px; line-height:1.38; margin-top:22px; }
    .doc-cover section,.doc-list section { margin-top:62px; display:grid; gap:16px; }
    .doc-row { display:grid; grid-template-columns:64px 1fr; align-items:center; min-height:88px; padding:20px 24px; background:white; border:1px solid #e5e7eb; border-radius:10px; }
    .doc-row span,.doc-step span,.doc-check span { color:#0066ff; font:800 22px "Noto Sans Mono CJK SC",monospace; }
    .doc-row p { font-size:30px; font-weight:850; line-height:1.3; }
    .doc-thesis { display:flex; flex-direction:column; justify-content:center; }
    .doc-thesis h1,.doc-quote h1 { margin-top:42px; font-size:94px; line-height:1.08; }
    .doc-thesis pre { white-space:pre-wrap; margin-top:44px; padding:28px; border-left:8px solid #0066ff; background:white; color:#4b5563; font:32px/1.45 "Noto Sans CJK SC",sans-serif; }
    .doc-body article { margin-top:46px; display:grid; gap:18px; }
    .doc-body article p { background:white; border:1px solid #e5e7eb; border-radius:10px; padding:26px; font-size:32px; line-height:1.45; }
    .doc-body aside { position:absolute; left:0; right:0; bottom:110px; color:#4b5563; font-size:28px; }
    .doc-pipeline section { margin-top:52px; display:grid; gap:18px; border-left:4px solid #0066ff; padding-left:30px; }
    .doc-step { min-height:106px; display:grid; grid-template-columns:64px 1fr; padding:22px 26px; background:white; border:1px solid #e5e7eb; border-radius:10px; }
    .doc-step h2 { font-size:32px; line-height:1.25; }
    .doc-step p { color:#4b5563; font-size:24px; margin-top:8px; }
    .doc-quote { display:flex; flex-direction:column; justify-content:center; }
    .doc-quote p { margin-top:38px; color:#4b5563; font-size:36px; line-height:1.45; }
    .doc-ending section { margin-top:50px; display:grid; gap:14px; }
    .doc-check { display:grid; grid-template-columns:58px 1fr; align-items:center; min-height:78px; border-bottom:1px solid #e5e7eb; }
    .doc-check p { font-size:28px; font-weight:800; }
    .doc-ending div { position:absolute; left:0; right:0; bottom:100px; background:#0066ff; color:#fff; padding:26px 32px; border-radius:10px; font-size:28px; font-weight:900; }
    """,
    "rednote-tech": """
    .pack-rednote-tech { --x:72px; --top:84px; --image-panel:rgba(15,24,52,.72); --image-border:1px solid rgba(0,212,255,.28); --image-hair:1px solid rgba(0,212,255,.22); --image-chip:rgba(15,24,52,.66); --image-muted:rgba(240,244,255,.62); --image-accent:#00d4ff; --image-shadow:0 0 28px rgba(0,212,255,.07); background:linear-gradient(165deg,#0a0e1a,#101936 56%,#0d1225); color:#f0f4ff; }
    .tech-grid { position:absolute; inset:0; opacity:.08; background-image:linear-gradient(#00d4ff 1px,transparent 1px),linear-gradient(90deg,#00d4ff 1px,transparent 1px); background-size:64px 64px; }
    .tech-glow { position:absolute; right:-160px; top:-110px; width:440px; height:440px; border-radius:50%; background:rgba(0,212,255,.16); filter:blur(48px); }
    .pack-rednote-tech .meta { color:rgba(240,244,255,.62); border-bottom:1px solid rgba(0,212,255,.25); padding-bottom:28px; }
    .pack-rednote-tech .footer { color:rgba(240,244,255,.62); border-top:1px solid rgba(0,212,255,.25); padding-top:24px; }
    .tech { position:absolute; z-index:2; left:72px; right:72px; top:176px; bottom:126px; }
    .tech code { color:#00d4ff; font:800 20px "Noto Sans Mono CJK SC",monospace; }
    .tech h1 { color:#f0f4ff; font-size:72px; line-height:1.12; font-weight:900; }
    .pack-rednote-tech .asset-frame,.pack-rednote-tech .asset-points article { border-radius:18px; }
    .tech-cover h1 { margin-top:58px; font-size:78px; max-width:900px; }
    .tech-cover p,.tech > p { color:rgba(240,244,255,.62); font-size:32px; line-height:1.38; margin-top:22px; }
    .tech-cover section { position:absolute; left:0; right:0; bottom:108px; display:grid; gap:16px; }
    .tech-cover section span,.node,.node-step,.tech-body article p { border:1px solid rgba(0,212,255,.24); background:rgba(15,24,52,.68); border-radius:18px; }
    .tech-cover section span { padding:22px 28px; color:#f0f4ff; font-size:28px; font-weight:800; }
    .tech-thesis,.tech-quote { display:flex; flex-direction:column; justify-content:center; }
    .tech-thesis h1,.tech-quote h1 { margin-top:34px; font-size:96px; line-height:1.08; }
    .tech-thesis p,.tech-quote p { max-width:820px; color:#00d4ff; font-size:36px; line-height:1.42; }
    .tech-list section,.tech-pipeline section { margin-top:52px; display:grid; gap:18px; }
    .node { min-height:88px; display:grid; grid-template-columns:58px 1fr; align-items:center; padding:20px 28px; }
    .node span,.node-step span { color:#00d4ff; font:800 22px "Noto Sans Mono CJK SC",monospace; }
    .node p { color:#f0f4ff; font-size:30px; line-height:1.3; font-weight:850; }
    .node-step { min-height:108px; display:grid; grid-template-columns:64px 1fr; padding:22px 28px; box-shadow:0 0 28px rgba(0,212,255,.07); }
    .node-step h2 { color:#f0f4ff; font-size:32px; line-height:1.25; }
    .node-step p { color:rgba(240,244,255,.62); font-size:24px; margin-top:8px; }
    .tech-body article { margin-top:44px; display:grid; gap:18px; }
    .tech-body article p { padding:26px 30px; color:#f0f4ff; font-size:32px; line-height:1.42; }
    .tech-body aside { position:absolute; left:0; right:0; bottom:110px; color:rgba(240,244,255,.62); font-size:28px; }
    .tech-ending section { margin-top:50px; display:grid; gap:16px; }
    .tech-ending div { position:absolute; left:0; right:0; bottom:100px; background:#00d4ff; color:#06101f; padding:26px 32px; font-size:28px; font-weight:900; box-shadow:0 0 42px rgba(0,212,255,.18); }
    """,
}
