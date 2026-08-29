import json
from pathlib import Path

from xhs_agent.package import write_package
from xhs_agent.pipeline import XHSPipeline, fix_card_plan
from xhs_agent.recipe_packs import render_recipe_pack_card
from xhs_agent.renderer import split_bullet
from xhs_agent.schemas import CardPlan, CardPlanItem, NoteDraft, SocialContentRequest


def test_split_bullet_does_not_split_chinese_comma() -> None:
    assert split_bullet("高效沟通，从准备开始") == ("高效沟通，从准备开始", "")
    assert split_bullet("简历：突出项目经历") == ("简历：", "突出项目经历")


def test_recipe_packs_use_distinct_dom() -> None:
    request = SocialContentRequest.model_validate(
        {
            "intent": "lab_recruit",
            "source": {"kind": "lab", "title": "实验室招新", "summary": "招新说明"},
        }
    )
    card = CardPlanItem(
        page=2,
        role="method",
        layout_recipe="pipeline",
        headline="联系前请准备",
        subtitle="高效沟通，从准备开始",
        bullets=["简历：突出项目经历", "GitHub/课程项目链接"],
    )

    morandi = render_recipe_pack_card(
        request,
        card,
        5,
        {"id": "converted.open-design-morandi-carousel", "pack": "morandi-carousel"},
        "pipeline",
    )
    tech = render_recipe_pack_card(
        request,
        card,
        5,
        {"id": "converted.rednote-tech", "pack": "rednote-tech"},
        "pipeline",
    )

    assert "soft-pipeline" in morandi
    assert "tech-pipeline" in tech
    assert "soft-step" in morandi
    assert "node-step" in tech


def test_fix_card_plan_supports_image_recipes() -> None:
    request = SocialContentRequest.model_validate_json(
        Path("fixtures/demo_paper_with_images.json").read_text(encoding="utf-8")
    )
    plan = CardPlan(
        cards=[
            CardPlanItem(page=1, role="cover", layout_recipe="image_cover", headline="图解论文", asset_ids=["lab_photo"]),
            CardPlanItem(page=2, role="method", layout_recipe=None, headline="方法图", asset_ids=["method_fig"]),
            CardPlanItem(page=3, role="result", layout_recipe="screenshot", headline="证据页", asset_ids=["method_fig"]),
            CardPlanItem(page=4, role="value", layout_recipe="list", headline="价值", asset_ids=["missing"]),
            CardPlanItem(page=5, role="cta", layout_recipe=None, headline="核对后发布"),
        ]
    )
    fixed = fix_card_plan(
        plan,
        5,
        NoteDraft(selected_title="图解论文", body="正文", tags=[]),
        request,
    )

    assert [card.layout_recipe for card in fixed.cards[:3]] == ["image_cover", "media", "evidence"]
    assert fixed.cards[3].asset_ids == []


def test_fix_card_plan_auto_attaches_method_asset() -> None:
    request = SocialContentRequest.model_validate_json(
        Path("fixtures/demo_paper_with_images.json").read_text(encoding="utf-8")
    )
    plan = CardPlan(
        cards=[
            CardPlanItem(page=1, role="cover", headline="图解论文"),
            CardPlanItem(page=2, role="problem", headline="问题"),
            CardPlanItem(page=3, role="method", headline="方法图"),
            CardPlanItem(page=4, role="info", headline="更多"),
            CardPlanItem(page=5, role="cta", headline="核对"),
        ]
    )
    fixed = fix_card_plan(
        plan,
        5,
        NoteDraft(selected_title="图解论文", body="正文", tags=[]),
        request,
    )

    assert fixed.cards[2].asset_ids == ["method_fig"]
    assert fixed.cards[2].layout_recipe == "media"
    assert fixed.cards[0].asset_ids == []


def test_recipe_pack_renders_image_asset_html() -> None:
    request = SocialContentRequest.model_validate_json(
        Path("fixtures/demo_paper_with_images.json").read_text(encoding="utf-8")
    )
    card = CardPlanItem(
        page=2,
        role="method",
        layout_recipe="media",
        headline="方法图一眼看懂",
        subtitle="图只做示例，发布前替换为论文原图",
        bullets=["先看流程", "再看证据", "最后核对图注"],
        asset_ids=["method_fig"],
    )
    html = render_recipe_pack_card(
        request,
        card,
        5,
        {"id": "native.research-editorial", "pack": "research-editorial"},
        "media",
        {
            "method_fig": {
                "src": "../../assets/method_fig.png",
                "label": "方法图",
                "caption": "方法图 caption",
                "fit": "contain",
                "object_position": "center center",
            }
        },
    )

    assert "media-main" in html
    assert "../../assets/method_fig.png" in html
    assert "--fit:contain" in html


def test_recipe_pack_package_copies_image_assets(tmp_path: Path) -> None:
    request = SocialContentRequest.model_validate_json(
        Path("fixtures/demo_paper_with_images.json").read_text(encoding="utf-8")
    )
    plan = CardPlan(
        cards=[
            CardPlanItem(page=1, role="cover", layout_recipe="image_cover", headline="图解论文", asset_ids=["lab_photo"]),
            CardPlanItem(page=2, role="method", layout_recipe="media", headline="方法图", asset_ids=["method_fig"]),
        ]
    )
    fixed = fix_card_plan(plan, 2, NoteDraft(selected_title="图解论文", body="正文", tags=[]), request)
    result = XHSPipeline.offline().run(request).model_copy(update={"card_plan": fixed})
    response = write_package(result, tmp_path, template_id="native.research-editorial")
    assert response.data is not None
    output_dir = Path(response.data.output_dir)

    assert (output_dir / "assets" / "method_fig.png").exists()
    assert (output_dir / "assets" / "lab_photo.jpg").exists()
    html_text = (output_dir / "cards" / "html" / "xhs-02-method-media.html").read_text(encoding="utf-8")
    assert "../../assets/method_fig.png" in html_text


def test_xhs_template_manifest_paths_exist() -> None:
    root = Path("templates/xhs")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["canvas"] == {
        "width": 1080,
        "height": 1440,
        "platform": "xiaohongshu",
    }

    seen: set[str] = set()
    for template in manifest["templates"]:
        assert template["id"] not in seen
        seen.add(template["id"])

        template_path = root / template["path"]
        assert template_path.exists(), template["id"]

        if template["status"] == "ready":
            assert template_path.suffix == ".html"
            text = template_path.read_text(encoding="utf-8")
            assert "{{TITLE}}" in text
            assert "1080px" in text
            assert "1440px" in text
            if template.get("type") == "recipe_pack":
                assert set(template["recipes"]) == {
                    "cover",
                    "thesis",
                    "list",
                    "body",
                    "pipeline",
                    "quote",
                    "ending",
                    "image_cover",
                    "media",
                    "evidence",
                }
