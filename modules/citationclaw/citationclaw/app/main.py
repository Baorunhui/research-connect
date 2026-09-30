import asyncio
import json
import os
import secrets
import shutil
import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from citationclaw.core.web_search_compat import web_search_extra


def _make_openai_client(api_key: str, base_url: str, timeout: float = 60.0):
    """Create an OpenAI client that bypasses system proxy settings."""
    import httpx
    from research_connect_core.llm import create_openai_client
    return create_openai_client(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        http_client=httpx.Client(trust_env=False, timeout=timeout),
    )

from citationclaw.app.config_manager import (
    ConfigManager, AppConfig, DATA_DIR, redact_api_config, strip_api_config,
)
from citationclaw.app.task_executor import TaskExecutor
from citationclaw.app.log_manager import LogManager


# ==================== Lifespan ====================
@asynccontextmanager
async def lifespan(app):
    """Application startup/shutdown lifecycle."""
    print("=" * 50)
    print("CitationClaw v2 has been activated.")
    print("=" * 50)
    yield
    print("应用已关闭")


# FastAPI应用
app = FastAPI(title="CitationClaw v2", version="2.0.0", lifespan=lifespan)

# 静态文件和模板（使用包内路径，兼容 pip install 和本地开发）
_PKG_DIR = Path(__file__).parent.parent
_ROOT_DIR = _PKG_DIR.parent
app.mount("/static", StaticFiles(directory=str(_PKG_DIR / "static")), name="static")
if (_ROOT_DIR / "docs" / "assets").exists():
    app.mount("/docs-assets", StaticFiles(directory=str(_ROOT_DIR / "docs" / "assets")), name="docs-assets")
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

# 全局对象
config_manager = ConfigManager()
log_manager = LogManager()
task_executor = TaskExecutor(log_manager, config_manager)
_connect_task_state: dict = {
    "schema_version": "connect.job.v1",
    "external_job_id": "",
    "status": "idle",
    "result": None,
    "error": "",
}


def _public_failure(exc: BaseException) -> str:
    """Keep a short reason we wrote. Hide tracebacks, URLs, and library errors."""
    text = " ".join(str(exc).split())
    noisy = bool(re.search(r"https?://|Traceback|\.py\b|\b[A-Za-z]+Error\b", text))
    if text and len(text) <= 180 and not noisy:
        return text
    return "这次没有完成，请稍后重试。"


# ── Helper: task done callback ──────────────────────────────────────────
def _make_task_done_callback(executor: TaskExecutor, lm: LogManager):
    """Create a done_callback that surfaces task exceptions to the UI."""
    def _cb(task: asyncio.Task):
        try:
            exc = task.exception()
            if exc:
                import traceback
                print(traceback.format_exc(), flush=True)
                message = _public_failure(exc)
                _connect_task_state.update(status="failed", error=message, result=None)
                lm.broadcast_event("task_error", {"message": message, "error": message})
            else:
                result = task.result()
                _connect_task_state.update(status="completed", error="", result=result)
        except asyncio.CancelledError:
            _connect_task_state.update(status="cancelled", error="这次查询已停下。", result=None)
            message = "这次查询已停下。"
            lm.warning(message)
            lm.broadcast_event("task_finished", {
                "status": "cancelled",
                "message": message,
                "success": False,
            })
        finally:
            executor.is_running = False
            executor.current_task = None
    return _cb


def _launch_task(coro, *, external_job_id: str = ""):
    """Set is_running, create task, attach done_callback. Returns task."""
    task_executor.is_running = True
    log_manager.set_task_log_suppressed(False)
    _connect_task_state.update(
        schema_version="connect.job.v1",
        external_job_id=str(external_job_id or "").strip(),
        status="running",
        result=None,
        error="",
    )
    task = asyncio.create_task(coro)
    task.add_done_callback(_make_task_done_callback(task_executor, log_manager))
    task_executor.current_task = task
    return task


# ==================== 页面路由 ====================
def _public_base_path() -> str:
    """Sub-path such as ``/citations`` when served behind a path-routing proxy."""
    value = str(os.getenv("CITATIONCLAW_PUBLIC_BASE") or "").strip().rstrip("/")
    return value if value.startswith("/") else ""


@app.get("/")
async def index(request: Request):
    context = {"request": request, "now": date.today().strftime("%Y-%m-%d")}
    base = _public_base_path()
    if not base:
        return templates.TemplateResponse("index.html", context)
    rendered = templates.get_template("index.html").render(context)
    marker = (
        "<script>window.CCR_PUBLIC_API_BASE = " + json.dumps(base)
        + "; window.CCR_WS_ENABLED = true;</script>\n"
    )
    rendered = rendered.replace("</head>", marker + "</head>", 1)
    for prefix in ("/static/", "/docs-assets/"):
        rendered = rendered.replace(f'href="{prefix}', f'href="{base}{prefix}')
        rendered = rendered.replace(f'src="{prefix}', f'src="{base}{prefix}')
    rendered = rendered.replace('href="/" class="agent-brand"', f'href="{base}/" class="agent-brand"')
    return HTMLResponse(rendered)


# ==================== API路由 ====================
def _api_config_write_allowed(request: Request) -> bool:
    """Server-side callers that know CITATIONCLAW_CONFIG_TOKEN may update credentials."""
    token = os.getenv("CITATIONCLAW_CONFIG_TOKEN", "").strip()
    if not token:
        return False
    provided = request.headers.get("x-citationclaw-config-token", "")
    return secrets.compare_digest(provided, token)


def _reject_public_model_test(request: Request):
    """The page does not configure models. These tests also call whatever address the browser sends."""
    if _api_config_write_allowed(request):
        return None
    return JSONResponse(
        status_code=403,
        content={"status": "error", "message": "页面上不能测试模型。"},
    )


