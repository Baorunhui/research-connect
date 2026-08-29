import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read_css() -> str:
    return (ROOT / "app" / "app.css").read_text(encoding="utf-8")


def _section_between(css: str, start_marker: str, end_marker: str) -> str:
    return css.split(start_marker, 1)[1].split(end_marker, 1)[0]


def _rule_body(css: str, selector: str) -> str:
    pattern = re.compile(re.escape(selector) + r"\s*\{([^}]*)\}", re.S)
    match = pattern.search(css)
    assert match, f"missing selector: {selector}"
    return match.group(1)


def test_home_dashboard_uses_a_two_by_two_desktop_grid():
    css = _read_css()
    section = _section_between(css, "/* 首页仪表盘卡片 */", "/* 侧边栏字体放大 */")

    grid_rule = _rule_body(section, ".markdown-section .dpr-home-dashboard-grid")
    assert "display: grid" in grid_rule
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in grid_rule
    assert "grid-template-rows: repeat(2, minmax(0, 1fr))" in grid_rule
    assert "align-items: stretch" in grid_rule


def test_home_dashboard_cards_keep_a_quiet_equal_height_surface():
    css = _read_css()
    section = _section_between(css, "/* 首页仪表盘卡片 */", "/* 侧边栏字体放大 */")

    shared_selector = ".markdown-section .dpr-home-dashboard-card"
    shared_rule = _rule_body(section, shared_selector)
    assert "display: flex" in shared_rule
    assert "flex-direction: column" in shared_rule
    assert "height: 100%" in shared_rule
    assert "position: relative" in shared_rule
    assert "overflow: hidden" in shared_rule
    assert "background: #ffffff" in shared_rule
    assert "border: 1px solid #d9dee3" in shared_rule
    assert "border-top: 4px solid var(--dpr-home-dashboard-accent-soft)" in shared_rule
    assert "border-radius: 8px" in shared_rule
    assert "box-shadow:" in shared_rule
    assert shared_rule.count("rgba(") >= 2
    assert re.search(r"box-shadow:\s*[^;]*0\s+[34]px\s+0", shared_rule)
    assert "border-left" not in shared_rule
    assert "gradient" not in shared_rule.lower()


def test_home_dashboard_variants_only_use_semantic_accent_colors():
    css = _read_css()
    section = _section_between(css, "/* 首页仪表盘卡片 */", "/* 侧边栏字体放大 */")

    title_rule = _rule_body(section, ".markdown-section .dpr-home-dashboard-title")
    count_rule = _rule_body(section, ".markdown-section .dpr-home-dashboard-count")
    assert "color: var(--dpr-home-dashboard-accent)" in title_rule
    assert "color: var(--dpr-home-dashboard-accent)" in count_rule

    expected_palette = (
        (".markdown-section .dpr-home-report-card", "#16a34a", "#86efac"),
        (".markdown-section .dpr-home-brief-card", "#2563eb", "#93c5fd"),
        (".markdown-section .dpr-home-deep-card", "#7c3aed", "#c4b5fd"),
        (".markdown-section .dpr-home-skim-card", "#dc2626", "#fca5a5"),
    )
    for selector, accent, soft in expected_palette:
        rule = _rule_body(section, selector)
        assert f"--dpr-home-dashboard-accent: {accent}" in rule
        assert f"--dpr-home-dashboard-accent-soft: {soft}" in rule
    shared_rule_start = section.index(".markdown-section .dpr-home-dashboard-card {")
    shared_rule_end = section.index("}", shared_rule_start)
    for selector in (
        ".markdown-section .dpr-home-report-card",
        ".markdown-section .dpr-home-brief-card",
        ".markdown-section .dpr-home-deep-card",
        ".markdown-section .dpr-home-skim-card",
    ):
        assert section.index(f"{selector} {{") > shared_rule_end


