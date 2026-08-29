from connect_hub.adapters.daily_paper import DailyPaperAdapter, DailyPaperRequest
from connect_hub.contracts import ConnectJobError


def test_recommend_wait_polls_until_complete(monkeypatch):
    adapter = DailyPaperAdapter(
        "http",
        "http://127.0.0.1:8757",
        timeout_seconds=60,
        poll_seconds=1,
        extra_env={"RERANK_PROFILE": "public-zwwen-rerank"},
    )
    responses = iter(
        [
            {"ok": True, "run": {"id": "pe-test-20260819", "status": "queued"}},
            {"ok": True, "id": "pe-test-20260819", "status": "in_progress"},
            {
                "ok": True,
                "id": "pe-test-20260819",
                "status": "completed",
                "result": {"deep_dive": [], "quick_skim": []},
            },
        ]
    )
    calls = []

    def fake_request(method, path, payload):
        calls.append((method, path, payload))
        return next(responses)

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr("connect_hub.adapters.daily_paper.time.sleep", lambda _: None)

    result = adapter.invoke(
        DailyPaperRequest(
            "recommend_wait",
            {
                "date": "2026-08-18",
                "topics": [
                    {
                        "tag": "Pose",
                        "keywords": ["pose estimation"],
                        "paper_sources": ["arxiv", "cvpr"],
                    }
                ],
            },
        )
    )

    assert result["status"] == "completed"
    assert calls[0][0:2] == ("POST", "/api/recommend")
    assert calls[0][2]["date"] == "20260818"
    assert calls[0][2]["topics"][0]["paper_sources"] == ["arxiv"]
    assert calls[0][2]["secrets"]["RERANK_PROFILE"] == "public-zwwen-rerank"
    assert calls[1][0:2] == ("GET", "/api/recommend/pe-test-20260819")


def test_recommend_wait_rejects_ambiguous_date():
    adapter = DailyPaperAdapter("http", "http://127.0.0.1:8757")

    try:
        adapter.invoke(
            DailyPaperRequest(
                "recommend_wait",
                {"date": "18", "topics": [{"tag": "x", "keywords": ["x"]}]},
            )
        )
    except ValueError as exc:
        assert "YYYY-MM-DD" in str(exc)
    else:
        raise AssertionError("ambiguous date should be rejected before HTTP request")


def test_reports_each_pipeline_step_once():
    messages = []
    reported = set()
    log = "\n".join(
        [
            "[START] Step 2.1 - BM25",
            "[START] Step 2.2 - Embedding",
            "[START] Step 2.3 - RRF",
            "[START] Step 3 - Rerank",
            "[START] Step 4 - Refine",
            "[START] Step 5 - Select",
        ]
    )

    DailyPaperAdapter._report_log_progress(log, reported, messages.append)
    DailyPaperAdapter._report_log_progress(log, reported, messages.append)

    assert len(messages) == 6
    assert "BM25" in messages[0]
    assert "最终论文" in messages[-1]


def test_reports_structured_events_once():
    received = []
    seen = set()
    record = {
        "events": [
            {
                "schema_version": "connect.job.v1",
                "event_id": "evt-refine-1",
                "event_type": "job.progress",
                "stage": "llm_refine",
                "message": "正在精筛",
                "current": 4,
                "total": 20,
            }
        ]
    }

    DailyPaperAdapter._report_structured_events(record, seen, received.append)
    DailyPaperAdapter._report_structured_events(record, seen, received.append)

    assert len(received) == 1
    assert received[0]["current"] == 4


def test_cancel_requires_backend_acknowledgement(monkeypatch):
    adapter = DailyPaperAdapter(
        "http", "http://127.0.0.1:8757", timeout_seconds=60, poll_seconds=1
    )
    responses = iter(
        [
            {"ok": True, "run": {"id": "pe-cancel", "status": "queued"}},
            {"ok": False},
        ]
    )
    monkeypatch.setattr(adapter, "_request", lambda method, path, payload: next(responses))

    try:
        adapter.invoke(
            DailyPaperRequest("recommend_wait", {"topics": []}),
            is_cancelled=lambda: True,
        )
    except ConnectJobError as exc:
        assert exc.code == "CANCEL_FAILED"
    else:
        raise AssertionError("unacknowledged remote cancellation must not report success")