@app.get("/api/config")
async def get_config():
    return redact_api_config(config_manager.get().model_dump())


class ConfigUpdate(BaseModel):
    """配置更新模型"""
    scraper_api_keys: list[str]
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    light_api_key: str = ""
    light_base_url: str = ""
    result_folder_prefix: str = ""
    default_output_prefix: str = "paper"
    sleep_between_pages: int = 10
    sleep_between_authors: float = 0.5
    parallel_author_search: int = 10
    resume_page_count: int = 0
    enable_year_traverse: bool = False
    debug_mode: bool = False
    test_mode: bool = False
    scraper_premium: bool = False
    scraper_ultra_premium: bool = False
    scraper_session: bool = False
    scholar_no_filter: bool = False
    scraper_geo_rotate: bool = False
    retry_max_attempts: int = 3
    retry_intervals: str = "5,10,20"
    dc_retry_max_attempts: int = 3
    author_search_prompt1: str = "这是一篇论文。请你根据这个paper_link和paper_title，去搜索查阅这篇论文的作者列表，然后输出每个作者的名字及其对应的单位名称。"
    author_search_prompt2: str = "这是一篇论文及作者列表。请你根据这篇论文、作者名字和作者单位，去搜索该每位作者的个人信息，输出每位作者的谷歌学术累积引用（如有）、重大学术头衔（比如是否IEEE/ACM/ACL等学术Fellow、中国科学院院士、中国工程院院士、国外院士如欧洲科学院院士、诺贝尔奖得主、图灵奖得主，国家杰青、长江学者、优青、在国外著名机构（例如google，deepmind，meta，openai）就业的人士，或在AI领域的国际知名人物），行政职位（如国内外知名大学的校长或院长）。"
    enable_renowned_scholar_filter: bool = True
    renowned_scholar_model: str = "gemini-3-flash-preview-nothinking"
    renowned_scholar_prompt: str = "这是一篇论文的作者列表信息。现在，请你根据这些作者信息，找到那些国内外享誉盛名的学者。对于中国学者，着重找到那些院士级别、校长等重要行政职务的学者。对于海外学者，着重找到那些来自国际著名研究机构如谷歌、微软（google，deepmind，meta，openai），以及有海外院士头衔的学者。若该作者列表里没有这样的重要学者，则输出\"无\"。"
    enable_author_verification: bool = False
    author_verify_model: str = "gemini-3-pro-preview-search"
    author_verify_prompt: str = "这是一份已经整理好的作者学术信息列表。请你对列表中的每一位作者信息进行真实性校验。"
    enable_citing_description: bool = True
    enable_dashboard: bool = True
    service_tier: str = "basic"
    citing_description_scope: str = "all"
    skip_author_search: bool = False
    specified_scholars: str = ""
    dashboard_skip_citing_analysis: bool = False
    dashboard_model: str = "gemini-3-flash-preview-nothinking"
    s2_api_key: str = ""
    wos_api_key: str = ""
    mineru_api_token: str = ""
    search_backend: str = "zhipu_native"
    cdp_debug_port: int = 0
    api_access_token: str = ""
    api_user_id: str = ""
    profile_fallback_api_keys: list[str] = []
    profile_fallback_base_url: str = ""
    profile_fallback_model: str = ""


@app.get("/api/presets")
async def get_presets():
    from citationclaw.app.config_manager import SERVICE_TIER_PRESETS
    return SERVICE_TIER_PRESETS


@app.get("/api/providers")
async def get_providers():
    """Return LLM provider presets for the setup wizard."""
    from citationclaw.config.provider_manager import ProviderManager
    pm = ProviderManager()
    presets = {}
    for name in pm.list_presets():
        info = pm.get_preset(name)
        presets[name] = {
            "name": info.get("name", name),
            "base_url": info.get("base_url", ""),
            "default_model": info.get("default_model", ""),
        }
    return {"presets": presets}


@app.post("/api/config")
async def save_config(config: ConfigUpdate, request: Request):
    try:
        # 合并保存：UI 未提交的字段（profile_* 等）保留当前值，
        # 而不是被重置为默认值。接口密钥只接受带服务器令牌的请求。
        data = config_manager.get().model_dump()
        incoming = config.model_dump()
        if not _api_config_write_allowed(request):
            incoming = strip_api_config(incoming)
        data.update(incoming)
        new_config = AppConfig(**data)
        config_manager.save(new_config)
        return {"status": "success", "message": "配置已保存"}
    except Exception as e:
        print(f"[citationclaw] config save failed: {e}", flush=True)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "设置没有保存，请稍后重试。"}
        )


# ── URL validation helper ──────────────────────────────────────────
_SCHOLAR_URL_RE = re.compile(r'^https?://(scholar\.google\.\w+|scholar\.google\.co\.\w+)')


def _validate_scholar_url(url: str) -> str:
    """Validate that the URL looks like a Google Scholar URL."""
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL 不能为空")
    if not re.match(r'^https?://', url):
        raise HTTPException(status_code=400, detail="URL 必须以 http:// 或 https:// 开头")
    return url


class TaskStartRequest(BaseModel):
    url: str
    output_prefix: str
    resume_page: int = 0


class YearTraverseResponse(BaseModel):
    enable: bool


@app.post("/api/task/start")
async def start_task(request: TaskStartRequest):
    if task_executor.is_running:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "已经有一次查询在进行，请等它完成。"}
        )

    url = _validate_scholar_url(request.url)
    config = config_manager.get()

    _launch_task(
        task_executor.execute_stage1_scraping(
            url=url,
            config=config,
            output_prefix=_safe_output_prefix(request.output_prefix, "paper"),
            resume_page=request.resume_page
        )
    )
    return {"status": "success", "message": "已开始查询这些论文被谁引用。"}