def test_home_dashboard_palette_matches_active_sidebar_status_buttons():
    css = _read_css()
    pairs = (
        ("report", "good"),
        ("brief", "blue"),
        ("deep", "orange"),
        ("skim", "bad"),
    )
    for card, status in pairs:
        card_rule = _rule_body(css, f".markdown-section .dpr-home-{card}-card")
        status_rule = _rule_body(css, f".dpr-sidebar-paper-status-{status}.is-active")
        accent = re.search(r"--dpr-home-dashboard-accent:\s*(#[0-9a-f]+)", card_rule)
        soft = re.search(r"--dpr-home-dashboard-accent-soft:\s*(#[0-9a-f]+)", card_rule)
        assert accent and f"color: {accent.group(1)}" in status_rule
        assert soft and f"background: {soft.group(1)}" in status_rule


def test_home_dashboard_stats_are_not_nested_cards():
    css = _read_css()
    section = _section_between(css, "/* 首页仪表盘卡片 */", "/* 侧边栏字体放大 */")
    stat_rule = _rule_body(section, ".markdown-section .dpr-home-dashboard-stat")
    assert "background" not in stat_rule
    assert "border-radius" not in stat_rule
    assert "border: 1px" not in stat_rule
    assert "flex-direction: column" in stat_rule


def test_home_dashboard_layout_keeps_wrapped_tags_and_truncated_non_link_titles():
    css = _read_css()
    section = _section_between(css, "/* 首页仪表盘卡片 */", "/* 侧边栏字体放大 */")

    body_rule = _rule_body(section, ".markdown-section .dpr-home-dashboard-body")
    tags_rule = _rule_body(section, ".markdown-section .dpr-home-dashboard-tags")
    title_rule = _rule_body(section, ".markdown-section .dpr-home-dashboard-title")
    paper_title_rule = _rule_body(
        section, ".markdown-section .dpr-home-dashboard-paper-title"
    )

    assert "flex: 1 1 auto" in body_rule
    assert "min-width: 0" in body_rule
    assert "font-size: 0.88rem !important" in body_rule
    assert "line-height: 1.6 !important" in body_rule
    body_paragraph_rule = _rule_body(
        section, ".markdown-section .dpr-home-dashboard-body p"
    )
    assert "font-size: inherit !important" in body_paragraph_rule
    assert "line-height: inherit !important" in body_paragraph_rule
    assert "display: flex" in tags_rule
    assert "flex-wrap: wrap" in tags_rule
    assert "overflow: hidden" in title_rule
    assert "text-overflow: ellipsis" in title_rule
    assert "white-space: nowrap" in title_rule
    assert "display: block" in paper_title_rule
    assert "min-width: 0" in paper_title_rule
    assert "overflow: hidden" in paper_title_rule
    assert "text-overflow: ellipsis" in paper_title_rule
    assert "white-space: nowrap" in paper_title_rule
    assert ".markdown-section .dpr-home-dashboard-link" not in section


def test_home_dashboard_cards_stay_static_without_hover_or_focus_motion():
    css = _read_css()
    section = _section_between(css, "/* 首页仪表盘卡片 */", "/* 侧边栏字体放大 */")

    base_rule = _rule_body(section, ".markdown-section .dpr-home-dashboard-card")
    assert "transition" not in base_rule
    assert "cursor: pointer" not in base_rule
    assert ":hover" not in section
    assert ":focus-within" not in section
    assert "@media (prefers-reduced-motion: reduce)" not in section
    assert "transform: translate" not in section


def test_home_dashboard_switches_to_single_column_on_small_screens():
    css = _read_css()
    section = _section_between(css, "/* 首页仪表盘卡片 */", "/* 侧边栏字体放大 */")

    mobile = section.split("@media (max-width: 760px)", 1)[1]
    grid_rule = _rule_body(mobile, ".markdown-section .dpr-home-dashboard-grid")
    assert "grid-template-columns: minmax(0, 1fr)" in grid_rule
    assert "grid-template-rows: none" in grid_rule


def test_init_homepage_renders_exactly_four_dashboard_cards():
    path = ROOT / "docs_init" / "README.md"
    content = path.read_text(encoding="utf-8")
    assert content.count('class="dpr-home-dashboard-card ') == 4, path
    assert content.count('class="dpr-home-dashboard-grid"') == 1, path
    dashboard_region = content.split('<div class="dpr-home-dashboard-grid">', 1)[1]
    assert "<a " not in dashboard_region, path
