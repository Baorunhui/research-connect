from connect_hub.adapters.daily_paper import (
    DailyPaperAdapter,
    DailyPaperRequest,
    _build_subscriptions,
)
from connect_hub.contracts import ConnectJobError
from connect_hub.tools import ToolContext
from connect_hub.tools.daily_paper import daily_paper_tools


def test_daily_tool_schema_requires_structured_intent_candidates(tmp_path):
    definitions = daily_paper_tools(
        DailyPaperAdapter(project_dir=tmp_path),
        output_dir=tmp_path / "output",
        inbound_dir=tmp_path / "inbound",
    )
    definition = definitions[0]
    topic_schema = definition.parameters["properties"]["topics"]["items"]

    assert "intent_queries" in topic_schema["required"]
    assert topic_schema["properties"]["intent_queries"]["minItems"] == 2
    assert [item.name for item in definitions] == [
        "generate_daily_paper_report",
        "summarize_paper",
        "generate_paper_survey",
    ]
    assert definition.timeout_seconds >= 6 * 60 * 60


def test_native_summary_job_polls_events_and_returns_result(monkeypatch):
    adapter = DailyPaperAdapter("local_http", "http://127.0.0.1:8567", poll_seconds=1)
    responses = iter(
        [
            {"ok": True, "schema_version": "connect.job.v1", "job_id": "sum-test", "status": "queued"},
            {"ok": True, "job": {"job_id": "sum-test", "status": "running", "events": [
                {"schema_version": "connect.job.v1", "event_id": "evt-1", "event_type": "job.progress", "stage": "parse_pdf", "message": "正在解析 PDF"}
            ]}},
            {"ok": True, "job": {"job_id": "sum-test", "status": "completed", "events": [], "result": {"meta": {"paper_id": "paper-1"}}}},
        ]
    )
    monkeypatch.setattr(adapter, "_request", lambda method, path, payload: next(responses))
    monkeypatch.setattr("connect_hub.adapters.daily_paper.time.sleep", lambda _: None)
    events = []

    result = adapter.invoke(
        DailyPaperRequest("paper_summarize_wait", {"source": "url", "url": "https://arxiv.org/abs/1"}),
        on_event=events.append,
    )

    assert result["id"] == "sum-test"
    assert result["result"]["meta"]["paper_id"] == "paper-1"
    assert events[0]["stage"] == "parse_pdf"


def test_apply_configuration_updates_adapter_and_runtime_without_name_error(monkeypatch):
    adapter = DailyPaperAdapter("local_http", "http://127.0.0.1:8567")
    calls = []
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda method, path, payload: calls.append((method, path, payload)) or {"ok": True},
    )
    config = {
        "local": {
            "chat": {
                "base_url": "https://llm.example/v1",
                "api_key": "llm-secret",
                "model": "model-a",
            },
            "rerank": {"profile": "public-zwwen-rerank"},
        }
    }

    adapter.apply_configuration(config)
    adapter.apply_runtime_environment({"SUPABASE_URL": "https://papers.example"})

    assert calls[0] == ("POST", "/api/local/config/partial", config)
    assert calls[1][0:2] == ("POST", "/api/local/runtime-env")
    assert adapter.extra_env["SUMMARY_API_KEY"] == "llm-secret"
    assert adapter.extra_env["SUMMARY_BASE_URL"] == "https://llm.example/v1"
    assert adapter.extra_env["SUMMARY_MODEL"] == "model-a"
    assert adapter.extra_env["RERANK_PROFILE"] == "public-zwwen-rerank"


