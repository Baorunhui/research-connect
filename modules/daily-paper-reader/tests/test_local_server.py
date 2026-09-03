import json
import os

import pytest

from src.local_server import build_chat_request_payload


def test_connect_hub_secret_env_allowlist_includes_remote_embedding():
    from src.local_server import build_secret_env

    env = build_secret_env(
        {
            "DPR_EMBED_API_URL": "https://zwwen.online/embed",
            "DPR_EMBED_API_KEY": "embed-token",
            "UNSAFE_ARBITRARY_ENV": "must-not-pass",
        }
    )
    assert env["DPR_EMBED_API_KEY"] == "embed-token"
    assert env["DPR_EMBED_API_URL"] == "https://zwwen.online/embed"
    assert "UNSAFE_ARBITRARY_ENV" not in env


def test_connect_hub_secret_env_allowlist_includes_external_academic_services():
    from src.local_server import build_secret_env

    env = build_secret_env({
        "DEEPXIV_API_BASE_URL": "https://deepxiv.example",
        "DEEPXIV_TOKEN": "deepxiv-token",
        "SEMANTIC_SCHOLAR_API_KEY": "s2-token",
        "DPR_DEFAULT_USE_DEEPXIV": "1",
        "DPR_DEFAULT_USE_KAGGLE": "0",
    })
    assert env["DEEPXIV_API_BASE_URL"] == "https://deepxiv.example"
    assert env["DEEPXIV_TOKEN"] == "deepxiv-token"
    assert env["SEMANTIC_SCHOLAR_API_KEY"] == "s2-token"
    assert env["DPR_DEFAULT_USE_DEEPXIV"] == "1"
    assert env["DPR_DEFAULT_USE_KAGGLE"] == "0"


def test_runtime_environment_applies_and_clears_allowlisted_values(monkeypatch):
    from src.local_server import apply_runtime_environment

    monkeypatch.setenv("DEEPXIV_TOKEN", "old-token")
    changed = apply_runtime_environment({
        "DEEPXIV_TOKEN": "new-token",
        "DPR_DEFAULT_USE_DEEPXIV": "1",
        "UNSAFE_ARBITRARY_ENV": "blocked",
    })
    assert "DEEPXIV_TOKEN" in changed
    assert "UNSAFE_ARBITRARY_ENV" not in changed
    assert os.environ["DEEPXIV_TOKEN"] == "new-token"
    assert os.environ["DPR_DEFAULT_USE_DEEPXIV"] == "1"

    apply_runtime_environment({"DEEPXIV_TOKEN": ""})
    assert "DEEPXIV_TOKEN" not in os.environ


def test_runtime_docs_shell_is_created_from_clean_templates(tmp_path, monkeypatch):
    from src import local_server

    templates = tmp_path / "docs_init"
    templates.mkdir()
    (templates / "README.md").write_text("clean home", encoding="utf-8")
    (templates / "_sidebar.md").write_text("* Daily Papers\n", encoding="utf-8")
    monkeypatch.setattr(local_server, "ROOT_DIR", tmp_path)

    local_server.ensure_runtime_docs_shell()

    assert (tmp_path / "docs" / "README.md").read_text(encoding="utf-8") == "clean home"
    assert (tmp_path / "docs" / "_sidebar.md").read_text(encoding="utf-8") == "* Daily Papers\n"


@pytest.fixture(autouse=True)
def _isolated_summarize_cache(tmp_path, monkeypatch):
    """总结缓存按论文身份跨进程落盘（.local-runs/paper_summarize_cache.json）。
    真实使用或先前测试运行留下的同 id/同内容条目会命中缓存、短路 persist，
    让断言拿到空结果（表现为：首跑通过、二次运行偶发失败）。
    每个用例换成临时空缓存，保证密闭。"""
    import src.local_server as ls

    monkeypatch.setattr(ls, "SUMMARIZE_CACHE", ls.SummarizeCache(tmp_path / "summarize-cache.json"))


def test_merge_local_section_preserves_other_sections():
    from src.local_server import merge_local_section
    existing = {"github": {"owner": "x"}, "local": {"chat": {"model": "m", "base_url": "u", "api_key": "k"}, "schedule": {"enabled": True, "time": "18:30", "fetch_days": ""}}}
    incoming = {"local": {"chat": {"base_url": "https://new"}, "schedule": {"enabled": False}}}
    merged = merge_local_section(existing, {"local": incoming})
    assert merged["github"] == {"owner": "x"}          # 其它段保留
    assert merged["local"]["chat"]["model"] == "m"      # 未传字段保留
    assert merged["local"]["chat"]["base_url"] == "https://new"
    assert merged["local"]["chat"]["api_key"] == "k"    # 未传字段保留
    assert merged["local"]["schedule"]["enabled"] is False
    assert merged["local"]["schedule"]["time"] == "18:30"  # 未传字段保留


def test_merge_top_level_section_replaces_key_preserves_others():
    from src.local_server import merge_top_level_section
    existing = {
        "github": {"owner": "x"},
        "arxiv_paper_setting": {"mode": "standard"},
        "subscriptions": {"intent_profiles": [{"tag": "OLD"}]},
    }
    incoming_subs = {
        "schema_migration": {"stage": "A"},
        "intent_profiles": [{"tag": "RAG", "enabled": True}],
    }
    merged = merge_top_level_section(existing, "subscriptions", incoming_subs)
    assert merged["github"] == {"owner": "x"}
    assert merged["arxiv_paper_setting"] == {"mode": "standard"}
    assert merged["subscriptions"] == incoming_subs  # 整体替换为传入对象


def test_load_subscriptions_section_returns_profiles_with_cache(tmp_path, monkeypatch):
    from src import local_server
    cfg = {
        "github": {"owner": "x"},
        "subscriptions": {
            "schema_migration": {"stage": "A"},
            "keyword_recall_mode": "or",
            "intent_profiles": [
                {
                    "tag": "RAG",
                    "enabled": True,
                    "keywords": [{"query": "q", "keyword": "q", "embedding_cache": {"version": 1}}],
                    "intent_queries": [{"query": "qq", "enabled": True}],
                }
            ],
        },
    }
    import yaml
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(local_server, "CONFIG_PATH", config_path)

    subs = local_server._load_subscriptions_section()
    assert subs["schema_migration"]["stage"] == "A"
    assert subs["keyword_recall_mode"] == "or"
    assert subs["intent_profiles"][0]["tag"] == "RAG"
    # embedding_cache 保留，供前端回写不丢
    assert subs["intent_profiles"][0]["keywords"][0]["embedding_cache"]["version"] == 1