@app.post("/api/task/continue")
async def continue_task():
    if task_executor.is_running:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "已经有一次查询在进行，请等它完成。"}
        )

    if not task_executor.stage1_result:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "还没有上一步的结果，请从首页重新开始查询。"}
        )

    _launch_task(task_executor.execute_stage2_and_3())
    return {"status": "success", "message": "已开始核对作者和单位。"}


async def _read_upload_capped(file: UploadFile, limit: int) -> bytes | None:
    """Read an upload and stop once it passes the limit, instead of buffering all of it."""
    chunks: list[bytes] = []
    total = 0
    while True:
        block = await file.read(1024 * 1024)
        if not block:
            break
        total += len(block)
        if total > limit:
            return None
        chunks.append(block)
    return b"".join(chunks)


def _paper_groups_problem(papers: list) -> str:
    """Refuse a full analysis that would search an unbounded list of titles."""
    kept = 0
    for paper in papers:
        title = str(getattr(paper, "title", "") or "").strip()
        if not title:
            continue
        if len(title) > 500:
            return "有一篇论文题目太长了，请缩短后再查。"
        aliases = [str(alias or "").strip() for alias in (getattr(paper, "aliases", None) or [])]
        aliases = [alias for alias in aliases if alias]
        if any(len(alias) > 500 for alias in aliases):
            return "有一个其他题名太长了，请缩短后再查。"
        if len(aliases) > 8:
            return "一篇论文的其他题名太多了，请留在 8 个以内。"
        kept += 1
    if kept == 0:
        return "请输入至少一篇论文题目"
    if kept > 30:
        return "一次最多查 30 篇，请先减少几篇。"
    return ""


def _safe_output_prefix(value: str, default: str) -> str:
    """Keep a result filename inside the result folder."""
    text = str(value or "").replace("\\", "/").split("/")[-1].strip().replace("..", "")
    cleaned = re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE).strip("._")
    if not cleaned:
        return default
    return cleaned[:80]


@app.post("/api/task/import")
async def import_task(file: UploadFile = File(...)):
    import tempfile

    temp_path = None
    try:
        content = await _read_upload_capped(file, 100 * 1024 * 1024)
        if content is None:
            raise HTTPException(status_code=413, detail="文件过大，限制 100MB")

        with tempfile.NamedTemporaryFile(mode='wb', suffix='.jsonl', delete=False) as temp_file:
            temp_file.write(content)
            temp_path = Path(temp_file.name)

        config = config_manager.get()
        result = await task_executor.import_history(temp_path, config)

        if result["success"]:
            return {
                "status": "success",
                "file_name": result["file_name"],
                "paper_count": result["paper_count"],
                "file_prefix": result["file_prefix"]
            }
        else:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": result["message"]}
            )

    except HTTPException:
        raise
    except Exception as e:
        print(f"[citationclaw] import failed: {e}", flush=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": "导入失败，请稍后重试。"}
        )
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


class PaperInput(BaseModel):
    title: str
    aliases: List[str] = []


class RunRequest(BaseModel):
    papers: List[PaperInput]
    output_prefix: str = "paper"
    external_job_id: str = ""


@app.get("/api/quota/check")
async def check_quota():
    config = config_manager.get()
    if not config.api_access_token or not config.api_user_id:
        return {"configured": False, "message": "未配置系统令牌或用户ID"}
    from citationclaw.app.cost_tracker import CostTracker
    ct = CostTracker()
    result = await ct.query_llm_quota(config.openai_base_url, config.api_access_token, config.api_user_id)
    if result:
        remaining = result["quota"] / 500_000
        return {
            "configured": True,
            "remaining": round(remaining, 2),
            "remaining_rmb": round(remaining * 2, 2),
        }
    return {"configured": True, "error": "查询失败，令牌可能无效"}


@app.post("/api/run")
async def run_pipeline(request: RunRequest):
    if task_executor.is_running:
        return JSONResponse(status_code=400,
            content={"status": "error", "message": "已经有一次查询在进行，请等它完成。"})

    problem = _paper_groups_problem(request.papers)
    if problem:
        return JSONResponse(status_code=400,
            content={"status": "error", "message": problem})
    groups = [{"title": p.title.strip(), "aliases": [a.strip() for a in p.aliases if a.strip()]}
              for p in request.papers if p.title.strip()]

    config = config_manager.get()
    _launch_task(
        task_executor.execute_for_titles(
            paper_groups=groups,
            config=config,
            output_prefix=_safe_output_prefix(request.output_prefix, "paper"),
        ),
        external_job_id=request.external_job_id,
    )
    total = sum(1 + len(g["aliases"]) for g in groups)
    return {
        "schema_version": "connect.job.v1",
        "status": "success",
        "job_id": request.external_job_id,
        "message": f"已启动，共 {len(groups)} 篇论文（含 {total} 个搜索标题）",
    }


class FromCacheRequest(BaseModel):
    paper_title: str
    output_prefix: str = "cached"


@app.post("/api/run/from-cache")
async def run_from_cache(request: FromCacheRequest):
    if task_executor.is_running:
        return JSONResponse(status_code=400,
            content={"status": "error", "message": "已经有一次查询在进行，请等它完成。"})

    if not request.paper_title.strip():
        return JSONResponse(status_code=400,
            content={"status": "error", "message": "请输入论文标题"})
    if len(request.paper_title.strip()) > 500:
        return JSONResponse(status_code=400,
            content={"status": "error", "message": "论文题目太长了，请缩短后再查。"})

    config = config_manager.get()
    _launch_task(
        task_executor.build_report_from_cache(
            paper_title=request.paper_title.strip(),
            config=config,
            output_prefix=_safe_output_prefix(request.output_prefix, "cached"),
        )
    )
    return {"status": "success", "message": f"已启动缓存报告生成: {request.paper_title.strip()}"}