def test_native_survey_passes_embedding_credentials_outside_business_input(monkeypatch):
    adapter = DailyPaperAdapter(
        "local_http",
        "http://127.0.0.1:8567",
        poll_seconds=1,
        extra_env={
            "DPR_EMBED_API_URL": "https://embed.example/api",
            "DPR_EMBED_API_KEY": "embed-secret",
        },
    )
    responses = iter([
        {"ok": True, "schema_version": "connect.job.v1", "job_id": "sv-test", "status": "queued"},
        {"ok": True, "job": {"job_id": "sv-test", "status": "completed", "events": [], "result": {"report": {}}}},
    ])
    calls = []

    def fake_request(method, path, payload):
        calls.append((method, path, payload))
        return next(responses)

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr("connect_hub.adapters.daily_paper.time.sleep", lambda _: None)

    adapter.invoke(DailyPaperRequest("survey_wait", {"query": "3D visual grounding"}))

    sent = calls[0][2]
    assert sent["query"] == "3D visual grounding"
    assert sent["_runtime_credentials"]["embedding"] == {
        "endpoint": "https://embed.example/api",
        "api_key": "embed-secret",
    }


def test_summary_and_survey_tools_encode_uploaded_pdf(tmp_path):
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    pdf = inbound / "seed.pdf"
    pdf.write_bytes(b"%PDF-test")

    class FakeAdapter:
        timeout_seconds = 60
        project_dir = tmp_path
        manifest = DailyPaperAdapter.manifest

        def __init__(self):
            self.calls = []

        def invoke(self, request, **kwargs):
            self.calls.append(request)
            if request.action == "paper_summarize_wait":
                return {"id": "sum-1", "result": {"meta": {"paper_id": "p1", "title": "Paper"}}}
            return {"id": "sv-1", "result": {"report": {"paper_id": "survey/s1", "title": "Survey", "n_papers": 12}}}

    adapter = FakeAdapter()
    definitions = daily_paper_tools(
        adapter,
        output_dir=tmp_path / "output",
        public_url="https://report.test/site/",
        inbound_dir=inbound,
    )
    by_name = {item.name: item for item in definitions}
    summary = by_name["summarize_paper"].handler(
        {"source": "pdf", "pdf_path": str(pdf)}, ToolContext("s")
    )
    survey = by_name["generate_paper_survey"].handler(
        {"query": "RAG evaluation", "seed": {"source": "pdf", "pdf_path": str(pdf)}},
        ToolContext("s"),
    )

    assert adapter.calls[0].arguments["data_b64"]
    assert adapter.calls[1].arguments["seed"]["data_b64"]
    assert summary["public_url"].endswith("#/p1")
    assert survey["public_url"].endswith("#/survey/s1")


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


