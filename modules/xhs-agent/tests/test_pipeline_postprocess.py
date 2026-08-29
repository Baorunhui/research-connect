from xhs_agent.pipeline import enrich_local_qa, evaluate_card_density, fix_card_plan, fix_note
from xhs_agent.schemas import CardPlan, CardPlanItem, NoteDraft, QAReport, SocialContentRequest


def test_fix_note_keeps_title_within_20_chars() -> None:
    note = NoteDraft(
        title_candidates=["短标题", "这是一个特别特别特别长的小红书标题"],
        selected_title="你的论文助手真的会查引用吗？这个基准说：不一定",
        body="正文",
        tags=["#AI论文"],
    )

    fixed = fix_note(note)

    assert fixed.selected_title == "短标题"
    assert len(fixed.selected_title) <= 20
    assert fixed.tags == ["AI论文"]


def test_fix_note_neutralizes_platform_link_claims() -> None:
    note = NoteDraft(
        title_candidates=[],
        selected_title="科研速览",
        body="论文链接和代码仓库我都放在主页了，需要的同学自取。",
        tags=[],
    )

    fixed = fix_note(note)

    assert "主页" not in fixed.body
    assert "发布时可补充论文和代码链接" in fixed.body


def test_fix_card_plan_neutralizes_platform_link_claims() -> None:
    note = NoteDraft(
        title_candidates=[],
        selected_title="科研速览",
        body="正文",
        tags=[],
    )
    plan = CardPlan(
        cards=[
            CardPlanItem(
                page=1,
                role="cover",
                headline="科研速览",
                bullets=["论文与代码链接见主页/评论区"],
            )
        ]
    )

    fixed = fix_card_plan(plan, 1, note)

    assert "主页" not in fixed.cards[0].bullets[0]
    assert "评论区" not in fixed.cards[0].bullets[0]


def test_fix_card_plan_assigns_layout_recipes() -> None:
    note = NoteDraft(title_candidates=[], selected_title="科研速览", body="正文", tags=[])
    plan = CardPlan(
        cards=[
            CardPlanItem(page=1, role="cover", headline="封面"),
            CardPlanItem(page=2, role="problem", headline="问题"),
            CardPlanItem(page=3, role="method", headline="方法"),
            CardPlanItem(page=4, role="value", headline="价值"),
            CardPlanItem(page=5, role="cta", headline="行动"),
        ]
    )

    fixed = fix_card_plan(plan, 5, note)

    assert [card.layout_recipe for card in fixed.cards] == [
        "cover",
        "thesis",
        "pipeline",
        "list",
        "ending",
    ]


def test_fix_note_rewrites_paper_code_placeholder_for_contact_request() -> None:
    request = SocialContentRequest.model_validate(
        {
            "intent": "lab_recruit",
            "source": {
                "kind": "lab",
                "title": "实验室招新",
                "summary": "招新说明",
                "links": [{"type": "contact", "url": "mailto:test@example.edu"}],
            },
        }
    )
    note = NoteDraft(
        title_candidates=[],
        selected_title="实验室招新",
        body="发布时可补充论文和代码链接。",
        tags=[],
    )

    fixed = fix_note(note, request)

    assert "论文和代码链接" not in fixed.body
    assert "联系方式和申请要求" in fixed.body


def test_fix_note_rewrites_paper_code_placeholder_for_code_only_request() -> None:
    request = SocialContentRequest.model_validate(
        {
            "intent": "project_promo",
            "source": {
                "kind": "project",
                "title": "工具宣传",
                "summary": "工具说明",
                "links": [{"type": "code", "url": "https://github.com/example/demo"}],
            },
        }
    )
    note = NoteDraft(
        title_candidates=[],
        selected_title="工具宣传",
        body="发布时可补充论文和代码链接。",
        tags=[],
    )

    fixed = fix_note(note, request)

    assert "论文和代码链接" not in fixed.body
    assert "代码仓库链接" in fixed.body


def test_evaluate_card_density_flags_sparse_and_overfull_cards() -> None:
    request = SocialContentRequest.model_validate(
        {
            "intent": "paper_promo",
            "source": {
                "kind": "paper",
                "title": "论文",
                "summary": "摘要",
            },
        }
    )
    plan = CardPlan(
        cards=[
            CardPlanItem(page=1, role="cover", layout_recipe="cover", headline="短"),
            CardPlanItem(
                page=2,
                role="method",
                layout_recipe="pipeline",
                headline="流程",
                bullets=[
                    "第一步需要解释很多很多内容，已经超过单页流程卡片适合承载的范围",
                    "第二步继续解释很多很多内容，读者会很难在一页里扫完",
                    "第三步还是很长很长很长很长很长很长",
                    "第四步也很长很长很长很长很长很长",
                    "第五步也很长很长很长很长很长很长",
                    "第六步不应该出现在同一张流程页",
                ],
            ),
            CardPlanItem(page=3, role="result", layout_recipe="media", headline="图证据"),
        ]
    )

    findings = evaluate_card_density(request, plan)

    assert any("P01" in finding and "偏空" in finding for finding in findings)
    assert any("P02" in finding and "要点超出显示上限" in finding for finding in findings)
    assert any("P03" in finding and "需要图片资产" in finding for finding in findings)


def test_enrich_local_qa_reports_density_warnings() -> None:
    request = SocialContentRequest.model_validate(
        {
            "intent": "paper_promo",
            "source": {
                "kind": "paper",
                "title": "论文",
                "summary": "摘要",
            },
        }
    )
    note = NoteDraft(selected_title="论文", body="正文", tags=[])
    plan = CardPlan(cards=[CardPlanItem(page=1, role="cover", layout_recipe="cover", headline="短")])

    qa = enrich_local_qa(request, note, plan, QAReport(fact_risk="low", style_risk="low"))

    assert qa.style_risk == "medium"
    assert any("内容密度警告" in check for check in qa.checks)
    assert any("内容密度提示" in item for item in qa.needs_human_check)