class ScholarProfileRequest(BaseModel):
    profile_url: str


@app.post("/api/scholar/papers")
async def fetch_scholar_papers(request: ScholarProfileRequest):
    url = _validate_scholar_url(request.profile_url)
    config = config_manager.get()
    from citationclaw.core.scholar_profile_cache import is_author_profile_url, openalex_author_id_from_url
    if not is_author_profile_url(url):
        return JSONResponse(status_code=400, content={
            "error": "请填写 OpenAlex 的作者主页（https://openalex.org/A…），或上传保存的主页。",
        })
    oa_author = openalex_author_id_from_url(url)
    if oa_author:
        from citationclaw.core.openalex_citing import OpenAlexCitingFetcher
        fetcher = OpenAlexCitingFetcher(
            DATA_DIR / "cache" / "openalex_citing",
            email=getattr(config, "openalex_email", "") or os.getenv("CITATIONCLAW_OPENALEX_MAILTO", ""),
            api_key=os.getenv("OPENALEX_API_KEY", ""),
            rate=float(os.getenv("CITATIONCLAW_OPENALEX_RPS", "8") or 8),
            log=print,
        )
        try:
            got = await fetcher.fetch_author_works(oa_author, top_n=200, min_citations=0)
        finally:
            await fetcher.close()
        papers = got.get("papers") or []
        if not papers:
            return JSONResponse(status_code=422, content={
                "error": "没有读到论文列表。请确认这是 OpenAlex 的作者主页。",
            })
        return {
            "papers": papers,
            "total": len(papers),
            "truncated": bool(got.get("truncated")),
            "incomplete": bool(got.get("incomplete")),
            "total": got.get("total") if isinstance(got.get("total"), int) else None,
            "scholar_name": got.get("name") or "",
        }

    from citationclaw.core.scholar_profile_scraper import ScholarProfileScraper, no_papers_message
    scraper = ScholarProfileScraper(
        api_keys=config.scraper_api_keys,
        log_callback=print,
        retry_max_attempts=config.retry_max_attempts,
        retry_intervals=config.retry_intervals,
        s2_api_key=getattr(config, 's2_api_key', ''),
    )
    try:
        papers = await scraper.fetch_all_papers(url)
        if not papers:
            return JSONResponse(status_code=422, content={
                "error": no_papers_message(url, scraper.scholar_blocked,
                                           scraper.s2_rate_limited)})
        from citationclaw.core.kaggle_arxiv_meta import enrich_papers_local
        local_hits = enrich_papers_local(papers)
        return {"papers": papers, "total": len(papers), "local_metadata_hits": local_hits}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": _public_failure(e)})
    except Exception as e:
        print(f"[citationclaw] scholar papers failed: {e}", flush=True)
        return JSONResponse(status_code=500, content={"error": _public_failure(e)})


@app.get("/api/paper/meta")
async def paper_metadata(arxiv_id: str = "", title: str = "", doi: str = ""):
    """Single-paper metadata: local Kaggle arXiv index first, external APIs only on a miss."""
    if not (arxiv_id or title or doi):
        return JSONResponse(status_code=400, content={"error": "需要 arxiv_id、title 或 doi"})
    from citationclaw.core.kaggle_arxiv_meta import get_kaggle_meta
    local_index = get_kaggle_meta()
    rec = local_index.lookup(title=title, arxiv_id=arxiv_id, doi=doi)
    if rec:
        print(f"[Kaggle] /api/paper/meta 本地命中 {rec['arxiv_id']}", flush=True)
        return {"source": "kaggle_arxiv", "paper": rec}

    config = config_manager.get()
    s2_key = getattr(config, "s2_api_key", "") or None
    print(f"[Kaggle] /api/paper/meta 本地未命中 (arxiv_id={arxiv_id!r} doi={doi!r} "
          f"title={title[:40]!r})，走外部后备", flush=True)
    paper = None
    error = ""
    try:
        if arxiv_id:
            from citationclaw.core.s2_client import S2Client
            s2 = S2Client(api_key=s2_key)
            try:
                paper = await s2.get_paper_by_arxiv_id(arxiv_id)
            finally:
                await s2.close()
        else:
            from citationclaw.core.metadata_collector import MetadataCollector
            collector = MetadataCollector(s2_api_key=s2_key)
            try:
                paper = await collector.collect(title or doi)
            finally:
                await collector.close()
    except Exception as e:
        print(f"[citationclaw] paper meta failed: {e}", flush=True)
        error = "这次没有查到这篇论文。"
    return {
        "source": "external_fallback",
        "local_index_available": local_index.available,
        "paper": paper,
        "error": error,
    }


class ProfileRunRequest(BaseModel):
    profile_url: str = ""
    output_prefix: str = "scholar_profile"
    top_n: int = 30
    min_citations: int = 0
    use_llm_fallback: bool = True
    force_refresh: bool = False
    mode: str = ""  # fast | full; empty = config.profile_mode


async def _serve_cached_profile(profile_url: str = "", profile_html: str = "",
                                scholar_name: str = "", *,
                                top_n: int = 30, min_citations: int = 0,
                                mode: str = "fast", use_llm_fallback: bool = True) -> Optional[dict]:
    """Answer a scholar-profile request from the local cache without starting a task."""
    from citationclaw.core.scholar_profile_cache import (
        ScholarProfileCache, scholar_cache_keys, profile_request_params,
    )
    cached = ScholarProfileCache().lookup(
        scholar_cache_keys(profile_url, profile_html, scholar_name),
        params=profile_request_params(top_n, min_citations, mode, use_llm_fallback),
    )
    if cached is None:
        return None
    result = await task_executor._serve_cached_scholar_profile(cached)
    _connect_task_state.update(
        schema_version="connect.job.v1", external_job_id="",
        status="completed", result=result, error="",
    )
    when = str(cached.get("updated_at") or "").replace("T", " ").replace("+00:00", " UTC")
    return {
        "status": "success",
        "cached": True,
        "message": f"设置没变，打开上次的结果（{when}）。要重查请勾选「重新查」。",
        "result": {
            name: task_executor._data_result_path(result.get(name))
            for name in ("excel", "json", "dashboard")
        },
    }