def test_reports_pipeline_diagnostics_once(tmp_path):
    messages = []
    reported = set()
    rank_path = tmp_path / "rank.json"
    rank_path.write_text(
        '{"queries":[{"ranked":['
        '{"paper_id":"a","star_rating":5},'
        '{"paper_id":"b","star_rating":4},'
        '{"paper_id":"c","star_rating":3}]}]}',
        encoding="utf-8",
    )
    llm_path = tmp_path / "rank.llm.json"
    llm_path.write_text(
        '{"llm_ranked":[{"paper_id":"a","score":9.2},'
        '{"paper_id":"b","score":8.4},{"paper_id":"c","score":7.1}]}',
        encoding="utf-8",
    )
    log = "\n".join(
        [
            "[INFO] DPR_RUN_DATE=20260816-20260830",
            "[INFO] fetch_days=15, run_mode=skims, fetch_mode=skims",
            "[CHECK] 需要扩充 keywords.related: 0 个",
            "[CHECK] 需要扩充 keywords.rewrite: 0 个",
            "[CHECK] 需要扩充 llm_queries.rewrite: 0 个",
            "[INFO] config.yaml 所有字段都完整，无需扩充。",
            "[INFO] 跳过 Step 1（全量数据拉取）：Supabase 已完全接管检索。",
            "[INFO] Step 2.1 - BM25: command",
            "[INFO] Supabase BM25 窗口计数（source=arxiv）：count 查询成功：11830 条",
            "[INFO] Supabase BM25 自适应 Top K = 600",
            "[INFO] Supabase BM25 命中 673 条（source=arxiv）。",
            "[INFO] 其中带 tag 的论文数：517",
            "[INFO] Step 2.2 - Embedding: command",
            "[INFO] 使用远程 embedding 服务：model=BAAI/bge-small-en-v1.5 endpoint=https://example/embed",
            "[INFO] 远程 embedding：model=BAAI/bge-small-en-v1.5 endpoint=https://example/embed total=5 batch=8",
            "[INFO] Supabase 向量召回窗口计数（source=arxiv）：count 查询成功：11830 条",
            "[INFO] Supabase 向量召回自适应 Top K = 600",
            "[INFO] Supabase 向量召回命中 3000 条。",
            "[INFO] 其中带 tag 的论文数：1890",
            "[INFO] Step 2.3 - RRF: command",
            "[INFO] RRF keys=5 | bm25_queries=5 | emb_queries=5",
            "[INFO] merged papers=2112",
            "[INFO] Step 3 - Rerank: command",
            "[INFO] reranker 配置：profile=auto，provider=public，model=Qwen/Reranker，global_pool_limit=auto",
            "[INFO] 开始 rerank：queries=2（仅 intent/语义查询），papers=2112，global_pool=108（lane_top_k=50, guaranteed_per_lane=12, global_top=100），batch_size=64，max_chars=850，token_safety=29000",
            f"[INFO] 已将打分结果写入：{rank_path}",
            "[INFO] Step 4 - LLM refine: command",
            "[INFO] start filter: queries=5, papers=2112, min_star=4, batch_size=10, max_chars=850, concurrency=4",
            "[INFO] global candidates=58 batches=6 | user_requirements=6",
            f"[INFO] saved: {llm_path}",
            "[INFO] Step 5 - Select: command",
            "[INFO] scored_papers=58",
            '[STATS] {"mode": "skims", "min_score": 8.0, "quick_candidates": 2, "deep_selected": 0, "quick_selected": 2}',
            "[WARN] Docling 提取降级：ModuleNotFoundError: No module named 'pypdfium2'",
            "[OK] daily state merged: runs=3, deep=0, quick=28, path=/tmp/state.json",
            "[OK] docs updated: /tmp/docs",
        ]
    )

    DailyPaperAdapter._report_log_diagnostics(log, reported, messages.append)
    DailyPaperAdapter._report_log_diagnostics(log, reported, messages.append)

    assert len(messages) == 17
    assert any("11830 篇 → 原始命中 673" in message for message in messages)
    assert any("向量语义召回完成" in message and "1890" in message for message in messages)
    assert any("Reranker 完成" in message and "star_rating≥4" in message for message in messages)
    assert any("LLM 精炼打分 完成" in message and "llm_score≥8" in message for message in messages)
    assert any("选定 0 篇待精读、2 篇待速读" in message for message in messages)
    assert any("缺少 pypdfium2" in message for message in messages)


def test_keyword_only_topic_gets_intent_query_fallback():
    subscriptions = _build_subscriptions(
        [
            {
                "tag": "RAG",
                "description": "retrieval-augmented generation for scientific QA",
                "keywords": ["RAG", "citation grounding", "retrieval evaluation"],
                "intent_queries": [],
            }
        ]
    )

    profile = subscriptions["intent_profiles"][0]
    assert profile["intent_queries"] == [
        {
            "query": (
                "Find recent papers on retrieval-augmented generation for scientific QA, "
                "especially citation grounding, retrieval evaluation."
            ),
            "enabled": True,
            "source": "connect-hub-template-fallback",
        }
    ]


def test_loads_actual_local_recommendation_instead_of_empty_result(tmp_path):
    recommend_dir = tmp_path / "archive" / "run" / "recommend"
    recommend_dir.mkdir(parents=True)
    result_path = recommend_dir / "papers.skims.json"
    result_path.write_text(
        '{"mode":"skims","generated_at":"2026-08-30T00:00:00Z",'
        '"deep_dive":[],"quick_skim":[{"id":"2608.1","title":"A paper"}]}',
        encoding="utf-8",
    )
    adapter = DailyPaperAdapter(project_dir=tmp_path)

    result = adapter._load_local_recommendation(
        f"[INFO] saved: {result_path}\n", {"mode": "skims"}
    )

    assert len(result["quick_skim"]) == 1
    assert result["quick_skim"][0]["title"] == "A paper"


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
