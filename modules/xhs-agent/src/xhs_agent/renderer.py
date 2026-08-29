from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .assets import prepare_render_assets
from .recipe_packs import (
    recipe_pack_choice as choose_recipe_pack_recipe,
    render_recipe_pack_card as render_recipe_pack_document,
)
from .schemas import CardPlan, CardPlanItem, SocialContentRequest


WIDTH = 1080
HEIGHT = 1440
SC_FACE_INDEX = 2

FONT_FILES = {
    "sans": {
        "regular": [("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", SC_FACE_INDEX)],
        "medium": [("/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc", SC_FACE_INDEX)],
        "bold": [("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", SC_FACE_INDEX)],
    },
    "serif": {
        "regular": [("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc", SC_FACE_INDEX)],
        "bold": [("/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc", SC_FACE_INDEX)],
    },
    "mono": {
        "regular": [
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 7),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0),
        ],
        "bold": [
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 7),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 0),
        ],
    },
}

THEMES = {
    "paper": {
        "paper": "#F3F0E8",
        "paper2": "#E8E1D3",
        "ink": "#111111",
        "muted": "#6B6257",
        "line": "#C8BFB1",
        "accent": "#202020",
        "accent_on": "#FFFFFF",
        "soft": "#DDD4C6",
    },
    "indigo": {
        "paper": "#F2F4F5",
        "paper2": "#E3EAF0",
        "ink": "#0A1F3D",
        "muted": "#5F6D78",
        "line": "#B8C6D1",
        "accent": "#315D93",
        "accent_on": "#FFFFFF",
        "soft": "#D7E1EC",
    },
    "forest": {
        "paper": "#F5F1E8",
        "paper2": "#E6DDCE",
        "ink": "#16251B",
        "muted": "#5D665D",
        "line": "#BBC9BB",
        "accent": "#2E6B4F",
        "accent_on": "#FFFFFF",
        "soft": "#D4DFD2",
    },
    "swiss_blue": {
        "paper": "#FAFAF8",
        "paper2": "#EFEFED",
        "ink": "#0A0A0A",
        "muted": "#6F6F6A",
        "line": "#D4D4D2",
        "accent": "#002FA7",
        "accent_on": "#FFFFFF",
        "soft": "#E5ECFF",
    },
}