@app.get("/api/profile/cache")
async def list_profile_cache():
    """List scholars whose profile-pipeline results are served from the local cache."""
    from citationclaw.core.scholar_profile_cache import ScholarProfileCache
    entries = ScholarProfileCache().list_entries()
    for entry in entries:
        for name in ("excel", "json", "dashboard"):
            if entry.get(name):
                entry[name] = task_executor._data_result_path(entry[name])
    return {"entries": entries}


@app.get("/api/honor-list/stats")
async def honor_list_stats():
    """Local honor list used by the quick report (sources, counts, fields present)."""
    from citationclaw.core.honor_list import get_honor_list
    return get_honor_list().stats()


def _clamp_profile_top_n(value) -> int:
    """Page and API both stay inside 1–500. Blank, zero, and negative become 30."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 30
    if n < 1:
        return 30
    return min(n, 500)


def _clamp_min_citations(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, 1_000_000))


def _apply_profile_params(config, top_n=None, min_citations=None, use_llm_fallback=None,
                          mode=None):
    """Override config.profile_* fields from request params (None = keep config default)."""
    if mode in ("fast", "full"):
        config.profile_mode = mode
    if top_n is not None:
        config.profile_top_n = _clamp_profile_top_n(top_n)
    if min_citations is not None:
        config.profile_min_citations = _clamp_min_citations(min_citations)
    if use_llm_fallback is not None:
        config.profile_use_llm_fallback = use_llm_fallback
    return config


@app.post("/api/profile/run")
async def run_profile_pipeline(request: ProfileRunRequest):
    """Launch the scholar-profile fast pipeline from a Google Scholar profile URL."""
    if task_executor.is_running:
        return JSONResponse(status_code=400,
            content={"status": "error", "message": "已经有一次查询在进行，请等它完成。"})
    from citationclaw.core.scholar_profile_cache import is_author_profile_url
    url = _validate_scholar_url(request.profile_url)
    if not is_author_profile_url(url):
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": "请填写 OpenAlex 的作者主页（https://openalex.org/A…），或上传保存的主页。",
        })
    config = config_manager.get()
    config = _apply_profile_params(config,
        top_n=request.top_n,
        min_citations=request.min_citations,
        use_llm_fallback=request.use_llm_fallback,
        mode=request.mode,
    )
    if not request.force_refresh:
        cached = await _serve_cached_profile(
            profile_url=url,
            top_n=request.top_n,
            min_citations=request.min_citations,
            mode=request.mode or "fast",
            use_llm_fallback=request.use_llm_fallback,
        )
        if cached is not None:
            return cached
    _launch_task(
        task_executor.execute_scholar_profile(
            config=config,
            output_prefix=_safe_output_prefix(request.output_prefix, "scholar_profile"),
            profile_url=url,
            force_refresh=request.force_refresh,
        )
    )
    return {"status": "success", "message": f"学者主页流水线已启动: {url}"}


@app.post("/api/profile/upload")
async def upload_profile_pipeline(request: Request,
                                  file: UploadFile = File(...),
                                  output_prefix: str = Form("scholar_profile"),
                                  top_n: int = Form(30),
                                  min_citations: int = Form(0),
                                  use_llm_fallback: bool = Form(True),
                                  force_refresh: bool = Form(False),
                                  mode: str = Form("")):
    """Launch the scholar-profile fast pipeline from an uploaded HTML file."""
    if mode not in ("fast", "full"):
        mode = request.query_params.get("mode", "")
    if task_executor.is_running:
        return JSONResponse(status_code=400,
            content={"status": "error", "message": "已经有一次查询在进行，请等它完成。"})
    content = await _read_upload_capped(file, 20 * 1024 * 1024)
    if content is None:
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": "这个文件超过 20MB。请另存为仅网页，不要连图片一起保存。",
        })
    if not content.strip():
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": "这个文件是空的。请重新保存学者主页后再上传。",
        })
    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError:
        html = content.decode("gbk", errors="replace")
    from citationclaw.core.scholar_profile_scraper import ScholarProfileScraper
    try:
        papers = await asyncio.to_thread(ScholarProfileScraper.parse_html, html)
    except Exception as exc:
        print(f"[citationclaw] profile html parse failed: {exc}", flush=True)
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": "这份文件读不出来。请重新保存学者主页后再上传。",
        })
    if not papers:
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": "这份文件里没有论文列表。请在自己电脑上打开学者主页，另存为网页后再上传。",
        })
    config = config_manager.get()
    config = _apply_profile_params(config,
        top_n=top_n,
        min_citations=min_citations,
        use_llm_fallback=use_llm_fallback,
        mode=mode,
    )
    if not force_refresh:
        cached = await _serve_cached_profile(
            profile_html=html,
            scholar_name=file.filename or "",
            top_n=top_n,
            min_citations=min_citations,
            mode=mode or "fast",
            use_llm_fallback=use_llm_fallback,
        )
        if cached is not None:
            return cached
    _launch_task(
        task_executor.execute_scholar_profile(
            config=config,
            output_prefix=_safe_output_prefix(output_prefix, "scholar_profile"),
            profile_html=html,
            scholar_name=file.filename or "",
            force_refresh=force_refresh,
        )
    )
    return {"status": "success", "message": f"已从上传文件启动学者主页流水线: {file.filename}"}


class ChatReportRequest(BaseModel):
    messages: list
    context: dict = {}


def _build_report_system_prompt(ctx: dict) -> str:
    targets   = ctx.get("target_papers", [])
    stats     = ctx.get("stats", {})
    scholars  = ctx.get("scholars", [])
    keywords  = ctx.get("keywords", [])
    top_p     = ctx.get("top_papers", [])
    insights  = ctx.get("insights", [])
    ctypes    = ctx.get("citation_types", [])
    cpos      = ctx.get("citation_positions", [])
    findings  = ctx.get("key_findings", [])
    year_dist = ctx.get("year_dist", {})

    def fmt_list(items, fn, limit=20):
        return "\n".join(f"  - {fn(x)}" for x in items[:limit]) or "  （无数据）"

    parts = [
        "你是 CitationClaw v2 智能分析助手，专门针对以下这份论文被引画像报告回答问题。",
        "请基于报告数据作答，语言简洁专业，必要时引用具体数字。",
        "若问题超出报告数据范围，请如实说明。",
        "",
        "## 目标论文",
        fmt_list(targets, lambda t: t),
        "",
        "## 核心统计",
        f"  - 引用论文总数：{stats.get('total', 'N/A')}",
        f"  - 知名学者数量：{stats.get('scholars', 'N/A')}",
        f"  - 院士/Fellow 数量：{stats.get('fellows', 'N/A')}",
        f"  - 覆盖国家/地区：{stats.get('countries', 'N/A')}",
        f"  - 最高单篇被引量：{stats.get('max_cit', 'N/A')}",
        "",
        "## 知名学者（前30位）",
        fmt_list(scholars, lambda s: f"{s.get('name','')} | {s.get('level','')} | {s.get('country','')}", 30),
        "",
        "## 研究关键词",
        "  " + "、".join(k.get("keyword", "") for k in keywords[:25]),
        "",
        "## 高影响力施引论文（Top 20）",
        fmt_list(top_p, lambda p:
            f"{p.get('title','')} ({p.get('year','')}, 被引{p.get('citations','')}次, {p.get('country','')})", 20),
        "",
        "## 年份分布",
        "  " + "  ".join(f"{y}:{n}" for y, n in sorted(year_dist.items())),
        "",
        "## 引用类型分布",
        fmt_list(ctypes, lambda c: f"{c.get('type','')} {c.get('count','')}篇"),
        "",
        "## 引用位置分布",
        fmt_list(cpos, lambda p: f"{p.get('position','')} {p.get('count','')}篇"),
        "",
        "## AI 关键发现",
        fmt_list(findings, lambda f: f),
        "",
        "## 数据洞察",
        fmt_list(insights, lambda i: f"{i.get('title','')}: {i.get('body','')}"),
    ]
    return "\n".join(parts)


_UI_SYSTEM_PROMPT = """你是 CitationClaw v2 使用助手，帮助用户操作 CitationClaw v2 学术引用分析工具。