def test_load_local_chat_full_includes_subscriptions(tmp_path, monkeypatch):
    from src import local_server
    import yaml
    cfg = {
        "local": {"chat": {"model": "m", "base_url": "u", "api_key": "k"}, "schedule": {"enabled": True, "time": "18:30"}},
        "subscriptions": {"intent_profiles": [{"tag": "RAG"}]},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(local_server, "CONFIG_PATH", config_path)

    full = local_server._load_local_chat_full()
    assert full["chat"]["model"] == "m"
    assert full["schedule"]["time"] == "18:30"
    assert full["subscriptions"]["intent_profiles"][0]["tag"] == "RAG"


def test_build_chat_request_payload_shape():
    payload = build_chat_request_payload(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["stream"] is True


def test_scheduler_ties_to_port_config():
    # 通过解析 local.port 确认服务配置存在（防止 config 段缺失）
    import yaml
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    assert "local" in cfg
    assert cfg["local"]["port"] == 8567


def test_merge_local_section_preserves_other_sections():
    from src.local_server import merge_local_section
    existing = {"github": {"owner": "x"}, "local": {"chat": {"model": "m", "base_url": "u", "api_key": "k"}, "schedule": {"enabled": True, "time": "18:30", "fetch_days": ""}}}
    incoming = {"local": {"chat": {"base_url": "https://new"}, "schedule": {"enabled": False}}}
    merged = merge_local_section(existing, incoming)
    assert merged["github"] == {"owner": "x"}          # 其它段保留
    assert merged["local"]["chat"]["model"] == "m"      # 未传字段保留
    assert merged["local"]["chat"]["base_url"] == "https://new"
    assert merged["local"]["chat"]["api_key"] == "k"    # 未传字段保留
    assert merged["local"]["schedule"]["enabled"] is False
    assert merged["local"]["schedule"]["time"] == "18:30"  # 未传字段保留

# --------------------------------------------------------------------------- #
# 论文总结端点 /api/paper/summarize 的纯函数单元测试（不联网、不调 LLM）
# --------------------------------------------------------------------------- #

def _tiny_pdf_b64():
    import base64
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Title: Summarize Me", fontsize=14)
    page.insert_text((72, 120), "Abstract: This paper proposes a novel method.", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return base64.b64encode(data).decode()


def test_extract_arxiv_id_variants():
    from src.local_server import _extract_arxiv_id
    cases = {
        "https://arxiv.org/abs/2301.12091": "2301.12091",
        "https://arxiv.org/pdf/2301.12091": "2301.12091",
        "https://arxiv.org/abs/2301.12091v2": "2301.12091v2",
        "arxiv:2301.12091": "2301.12091",
        "   2301.12091  ": "2301.12091",
        "https://example.com/not-arxiv": None,
    }
    for url, expected in cases.items():
        assert _extract_arxiv_id(url) == expected, url


def test_fetch_arxiv_metadata_falls_back_to_abs_page_on_atom_429(monkeypatch):
    import urllib.error
    import src.local_server as ls

    html = b'''<html><head>
      <meta property="og:description" content="A grounded 3D vision abstract." />
      <meta name="citation_title" content="Scene Graph Grounder" />
      <meta name="citation_author" content="Author One" />
      <meta name="citation_author" content="Author Two" />
      <meta name="citation_date" content="2026/05/20" />
      <meta name="citation_pdf_url" content="https://arxiv.org/pdf/2605.21788" />
    </head></html>'''
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "export.arxiv.org" in url:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        return html

    monkeypatch.setattr(ls, "_http_get_with_retry", fake_get)

    meta = ls._fetch_arxiv_metadata("2605.21788")

    assert calls == [
        "https://export.arxiv.org/api/query?id_list=2605.21788",
        "https://arxiv.org/abs/2605.21788",
    ]
    assert meta["title"] == "Scene Graph Grounder"
    assert meta["authors"] == ["Author One", "Author Two"]
    assert meta["abstract"] == "A grounded 3D vision abstract."
    assert meta["published"] == "2026-05-20"
    assert meta["metadata_source"] == "arxiv_abs_fallback"


def test_extract_pdf_text_returns_text():
    from src.local_server import _extract_pdf_text
    text = _extract_pdf_text(_tiny_pdf_b64())
    assert "Title: Summarize Me" in text
    assert "novel method" in text


def test_pdf_max_bytes_default_and_override(monkeypatch):
    from src.local_server import _pdf_max_bytes
    monkeypatch.delenv("DPR_PDF_MAX_MB", raising=False)
    assert _pdf_max_bytes() == 50 * 1024 * 1024
    monkeypatch.setenv("DPR_PDF_MAX_MB", "10")
    assert _pdf_max_bytes() == 10 * 1024 * 1024
    monkeypatch.setenv("DPR_PDF_MAX_MB", "abc")
    assert _pdf_max_bytes() == 50 * 1024 * 1024


def test_extract_pdf_text_oversize_rejected():
    from src.local_server import _extract_pdf_text, _pdf_max_bytes
    import base64
    import pytest
    # 超过当前上限的原始字节 → base64 后必然超限被拒绝
    big_b64 = base64.b64encode(b"x" * (_pdf_max_bytes() + 1)).decode("ascii")
    with pytest.raises(ValueError):
        _extract_pdf_text(big_b64)


# --------------------------------------------------------------------------- #
# 论文总结缓存 SummarizeCache
# --------------------------------------------------------------------------- #

def test_summarize_cache_roundtrip(tmp_path):
    from src.local_server import SummarizeCache
    cache = SummarizeCache(tmp_path / "c.json", max_entries=10)
    assert cache.get("arxiv:2301.12091") is None
    cache.put("arxiv:2301.12091", {"ok": True, "summary": {"tl_dr": "x"}, "figures": []})
    assert cache.get("arxiv:2301.12091") == {"ok": True, "summary": {"tl_dr": "x"}, "figures": []}


def test_summarize_cache_persists_on_new_instance(tmp_path):
    from src.local_server import SummarizeCache
    p = tmp_path / "c.json"
    SummarizeCache(p, 10).put("sha256:abc", {"ok": True})
    got = SummarizeCache(p, 10).get("sha256:abc")  # 新实例从磁盘重新加载
    assert got == {"ok": True}


def test_summarize_cache_prunes_oldest(tmp_path):
    from src.local_server import SummarizeCache
    cache = SummarizeCache(tmp_path / "c.json", max_entries=2)
    cache.put("a", {"ok": True})
    cache.put("b", {"ok": True})
    cache.put("c", {"ok": True})  # 超上限，应淘汰最旧的 "a"
    assert cache.get("a") is None
    assert cache.get("b") == {"ok": True}


# --------------------------------------------------------------------------- #
# HTTP GET 重试（捕获 WinError 10054 等连接重置）
# --------------------------------------------------------------------------- #

def test_http_get_with_retry_succeeds_after_reset(monkeypatch):
    """首次连接重置（WinError 10054），第二次成功——应返回响应体。"""
    import src.local_server as ls

    calls = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, limit=None):
            return b"OK"

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")
        return _Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ls.time, "sleep", lambda s: None)  # 跳过退避等待

    body = ls._http_get_with_retry("http://x", timeout=5, max_retries=3)
    assert body == b"OK"
    assert calls["n"] == 2


def test_http_get_with_retry_exhausts_retries(monkeypatch):
    """连续 ConnectionResetError 到达上限——应抛 ConnectionResetError。"""
    import src.local_server as ls

    def fake_urlopen(req, timeout=None):
        raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ls.time, "sleep", lambda s: None)

    import pytest
    with pytest.raises(ConnectionResetError):
        ls._http_get_with_retry("http://x", timeout=5, max_retries=2)


def test_http_get_with_retry_no_retry_on_4xx(monkeypatch):
    """HTTP 4xx 错误不应重试，应立即抛出。"""
    import src.local_server as ls
    import urllib.error

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ls.time, "sleep", lambda s: None)

    import pytest
    with pytest.raises(urllib.error.HTTPError):
        ls._http_get_with_retry("http://x", timeout=5, max_retries=3)
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# 论文总结异步 Job（connect.job.v1 契约）
# --------------------------------------------------------------------------- #

def test_summarize_job_store_create_and_get():
    """建 job 后能 get 到，状态机与事件结构符合 connect.job.v1。"""
    from src.local_server import SummarizeJobStore, _job_event

    store = SummarizeJobStore()
    job = store.create({"source": "url", "url": "https://arxiv.org/abs/1234.5678"})
    job_id = job["job_id"]
    assert job_id.startswith("sum-")
    assert job["status"] in ("queued", "running")  # worker 可能已开始
    events = job["events"]
    assert events[0]["event_type"] == "job.accepted"
    assert events[0]["schema_version"] == "connect.job.v1"
    assert events[0]["event_id"]
    # get 返回同样数据，但不含 input（避免泄露 base64 PDF）
    got = store.get(job_id)
    assert got is not None
    assert got["job_id"] == job_id
    assert "input" not in got


def test_summarize_job_store_get_unknown_returns_none():
    from src.local_server import SummarizeJobStore

    store = SummarizeJobStore()
    assert store.get("sum-nonexistent") is None


