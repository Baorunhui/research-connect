from connect_hub.storage import ConversationStore


def test_history_and_clear(tmp_path):
    store = ConversationStore(tmp_path / "messages.sqlite3")
    store.append("s1", "user", "one")
    store.append("s1", "assistant", "two")
    store.append("s1", "user", "three")

    assert store.history("s1", 2) == [
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    assert store.clear("s1") == 3
    assert store.history("s1") == []


def test_feishu_bot_menu_web_mode_applies_to_known_and_future_sessions(tmp_path):
    store = ConversationStore(tmp_path / "web-mode.sqlite3")
    known = "feishu:oc_known:ou_user"
    future = "feishu:oc_future:ou_user"
    other = "feishu:oc_other:ou_other"
    store.set_web_mode(known, "off")
    store.set_web_mode(other, "off")

    store.set_web_mode_for_feishu_user("ou_user", "on")

    assert store.get_web_mode(known) == "on"
    assert store.get_web_mode(future) == "on"
    assert store.get_web_mode(other) == "off"


def test_web_search_defaults_to_on(tmp_path):
    store = ConversationStore(tmp_path / "web-default.sqlite3")

    assert store.get_web_mode("new-session") == "on"
    assert store.get_web_mode("feishu:oc_new:ou_new") == "on"


def test_agent_run_and_step_audit(tmp_path):
    store = ConversationStore(tmp_path / "agent-audit.sqlite3")
    store.start_agent_run("agent-1", "s1", "OCC 是什么？")
    step_id = store.append_agent_step(
        "agent-1",
        step_index=1,
        step_type="model",
        status="completed",
        provider="school",
        model="test-model",
        input_tokens=20,
        output_tokens=8,
        duration_ms=30,
        payload={"content": "需要搜索"},
    )
    store.finish_agent_run(
        "agent-1",
        status="completed",
        model_calls=1,
        search_calls=0,
        url_fetch_calls=0,
        business_tool_calls=0,
        input_tokens=20,
        output_tokens=8,
    )

    assert step_id > 0
    run = store.get_agent_run("agent-1")
    assert run["status"] == "completed"
    assert run["input_tokens"] == 20
    assert store.recent_agent_runs("s1", 1)[0]["id"] == "agent-1"
    step = store.agent_steps("agent-1")[0]
    assert step["provider"] == "school"
    assert step["payload"] == {"content": "需要搜索"}


def test_job_event_artifact_cache_and_usage_storage(tmp_path):
    store = ConversationStore(tmp_path / "jobs.sqlite3")
    store.create_job(
        "job-1",
        session_key="s1",
        job_type="daily_report",
        module_name="daily-paper",
        module_version="0.1.0",
        input_data={"topic": "3DVG"},
    )
    store.update_job("job-1", status="running", pid=123)
    assert store.latest_active_job("s1")["id"] == "job-1"
    assert store.active_jobs()[0]["id"] == "job-1"
    assert store.find_job("s1", "1")["id"] == "job-1"

    store.set_job_process("job-1", 456, "/usr/bin/python")
    assert store.get_job("job-1")["pid"] == 456
    assert store.get_job("job-1")["process_executable"] == "/usr/bin/python"
    store.clear_job_process("job-1", 456)
    assert store.get_job("job-1")["pid"] is None

    inserted = store.append_job_event(
        event_id="evt-1",
        job_id="job-1",
        event_type="job.progress",
        stage="recall",
        message="召回中",
        current=1,
        total=3,
        payload={"provider": "remote"},
    )
    duplicate = store.append_job_event(
        event_id="evt-1",
        job_id="job-1",
        event_type="job.progress",
    )
    assert inserted is True
    assert duplicate is False
    assert store.job_events("job-1")[0]["payload"] == {"provider": "remote"}

    artifact_id = store.add_artifact(
        "job-1", kind="file", path="/tmp/report.md", name="report.md"
    )
    assert artifact_id > 0
    assert store.artifacts("job-1")[0]["name"] == "report.md"

    store.upsert_cache(
        "paper:1234v1:pdf",
        kind="pdf",
        path="/tmp/paper.pdf",
        size_bytes=10,
        metadata={"paper_id": "1234v1"},
    )
    assert store.get_cache("paper:1234v1:pdf")["metadata"]["paper_id"] == "1234v1"

    store.record_usage(
        "job-1",
        provider="school-llm",
        operation="llm_refine",
        input_tokens=100,
        output_tokens=20,
        api_calls=2,
        duration_ms=500,
        status_code=200,
    )
    assert store.usage_summary("job-1") == {
        "input_tokens": 100,
        "output_tokens": 20,
        "api_calls": 2,
        "duration_ms": 500,
    }

    store.update_job("job-1", status="completed", result={"ok": True}, set_result=True)
    job = store.get_job("job-1")
    assert job["status"] == "completed"
    assert job["result"] == {"ok": True}


def test_restart_marks_active_jobs_interrupted(tmp_path):
    store = ConversationStore(tmp_path / "restart.sqlite3")
    store.create_job(
        "job-stale",
        session_key="s1",
        job_type="paper_research",
        module_name="daily-paper",
        module_version="0.1.0",
        input_data={},
    )
    store.update_job("job-stale", status="running")
    assert store.interrupt_active_jobs() == 1
    assert store.get_job("job-stale")["status"] == "interrupted"