## CitationClaw v2 核心功能
- 输入论文题目（或 Google Scholar 主页 URL）→ 自动爬取所有施引文献
- 识别院士/Fellow 等知名学者，生成可视化 HTML 画像报告
- 支持多篇论文批量分析、断点续爬、年份遍历模式（突破1000篇限制）

## 关键配置
- **ScraperAPI Key**：用于爬取 Google Scholar（免费账户有1000积分试用）
- **LLM API Key + Base URL**：推荐 V-API，Search Model 必须支持实时 web search
- **分析层级**：基础版（仅统计）/ 进阶版（院士才查引用原句）/ 全面版（所有施引文献查引用原句）

## 常见问题
- 请求失败/积分不足 → 检查 ScraperAPI Key 余额，建议配置3个以上轮换
- LLM 编造学者信息 → Search Model 必须具备实时 web search 能力
- 引用超过1000篇 → 开启年份遍历模式
- 任务中断 → 设置 resume_page_count 为中断页码重新启动

## 配置指引
如果用户询问如何配置 API、如何快速开始或遇到配置相关问题，请主动引导用户查阅官方配置指引文档：
https://visionxlab.github.io/CitationClaw/guidelines.html
该文档包含完整的安装步骤、API 申请与填写说明、各参数含义及截图示例，是解决配置问题的最佳参考。

