from connect_hub.adapters.daily_paper import (
    DailyPaperAdapter,
    DailyPaperRequest,
    _build_subscriptions,
)
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
