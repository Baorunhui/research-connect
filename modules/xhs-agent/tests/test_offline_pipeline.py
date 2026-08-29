from pathlib import Path

from xhs_agent.package import write_package
from xhs_agent.pipeline import XHSPipeline
from xhs_agent.schemas import SocialContentRequest


def test_offline_pipeline_writes_package(tmp_path: Path) -> None:
    fixture = Path("fixtures/paper_promo.json").read_text(encoding="utf-8")
    request = SocialContentRequest.model_validate_json(fixture)
    result = XHSPipeline.offline().run(request)
    response = write_package(result, tmp_path)

    assert response.status == "completed"
    assert response.data is not None
    assert len(response.data.artifacts.cards) == request.requirements.card_count
    assert Path(response.data.artifacts.note_md).exists()
    assert Path(response.data.artifacts.qa_report_json).exists()
    html_dir = Path(response.data.artifacts.cards[0]).parent / "html"
    assert html_dir.exists()
    assert len(list(html_dir.glob("*.html"))) == request.requirements.card_count
    for card in response.data.artifacts.cards:
        path = Path(card)
        assert path.exists()
        assert path.stat().st_size > 10_000