def render_cards(
    request: SocialContentRequest,
    card_plan: CardPlan,
    out_dir: Path,
    template_id: str | None = None,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    renderer = os.getenv("XHS_AGENT_RENDERER", "html").lower()
    template_id = template_id or os.getenv("XHS_AGENT_TEMPLATE_ID")
    if renderer != "pillow":
        try:
            if template_id and template_id != "default":
                return render_cards_from_template(request, card_plan, out_dir, template_id)
            return render_cards_html(request, card_plan, out_dir)
        except Exception as exc:
            (out_dir / "render_warning.txt").write_text(
                f"HTML renderer failed, fell back to Pillow: {exc}\n",
                encoding="utf-8",
            )
            if renderer == "html-strict":
                raise
    return render_cards_pillow(request, card_plan, out_dir)


def render_cards_from_template(
    request: SocialContentRequest,
    card_plan: CardPlan,
    out_dir: Path,
    template_id: str,
) -> list[Path]:
    entry = template_entry(template_id)
    if entry.get("type") == "recipe_pack":
        return render_cards_recipe_pack(request, card_plan, out_dir, entry)

    template_path = template_root() / entry["path"]
    template = template_path.read_text(encoding="utf-8")

    html_dir = out_dir / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    clean_previous_cards(out_dir, html_dir)
    (html_dir / "template_id.txt").write_text(template_id + "\n", encoding="utf-8")

    html_paths = []
    for card in card_plan.cards:
        html_path = html_dir / f"xhs-{card.page:02d}-{safe_name(card.role)}.html"
        html_path.write_text(fill_template(template, request, card, len(card_plan.cards)), encoding="utf-8")
        html_paths.append((card, html_path))

    return screenshot_html_cards(html_paths, out_dir, ".card")


def render_cards_recipe_pack(
    request: SocialContentRequest,
    card_plan: CardPlan,
    out_dir: Path,
    entry: dict,
) -> list[Path]:
    html_dir = out_dir / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    clean_previous_cards(out_dir, html_dir)
    template_id = entry["id"]
    (html_dir / "template_id.txt").write_text(template_id + "\n", encoding="utf-8")
    asset_map = prepare_render_assets(request, out_dir.parent / "assets", html_dir)

    html_paths = []
    for card in card_plan.cards:
        recipe = choose_recipe_pack_recipe(card, entry)
        html_path = html_dir / f"xhs-{card.page:02d}-{safe_name(card.role)}-{recipe}.html"
        html_path.write_text(
            render_recipe_pack_document(request, card, len(card_plan.cards), entry, recipe, asset_map),
            encoding="utf-8",
        )
        html_paths.append((card, html_path))

    return screenshot_html_cards(html_paths, out_dir, ".card")


def screenshot_html_cards(
    html_paths: list[tuple[CardPlanItem, Path]],
    out_dir: Path,
    selector: str,
) -> list[Path]:
    from research_connect_core import configure_playwright_browsers
    configure_playwright_browsers()
    from playwright.sync_api import sync_playwright

    paths: list[Path] = []
    executable = chrome_executable()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
        for card, html_path in html_paths:
            path = out_dir / f"xhs-{card.page:02d}-{safe_name(card.role)}.png"
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            page.locator(selector).first.screenshot(path=str(path))
            paths.append(path)
        browser.close()
    return paths


def fill_template(template: str, request: SocialContentRequest, card: CardPlanItem, total: int) -> str:
    points = point_values(request, card)
    values = {
        "TITLE": card.headline or request.source.title,
        "SUBTITLE": card.subtitle or request.source.summary,
        "KICKER": f"{intent_label(request.intent.value)} / {role_label(card.role)}",
        "SOURCE": source_marker(request),
        "FOOTER": footer_text(request),
        "BADGE": role_label(card.role),
        "SIGNATURE": source_marker(request),
        "PAGE": f"{card.page:02d}",
        "TOTAL": f"{total:02d}",
    }
    for idx in range(1, 9):
        values[f"POINT_{idx}"] = points[idx - 1] if idx <= len(points) else ""

    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", escape_template_value(value))
    return re.sub(r"\{\{[A-Z0-9_]+\}\}", "", rendered)


def point_values(request: SocialContentRequest, card: CardPlanItem) -> list[str]:
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
    return values[:8]


def escape_template_value(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def template_root() -> Path:
    if os.getenv("XHS_AGENT_TEMPLATE_ROOT"):
        return Path(os.environ["XHS_AGENT_TEMPLATE_ROOT"])
    return Path(__file__).resolve().parents[2] / "templates" / "xhs"


def template_manifest() -> dict:
    return json.loads((template_root() / "manifest.json").read_text(encoding="utf-8"))


def template_entry(template_id: str) -> dict:
    for entry in template_manifest()["templates"]:
        if entry["id"] == template_id:
            if entry.get("status") != "ready":
                raise ValueError(f"Template is not renderer-ready: {template_id}")
            return entry
    raise ValueError(f"Unknown XHS template_id: {template_id}")


def render_cards_html(request: SocialContentRequest, card_plan: CardPlan, out_dir: Path) -> list[Path]:
    html_dir = out_dir / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    clean_previous_cards(out_dir, html_dir)
    theme = theme_for(request)
    html_paths = []
    for card in card_plan.cards:
        html_path = html_dir / f"xhs-{card.page:02d}-{safe_name(card.role)}.html"
        html_path.write_text(render_card_html(request, card, theme), encoding="utf-8")
        html_paths.append((card, html_path))

    return screenshot_html_cards(html_paths, out_dir, ".poster")


def render_cards_pillow(request: SocialContentRequest, card_plan: CardPlan, out_dir: Path) -> list[Path]:
    clean_previous_cards(out_dir, out_dir / "html")
    paths: list[Path] = []
    theme = theme_for(request)
    for card in card_plan.cards:
        path = out_dir / f"xhs-{card.page:02d}-{safe_name(card.role)}.png"
        render_card(request, card, path, theme)
        paths.append(path)
    return paths


def clean_previous_cards(out_dir: Path, html_dir: Path) -> None:
    for pattern in ("xhs-*.png", "render_warning.txt"):
        for path in out_dir.glob(pattern):
            if path.is_file():
                path.unlink()
    if html_dir.exists():
        for path in html_dir.glob("xhs-*.html"):
            if path.is_file():
                path.unlink()


def chrome_executable() -> str | None:
    for candidate in [
        os.getenv("XHS_AGENT_CHROME"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def render_card_html(request: SocialContentRequest, card: CardPlanItem, theme: dict[str, str]) -> str:
    recipe = recipe_for(card)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=1080, initial-scale=1">
  <title>{escape(card.headline)}</title>
  <style>
    {base_css(theme)}
  </style>
</head>
<body>
  <section class="poster {recipe}">
    <div class="top-bar"></div>
    <div class="grain"></div>
    <header class="issue">
      <span>{escape(intent_label(request.intent.value))} / {escape(role_label(card.role))}</span>
      <span>{card.page:02d}/{request.requirements.card_count:02d}</span>
    </header>
    {card_body_html(request, card, recipe)}
    <footer class="footer">{escape(footer_text(request))}</footer>
  </section>
</body>
</html>
"""


def card_body_html(request: SocialContentRequest, card: CardPlanItem, recipe: str) -> str:
    if recipe == "cover":
        return cover_html(request, card)
    if recipe == "closing":
        return closing_html(card)
    if recipe == "bento":
        return bento_html(card)
    if recipe == "pullquote":
        return pullquote_html(card)
    return ledger_html(card)


def cover_html(request: SocialContentRequest, card: CardPlanItem) -> str:
    bullets = card.bullets[:4] or [request.source.summary]
    return f"""
    <main class="content cover-content">
      <p class="kicker">{escape(source_marker(request))}</p>
      <h1>{escape(card.headline)}</h1>
      <p class="subtitle">{escape(card.subtitle)}</p>
      <div class="accent-rule"></div>
      <ol class="teasers">
        {''.join(f'<li><span>{idx:02d}</span><strong>{escape(item)}</strong></li>' for idx, item in enumerate(bullets, 1))}
      </ol>
    </main>
    """


def ledger_html(card: CardPlanItem) -> str:
    bullets = card.bullets[:6] or ["发布前人工核对关键信息"]
    return f"""
    <main class="content ledger-content">
      <h1>{escape(card.headline)}</h1>
      <p class="subtitle">{escape(card.subtitle)}</p>
      <div class="ledger-list">
        {''.join(f'<div class="ledger-row"><span>{idx:02d}</span><p>{escape(item)}</p></div>' for idx, item in enumerate(bullets, 1))}
      </div>
      <div class="side-label">{escape(role_label(card.role))}</div>
    </main>
    """


def bento_html(card: CardPlanItem) -> str:
    bullets = card.bullets[:4] or ["发布前人工核对关键信息"]
    return f"""
    <main class="content bento-content">
      <h1>{escape(card.headline)}</h1>
      <p class="subtitle">{escape(card.subtitle)}</p>
      <div class="bento-grid">
        {''.join(bento_item_html(idx, item) for idx, item in enumerate(bullets, 1))}
      </div>
    </main>
    """


def bento_item_html(idx: int, item: str) -> str:
    first, rest = split_bullet(item)
    return f"""
    <article class="bento-item">
      <span>{idx:02d}</span>
      <h2>{escape(first)}</h2>
      <p>{escape(rest)}</p>
    </article>
    """


def closing_html(card: CardPlanItem) -> str:
    bullets = card.bullets[:5] or ["收藏后再读原始材料", "发布前核对关键事实"]
    return f"""
    <main class="content closing-content">
      <h1>{escape(card.headline)}</h1>
      <p class="subtitle">{escape(card.subtitle)}</p>
      <div class="ledger-list compact">
        {''.join(f'<div class="ledger-row"><span>{idx}</span><p>{escape(item)}</p></div>' for idx, item in enumerate(bullets, 1))}
      </div>
      <div class="confirm-band">人工确认后再发布</div>
    </main>
    """


def pullquote_html(card: CardPlanItem) -> str:
    prompt = card.bullets[0] if card.bullets else card.subtitle
    return f"""
    <main class="content pullquote-content">
      <p class="kicker">{escape(role_label(card.role))}</p>
      <h1>{escape(card.headline)}</h1>
      <p class="subtitle">{escape(prompt)}</p>
    </main>
    """


def recipe_for(card: CardPlanItem) -> str:
    if card.role == "cover":
        return "cover"
    if card.role == "cta":
        return "closing"
    if card.role in {"method", "result", "value"} and len(card.bullets) <= 4:
        return "bento"
    if card.role in {"question", "thesis"}:
        return "pullquote"
    return "ledger"


def base_css(theme: dict[str, str]) -> str:
    return f"""
    @page {{ size: 1080px 1440px; margin: 0; }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; width: 1080px; height: 1440px; background: #111; }}
    body {{
      font-family: "Inter", "Noto Sans CJK SC", "Noto Sans SC", -apple-system, "PingFang SC", "Microsoft YaHei UI", sans-serif;
      -webkit-font-smoothing: antialiased;
      text-rendering: geometricPrecision;
    }}
    .poster {{
      position: relative;
      width: 1080px;
      height: 1440px;
      overflow: hidden;
      color: {theme["ink"]};
      background: {theme["paper"]};
      isolation: isolate;
    }}
    .top-bar {{ position: absolute; inset: 0 0 auto 0; height: 28px; background: {theme["accent"]}; }}
    .grain {{
      position: absolute;
      inset: 0;
      opacity: .20;
      background-image: radial-gradient({theme["paper2"]} 1px, transparent 1.5px);
      background-size: 24px 24px;
      pointer-events: none;
    }}
    .issue {{
      position: absolute;
      top: 84px;
      left: 72px;
      right: 72px;
      display: flex;
      justify-content: space-between;
      border-bottom: 2px solid {theme["line"]};
      padding-bottom: 28px;
      font-family: "Noto Sans Mono CJK SC", "IBM Plex Mono", monospace;
      font-size: 22px;
      color: {theme["muted"]};
      letter-spacing: .02em;
    }}
    .issue span:last-child {{ color: {theme["accent"]}; }}
    .content {{ position: absolute; left: 72px; right: 72px; top: 176px; bottom: 150px; z-index: 2; }}
    .footer {{
      position: absolute;
      left: 72px;
      right: 72px;
      bottom: 54px;
      border-top: 2px solid {theme["line"]};
      padding-top: 28px;
      font-size: 26px;
      color: {theme["muted"]};
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    h1 {{
      margin: 0;
      font-family: "Noto Serif CJK SC", "Noto Serif SC", "Songti SC", serif;
      font-weight: 700;
      letter-spacing: 0;
      color: {theme["ink"]};
    }}
    .subtitle {{
      margin: 20px 0 0;
      font-size: 34px;
      line-height: 1.35;
      color: {theme["accent"]};
    }}
    .kicker {{
      margin: 0 0 26px;
      font-family: "Noto Sans Mono CJK SC", "IBM Plex Mono", monospace;
      font-size: 22px;
      color: {theme["muted"]};
    }}
    .accent-rule {{ height: 8px; width: 100%; margin: 126px 0 70px; background: {theme["accent"]}; }}
    .cover-content h1 {{ margin-top: 82px; font-size: clamp(76px, 8.3vw, 98px); line-height: 1.12; max-width: 920px; }}
    .cover-content .subtitle {{ font-weight: 600; font-size: 38px; }}
    .teasers {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 26px; }}
    .teasers li {{ display: grid; grid-template-columns: 76px 1fr; align-items: baseline; }}
    .teasers span {{
      font-family: "Noto Sans Mono CJK SC", monospace;
      font-size: 24px;
      color: {theme["muted"]};
    }}
    .teasers strong {{ font-size: 40px; line-height: 1.25; }}
    .ledger-content h1, .bento-content h1, .closing-content h1 {{ margin-top: 48px; font-size: 72px; line-height: 1.14; }}
    .ledger-list {{ margin-top: 78px; border-top: 2px solid {theme["line"]}; }}
    .ledger-row {{
      min-height: 122px;
      display: grid;
      grid-template-columns: 86px 1fr;
      align-items: center;
      border-bottom: 2px solid {theme["line"]};
      padding: 24px 8px;
    }}
    .ledger-row span {{
      font-family: "Noto Sans Mono CJK SC", monospace;
      font-size: 24px;
      color: {theme["accent"]};
    }}
    .ledger-row p {{ margin: 0; font-size: 40px; line-height: 1.28; font-weight: 600; }}
    .side-label {{
      position: absolute;
      left: -54px;
      bottom: 240px;
      transform: rotate(90deg);
      transform-origin: left top;
      color: rgba(255,255,255,.65);
      font-size: 18px;
      letter-spacing: .12em;
    }}
    .bento-grid {{ margin-top: 128px; display: grid; grid-template-columns: 1fr 1fr; gap: 48px; }}
    .bento-item {{
      min-height: 288px;
      border-radius: 20px;
      padding: 34px;
      background: {theme["paper2"]};
    }}
    .bento-item:nth-child(even) {{ background: {theme["soft"]}; }}
    .bento-item span {{ font-family: "Noto Sans Mono CJK SC", monospace; font-size: 22px; color: {theme["accent"]}; }}
    .bento-item h2 {{ margin: 36px 0 16px; font-size: 38px; line-height: 1.2; }}
    .bento-item p {{ margin: 0; font-size: 28px; line-height: 1.42; color: {theme["muted"]}; }}
    .closing-content h1 {{ margin-top: 72px; font-size: 82px; }}
    .compact {{ margin-top: 64px; }}
    .compact .ledger-row {{ min-height: 106px; }}
    .compact .ledger-row p {{ font-size: 34px; }}
    .confirm-band {{
      position: absolute;
      left: 0;
      right: 0;
      bottom: 58px;
      border-radius: 12px;
      background: {theme["accent"]};
      color: {theme["accent_on"]};
      padding: 30px 36px;
      font-size: 34px;
      font-weight: 700;
    }}
    .pullquote-content {{
      display: flex;
      flex-direction: column;
      justify-content: center;
    }}
    .pullquote-content h1 {{ font-size: 118px; line-height: 1.08; }}
    .pullquote-content .subtitle {{ font-size: 36px; max-width: 820px; }}
    """


def escape(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def render_card(request: SocialContentRequest, card: CardPlanItem, path: Path, theme: dict[str, str]) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), theme["paper"])
    draw = ImageDraw.Draw(image)
    draw_grain(draw, theme)

    if card.role == "cover":
        render_cover(draw, request, card, theme)
    elif card.role == "cta":
        render_closing(draw, request, card, theme)
    elif card.role in {"method", "result", "value"} and len(card.bullets) <= 4:
        render_bento(draw, request, card, theme)
    else:
        render_ledger(draw, request, card, theme)

    draw_footer(draw, request, card, theme)
    image.save(path)


def render_cover(
    draw: ImageDraw.ImageDraw,
    request: SocialContentRequest,
    card: CardPlanItem,
    theme: dict[str, str],
) -> None:
    draw_issue_header(draw, request, card, theme)
    title_font = fit_font(draw, card.headline, "serif", "bold", 92, 72, 900, max_lines=3)
    subtitle_font = load_font("sans", 36, "medium")
    bullet_font = load_font("sans", 36, "medium")
    small_font = load_font("mono", 22, "regular")

    y = 238
    y = draw_wrapped(draw, card.headline, (72, y), title_font, theme["ink"], max_width=910, line_gap=12, max_lines=3)
    if card.subtitle:
        y += 28
        y = draw_wrapped(draw, card.subtitle, (76, y), subtitle_font, theme["accent"], max_width=900, line_gap=12, max_lines=2)

    draw.rectangle((72, 650, 1008, 658), fill=theme["accent"])
    y = 724
    bullets = card.bullets[:4] or [request.source.summary]
    for idx, bullet in enumerate(bullets, start=1):
        number = f"{idx:02d}"
        draw.text((78, y + 4), number, font=small_font, fill=theme["muted"])
        y = draw_wrapped(draw, bullet, (164, y), bullet_font, theme["ink"], max_width=780, line_gap=10, max_lines=2) + 32

    draw.text((72, 1224), source_marker(request), font=load_font("mono", 24, "regular"), fill=theme["muted"])


def render_ledger(
    draw: ImageDraw.ImageDraw,
    request: SocialContentRequest,
    card: CardPlanItem,
    theme: dict[str, str],
) -> None:
    draw_issue_header(draw, request, card, theme)
    title_font = fit_font(draw, card.headline, "serif", "bold", 68, 54, 900, max_lines=2)
    subtitle_font = load_font("sans", 30, "regular")
    item_font = load_font("sans", 40, "medium")
    meta_font = load_font("mono", 22, "regular")

    y = 202
    y = draw_wrapped(draw, card.headline, (72, y), title_font, theme["ink"], max_width=900, line_gap=10, max_lines=2)
    if card.subtitle:
        y += 20
        y = draw_wrapped(draw, card.subtitle, (74, y), subtitle_font, theme["accent"], max_width=900, line_gap=8, max_lines=2)

    y = max(y + 68, 420)
    bullets = card.bullets[:6] or ["发布前人工核对关键信息"]
    row_h = min(150, max(118, (1260 - y) // max(len(bullets), 1)))
    for idx, bullet in enumerate(bullets, start=1):
        row_top = y + (idx - 1) * row_h
        draw.line((72, row_top, 1008, row_top), fill=theme["line"], width=2)
        draw.text((80, row_top + 34), f"{idx:02d}", font=meta_font, fill=theme["accent"])
        draw_wrapped(draw, bullet, (176, row_top + 24), item_font, theme["ink"], max_width=760, line_gap=8, max_lines=2)
    draw.line((72, y + len(bullets) * row_h, 1008, y + len(bullets) * row_h), fill=theme["line"], width=2)

    role = role_label(card.role)
    draw_rotated_label(draw, role, (40, 920), theme)


def render_bento(
    draw: ImageDraw.ImageDraw,
    request: SocialContentRequest,
    card: CardPlanItem,
    theme: dict[str, str],
) -> None:
    draw_issue_header(draw, request, card, theme)
    title_font = fit_font(draw, card.headline, "sans", "bold", 66, 52, 900, max_lines=2)
    subtitle_font = load_font("sans", 30, "regular")
    item_title_font = load_font("sans", 38, "bold")
    item_font = load_font("sans", 28, "regular")
    mono_font = load_font("mono", 20, "regular")

    y = 202
    y = draw_wrapped(draw, card.headline, (72, y), title_font, theme["ink"], max_width=900, line_gap=10, max_lines=2)
    if card.subtitle:
        y += 18
        draw_wrapped(draw, card.subtitle, (74, y), subtitle_font, theme["muted"], max_width=900, line_gap=8, max_lines=2)

    bullets = card.bullets[:4] or ["发布前人工核对关键信息"]
    boxes = [
        (72, 500, 516, 790),
        (564, 500, 1008, 790),
        (72, 838, 516, 1128),
        (564, 838, 1008, 1128),
    ]
    for idx, box in enumerate(boxes[: len(bullets)], start=1):
        x1, y1, x2, y2 = box
        fill = theme["paper2"] if idx % 2 else theme["soft"]
        draw.rounded_rectangle(box, radius=18, fill=fill)
        draw.text((x1 + 34, y1 + 28), f"{idx:02d}", font=mono_font, fill=theme["accent"])
        text = bullets[idx - 1]
        first, rest = split_bullet(text)
        draw_wrapped(draw, first, (x1 + 34, y1 + 80), item_title_font, theme["ink"], max_width=360, line_gap=8, max_lines=2)
        if rest:
            draw_wrapped(draw, rest, (x1 + 34, y1 + 184), item_font, theme["muted"], max_width=360, line_gap=8, max_lines=2)


def render_closing(
    draw: ImageDraw.ImageDraw,
    request: SocialContentRequest,
    card: CardPlanItem,
    theme: dict[str, str],
) -> None:
    draw_issue_header(draw, request, card, theme)
    title_font = fit_font(draw, card.headline, "serif", "bold", 78, 60, 900, max_lines=2)
    subtitle_font = load_font("serif", 36, "regular")
    item_font = load_font("sans", 34, "medium")
    mono_font = load_font("mono", 22, "regular")

    y = 250
    y = draw_wrapped(draw, card.headline, (72, y), title_font, theme["ink"], max_width=900, line_gap=12, max_lines=2)
    if card.subtitle:
        y += 26
        y = draw_wrapped(draw, card.subtitle, (76, y), subtitle_font, theme["accent"], max_width=900, line_gap=12, max_lines=2)

    y += 70
    bullets = card.bullets[:5] or ["收藏后再读原始材料", "发布前核对关键事实"]
    for idx, bullet in enumerate(bullets, start=1):
        draw.line((72, y, 1008, y), fill=theme["line"], width=2)
        draw.text((78, y + 30), f"{idx}", font=mono_font, fill=theme["accent"])
        draw_wrapped(draw, bullet, (146, y + 22), item_font, theme["ink"], max_width=810, line_gap=8, max_lines=2)
        y += 116

    draw.rounded_rectangle((72, 1152, 1008, 1248), radius=12, fill=theme["accent"])
    draw.text((108, 1184), "人工确认后再发布", font=load_font("sans", 32, "bold"), fill=theme["accent_on"])


def draw_issue_header(
    draw: ImageDraw.ImageDraw,
    request: SocialContentRequest,
    card: CardPlanItem,
    theme: dict[str, str],
) -> None:
    mono = load_font("mono", 22, "regular")
    draw.rectangle((0, 0, WIDTH, 28), fill=theme["accent"])
    label = f"{intent_label(request.intent.value)} / {role_label(card.role)}"
    draw.text((72, 84), label.upper(), font=mono, fill=theme["muted"])
    draw.text((806, 84), f"{card.page:02d}/{request.requirements.card_count:02d}", font=mono, fill=theme["accent"])
    draw.line((72, 142, 1008, 142), fill=theme["line"], width=2)


def draw_footer(
    draw: ImageDraw.ImageDraw,
    request: SocialContentRequest,
    card: CardPlanItem,
    theme: dict[str, str],
) -> None:
    small = load_font("sans", 26, "regular")
    draw.line((72, 1310, 1008, 1310), fill=theme["line"], width=2)
    draw_wrapped(draw, footer_text(request), (72, 1340), small, theme["muted"], max_width=900, line_gap=8, max_lines=2)


def draw_grain(draw: ImageDraw.ImageDraw, theme: dict[str, str]) -> None:
    for x in range(0, WIDTH, 28):
        for y in range(0, HEIGHT, 28):
            if (x * 13 + y * 7) % 5 == 0:
                draw.point((x + 3, y + 5), fill=theme["paper2"])


def draw_rotated_label(draw: ImageDraw.ImageDraw, text: str, xy: tuple[int, int], theme: dict[str, str]) -> None:
    font = load_font("mono", 18, "regular")
    bbox = draw.textbbox((0, 0), text, font=font)
    label = Image.new("RGBA", (bbox[2] - bbox[0] + 24, bbox[3] - bbox[1] + 24), (0, 0, 0, 0))
    label_draw = ImageDraw.Draw(label)
    label_draw.text((12, 8), text, font=font, fill=theme["muted"])
    rotated = label.rotate(90, expand=True)
    draw.bitmap(xy, rotated, fill=None)


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
    *,
    max_width: int,
    line_gap: int,
    max_lines: int | None = None,
) -> int:
    x, y = xy
    lines = wrap_text(draw, text, font, max_width)
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = trim_to_width(draw, lines[-1] + "...", font, max_width)
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        bbox = draw.textbbox((x, y), line, font=font)
        y += bbox[3] - bbox[1] + line_gap
    return y


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int) -> list[str]:
    text = " ".join(text.strip().split())
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


def trim_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> str:
    while text and draw.textbbox((0, 0), text, font=font)[2] > max_width:
        text = text[:-4] + "..."
    return text


def fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    family: str,
    weight: str,
    start: int,
    floor: int,
    max_width: int,
    *,
    max_lines: int,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for size in range(start, floor - 1, -4):
        font = load_font(family, size, weight)
        if len(wrap_text(draw, text, font, max_width)) <= max_lines:
            return font
    return load_font(family, floor, weight)


def load_font(family: str, size: int, weight: str = "regular") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = FONT_FILES.get(family, {}).get(weight, [])
    candidates += FONT_FILES["sans"]["regular"]
    for candidate, index in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size, index=index)
    return ImageFont.load_default()


def split_bullet(text: str) -> tuple[str, str]:
    for sep in ["：", ":"]:
        if sep in text:
            first, rest = text.split(sep, 1)
            return first.strip() + sep, rest.strip()
    return text, ""


def theme_for(request: SocialContentRequest) -> dict[str, str]:
    if request.intent.value == "daily_paper":
        return THEMES["indigo"]
    if request.intent.value == "lab_recruit":
        return THEMES["forest"]
    if request.intent.value == "project_promo":
        return THEMES["swiss_blue"]
    return THEMES["paper"]


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


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)[:32] or "card"