def test_summarize_job_store_cancel_marks_and_blocks_double():
    """request_cancel 标记取消；对已完成 job 取消返回 False。"""
    from src.local_server import SummarizeJobStore

    store = SummarizeJobStore()
    job = store.create({"source": "url", "url": "https://arxiv.org/abs/1234.5678"})
    job_id = job["job_id"]
    assert store.request_cancel(job_id) is True
    # 再取消一次同一 job 仍可标记（幂等）
    # 取消不存在的 job 返回 False
    assert store.request_cancel("sum-nope") is False


def test_job_event_schema_matches_contract():
    """事件 JSON 含 schema_version/event_id/job_id/event_type，符合 docs/superpowers/specs/2026-08-19-connect-job-v1.md 契约。"""
    from src.local_server import _job_event

    ev = _job_event("job.progress", "sum-abc", stage="llm_summarize",
                    message="正在请求 LLM", current=1, total=2)
    assert ev["schema_version"] == "connect.job.v1"
    assert ev["event_id"].startswith("evt-")
    assert ev["job_id"] == "sum-abc"
    assert ev["event_type"] == "job.progress"
    assert ev["stage"] == "llm_summarize"
    assert ev["current"] == 1
    assert ev["total"] == 2


def test_run_summarize_job_emit_supports_payload(tmp_path, monkeypatch):
    """回归：_run_summarize_job 的 emit() 必须支持 payload 参数（persist 阶段用到），
    此前缺少该参数导致 persist 事件抛出 TypeError。"""
    import base64
    from src.local_server import SummarizeJobStore

    import fitz
    doc = fitz.open()
    page = doc.new_page()
    blob = ("A research paper about retrieval augmented generation. " * 40)
    page.insert_text((50, 50), blob[:300], fontsize=9)
    pdf_bytes = doc.tobytes()
    doc.close()
    data_b64 = base64.b64encode(pdf_bytes).decode("ascii")

    import src.local_server as ls
    # 返回 paper_id 触发 emit("persist", payload=...)
    monkeypatch.setattr(ls, "_persist_summarize_as_daily_paper", lambda **kw: {
        "paper_id": "202608/27/t-paper", "md_path": "x.md", "registered": True,
    })
    # 文件名无 arXiv id 会触发 LLM 元数据抽取，测试环境屏蔽
    monkeypatch.setattr(ls, "_extract_paper_meta_with_llm", lambda text: {})

    store = SummarizeJobStore()
    job = store.create({"source": "pdf", "filename": "t.pdf", "data_b64": data_b64})
    job_id = job["job_id"]
    import time
    deadline = time.time() + 10
    while time.time() < deadline:
        s = store.get(job_id)
        if s["status"] in ("completed", "failed"):
            break
        time.sleep(0.3)
    got = store.get(job_id)
    assert got["status"] == "completed", got["error"]
    assert got["result"]["meta"]["paper_id"] == "202608/27/t-paper"
    # 必须无报错，persist 阶段带 payload 的事件存在
    persist_ev = [e for e in got["events"] if e.get("stage") == "persist" and e.get("payload")]
    assert persist_ev, "persist 阶段应发出带 payload 的事件"
    assert persist_ev[0]["payload"].get("paper_id") == "202608/27/t-paper"


def test_run_summarize_job_persist_failure_fails_job(monkeypatch):
    """回归：总结只走日报流水线（generate_external_paper_docs），不再有 VLM / 独立文本 LLM 分支。
    落盘（paper_id 为空）时 job 必须以明确错误失败，而不是返回空总结。"""
    import base64
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    blob = ("A research paper about sparse mixture of experts. " * 40)
    page.insert_text((50, 50), blob[:300], fontsize=9)
    pdf_bytes = doc.tobytes()
    doc.close()
    data_b64 = base64.b64encode(pdf_bytes).decode("ascii")

    import src.local_server as ls

    monkeypatch.setattr(ls, "_persist_summarize_as_daily_paper", lambda **kw: {
        "paper_id": "", "md_path": "", "registered": False, "error": "LLM timeout",
    })
    # 文件名无 arXiv id 会触发 LLM 元数据抽取，测试环境屏蔽
    monkeypatch.setattr(ls, "_extract_paper_meta_with_llm", lambda text: {})

    store = ls.SummarizeJobStore()
    job = store.create({"source": "pdf", "filename": "t.pdf", "data_b64": data_b64})
    job_id = job["job_id"]
    import time
    deadline = time.time() + 10
    while time.time() < deadline:
        s = store.get(job_id)
        if s["status"] in ("completed", "failed"):
            break
        time.sleep(0.3)
    got = store.get(job_id)
    assert got["status"] == "failed", got
    assert "日报流水线生成纸张页失败" in str(got.get("error") or ""), got.get("error")
    assert "LLM timeout" in str(got.get("error") or ""), got.get("error")