请简洁、准确地回答用户关于使用 CitationClaw v2 的问题，不要涉及报告数据内容。"""


class ChatUIRequest(BaseModel):
    messages: list


@app.post("/api/chat/ui")
async def chat_ui(request: ChatUIRequest):
    del request
    return StreamingResponse(iter(["页面上的助手已关闭。"]), media_type="text/plain; charset=utf-8")


@app.post("/api/chat/report")
async def chat_report(request: ChatReportRequest):
    del request
    return StreamingResponse(iter(["报告里的助手已关闭。"]), media_type="text/plain; charset=utf-8")


class CancelTaskRequest(BaseModel):
    external_job_id: str = ""


@app.post("/api/task/cancel")
async def cancel_task(request: CancelTaskRequest | None = None):
    requested_id = str(request.external_job_id if request else "").strip()
    active_id = str(_connect_task_state.get("external_job_id") or "").strip()
    if requested_id and active_id and requested_id != active_id:
        return JSONResponse(
            status_code=409,
            content={"status": "error", "error_code": "JOB_ID_MISMATCH", "message": "对不上这次查询，请刷新后再停。"},
        )
    task_executor.cancel()
    return {
        "schema_version": "connect.job.v1",
        "status": "success",
        "job_id": active_id,
        "message": "正在停掉这次查询。",
    }


@app.post("/api/task/year-traverse-respond")
async def year_traverse_respond(request: YearTraverseResponse):
    if task_executor._year_traverse_event is None:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "当前无等待确认的年份遍历提示"}
        )
    task_executor._year_traverse_choice = request.enable
    task_executor._year_traverse_event.set()
    return {"status": "success", "enable": request.enable}


class APITestRequest(BaseModel):
    api_key: str
    base_url: str
    model: str
    test_query: str = "请告诉我现在的准确日期和时间（年月日时分秒）。"


@app.post("/api/test_openai")
async def test_openai_api(request: APITestRequest, http_request: Request):
    denied = _reject_public_model_test(http_request)
    if denied is not None:
        return denied
    try:
        client = _make_openai_client(request.api_key, request.base_url, timeout=60.0)

        try:
            response_no_web = client.chat.completions.create(
                model=request.model,
                messages=[{"role": "user", "content": request.test_query}],
                temperature=0.1
            )
            result_no_web = response_no_web.choices[0].message.content
        except Exception as e:
            print(f"[citationclaw] model test failed: {e}", flush=True)
            result_no_web = "错误: 这次没有连上。"

        try:
            response_with_web = client.chat.completions.create(
                model=request.model,
                messages=[{"role": "user", "content": request.test_query}],
                temperature=0.1,
                extra_body=web_search_extra(request.base_url)
            )
            result_with_web = response_with_web.choices[0].message.content
        except Exception as e:
            print(f"[citationclaw] model test failed: {e}", flush=True)
            result_with_web = "错误: 这次没有连上。"

        has_web_search = "错误" not in result_with_web and result_with_web != result_no_web

        return {
            "status": "success",
            "has_web_search": has_web_search,
            "test_results": {
                "without_web_search": result_no_web,
                "with_web_search": result_with_web
            },
            "message": "API连接成功" if has_web_search else "API可用但可能不支持web search"
        }

    except Exception as e:
        print(f"[citationclaw] model test failed: {e}", flush=True)
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": "这次没有连上模型。",
                "has_web_search": False
            }
        )


# ── Lightweight pre-test endpoints ───────────────────────────────────────
class PretestRequest(BaseModel):
    api_key: str
    base_url: str
    model: str


@app.post("/api/pretest/search_llm")
async def pretest_search_llm(req: PretestRequest, request: Request):
    """Quick test: verify Search LLM with web_search_options works."""
    denied = _reject_public_model_test(request)
    if denied is not None:
        return denied
    from datetime import datetime
    try:
        client = _make_openai_client(req.api_key, req.base_url, timeout=30.0)
        resp = client.chat.completions.create(
            model=req.model,
            messages=[{"role": "user", "content": "请告诉我现在的准确日期和时间。"}],
            temperature=0.0,
            extra_body=web_search_extra(req.base_url),
        )
        answer = resp.choices[0].message.content or ""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {"status": "success", "message": f"Search LLM 可用 ✓（{now}）", "reply": answer}
    except Exception as e:
        print(f"[citationclaw] model test failed: {e}", flush=True)
        return JSONResponse(status_code=400, content={"status": "error", "message": "这次没有连上模型。"})


@app.post("/api/pretest/light_model")
async def pretest_light_model(req: PretestRequest, request: Request):
    """Quick test: verify lightweight model works."""
    denied = _reject_public_model_test(request)
    if denied is not None:
        return denied
    from datetime import datetime
    try:
        client = _make_openai_client(req.api_key, req.base_url, timeout=30.0)
        resp = client.chat.completions.create(
            model=req.model,
            messages=[{"role": "user", "content": "请回复OK两个字母。"}],
            temperature=0.0,
        )
        answer = resp.choices[0].message.content or ""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {"status": "success", "message": f"轻量模型可用 ✓（{now}）", "reply": answer}
    except Exception as e:
        print(f"[citationclaw] model test failed: {e}", flush=True)
        return JSONResponse(status_code=400, content={"status": "error", "message": "这次没有连上模型。"})



@app.get("/api/task/status")
async def get_task_status():
    return {
        **task_executor.get_status(),
        **_connect_task_state,
        "progress": dict(log_manager.current_progress),
        "logs": log_manager.get_recent_logs(80),
    }


# ── Results endpoints with absolute path ──────────────────────────────────

_FOLDER_LABELS: dict[str, tuple[float, int, str]] = {}


def _result_folder_label(folder: Path) -> str:
    """用报告标题代替 result-时间戳。同一份文件不重复打开。"""
    html_files = sorted(path for path in folder.glob("*.html") if path.is_file())
    html = html_files[0] if html_files else None
    if html is None:
        return folder.name
    try:
        stat = html.stat()
    except OSError:
        return folder.name
    key = str(html)
    cached = _FOLDER_LABELS.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    title = folder.name
    try:
        head = html.read_text(encoding="utf-8", errors="ignore")[:4000]
    except OSError:
        head = ""
    match = re.search(r"<title>(.*?)</title>", head, re.I | re.S)
    if match:
        cleaned = re.sub(r"\s+", " ", match.group(1)).strip()
        cleaned = re.sub(r"\s*·\s*被引画像报告\s*$", "", cleaned).strip()
        if cleaned:
            title = cleaned[:80]
    if len(_FOLDER_LABELS) > 200:
        _FOLDER_LABELS.pop(next(iter(_FOLDER_LABELS)))
    _FOLDER_LABELS[key] = (stat.st_mtime, stat.st_size, title)
    return title


@app.get("/api/results/folders")
async def list_result_folders():
    folders = []
    if DATA_DIR.exists():
        for sub in DATA_DIR.iterdir():
            if not (sub.is_dir() and sub.name.startswith("result-")):
                continue
            files = [f for f in sub.iterdir() if f.is_file()]
            folders.append({
                "name": sub.name,
                "display_name": _result_folder_label(sub),
                "file_count": len(files),
                "modified": max((f.stat().st_mtime for f in files), default=sub.stat().st_mtime),
                "size": sum(f.stat().st_size for f in files),
            })

    # 旧版扁平目录
    legacy_files = []
    for dir_path in [DATA_DIR / "excel", DATA_DIR / "json", DATA_DIR / "jsonl"]:
        if dir_path.exists():
            legacy_files.extend([f for f in dir_path.iterdir() if f.is_file()])
    if legacy_files:
        folders.append({
            "name": "__legacy__",
            "display_name": "旧版结果文件",
            "file_count": len(legacy_files),
            "modified": max(f.stat().st_mtime for f in legacy_files),
            "size": sum(f.stat().st_size for f in legacy_files),
        })

    folders.sort(key=lambda x: x["modified"], reverse=True)
    return folders


@app.get("/api/results/list")
async def list_results(folder: str = None):
    results = []

    def data_relative_path(file: Path) -> str:
        return file.resolve().relative_to(DATA_DIR.resolve()).as_posix()

    def add_file(file: Path):
        results.append({
            "name": file.name,
            "size": file.stat().st_size,
            "type": file.suffix,
            "path": data_relative_path(file),
            "modified": file.stat().st_mtime
        })

    if folder == "__legacy__" or (folder is None):
        for dir_path in [DATA_DIR / "excel", DATA_DIR / "json", DATA_DIR / "jsonl"]:
            if dir_path.exists():
                for file in dir_path.iterdir():
                    if file.is_file():
                        add_file(file)

    if folder != "__legacy__":
        if DATA_DIR.exists():
            for sub in DATA_DIR.iterdir():
                if sub.is_dir() and sub.name.startswith("result-"):
                    if folder is None or sub.name == folder:
                        for file in sub.iterdir():
                            if file.is_file():
                                add_file(file)

    results.sort(key=lambda x: x["modified"], reverse=True)
    return results


def _safe_data_path(filepath: str) -> Path:
    """Normalize path and verify it's inside DATA_DIR (prevent traversal)."""
    norm = filepath.replace("\\", "/")
    p = Path(norm)
    candidate = p if p.is_absolute() else DATA_DIR / p
    resolved = candidate.resolve()
    try:
        resolved.relative_to(DATA_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="不允许访问该路径")
    return resolved