def test_run_summarize_job_cancel_during_pipeline_is_cancelled(monkeypatch):
    """回归：日报流水线进度回调（on_progress → emit → _CancelRequested）触发的取消信号，
    必须让 job 以 cancelled 结束，而不是被 persist 层宽 except 吞成 failed。"""
    import base64
    import fitz
    import threading
    import time

    doc = fitz.open()
    page = doc.new_page()
    blob = ("A research paper about diffusion policy learning. " * 40)
    page.insert_text((50, 50), blob[:300], fontsize=9)
    pdf_bytes = doc.tobytes()
    doc.close()
    data_b64 = base64.b64encode(pdf_bytes).decode("ascii")

    import src.local_server as ls

    reached = threading.Event()
    release = threading.Event()

    def fake_persist(**kw):
        reached.set()
        assert release.wait(5), "test did not release persist"
        kw["on_progress"]("glance", "模拟日报流水线进度")
        return {"paper_id": "202608/27/t-paper", "md_path": "x.md", "registered": True}

    monkeypatch.setattr(ls, "_persist_summarize_as_daily_paper", fake_persist)
    # 文件名无 arXiv id 会触发 LLM 元数据抽取，测试环境屏蔽
    monkeypatch.setattr(ls, "_extract_paper_meta_with_llm", lambda text: {})

    store = ls.SummarizeJobStore()
    job = store.create({"source": "pdf", "filename": "t.pdf", "data_b64": data_b64})
    job_id = job["job_id"]
    assert reached.wait(5), "job 未到达 persist 阶段"
    assert store.request_cancel(job_id)
    release.set()

    deadline = time.time() + 10
    while time.time() < deadline:
        s = store.get(job_id)
        if s["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.1)
    got = store.get(job_id)
    assert got["status"] == "cancelled", got
    assert got.get("result") is None


def test_persist_summarize_propagates_cancel_request(tmp_path, monkeypatch):
    """回归：_persist_summarize_as_daily_paper 内部 generate_external_paper_docs
    经 on_progress 抛出取消信号时，必须原样传播，不得被宽 except 转成落盘失败 dict。"""
    import src.local_server as ls
    from src.local_server import _CancelRequested

    fake_module = tmp_path / "6.generate_docs.py"
    fake_module.write_text(
        "from src.local_server import _CancelRequested\n"
        "def generate_external_paper_docs(paper, *, date_str, docs_dir, section,\n"
        "                                    pdf_bytes=None, full_text='', on_progress=None):\n"
        "    if on_progress:\n"
        "        on_progress('glance', 'x')\n"
        "    raise _CancelRequested('用户取消')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ls, "SRC_DIR", tmp_path)

    def _raise_cancel(stage, message):
        raise _CancelRequested("用户取消")

    try:
        ls._persist_summarize_as_daily_paper(
            paper_meta={}, title="t", text="x" * 30, kind="pdf", source="pdf",
            arxiv_id=None, on_progress=_raise_cancel,
        )
    except _CancelRequested:
        return  # 期望：取消信号原样传播
    raise AssertionError("取消信号被吞掉，未传播出 _persist_summarize_as_daily_paper")


def test_persist_summarize_registers_manual_tag(tmp_path, monkeypatch):
    """回归：手动总结生成的论文注册侧边栏时必须带「手动总结」标签，
    侧边栏三级标签分组才不会回退成「未标注」，且与日报论文区分开。
    无原文链接（PDF 上传）时 link 应指向纸张页自身，不得拼出假 arxiv 链接。"""
    import sys
    import src.local_server as ls

    fake_module = tmp_path / "6.generate_docs.py"
    fake_module.write_text(
        "_captured = {}\n"
        "def generate_external_paper_docs(paper, *, date_str, docs_dir, section,\n"
        "                                    pdf_bytes=None, full_text='', on_progress=None):\n"
        "    return '202608/27/t-paper', 'T', 'x.md'\n"
        "def update_sidebar(sidebar_path, date_str, deep_entries, quick_entries, evidence, **kw):\n"
        "    _captured['deep'] = deep_entries\n"
        "    _captured['links'] = kw.get('paper_link_by_id')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ls, "SRC_DIR", tmp_path)

    res = ls._persist_summarize_as_daily_paper(
        paper_meta={}, title="T", text="x" * 30, kind="pdf", source="pdf",
        arxiv_id=None,
    )
    assert res["paper_id"] == "202608/27/t-paper", res
    mod = sys.modules["dpr_generate_docs_for_summarize"]
    deep = mod._captured["deep"]
    assert deep[0][0] == "202608/27/t-paper"
    assert ("manual", "手动总结") in deep[0][2], deep[0][2]
    # PDF 上传无原文链接 → 指向纸张页自身（route_href），而不是 arxiv.org/abs/<slug>
    assert mod._captured["links"]["202608/27/t-paper"] == "#/202608/27/t-paper"


def test_arxiv_id_from_filename_variants():
    from src.local_server import _arxiv_id_from_filename

    assert _arxiv_id_from_filename("2601.01234v1.pdf") == "2601.01234v1"
    assert _arxiv_id_from_filename("ArXiv-2601.01234.PDF") == "2601.01234"
    assert _arxiv_id_from_filename("attention_is_all_you_need.pdf") is None
    assert _arxiv_id_from_filename("") is None
    assert _arxiv_id_from_filename(None) is None


def test_merge_extracted_paper_meta_skips_empty_fields():
    import src.local_server as ls

    meta = {"title": "paper.pdf"}
    ls._merge_extracted_paper_meta(meta, {"title": "", "authors": [], "venue": "", "source_kind": ""})
    assert meta == {"title": "paper.pdf"}
    ls._merge_extracted_paper_meta(meta, {"title": "Real Title", "authors": ["A"], "source_kind": "biorxiv"})
    assert meta["title"] == "Real Title"
    assert meta["authors"] == ["A"]
    assert meta["source_kind"] == "biorxiv"
    ls._merge_extracted_paper_meta(meta, None)
    assert meta["title"] == "Real Title"  # 非法输入不破坏已有字段


def test_resolve_summarize_source_real_tokens():
    """回归：非 arXiv 来源不得再硬编码成 arxiv；venue 与 source 分离。"""
    from src.local_server import _resolve_summarize_source

    assert _resolve_summarize_source("arxiv", {}) == ("arxiv", "")
    assert _resolve_summarize_source("pdf", {}) == ("pdf", "")
    assert _resolve_summarize_source("url", {}) == ("web", "")
    assert _resolve_summarize_source("pdf", {"source_kind": "bioRxiv"}) == ("biorxiv", "")
    assert _resolve_summarize_source("pdf", {"source_kind": "conference", "venue": "NeurIPS 2025"}) == (
        "conference",
        "NeurIPS 2025",
    )
    assert _resolve_summarize_source("pdf", {"source_kind": "other"})[0] == "pdf"


def test_extract_paper_meta_with_llm_without_key_returns_empty(monkeypatch):
    import llm
    import src.local_server as ls

    monkeypatch.setattr(llm, "resolve_llm_api_key", lambda *a, **k: "")
    assert ls._extract_paper_meta_with_llm("x" * 50) == {}


def test_extract_paper_meta_with_llm_parses_structured_response(monkeypatch):
    import llm
    import src.local_server as ls

    monkeypatch.setattr(llm, "resolve_llm_api_key", lambda *a, **k: "k")
    monkeypatch.setattr(llm, "resolve_llm_model", lambda *a, **k: "m")
    monkeypatch.setattr(llm, "resolve_llm_base_url", lambda *a, **k: "u")

    class FakeClient:
        def __init__(self, api_key, model, base_url):
            pass

        def chat_structured(self, messages, schema_name, schema):
            assert schema_name == "summarize_paper_meta"
            assert "论文开头内容" in messages[1]["content"]
            return {
                "parsed": {
                    "title": "T",
                    "authors": ["A"],
                    "venue": "ICML 2026",
                    "source_kind": "conference",
                }
            }

    monkeypatch.setattr(llm, "OpenAIClient", FakeClient)
    meta = ls._extract_paper_meta_with_llm("Title: T\nAuthors: A" + "x" * 50)
    assert meta["source_kind"] == "conference"
    assert meta["venue"] == "ICML 2026"


def test_extract_paper_meta_with_llm_failure_returns_empty(monkeypatch):
    import llm
    import src.local_server as ls

    monkeypatch.setattr(llm, "resolve_llm_api_key", lambda *a, **k: "k")
    monkeypatch.setattr(llm, "resolve_llm_model", lambda *a, **k: "m")
    monkeypatch.setattr(llm, "resolve_llm_base_url", lambda *a, **k: "u")

    class BoomClient:
        def __init__(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(llm, "OpenAIClient", BoomClient)
    assert ls._extract_paper_meta_with_llm("x" * 50) == {}


def test_run_summarize_job_pdf_filename_arxiv_id_uses_arxiv_meta(monkeypatch):
    """回归：上传 PDF 文件名含 arXiv id 时应反查官方元数据，kind 升级为 arxiv，
    不拿文件名当标题，也不走 LLM 抽取。"""
    import base64
    import time

    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "A research paper about graph neural networks. " * 20, fontsize=9)
    data_b64 = base64.b64encode(doc.tobytes()).decode("ascii")
    doc.close()

    import src.local_server as ls

    arxiv_meta = {
        "title": "Real arXiv Title",
        "abstract": "Real abstract",
        "authors": ["Alice", "Bob"],
        "published": "2026-01-02",
        "link": "https://arxiv.org/abs/2601.01234v1",
        "pdf_url": "https://arxiv.org/pdf/2601.01234v1",
    }
    monkeypatch.setattr(ls, "_fetch_arxiv_metadata", lambda arxiv_id: arxiv_meta)
    captured = {}

    def fake_persist(**kw):
        captured.update(kw)
        return {"paper_id": "202608/27/t-paper", "md_path": "x.md", "registered": True}

    monkeypatch.setattr(ls, "_persist_summarize_as_daily_paper", fake_persist)

    store = ls.SummarizeJobStore()
    job = store.create({"source": "pdf", "filename": "2601.01234v1.pdf", "data_b64": data_b64})
    job_id = job["job_id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        s = store.get(job_id)
        if s["status"] in ("completed", "failed"):
            break
        time.sleep(0.3)
    got = store.get(job_id)
    assert got["status"] == "completed", got.get("error")
    assert captured["arxiv_id"] == "2601.01234v1"
    assert captured["kind"] == "arxiv"
    assert captured["title"] == "Real arXiv Title"
    assert captured["paper_meta"]["authors"] == ["Alice", "Bob"]


def test_persist_summarize_uses_real_source_and_venue(tmp_path, monkeypatch):
    """回归：上传 PDF 的纸张页 source 不得硬编码 arxiv；LLM 抽到的 venue/authors 透传。"""
    import sys
    import src.local_server as ls

    fake_module = tmp_path / "6.generate_docs.py"
    fake_module.write_text(
        "_captured = {}\n"
        "def generate_external_paper_docs(paper, *, date_str, docs_dir, section,\n"
        "                                    pdf_bytes=None, full_text='', on_progress=None):\n"
        "    _captured['paper'] = paper\n"
        "    return '202608/27/t-paper', 'T', 'x.md'\n"
        "def update_sidebar(sidebar_path, date_str, deep_entries, quick_entries, evidence, **kw):\n"
        "    return None\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ls, "SRC_DIR", tmp_path)

    ls._persist_summarize_as_daily_paper(
        paper_meta={
            "title": "T",
            "authors": ["A", "B"],
            "venue": "NeurIPS 2025",
            "source_kind": "conference",
        },
        title="T",
        text="x" * 30,
        kind="pdf",
        source="pdf",
        arxiv_id=None,
    )
    paper = sys.modules["dpr_generate_docs_for_summarize"]._captured["paper"]
    assert paper["source"] == "conference"
    assert paper["venue"] == "NeurIPS 2025"
    assert paper["authors"] == ["A", "B"]

    # 无元数据时回退 pdf，不再硬编码 arxiv
    ls._persist_summarize_as_daily_paper(
        paper_meta={}, title="t", text="x" * 30, kind="pdf", source="pdf", arxiv_id=None,
    )
    paper2 = sys.modules["dpr_generate_docs_for_summarize"]._captured["paper"]
    assert paper2["source"] == "pdf"
    assert not paper2.get("venue")


# --------------------------------------------------------------------------- #
# 日报流水线步骤进度事件（RunStore / _StepTracker）
# --------------------------------------------------------------------------- #
def test_run_event_factory_is_defined_before_global_store_initialization():
    """有遗留 active run 时，全局 RunStore 初始化会立刻创建 interrupted 事件。"""
    from pathlib import Path

    source = Path("src/local_server.py").read_text(encoding="utf-8")
    assert source.index("def _new_event_id") < source.index("RUN_STORE = RunStore()")


def test_step_tracker_parses_step_markers():
    """[INFO] Step X - 标签: 命令 锚点 → run.progress 事件；新步骤开始时补发上一步 completed。"""
    from src.local_server import _StepTracker

    t = _StepTracker("run-1")
    evs = t.observe("[INFO] Step 1 - fetch arxiv: python fetch.py\n")
    assert [(e["payload"]["step"], e["payload"]["state"]) for e in evs] == [("step_1_fetch", "started")]
    assert evs[0]["message"] == "抓取 arXiv 论文 开始"

    evs = t.observe("[INFO] Step 2.1 - BM25: python bm25.py\n")
    assert [(e["payload"]["step"], e["payload"]["state"]) for e in evs] == [
        ("step_1_fetch", "completed"),
        ("step_2_1_bm25", "started"),
    ]

    evs = t.finalize(True)
    assert [(e["payload"]["step"], e["payload"]["state"]) for e in evs] == [("step_2_1_bm25", "completed")]
    assert t.finalize(True) == []  # 幂等


def test_step_tracker_skip_and_failure():
    """「跳过 Step 1」→ skipped 事件；finalize(False) → 当前步骤 failed。"""
    from src.local_server import _StepTracker

    t = _StepTracker("run-1")
    evs = t.observe("[INFO] 跳过 Step 1（全量数据拉取）：Supabase 已完全接管检索。\n")
    assert [(e["payload"]["step"], e["payload"]["state"]) for e in evs] == [("step_1_fetch", "skipped")]

    t.observe("[INFO] Step 4 - LLM refine: python llm.py\n")
    evs = t.finalize(False)
    assert [(e["payload"]["step"], e["payload"]["state"]) for e in evs] == [("step_4_llm_refine", "failed")]


def test_step_tracker_ignores_non_step_lines():
    from src.local_server import _StepTracker

    t = _StepTracker("run-1")
    assert t.observe("[TRACE] 启用论文追踪: 2506.21924\n") == []
    assert t.observe("random log line\n") == []
    assert t.observe("[INFO] DPR_RUN_DATE=20260827\n") == []


def test_step_tracker_emits_throttled_embedding_progress():
    from src.local_server import _StepTracker

    t = _StepTracker("run-1")
    first = t.observe("[2026-09-01][INFO] Embedding 进度: 300/5816 (~4.53 paper/s)\n")
    assert len(first) == 1
    assert first[0]["current"] == 300
    assert first[0]["total"] == 5816
    assert first[0]["payload"]["state"] == "running"
    assert first[0]["payload"]["rate"] == 4.53
    assert first[0]["payload"]["eta_seconds"] > 0

    # 同一 5% 桶内不重复发事件，避免公网快照与前端列表被刷屏。
    assert t.observe("[INFO] Embedding 进度: 320/5816 (~4.50 paper/s)\n") == []
    next_bucket = t.observe("[INFO] Embedding 进度: 600/5816 (~4.50 paper/s)\n")
    assert len(next_bucket) == 1


def test_step_tracker_emits_step6_document_progress_with_eta():
    from src.local_server import _StepTracker

    t = _StepTracker("run-1")
    started = t.observe(
        "[2026-09-01 03:10:00] [PROGRESS] Step 6 docs: 0/19 "
        "| section=queued | status=completed | elapsed=0.0s | eta=unknown | paper= | title=\n"
    )
    assert len(started) == 1
    assert started[0]["current"] == 0
    assert started[0]["total"] == 19
    assert "准备并发生成 19 篇" in started[0]["message"]

    progress = t.observe(
        "[2026-09-01 03:12:00] [PROGRESS] Step 6 docs: 3/19 "
        "| section=deep | status=completed | elapsed=120.0s | eta=640.0s "
        "| paper=20260823-20260901/2608.1v1-a-vlm-paper | title=A VLM Paper\n"
    )
    assert len(progress) == 1
    event = progress[0]
    assert event["payload"]["step"] == "step_6_generate"
    assert event["payload"]["state"] == "running"
    assert event["payload"]["eta_seconds"] == 640
    assert event["payload"]["paper_title"] == "A VLM Paper"
    assert event["payload"]["paper_id"] == "20260823-20260901/2608.1v1-a-vlm-paper"
    assert event["payload"]["percent"] > 15


def test_run_store_streams_step_events(tmp_path):
    """回归：本地运行子进程 stdout 逐行解析成 run.progress 事件，前端可渲染步骤清单。"""
    import sys
    import time

    import src.local_server as ls

    script = (
        "print('[INFO] DPR_RUN_DATE=20260827', flush=True)\n"
        "print('[INFO] Step 2.1 - BM25: python x.py', flush=True)\n"
        "print('noise line', flush=True)\n"
        "print('[INFO] Step 3 - Rerank: python y.py', flush=True)\n"
        "print('done', flush=True)\n"
    )
    store = ls.RunStore(tmp_path / "runs")
    run = store.create("daily-now", "daily-paper-reader.yml", {}, [sys.executable, "-c", script])
    run_id = run["id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        r = store.get(run_id)
        if r["status"] == "completed":
            break
        time.sleep(0.2)
    r = store.get(run_id)
    assert r["status"] == "completed", r
    assert r["conclusion"] == "success"

    types = [e["event_type"] for e in r["events"]]
    assert types[0] == "run.accepted"
    assert "run.started" in types
    assert types[-1] == "run.completed"

    steps = [(e["payload"]["step"], e["payload"]["state"]) for e in r["events"] if e["event_type"] == "run.progress"]
    assert ("step_2_1_bm25", "started") in steps
    assert ("step_2_1_bm25", "completed") in steps
    assert ("step_3_rerank", "started") in steps
    assert ("step_3_rerank", "completed") in steps
    # 事件顺序：2.1 started 必须先于 2.1 completed
    assert steps.index(("step_2_1_bm25", "started")) < steps.index(("step_2_1_bm25", "completed"))


def test_run_store_failure_emits_failed_event(tmp_path):
    """子进程非零退出 → run.failed 事件 + conclusion=failure。"""
    import sys
    import time

    import src.local_server as ls

    store = ls.RunStore(tmp_path / "runs")
    run = store.create("daily-now", "daily-paper-reader.yml", {}, [sys.executable, "-c", "import sys; sys.exit(3)"])
    run_id = run["id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        r = store.get(run_id)
        if r["status"] == "completed":
            break
        time.sleep(0.2)
    r = store.get(run_id)
    assert r["status"] == "completed"
    assert r["conclusion"] == "failure"
    types = [e["event_type"] for e in r["events"]]
    assert types[-1] == "run.failed"


def test_run_store_marks_orphaned_run_interrupted_and_can_delete(tmp_path):
    """服务重启后，磁盘上的活跃状态不能继续冒充正在运行。"""
    import json

    import src.local_server as ls

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "orphan123"
    run_dir.mkdir(parents=True)
    record = {
        "id": "orphan123",
        "run_number": 7,
        "workflow_key": "daily-now",
        "workflow_file": "daily-paper-reader.yml",
        "inputs": {},
        "command": ["python", "src/main.py"],
        "status": "in_progress",
        "conclusion": None,
        "created_at": "2026-09-01T01:00:00+00:00",
        "updated_at": "2026-09-01T01:01:00+00:00",
        "started_at": "2026-09-01T01:00:01+00:00",
        "completed_at": None,
        "log_path": str(run_dir / "run.log"),
        "config_path": "",
        "events": [],
        "cancel_requested": False,
    }
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    (run_dir / "run.log").write_text("partial output", encoding="utf-8")

    store = ls.RunStore(runs_dir)
    run = store.get("orphan123")
    assert run["status"] == "completed"
    assert run["conclusion"] == "interrupted"
    assert run["completed_at"]
    assert run["events"][-1]["event_type"] == "run.interrupted"
    persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert persisted["conclusion"] == "interrupted"

    ok, error = store.delete("orphan123")
    assert ok is True
    assert error == ""
    assert not run_dir.exists()


# --------------------------------------------------------------------------- #
# 综述生成异步 Job（connect.job.v1 契约）
# --------------------------------------------------------------------------- #

def _patch_survey_modules(monkeypatch):
    """让 _run_survey_job 的函数内延迟导入命中打桩模块。

    local_server 用「脚本模式优先、包模式兜底」双路径导入 survey_pipeline/survey_docs；
    一旦 src 被插入 sys.path，脚本模式会拿到一个全新模块实例，绕开 monkeypatch。
    这里显式把包模式模块注册为顶层名，保证两种导入路径都命中同一（打桩后的）模块。
    """
    import sys

    import src.local_server as ls
    import src.survey_pipeline as sp
    import src.survey_docs as sd

    captured = {"kwargs": None}

    def fake_run_survey(query, **kwargs):
        captured["kwargs"] = dict(kwargs, query=query)
        for stage in ("recall", "extract", "write"):
            cb = kwargs.get("on_progress")
            if cb:
                cb(stage, f"{stage} done", current=1, total=2)
        return {
            "query": query,
            "papers": [{"paper_id": "2608.1", "title": "P1", "link": "l"}],
            "clusters": [{"cluster_id": 0, "name_zh": "方向", "paper_ids": ["2608.1"]}],
            "outline": {"title_zh": "测试综述", "sections": ["引言"]},
            "report_markdown": "# 测试综述\n\n正文 [1]",
            "report_meta": {"generated_at": "2026-08-27 08:00 UTC", "n_papers": 1, "n_clusters": 1},
            "warnings": [],
        }

    def fake_persist(result):
        return {
            "report_id": "abc123def0",
            "basename": "survey-abc123def0",
            "route": "survey/survey-abc123def0",
            "paper_id": "survey/survey-abc123def0",
            "md_path": "docs/survey/survey-abc123def0.md",
            "title_zh": "测试综述",
            "date": "2026-08-27",
            "registered": True,
        }

    monkeypatch.setattr(sp, "run_survey", fake_run_survey)
    monkeypatch.setattr(sd, "persist_survey_report", fake_persist)
    monkeypatch.setitem(sys.modules, "survey_pipeline", sp)
    monkeypatch.setitem(sys.modules, "survey_docs", sd)
    return captured


def test_survey_job_store_lifecycle_completes(monkeypatch):
    import time

    import src.local_server as ls

    _patch_survey_modules(monkeypatch)
    store = ls.SurveyJobStore()
    job = store.create({"query": "测试主题", "max_papers": 999, "fetch_days": 3})
    job_id = job["job_id"]
    assert job_id.startswith("sv-")
    assert job["events"][0]["event_type"] == "job.accepted"
    assert job["events"][0]["schema_version"] == "connect.job.v1"

    got = None
    deadline = time.time() + 10
    while time.time() < deadline:
        got = store.get(job_id)
        if got and got["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert got is not None and got["status"] == "completed", got and got.get("error")

    # public 视图保留 input（历史列表展示），result 只留摘要
    assert got["input"]["query"] == "测试主题"
    report = got["result"]["report"]
    assert report["route"] == "survey/survey-abc123def0"
    assert "report_markdown" not in got["result"]

    types = [e["event_type"] for e in got["events"]]
    assert types[0] == "job.accepted"
    assert "job.started" in types
    assert types[-1] == "job.completed"
    stages = [e.get("stage") for e in got["events"] if e.get("event_type") == "job.progress"]
    assert "recall" in stages and "write" in stages and "render" in stages

    # 已完成的 job 不可再取消
    assert store.request_cancel(job_id) is False


def test_survey_job_clamps_inputs_and_passes_flags(monkeypatch):
    import time

    import src.local_server as ls

    captured = _patch_survey_modules(monkeypatch)
    store = ls.SurveyJobStore()
    job = store.create({"query": "q", "max_papers": 999, "fetch_days": 0, "use_rerank": False, "deep_read": False})
    deadline = time.time() + 10
    got = None
    while time.time() < deadline:
        got = store.get(job["job_id"])
        if got and got["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert got["status"] == "completed"
    kwargs = captured["kwargs"]
    assert kwargs["max_papers"] == 200  # clamp 5-200
    assert kwargs["fetch_days"] == 1    # clamp 下界：0 → 1
    assert kwargs["use_rerank"] is False
    assert kwargs["deep_read"] is False


def test_survey_job_fetch_days_allows_year_scale(monkeypatch):
    """回溯以年为单位：上限 1095 天（3 年），默认 365 天。"""
    import time

    import src.local_server as ls

    captured = _patch_survey_modules(monkeypatch)
    store = ls.SurveyJobStore()
    job = store.create({"query": "q", "fetch_days": 99999})
    deadline = time.time() + 10
    got = None
    while time.time() < deadline:
        got = store.get(job["job_id"])
        if got and got["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert got["status"] == "completed", got.get("error")
    assert captured["kwargs"]["fetch_days"] == ls.SURVEY_MAX_FETCH_DAYS

    job2 = store.create({"query": "q"})  # 缺省 → 默认回溯 1 年
    deadline = time.time() + 10
    while time.time() < deadline:
        got2 = store.get(job2["job_id"])
        if got2 and got2["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert got2["status"] == "completed"
    assert captured["kwargs"]["fetch_days"] == 365


def test_survey_job_missing_query_fails_fast(monkeypatch):
    import time

    import src.local_server as ls

    _patch_survey_modules(monkeypatch)
    store = ls.SurveyJobStore()
    job = store.create({"query": "   "})
    deadline = time.time() + 10
    got = None
    while time.time() < deadline:
        got = store.get(job["job_id"])
        if got and got["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert got["status"] == "failed"
    assert "query" in (got.get("error") or "")
    types = [e["event_type"] for e in got["events"]]
    assert types[-1] == "job.failed"


def test_survey_job_store_prunes_finished_fifo(monkeypatch):
    """终态 job 超过上限后按 FIFO 淘汰最旧的；运行中的 job 不被淘汰。"""
    import time

    import src.local_server as ls

    _patch_survey_modules(monkeypatch)
    store = ls.SurveyJobStore()
    ls.SURVEY_MAX_FINISHED_JOBS = 3

    # 依次创建 4 个 job，等待每个跑完（query 唯一保证 updated_at 可排序）
    ids = []
    for i in range(4):
        job = store.create({"query": f"prune-{i}", "fetch_days": 1})
        ids.append(job["job_id"])
        deadline = time.time() + 10
        while time.time() < deadline:
            got = store.get(job["job_id"])
            if got and got["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.05)

    # 上限 3：4 个终态中只淘汰最旧的 1 个
    assert store.get(ids[0]) is None
    assert store.get(ids[1]) is not None
    assert store.get(ids[2]) is not None
    assert store.get(ids[3]) is not None


def test_survey_job_seed_payload_passthrough(monkeypatch):
    """seed url → 解析出 arxiv_id 透传；use_deepxiv 透传。"""
    import time

    import src.local_server as ls

    captured = _patch_survey_modules(monkeypatch)
    store = ls.SurveyJobStore()
    job = store.create({
        "query": "q",
        "seed": {"source": "url", "url": "https://arxiv.org/abs/2411.18011v2"},
        "use_deepxiv": False,
    })
    deadline = time.time() + 10
    got = None
    while time.time() < deadline:
        got = store.get(job["job_id"])
        if got and got["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert got["status"] == "completed", got.get("error")
    kwargs = captured["kwargs"]
    assert kwargs["seed_paper"] == {"arxiv_id": "2411.18011"}
    assert kwargs["use_deepxiv"] is False


def test_survey_runtime_embedding_credentials_are_private_and_forwarded(monkeypatch):
    import time

    import src.local_server as ls

    captured = _patch_survey_modules(monkeypatch)
    store = ls.SurveyJobStore()
    job = store.create(
        {"query": "q"},
        runtime_credentials={
            "embedding": {
                "endpoint": "https://embed.example/api",
                "api_key": "embed-secret",
            }
        },
    )
    deadline = time.time() + 10
    got = None
    while time.time() < deadline:
        got = store.get(job["job_id"])
        if got and got["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)

    assert got["status"] == "completed", got.get("error")
    assert got["input"] == {"query": "q"}
    assert captured["kwargs"]["embedding_endpoint"] == "https://embed.example/api"
    assert captured["kwargs"]["embedding_api_key"] == "embed-secret"


def test_survey_job_kaggle_payload_passthrough(monkeypatch):
    """use_kaggle / coarse_top_k 透传 + clamp（超界钳到 500-30000）。"""
    import time

    import src.local_server as ls

    captured = _patch_survey_modules(monkeypatch)
    store = ls.SurveyJobStore()
    job = store.create({
        "query": "q",
        "use_kaggle": False,
        "coarse_top_k": 999999,
    })
    deadline = time.time() + 10
    got = None
    while time.time() < deadline:
        got = store.get(job["job_id"])
        if got and got["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert got["status"] == "completed", got.get("error")
    kwargs = captured["kwargs"]
    assert kwargs["use_kaggle"] is False
    assert kwargs["coarse_top_k"] == 30000, "粗筛量级应钳到上限 30000"

    # 轻量部署默认不启用 Kaggle；用户显式配置本地快照后才能打开。
    captured2 = _patch_survey_modules(monkeypatch)
    store2 = ls.SurveyJobStore()
    job2 = store2.create({"query": "q"})
    deadline = time.time() + 10
    got2 = None
    while time.time() < deadline:
        got2 = store2.get(job2["job_id"])
        if got2 and got2["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert got2["status"] == "completed", got2.get("error")
    assert captured2["kwargs"]["use_kaggle"] is False
    assert captured2["kwargs"]["use_deepxiv"] is False
    assert captured2["kwargs"]["coarse_top_k"] == 10000


def test_survey_job_invalid_seed_url_fails_fast(monkeypatch):
    import time

    import src.local_server as ls

    _patch_survey_modules(monkeypatch)
    store = ls.SurveyJobStore()
    job = store.create({"query": "q", "seed": {"source": "url", "url": "https://example.com/not-arxiv"}})
    deadline = time.time() + 10
    got = None
    while time.time() < deadline:
        got = store.get(job["job_id"])
        if got and got["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert got["status"] == "failed"
    assert "arXiv" in (got.get("error") or "")


# ===== /api/local/smart-query 订阅候选生成 =====


class _FakeStructuredClient:
    """替身 LLM 客户端：记录构造参数与 chat_structured 入参，返回固定 parsed。"""

    instances: list = []
    calls: list = []
    parsed: dict = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).instances.append(self)

    def chat_structured(self, messages, schema_name, schema):
        type(self).calls.append({"messages": messages, "schema_name": schema_name, "schema": schema})
        return {"parsed": dict(type(self).parsed)}


def _write_smart_query_config(tmp_path, monkeypatch, api_key="k"):
    import yaml

    from src import local_server
    cfg = {"local": {"chat": {"model": "chat-model", "base_url": "https://llm.example", "api_key": api_key}}}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(local_server, "CONFIG_PATH", config_path)


def test_generate_subscription_candidates_normalizes_and_prefers_local_chat(tmp_path, monkeypatch):
    import src.local_server as ls

    _write_smart_query_config(tmp_path, monkeypatch)
    _FakeStructuredClient.instances = []
    _FakeStructuredClient.calls = []
    _FakeStructuredClient.parsed = {
        "tag": "RAG",
        "description": "检索增强生成",
        "keywords": [
            {"en": "retrieval augmented generation", "zh": "检索增强生成"},
            {"en": "  ", "zh": "空 en 丢弃"},
            "not-a-dict",
            {"zh": "缺 en 丢弃"},
        ],
        "queries": [{"en": "Find recent papers on RAG", "zh": "查找 RAG 新论文"}],
    }
    monkeypatch.setattr("llm.OpenAIClient", _FakeStructuredClient)

    result = ls._generate_subscription_candidates("追踪检索增强生成新论文", "RAG")

    # 优先用 local.chat 配置构造客户端
    assert len(_FakeStructuredClient.instances) == 1
    kwargs = _FakeStructuredClient.instances[0].kwargs
    assert kwargs["api_key"] == "k"
    assert kwargs["model"] == "chat-model"
    assert kwargs["base_url"] == "https://llm.example"

    # 用户意图与标签提示进入 prompt
    prompt = _FakeStructuredClient.calls[0]["messages"][-1]["content"]
    assert "追踪检索增强生成新论文" in prompt
    assert "RAG" in prompt

    # 候选清洗：非 dict / 空 en 的条目被剔除
    assert result["keywords"] == [{"en": "retrieval augmented generation", "zh": "检索增强生成"}]
    assert result["queries"] == [{"en": "Find recent papers on RAG", "zh": "查找 RAG 新论文"}]
    assert result["tag"] == "RAG"


def test_generate_subscription_candidates_tag_fallback_when_llm_tag_blank(tmp_path, monkeypatch):
    import src.local_server as ls

    _write_smart_query_config(tmp_path, monkeypatch)
    _FakeStructuredClient.parsed = {"tag": "", "description": "", "keywords": [], "queries": []}
    monkeypatch.setattr("llm.OpenAIClient", _FakeStructuredClient)

    result = ls._generate_subscription_candidates("q", tag_hint="AHD-EA")
    assert result["tag"] == "AHD-EA", "LLM 未给标签时回退用户已有标签"


def test_generate_subscription_candidates_requires_api_key(tmp_path, monkeypatch):
    import pytest

    import src.local_server as ls

    _write_smart_query_config(tmp_path, monkeypatch, api_key="")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("llm.resolve_llm_api_key", lambda: "")

    with pytest.raises(ValueError, match="API Key"):
        ls._generate_subscription_candidates("q")


# --------------------------------------------------------------------------- #
# 设置面板：模型列表拉取 + 连通性测试
# --------------------------------------------------------------------------- #
def test_build_openai_models_url_variants():
    from src.local_server import _build_openai_models_url

    assert _build_openai_models_url("https://api.x.com") == "https://api.x.com/v1/models"
    assert _build_openai_models_url("https://api.x.com/") == "https://api.x.com/v1/models"
    assert _build_openai_models_url("https://api.x.com/v1") == "https://api.x.com/v1/models"
    assert (
        _build_openai_models_url("https://api.x.com:58443/v1")
        == "https://api.x.com:58443/v1/models"
    )
    assert (
        _build_openai_models_url("https://api.x.com/v1/chat/completions")
        == "https://api.x.com/v1/models"
    )
    import pytest

    with pytest.raises(ValueError, match="API 端点"):
        _build_openai_models_url("")


def test_parse_model_ids_openai_and_ollama_shapes():
    import json as _json

    from src.local_server import _parse_model_ids

    openai_body = _json.dumps({"data": [{"id": "b-model"}, {"id": "a-model"}, {"id": "a-model"}]}).encode()
    assert _parse_model_ids(openai_body) == ["a-model", "b-model"]
    ollama_body = _json.dumps({"models": [{"name": "qwen:7b"}, {"name": "llama3"}]}).encode()
    assert _parse_model_ids(ollama_body) == ["llama3", "qwen:7b"]
    assert _parse_model_ids(_json.dumps({"data": []}).encode()) == []


def test_fetch_chat_model_list_sends_bearer_and_parses(monkeypatch):
    import json as _json

    import src.local_server as ls

    captured = {}

    def fake_get(url, *, headers=None, timeout=0, **kw):
        captured["url"] = url
        captured["headers"] = headers
        return _json.dumps({"data": [{"id": "m2"}, {"id": "m1"}]}).encode()

    monkeypatch.setattr(ls, "_http_get_with_retry", fake_get)
    models = ls._fetch_chat_model_list("https://api.x.com", "sk-test")
    assert models == ["m1", "m2"]
    assert captured["url"] == "https://api.x.com/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_resolve_chat_credentials_fallback_chain(monkeypatch):
    import src.local_server as ls

    monkeypatch.setattr(
        ls,
        "_load_local_chat_config",
        lambda: {"model": "m", "base_url": "https://saved", "api_key": "saved-key"},
    )
    # 请求体优先
    assert ls._resolve_chat_credentials("https://body", "body-key") == ("https://body", "body-key")
    # 留空回退 config.yaml local.chat
    assert ls._resolve_chat_credentials("", "") == ("https://saved", "saved-key")
    # config 也没有 → 回退 .env 解析
    monkeypatch.setattr(ls, "_load_local_chat_config", lambda: {"model": "", "base_url": "", "api_key": ""})
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://env-url")
    url, key = ls._resolve_chat_credentials("", "")
    assert key == "env-key"
    assert url == "https://env-url"


def test_probe_chat_completion_parses_reply_and_latency(monkeypatch):
    from types import SimpleNamespace

    import src.local_server as ls

    captured = {}
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))]
    )

    def fake_client(**kwargs):
        captured["client"] = kwargs

        def create(**request):
            captured["request"] = request
            return completion

        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

    monkeypatch.setattr(ls, "create_openai_client", fake_client)
    latency, snippet = ls._probe_chat_completion("https://api.x.com", "sk-t", "m1")
    assert snippet == "pong"
    assert latency >= 0
    assert captured["client"]["base_url"] == "https://api.x.com/v1"
    assert captured["request"]["model"] == "m1"
    assert captured["request"]["max_tokens"] == 8
    assert captured["request"]["messages"][0]["content"] == "ping"


# --------------------------------------------------------------------------- #
# 设置面板：Reranker 后端选择（local.rerank.profile）
# --------------------------------------------------------------------------- #
def test_merge_local_section_merges_rerank_section():
    from src.local_server import merge_local_section

    existing = {"local": {"chat": {"model": "m"}, "rerank": {"profile": "local-qwen3-0.6b"}}}
    merged = merge_local_section(existing, {"local": {"rerank": {"profile": "public-zwwen-rerank"}}})
    assert merged["local"]["rerank"]["profile"] == "public-zwwen-rerank"
    assert merged["local"]["chat"] == {"model": "m"}  # 其它段不受影响


def test_load_local_chat_full_includes_rerank(tmp_path, monkeypatch):
    import yaml

    import src.local_server as ls

    cfg = {"local": {"rerank": {"profile": "public-sinksilk-rerank"}}}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(ls, "CONFIG_PATH", config_path)

    full = ls._load_local_chat_full()
    assert full["rerank"]["profile"] == "public-sinksilk-rerank"


def test_configured_rerank_profile_whitelist(tmp_path, monkeypatch):
    import yaml

    import src.local_server as ls

    def write(profile):
        cfg = {"local": {"rerank": {"profile": profile}}}
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(ls, "CONFIG_PATH", p)
        return p

    write("public-zwwen-rerank")
    assert ls._configured_rerank_profile() == "public-zwwen-rerank"
    write("LOCAL-QWEN3-0.6B")  # 大小写不敏感
    assert ls._configured_rerank_profile() == "local-qwen3-0.6b"
    write("auto")
    assert ls._configured_rerank_profile() == ""  # auto → 不干预
    write("bogus-profile")
    assert ls._configured_rerank_profile() == ""  # 未知值不透传


def test_apply_rerank_profile_prefers_panel_then_env_file(tmp_path, monkeypatch):
    import yaml

    import src.local_server as ls

    panel = tmp_path / "config.yaml"
    env_file = tmp_path / ".env"

    def set_configs(panel_profile, env_profile):
        cfg = {"local": {"rerank": {"profile": panel_profile}}} if panel_profile != "__absent__" else {}
        panel.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(ls, "CONFIG_PATH", panel)
        env_file.write_text(f"RERANK_PROFILE={env_profile}\n", encoding="utf-8")
        monkeypatch.setattr(ls, "ROOT_DIR", tmp_path)

    for key in ("RERANK_PROFILE", "RERANK_PROVIDER", "RERANK_MODEL"):
        monkeypatch.delenv(key, raising=False)

    # 面板优先于 .env
    set_configs("local-qwen3-0.6b", "public-zwwen-rerank")
    import os as _os

    # 预置旧 base_url 覆盖键（曾把 zwwen profile 劫持回 sinksilk → 401）
    _os.environ["RERANK_API_BASE_URL"] = "https://old.example/rerank"
    _os.environ["PUBLIC_RERANK_API_BASE_URL"] = "https://old.example/rerank"
    _os.environ["SILICONFLOW_RERANK_URL"] = "https://old.example/rerank"
    ls._apply_rerank_profile_to_env()
    assert _os.environ["RERANK_PROFILE"] == "local-qwen3-0.6b"
    assert "RERANK_PROVIDER" not in _os.environ  # 干扰键被清掉
    assert "RERANK_API_BASE_URL" not in _os.environ  # base_url 覆盖键也必须清掉
    assert "PUBLIC_RERANK_API_BASE_URL" not in _os.environ
    assert "SILICONFLOW_RERANK_URL" not in _os.environ

    # 面板 auto → 回退 .env 文件
    set_configs("auto", "public-zwwen-rerank")
    ls._apply_rerank_profile_to_env()
    assert _os.environ["RERANK_PROFILE"] == "public-zwwen-rerank"

    # 都没有 → 清空，交给 3.rank 内建默认（远程公益端点）
    set_configs("auto", "")
    monkeypatch.setenv("RERANK_PROFILE", "local-qwen3-0.6b")
    ls._apply_rerank_profile_to_env()
    assert "RERANK_PROFILE" not in _os.environ