_REPORT_ASSET_URLS = (
    ("https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js", "/static/vendor/chart.umd.min.js"),
    (
        "https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js",
        "/static/vendor/chartjs-plugin-datalabels.min.js",
    ),
    ("https://cdn.jsdelivr.net/npm/marked@9/marked.min.js", "/static/vendor/marked.min.js"),
    ("https://cdn.jsdelivr.net/npm/marked@9.1.6/marked.min.js", "/static/vendor/marked.min.js"),
)


def _html_for_public_view(raw: str) -> str:
    """Keep report downloads under the public prefix, and hide the in-report assistant.

    Root-relative ``/api/results/`` links miss ``/citations`` behind the gateway.
    Chart and markdown scripts used to come from public CDNs; serve the copies in this app.
    The floating assistant also calls the model, which this server does not expose on the report page.
    """
    base = _public_base_path()
    raw = re.sub(r'\s*<link rel="preconnect" href="https://fonts\.googleapis\.com">', "", raw)
    raw = re.sub(r'\s*<link href="https://fonts\.googleapis\.com/css2[^"]*" rel="stylesheet">', "", raw)
    for remote, local in _REPORT_ASSET_URLS:
        raw = raw.replace(remote, f"{base}{local}" if base else local)
    if not base:
        return raw
    raw = raw.replace('src="/static/vendor/', f'src="{base}/static/vendor/')
    raw = raw.replace('href="/api/results/', f'href="{base}/api/results/')
    raw = raw.replace("href='/api/results/", f"href='{base}/api/results/")
    hide = "<style>#cc-fab,#cc-win{display:none!important}</style>"
    if "</head>" in raw:
        return raw.replace("</head>", hide + "</head>", 1)
    return hide + raw


@app.get("/api/results/view/{filepath:path}")
async def view_result_html(filepath: str):
    p = _safe_data_path(filepath)
    if p.exists() and p.is_file():
        text = p.read_text(encoding="utf-8", errors="replace")
        return HTMLResponse(_html_for_public_view(text))
    raise HTTPException(status_code=404, detail="文件不存在")


@app.get("/api/results/download/{filepath:path}")
async def download_result(filepath: str):
    p = _safe_data_path(filepath)
    if p.exists() and p.is_file():
        return FileResponse(path=p, filename=p.name, media_type="application/octet-stream")
    # 向下兼容：仅传文件名时在旧目录中查找
    fname = Path(filepath).name
    for dir_path in [DATA_DIR / "excel", DATA_DIR / "json", DATA_DIR / "jsonl"]:
        legacy = dir_path / fname
        if legacy.exists() and legacy.is_file():
            return FileResponse(path=legacy, filename=fname, media_type="application/octet-stream")
    raise HTTPException(status_code=404, detail="文件不存在")


@app.delete("/api/results/folder/{folder_name}")
async def delete_result_folder(folder_name: str):
    if not folder_name.startswith("result-"):
        raise HTTPException(status_code=400, detail="只允许删除 result- 开头的文件夹")
    # Reject any path traversal characters
    if "/" in folder_name or "\\" in folder_name or ".." in folder_name:
        raise HTTPException(status_code=400, detail="文件夹名称包含非法字符")
    folder_path = DATA_DIR / folder_name
    try:
        folder_path.resolve().relative_to(DATA_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="不允许访问该路径")
    if not folder_path.exists() or not folder_path.is_dir():
        raise HTTPException(status_code=404, detail="文件夹不存在")
    shutil.rmtree(folder_path)
    return {"success": True}


# ==================== WebSocket ====================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    log_manager.add_websocket(websocket)

    try:
        await websocket.send_json({
            "type": "history",
            "data": log_manager.get_recent_logs()
        })

        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break  # connection lost

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket错误: {e}")
    finally:
        log_manager.remove_websocket(websocket)
