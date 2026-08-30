#!/usr/bin/env python3
"""本地服务后端：静态托管前端，工作流触发映射成本地子进程，AI 问答代理，定时调度。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from research_connect_core.llm import RetryPolicy, create_openai_client

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

# 以脚本方式运行（python src/local_server.py）时，Python 只把脚本所在目录 src/
# 加入 sys.path，仓库根目录不在其中，导致 `from src.local_scheduler import ...` 失败。
# 这里把仓库根目录与 src/ 一起插入 sys.path，保证两种运行方式都能导入 src.* 与 local_env。
ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = Path(__file__).resolve().parent
for _p in (ROOT_DIR, SRC_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.local_scheduler import SchedulerThread

try:
    from local_env import load_local_env
except Exception:  # pragma: no cover - 兼容 package 导入路径
    from src.local_env import load_local_env

# 显式加载 .env，保证以任何方式启动（python src/local_server.py / python -m / .bat）
# 本进程都能拿到 DEEPSEEK_API_KEY 等密钥；否则 sitecustomize 在脚本模式下未必触发。
load_local_env()
RUNS_DIR = ROOT_DIR / ".local-runs"
CONFIG_PATH = ROOT_DIR / "config.yaml"
SECRET_PATH = ROOT_DIR / "secret.private"
ENV_PATH = ROOT_DIR / ".env"


def ensure_runtime_docs_shell() -> None:
    """Create ignored runtime entry files from tracked clean templates."""
    docs_dir = ROOT_DIR / "docs"
    template_dir = ROOT_DIR / "docs_init"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for name in ("README.md", "_sidebar.md"):
        target = docs_dir / name
        template = template_dir / name
        if not target.exists() and template.is_file():
            target.write_bytes(template.read_bytes())


ensure_runtime_docs_shell()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_text(value: Any) -> str:
    return str(value or "").strip()


def build_secret_env(secret: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(secret, dict):
        return {}
    summarized = secret.get("summarizedLLM") if isinstance(secret.get("summarizedLLM"), dict) else {}
    chat_llms = secret.get("chatLLMs") if isinstance(secret.get("chatLLMs"), list) else []
    first_chat = chat_llms[0] if chat_llms and isinstance(chat_llms[0], dict) else {}

    api_key = norm_text(summarized.get("apiKey") or first_chat.get("apiKey"))
    base_url = norm_text(summarized.get("baseUrl") or first_chat.get("baseUrl"))
    model = norm_text(summarized.get("model"))
    if not model and isinstance(first_chat.get("models"), list) and first_chat.get("models"):
        model = norm_text(first_chat.get("models")[0])

    env: dict[str, str] = {}
    if summarized or first_chat:
        env["SUMMARY_API_KEY"] = api_key
        env["DEEPSEEK_API_KEY"] = api_key
        env["SUMMARY_BASE_URL"] = base_url
        env["DEEPSEEK_BASE_URL"] = base_url
        env["LLM_PRIMARY_BASE_URL"] = base_url
        env["SUMMARY_MODEL"] = model
        env["DEEPSEEK_MODEL"] = model

    reranker = secret.get("rerankerLLM") if isinstance(secret.get("rerankerLLM"), dict) else {}
    rerank_profile = norm_text(reranker.get("profile"))
    rerank_provider = norm_text(reranker.get("provider") or reranker.get("type"))
    rerank_model = norm_text(reranker.get("model"))
    rerank_key = norm_text(reranker.get("apiKey"))
    rerank_base = norm_text(reranker.get("baseUrl"))
    if reranker:
        env["RERANK_PROFILE"] = rerank_profile
        env["RERANK_PROVIDER"] = rerank_provider
        env["RERANK_MODEL"] = rerank_model
        env["RERANK_API_KEY"] = rerank_key
        env["RERANK_API_BASE_URL"] = rerank_base
        if rerank_provider == "public_zwwen":
            env["PUBLIC_RERANK_API_KEY"] = rerank_key
            env["PUBLIC_RERANK_API_BASE_URL"] = rerank_base
        if rerank_provider == "siliconflow":
            env["SILICONFLOW_API_KEY"] = rerank_key
            env["SILICONFLOW_RERANK_URL"] = rerank_base
    # Connect Hub uses an explicit allowlist of process-environment keys so a
    # CLI/bot invocation can inject credentials per run without modifying this
    # separately maintained project's .env file. Never pass arbitrary keys.
    passthrough_keys = {
        "DPR_EMBED_API_URL",
        "DPR_EMBED_API_KEY",
        "DPR_EMBED_ALLOW_LOCAL_FALLBACK",
        "DPR_EMBED_API_TIMEOUT",
        "SUMMARY_API_KEY",
        "DEEPSEEK_API_KEY",
        "SUMMARY_BASE_URL",
        "DEEPSEEK_BASE_URL",
        "LLM_PRIMARY_BASE_URL",
        "SUMMARY_MODEL",
        "DEEPSEEK_MODEL",
        "RERANK_PROFILE",
        "RERANK_PROVIDER",
        "RERANK_MODEL",
        "RERANK_API_KEY",
        "RERANK_API_BASE_URL",
        "PUBLIC_RERANK_API_KEY",
        "PUBLIC_RERANK_API_BASE_URL",
        "SILICONFLOW_API_KEY",
        "SILICONFLOW_RERANK_URL",
        "DPR_PUBLIC_SERVICE_API_KEY",
    }
    for key in passthrough_keys:
        value = norm_text(secret.get(key))
        if value:
            env[key] = value
    return env


def quote_env_value(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if any(ch.isspace() or ch in {'"', "'", "#", "\\"} for ch in text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def update_env_file(path: Path, values: dict[str, str]) -> None:
    clean_values = {str(k): str(v).strip() for k, v in values.items() if str(k).strip()}
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated_keys: set[str] = set()
    next_lines: list[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            next_lines.append(line)
            continue
        prefix = "export " if stripped.startswith("export ") else ""
        body = stripped[len("export ") :] if prefix else stripped
        key = body.split("=", 1)[0].strip()
        if key in clean_values:
            next_lines.append(f"{prefix}{key}={quote_env_value(clean_values[key])}")
            updated_keys.add(key)
        else:
            next_lines.append(line)
    for key in clean_values:
        if key not in updated_keys:
            next_lines.append(f"{key}={quote_env_value(clean_values[key])}")
    path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 日报流水线步骤进度事件（connect.job.v1 风格）
# ---------------------------------------------------------------------------
# main.py 通过 run_step() 打印 `[INFO] Step X - 标签: 命令` 作为步骤锚点；
# 这里把子进程 stdout 里的锚点解析成 run.progress 事件，供前端渲染步骤清单。
PIPELINE_STEPS: list[tuple[str, str, str]] = [
    # (步骤号, step key, 中文名)
    ("0", "step_0_enrich", "LLM 扩充检索关键词"),
    ("1", "step_1_fetch", "抓取 arXiv 论文"),
    ("2.1", "step_2_1_bm25", "BM25 关键词召回"),
    ("2.2", "step_2_2_embedding", "向量语义召回"),
    ("2.3", "step_2_3_rrf", "RRF 融合候选池"),
    ("3", "step_3_rerank", "Reranker 重排"),
    ("4", "step_4_llm_refine", "LLM 精炼打分"),
    ("5", "step_5_select", "选择论文（精读/速读）"),
    ("6", "step_6_generate", "生成日报文档"),
]
_STEP_INDEX: dict[str, tuple[str, str]] = {num: (key, label) for num, key, label in PIPELINE_STEPS}
_STEP_START_RE = re.compile(r"^\[INFO\] Step (\d+(?:\.\d+)?) - ([^:]+):")
_STEP_SKIP_RE = re.compile(r"^\[INFO\] 跳过 Step (\d+)")
_STEP_STATE_VERB = {"started": "开始", "completed": "完成", "skipped": "已跳过", "failed": "失败"}


def _run_event(event_type: str, run_id: str, *, stage: str = "", message: str = "",
               current: int | None = None, total: int | None = None,
               payload: dict[str, Any] | None = None) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "schema_version": "connect.job.v1",
        "event_id": _new_event_id(),
        "run_id": run_id,
        "event_type": event_type,
    }
    if stage:
        ev["stage"] = stage
    if message:
        ev["message"] = message
    if current is not None:
        ev["current"] = current
    if total is not None:
        ev["total"] = total
    if payload:
        ev["payload"] = payload
    return ev


class _StepTracker:
    """解析 main.py 输出的 `[INFO] Step X - ...` 锚点，转成 run.progress 事件序列。

    - 新步骤开始：先补发上一步骤 completed，再发当前步骤 started
    - `[INFO] 跳过 Step 1 ...`：发对应步骤 skipped
    - finalize(success)：进程结束时收尾当前步骤（completed / failed）
    非 main.py 命令（会议/维护类 workflow）没有锚点，只产生 started/completed 事件。
    """

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._current: tuple[str, str] | None = None  # (step_key, 中文名)

    def observe(self, line: str) -> list[dict[str, Any]]:
        text = line.rstrip("\n")
        events: list[dict[str, Any]] = []
        m = _STEP_START_RE.match(text)
        if m:
            num = m.group(1)
            key, label = _STEP_INDEX.get(num, (f"step_{num.replace('.', '_')}", m.group(2).strip()))
            if self._current and self._current[0] != key:
                events.append(self._progress(self._current[0], self._current[1], "completed"))
            self._current = (key, label)
            events.append(self._progress(key, label, "started"))
            return events
        m2 = _STEP_SKIP_RE.match(text)
        if m2:
            key_label = _STEP_INDEX.get(m2.group(1))
            if key_label:
                self._current = None
                events.append(self._progress(key_label[0], key_label[1], "skipped"))
        return events

    def finalize(self, success: bool) -> list[dict[str, Any]]:
        if not self._current:
            return []
        key, label = self._current
        self._current = None
        return [self._progress(key, label, "completed" if success else "failed")]

    def _progress(self, key: str, label: str, state: str) -> dict[str, Any]:
        return _run_event(
            "run.progress",
            self._run_id,
            stage=key,
            message=f"{label} {_STEP_STATE_VERB.get(state, state)}",
            payload={"step": key, "step_label": label, "state": state},
        )


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass


class RunStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}

    def create(
        self,
        workflow_key: str,
        workflow_file: str,
        inputs: dict[str, str],
        command: list[str],
        config: dict[str, Any] | None = None,
        secret: dict[str, Any] | None = None,
        external_job_id: str = "",
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex[:12]
        run_dir = RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        config_path = ""
        if config:
            if yaml is None:
                raise RuntimeError("本地调试后端缺少 PyYAML，无法写入浏览器缓存配置。")
            config_path = str(run_dir / "config.yaml")
            Path(config_path).write_text(
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False, width=10**9),
                encoding="utf-8",
            )
        run = {
            "id": run_id,
            "run_number": len(self._runs) + 1,
            "workflow_key": workflow_key,
            "workflow_file": workflow_file,
            "inputs": inputs,
            "command": command,
            "status": "queued",
            "conclusion": None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "started_at": None,
            "completed_at": None,
            "log_path": str(run_dir / "run.log"),
            "config_path": config_path,
            "secret_env": build_secret_env(secret),
            "events": [_run_event("run.accepted", run_id, message="已接收本地运行请求")],
            "external_job_id": str(external_job_id or "").strip(),
            "cancel_requested": False,
        }
        with self._lock:
            self._runs[run_id] = run
        thread = threading.Thread(target=self._run_process, args=(run_id,), daemon=True)
        thread.start()
        return self._public_run(run)

    def _public_run(self, run: dict[str, Any]) -> dict[str, Any]:
        public = dict(run)
        public.pop("secret_env", None)
        return public

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((self._public_run(item) for item in self._runs.values()), key=lambda r: r["created_at"], reverse=True)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            return self._public_run(run) if run else None

    def _get_private(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            return dict(run) if run else None

    def log(self, run_id: str) -> str:
        run = self.get(run_id)
        if not run:
            return ""
        path = Path(str(run.get("log_path") or ""))
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[-20000:]

    def _update(self, run_id: str, **patch: Any) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return
            run.update(patch)
            run["updated_at"] = utc_now()

    def _emit(self, run_id: str, event: dict[str, Any]) -> None:
        if not event:
            return
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return
            run["events"].append(event)
            run["updated_at"] = utc_now()

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            proc = self._processes.get(run_id)
            if not run or run.get("status") not in {"queued", "in_progress"}:
                return False
            run["cancel_requested"] = True
            run["updated_at"] = utc_now()
        if proc is not None and proc.poll() is None:
            _terminate_process_tree(proc)
        self._emit(run_id, _run_event("run.cancelled", run_id, message="任务已取消，子进程树已停止"))
        self._update(
            run_id,
            status="cancelled",
            conclusion="cancelled",
            completed_at=utc_now(),
        )
        return True

    def _run_process(self, run_id: str) -> None:
        run = self._get_private(run_id)
        if not run:
            return
        log_path = Path(str(run["log_path"]))
        self._update(run_id, status="in_progress", started_at=utc_now())
        self._emit(run_id, _run_event("run.started", run_id, message="流水线开始执行"))
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("MKL_THREADING_LAYER", "GNU")
        config_path = str(run.get("config_path") or "")
        if config_path:
            env["DPR_CONFIG_FILE"] = config_path
        secret_env = run.get("secret_env") if isinstance(run.get("secret_env"), dict) else {}
        for key, value in secret_env.items():
            text = norm_text(value)
            if text:
                env[str(key)] = text
        configured_rerank = _configured_rerank_profile()
        if configured_rerank:
            # 面板选了具体后端：显式注入覆盖 .env（sitecustomize 用 setdefault，
            # 已存在的 env 优先）；清空 provider/model/base_url 覆盖键，防止旧值
            # 把 profile 自带端点劫持走（曾发生：残留 sinksilk base_url → 401）。
            env["RERANK_PROFILE"] = configured_rerank
            env["RERANK_PROVIDER"] = ""
            env["RERANK_MODEL"] = ""
            env["RERANK_API_BASE_URL"] = ""
            env["PUBLIC_RERANK_API_BASE_URL"] = ""
            env["SILICONFLOW_RERANK_URL"] = ""
        tracker = _StepTracker(run_id)
        try:
            with log_path.open("w", encoding="utf-8") as log:
                log.write(f"[local-debug] started_at={utc_now()}\n")
                log.write(f"[local-debug] cwd={ROOT_DIR}\n")
                if config_path:
                    log.write(f"[local-debug] config={config_path}\n")
                if secret_env:
                    log.write("[local-debug] secret_env=SUMMARY/DEEPSEEK/RERANK variables injected\n")
                log.write(f"[local-debug] command={' '.join(run['command'])}\n\n")
                log.flush()
                # Popen 逐行流式读 stdout：实时落盘日志 + 解析步骤锚点发进度事件。
                popen_options: dict[str, Any] = {}
                if os.name == "nt":
                    popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_options["start_new_session"] = True
                proc = subprocess.Popen(
                    run["command"],
                    cwd=str(ROOT_DIR),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **popen_options,
                )
                with self._lock:
                    self._processes[run_id] = proc
                assert proc.stdout is not None
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    for event in tracker.observe(line):
                        self._emit(run_id, event)
                returncode = proc.wait()
                for event in tracker.finalize(returncode == 0):
                    self._emit(run_id, event)
                latest = self.get(run_id) or {}
                was_cancelled = bool(latest.get("cancel_requested"))
                conclusion = "cancelled" if was_cancelled else ("success" if returncode == 0 else "failure")
                log.write(f"\n[local-debug] completed_at={utc_now()} returncode={returncode}\n")
                self._emit(
                    run_id,
                    _run_event(
                        "run.cancelled" if was_cancelled else ("run.completed" if conclusion == "success" else "run.failed"),
                        run_id,
                        message="流水线执行完成" if conclusion == "success" else f"流水线执行失败（returncode={returncode}）",
                    ),
                )
            self._update(
                run_id,
                status="cancelled" if conclusion == "cancelled" else "completed",
                conclusion=conclusion,
                completed_at=utc_now(),
                returncode=returncode,
            )
        except Exception as exc:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n[local-debug] exception={exc!r}\n")
            self._emit(run_id, _run_event("run.failed", run_id, message=f"执行异常：{exc}"))
            self._update(run_id, status="completed", conclusion="failure", completed_at=utc_now(), error=repr(exc))
        finally:
            with self._lock:
                self._processes.pop(run_id, None)


RUN_STORE = RunStore()


# --------------------------------------------------------------------------- #
# 论文总结异步 Job（connect.job.v1 契约）
#
# 把原先同步的 /api/paper/summarize handler 改成异步 job：
#   POST /api/paper/summarize        建job，立即返回 job_id（status=queued）
#   GET  /api/paper/summarize/<id>   轮询 status + events（按生成顺序返回）
#   POST /api/paper/summarize/<id>/cancel  请求取消（best-effort）
#
# 事件类型遵循 connect.job.v1：job.accepted/started/progress/artifact/completed/failed。
# 进度事件带 stage/current/total，前端据此渲染分阶段进度。
# --------------------------------------------------------------------------- #
def _new_event_id() -> str:
    return "evt-" + uuid.uuid4().hex[:16]


def _job_event(event_type: str, job_id: str, *, stage: str = "", message: str = "",
               current: int | None = None, total: int | None = None,
               payload: dict[str, Any] | None = None) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "schema_version": "connect.job.v1",
        "event_id": _new_event_id(),
        "job_id": job_id,
        "event_type": event_type,
    }
    if stage:
        ev["stage"] = stage
    if message:
        ev["message"] = message
    if current is not None:
        ev["current"] = current
    if total is not None:
        ev["total"] = total
    if payload:
        ev["payload"] = payload
    return ev


class SummarizeJobStore:
    """论文总结异步 job 存储 + 后台执行。线程安全。

    job 状态机：queued -> running -> completed | failed | cancelled
    事件列表 events 按 append 顺序增长，轮询时整体返回，前端按 event_id 去重。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = "sum-" + uuid.uuid4().hex[:12]
        now = utc_now()
        job: dict[str, Any] = {
            "schema_version": "connect.job.v1",
            "job_id": job_id,
            "status": "queued",
            "input": payload,
            "events": [_job_event("job.accepted", job_id, message="已接收总结请求")],
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "cancel_requested": False,
        }
        with self._lock:
            self._jobs[job_id] = job
        thread = threading.Thread(target=self._worker, args=(job_id, payload), daemon=True)
        thread.start()
        return self.public(job_id)  # type: ignore[arg-type]

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._copy_public(job) if job else None

    def request_cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job["status"] in ("completed", "failed", "cancelled"):
                return False
            job["cancel_requested"] = True
            job["updated_at"] = utc_now()
            return True

    def _emit(self, job_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["events"].append(event)
            job["updated_at"] = utc_now()

    def _set_status(self, job_id: str, status: str, **extra: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["status"] = status
            job["updated_at"] = utc_now()
            for k, v in extra.items():
                job[k] = v

    def _cancel_check(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.get("cancel_requested"))

    def public(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._copy_public(self._jobs.get(job_id))

    def _copy_public(self, job: dict[str, Any] | None) -> dict[str, Any]:
        if not job:
            return None  # type: ignore[return-value]
        out = {k: v for k, v in job.items() if k != "input"}
        # input 可能含 base64 PDF，不返回给轮询；events 整体返回
        return out

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                (self._copy_public(j) for j in self._jobs.values()),
                key=lambda r: str(r.get("created_at") or ""),
                reverse=True,
            )

    # ------------------------------------------------------------------ #
    # 后台 worker：执行原同步 summarize 逻辑，分阶段发事件
    # ------------------------------------------------------------------ #
    def _worker(self, job_id: str, payload: dict[str, Any]) -> None:
        self._emit(job_id, _job_event("job.started", job_id, message="开始处理"))
        self._set_status(job_id, "running")
        try:
            result = _run_summarize_job(job_id, payload, self)
            if self._cancel_check(job_id):
                self._set_status(job_id, "cancelled")
                self._emit(job_id, _job_event("job.cancelled", job_id, message="已取消"))
            else:
                self._set_status(job_id, "completed", result=result)
                self._emit(job_id, _job_event("job.completed", job_id, message="总结完成"))
        except _CancelRequested as exc:
            self._set_status(job_id, "cancelled", error=str(exc))
            self._emit(job_id, _job_event("job.cancelled", job_id, message=str(exc)))
        except Exception as exc:
            self._set_status(job_id, "failed", error=str(exc))
            self._emit(job_id, _job_event("job.failed", job_id, stage="error",
                                          message=str(exc), payload={"error_type": type(exc).__name__}))


SUMMARIZE_JOB_STORE = SummarizeJobStore()


class _CancelRequested(Exception):
    """worker 内部检测到取消请求时抛出，跳出整个流程。"""
    pass


def _run_summarize_job(job_id: str, payload: dict[str, Any], store: SummarizeJobStore) -> dict[str, Any]:
    """执行原 _handle_summarize 的核心逻辑，分阶段发进度事件。失败抛异常。"""
    def emit(stage: str, message: str, *, current: int | None = None, total: int | None = None, payload: dict[str, Any] | None = None) -> None:
        if store._cancel_check(job_id):
            raise _CancelRequested("用户取消")
        store._emit(job_id, _job_event("job.progress", job_id, stage=stage, message=message,
                                       current=current, total=total, payload=payload))

    source = str(payload.get("source") or "").strip()
    if source not in ("url", "pdf"):
        raise ValueError("source 必须是 url 或 pdf")

    arxiv_id: str | None = None
    pdf_bytes_opt: bytes | None = None
    paper_meta: dict[str, Any] = {}

    # ---- 阶段 1：抓取论文文本 ----
    if source == "url":
        url = (payload.get("url") or "").strip()
        if not url:
            raise ValueError("缺少 url")
        arxiv_id = _extract_arxiv_id(url)
        if arxiv_id:
            emit("fetch_arxiv", f"正在从 arXiv 获取元数据（{arxiv_id}）")
            meta = _fetch_arxiv_metadata(arxiv_id)
            title, text = meta["title"], "标题：" + meta["title"] + "\n\n摘要：" + meta["abstract"]
            kind = "arxiv"
            paper_meta = meta
            emit("fetch_pdf", "正在下载 arXiv PDF（用于抽图）", current=1, total=2)
            try:
                pdf_bytes_opt = _download_pdf_bytes_public(f"https://arxiv.org/pdf/{arxiv_id}")
                emit("fetch_pdf", "PDF 下载完成", current=2, total=2)
            except Exception as exc:
                pdf_bytes_opt = None
                emit("fetch_pdf", f"PDF 下载失败（不影响总结）：{exc}", current=2, total=2)
        else:
            emit("fetch_web", f"正在抓取网页内容")
            title, text = _fetch_web_text(url)
            kind = "url"
            pdf_bytes_opt = None
            paper_meta = {"title": title, "link": url}
            emit("extract_meta", "正在用 LLM 抽取作者/来源元数据")
            _merge_extracted_paper_meta(paper_meta, _extract_paper_meta_with_llm(text))
            title = str(paper_meta.get("title") or title)
    else:
        data_b64 = payload.get("data_b64") or ""
        if not data_b64:
            raise ValueError("缺少 PDF 数据")
        filename = str(payload.get("filename") or "").strip() or "paper.pdf"
        emit("parse_pdf", "正在解析 PDF 全文")
        text = _extract_pdf_text(data_b64)
        kind = "pdf"
        paper_meta = {"title": filename}
        try:
            import base64 as _b64
            pdf_bytes_opt = _b64.b64decode(data_b64)
        except Exception:
            pdf_bytes_opt = None
        # 文件名带 arXiv id 时优先反查官方元数据（零 LLM 成本，字段最全）
        filename_id = _arxiv_id_from_filename(filename)
        if filename_id:
            emit("fetch_arxiv", f"文件名含 arXiv id（{filename_id}），反查官方元数据")
            try:
                meta = _fetch_arxiv_metadata(filename_id)
                arxiv_id = filename_id
                title = meta["title"]
                paper_meta = meta
                kind = "arxiv"
            except Exception as exc:  # noqa: BLE001
                emit("fetch_arxiv", f"arXiv 反查失败，改用 LLM 抽取元数据：{exc}")
        if not arxiv_id:
            emit("extract_meta", "正在用 LLM 抽取标题/作者/来源")
            _merge_extracted_paper_meta(paper_meta, _extract_paper_meta_with_llm(text))
            title = str(paper_meta.get("title") or filename)

    if not text or len(text.strip()) < 20:
        raise ValueError("未能抽取到足够论文文本，请换用 arXiv 链接或上传 PDF")

    # 缓存命中检查
    cache_key: str | None = None
    if arxiv_id:
        cache_key = "arxiv:" + arxiv_id
    elif pdf_bytes_opt:
        cache_key = "sha256:" + hashlib.sha256(pdf_bytes_opt).hexdigest()
    if cache_key:
        cached_payload = SUMMARIZE_CACHE.get(cache_key)
        if cached_payload is not None:
            emit("cache_hit", "命中缓存，直接返回上次结果")
            return cached_payload

    # ---- 阶段 2：直接用日报流水线生成纸张页（速览 + 抽图 + 图表解读 + 翻译 + 精读总结） ----
    # 与每日日报同一套纯文本 LLM 链路（generate_external_paper_docs），不再走多模态 VLM。
    # 前端拿到 paper_id 后直接跳转该纸张页（日报展示层渲染），无需额外 JSON 总结。
    emit("daily_pipeline", "正在用日报流水线生成总结（速览 + 图表 + 精读）")
    persist = _persist_summarize_as_daily_paper(
        paper_meta=paper_meta,
        title=title,
        text=text,
        kind=kind,
        source=source,
        arxiv_id=arxiv_id,
        pdf_bytes=pdf_bytes_opt,
        on_progress=lambda stage, message: emit(stage, message),
    )
    if not persist.get("paper_id"):
        raise ValueError(f"日报流水线生成纸张页失败：{persist.get('error') or '未知错误'}")
    emit("persist", "纸张页已落盘并注册侧边栏", payload={"paper_id": persist["paper_id"]})

    preview = text if len(text) <= 400 else text[:400] + "…"
    resp_payload = {
        "ok": True,
        "meta": {
            "title": title,
            "kind": kind,
            "source": source,
            "vision": False,
            "cached": False,
            "paper_id": persist["paper_id"],
            "md_path": persist.get("md_path", ""),
            "registered": bool(persist.get("registered")),
        },
        "summary": {},
        "figures": [],
        "preview": preview,
    }
    if cache_key and resp_payload.get("ok"):
        SUMMARIZE_CACHE.put(cache_key, resp_payload)
    return resp_payload


# --------------------------------------------------------------------------- #
# 综述生成异步 Job（connect.job.v1 契约，结构与论文总结 Job 对齐）
#
#   POST /api/survey              建job，立即返回 job_id（status=queued）
#   GET  /api/survey              列出历史 job（前端历史列表）
#   GET  /api/survey/<id>         轮询 status + events（按生成顺序返回）
#   POST /api/survey/<id>/cancel  请求取消（best-effort，协作式）
#   GET  /api/survey/<id>/log     运行日志尾部
#
# 综述编排（召回→精排→抽取→聚类→深读→分析→大纲→写作→审校）全部在
# survey_pipeline 内完成；本层只负责 Job 生命周期与事件契约。
# --------------------------------------------------------------------------- #

SURVEY_WALL_CLOCK_BUDGET_SECONDS = 40 * 60
SURVEY_MAX_FETCH_DAYS = 1095  # 回溯上限 3 年
SURVEY_MAX_FINISHED_JOBS = 50  # 终态 job 内存驻留上限（FIFO 淘汰最旧的）


class SurveyJobStore:
    """综述异步 job 存储 + 后台执行。线程安全。

    job 状态机：queued -> running -> completed | failed | cancelled
    events 按 append 顺序增长，轮询时整体返回，前端按 event_id 去重。
    与 SummarizeJobStore 的差异：input 很小（query+参数），public 视图保留 input
    供历史列表展示；运行日志额外写 .local-runs/<job_id>/survey.log。

    内存管理：终态 job 记录（状态+事件流+结果摘要，KB 级/条）按 FIFO 淘汰，
    只保留最近 SURVEY_MAX_FINISHED_JOBS 条；运行中的 job 永不淘汰。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def _prune_finished_locked(self) -> None:
        """淘汰最旧的终态 job（调用方必须已持锁）。"""
        finished = [
            (job_id, job)
            for job_id, job in self._jobs.items()
            if job.get("status") in ("completed", "failed", "cancelled")
        ]
        overflow = len(finished) - SURVEY_MAX_FINISHED_JOBS
        if overflow <= 0:
            return
        finished.sort(key=lambda item: str(item[1].get("updated_at") or ""))
        for stale_id, _ in finished[:overflow]:
            del self._jobs[stale_id]

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = "sv-" + uuid.uuid4().hex[:12]
        now = utc_now()
        job: dict[str, Any] = {
            "schema_version": "connect.job.v1",
            "job_id": job_id,
            "status": "queued",
            "input": payload,
            "events": [_job_event("job.accepted", job_id, message="已接收综述请求")],
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "cancel_requested": False,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._prune_finished_locked()
        thread = threading.Thread(target=self._worker, args=(job_id, payload), daemon=True)
        thread.start()
        return self.public(job_id)  # type: ignore[arg-type]

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._copy_public(self._jobs.get(job_id))

    def request_cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.get("status") in {"completed", "failed", "cancelled"}:
                return False
            job["cancel_requested"] = True
            job["updated_at"] = utc_now()
            return True

    def _emit(self, job_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["events"].append(event)
            job["updated_at"] = utc_now()

    def _set_status(self, job_id: str, status: str, **extra: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["status"] = status
            job["updated_at"] = utc_now()
            for key, value in extra.items():
                job[key] = value
            # 进入终态即触发 FIFO 淘汰，保证任何时刻驻留的终态记录不超过上限
            if status in ("completed", "failed", "cancelled"):
                self._prune_finished_locked()

    def _cancel_check(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.get("cancel_requested"))

    def public(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._copy_public(self._jobs.get(job_id))

    def _copy_public(self, job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        public = {key: value for key, value in job.items() if key != "cancel_requested"}
        # result 里可能带整份报告正文，历史列表只需要摘要信息
        result = public.get("result")
        if isinstance(result, dict):
            public["result"] = {
                "ok": result.get("ok"),
                "report": result.get("report"),
                "meta": result.get("meta"),
            }
        return public

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [self._copy_public(job) for job in self._jobs.values()]
        jobs = [job for job in jobs if job]
        jobs.sort(key=lambda job: str(job.get("created_at") or ""), reverse=True)
        return jobs  # type: ignore[return-value]

    def _worker(self, job_id: str, payload: dict[str, Any]) -> None:
        self._emit(job_id, _job_event("job.started", job_id, message="综述流水线启动"))
        self._set_status(job_id, "running")
        try:
            result = _run_survey_job(job_id, payload, self)
            self._set_status(job_id, "completed", result=result)
            self._emit(job_id, _job_event("job.completed", job_id, message="综述完成",
                                          payload={"report": result.get("report")}))
        except _CancelRequested as exc:
            self._set_status(job_id, "cancelled")
            self._emit(job_id, _job_event("job.cancelled", job_id, message=str(exc)))
        except Exception as exc:  # noqa: BLE001
            self._set_status(job_id, "failed", error=str(exc))
            self._emit(job_id, _job_event("job.failed", job_id, stage="error",
                                          message=str(exc), payload={"error_type": type(exc).__name__}))


SURVEY_JOB_STORE = SurveyJobStore()


def _survey_job_log_path(job_id: str) -> Path:
    return RUNS_DIR / job_id / "survey.log"


def _survey_job_log(job_id: str, tail: int = 20000) -> str:
    path = _survey_job_log_path(job_id)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-tail:]


def _run_survey_job(job_id: str, payload: dict[str, Any], store: SurveyJobStore) -> dict[str, Any]:
    """执行综述流水线并落盘报告，分阶段发进度事件。失败抛异常。"""
    log_lines: list[str] = [f"[survey] job_id={job_id}"]

    def _flush_log() -> None:
        try:
            path = _survey_job_log_path(job_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(log_lines), encoding="utf-8")
        except Exception:  # noqa: BLE001 - 日志失败不影响主流程
            pass

    def emit(stage: str, message: str, *, current: int | None = None, total: int | None = None,
             payload: dict[str, Any] | None = None) -> None:
        if store._cancel_check(job_id):
            raise _CancelRequested("用户取消")
        log_lines.append(f"[{stage}] {message}")
        _flush_log()
        store._emit(job_id, _job_event("job.progress", job_id, stage=stage, message=message,
                                       current=current, total=total, payload=payload))

    query = str(payload.get("query") or "").strip()
    if not query:
        raise ValueError("缺少综述主题 query")

    def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
        try:
            num = int(value)
        except (TypeError, ValueError):
            num = default
        return min(hi, max(lo, num))

    max_papers = _clamp(payload.get("max_papers"), 5, 200, 30)
    # 回溯以年为单位是综述常态：上限 3 年（1095 天），前端按天/月/年换算成天数传入
    fetch_days = _clamp(payload.get("fetch_days"), 1, SURVEY_MAX_FETCH_DAYS, 365)
    use_rerank = as_bool(payload.get("use_rerank"), True)
    deep_read = as_bool(payload.get("deep_read"), True)
    # 默认 Kaggle 快照粗筛为主路（零网络零限流）；DeepXiv 语义检索默认关——
    # 外部服务有 token 限额/波动，需要被引数与最新论文时前端勾选开启
    use_deepxiv = as_bool(payload.get("use_deepxiv"), False)
    use_kaggle = as_bool(payload.get("use_kaggle"), True)
    # Kaggle 词法粗筛量级：500-30000（默认 1 万，前端三档 3千/1万/3万）
    coarse_top_k = _clamp(payload.get("coarse_top_k"), 500, 30000, 10000)

    # 种子论文（可选）：arXiv 链接/id 或已抽取全文的 PDF。
    # 用于锚定任务范式 + 直取其参考文献（滚雪球补齐本任务原生文献）。
    seed_payload = payload.get("seed") if isinstance(payload.get("seed"), dict) else None
    seed_paper: dict[str, Any] | None = None
    if seed_payload:
        seed_source = str(seed_payload.get("source") or "").strip()
        if seed_source == "pdf":
            data_b64 = str(seed_payload.get("data_b64") or "")
            if not data_b64:
                raise ValueError("种子 PDF 缺少 data_b64 数据")
            emit("seed", "解析种子 PDF 全文")
            seed_text = _extract_pdf_text(data_b64)
            if not seed_text or len(seed_text.strip()) < 50:
                raise ValueError("种子 PDF 未能抽取到足够文本，请换用 arXiv 链接")
            seed_paper = {
                "text": seed_text,
                "title": str(seed_payload.get("filename") or "").strip().removesuffix(".pdf") or "（PDF 种子）",
            }
        else:
            seed_url = str(seed_payload.get("url") or "").strip()
            if not seed_url:
                raise ValueError("种子论文缺少 url（arXiv 链接）")
            seed_id = _extract_arxiv_id(seed_url)
            if not seed_id:
                raise ValueError("种子论文链接无法解析出 arXiv id，请提供 arXiv 链接或改用 PDF 上传")
            import re as _re

            version_match = _re.match(r"^(\d{4}\.\d{4,5})", seed_id)
            if version_match:
                seed_id = version_match.group(1)  # 去版本号，与综述全链归一化约定一致
            seed_paper = {"arxiv_id": seed_id}

    started = time.time()

    def _gate() -> None:
        """协作式取消 + 墙钟预算闸门（综述含 PDF 深读，整体耗时可达十几分钟）。"""
        if store._cancel_check(job_id):
            raise _CancelRequested("用户取消")
        if time.time() - started > SURVEY_WALL_CLOCK_BUDGET_SECONDS:
            raise TimeoutError("综述流水线超过 40 分钟墙钟预算，已终止")

    # 延迟导入：综述模块连带 sklearn / torch / 抽图链，服务启动不背这些依赖。
    # 脚本模式（python src/local_server.py）与包模式（src.local_server）双路径兼容。
    try:
        from survey_pipeline import run_survey
    except ImportError:  # pragma: no cover - 包模式导入路径
        from src.survey_pipeline import run_survey

    try:
        result = run_survey(
            query,
            max_papers=max_papers,
            fetch_days=fetch_days,
            use_rerank=use_rerank,
            deep_read=deep_read,
            seed_paper=seed_paper,
            use_deepxiv=use_deepxiv,
            use_kaggle=use_kaggle,
            coarse_top_k=coarse_top_k,
            on_progress=lambda stage, message, current=None, total=None: emit(
                stage, message, current=current, total=total
            ),
            cancel_check=_gate,
        )
        emit("render", "报告落盘并注册侧栏")
        try:
            from survey_docs import persist_survey_report
        except ImportError:  # pragma: no cover - 包模式导入路径
            from src.survey_docs import persist_survey_report

        info = persist_survey_report(result)
        emit("render", "报告已生成", payload={"paper_id": info["paper_id"], "route": info["route"]})
        log_lines.append(f"[survey] completed route={info['route']}")
        _flush_log()
        return {
            "ok": True,
            "report": {
                "paper_id": info["paper_id"],
                "route": info["route"],
                "title": info["title_zh"],
                "md_path": info["md_path"],
                "registered": bool(info.get("registered")),
                "n_papers": (result.get("report_meta") or {}).get("n_papers"),
                "cluster_names": [c.get("name_zh") for c in result.get("clusters") or []],
            },
            "meta": result.get("report_meta"),
            "warnings": result.get("warnings") or [],
        }
    except Exception:
        log_lines.append(f"[survey] failed")
        _flush_log()
        raise


def as_bool(value: Any, default: bool = False) -> bool:
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def build_command(workflow_key: str, workflow_file: str, inputs: dict[str, str]) -> list[str]:
    python = sys.executable
    if workflow_file == "daily-paper-reader.yml" or workflow_key == "daily-now":
        cmd = [python, "src/main.py"]
        if as_bool(inputs.get("run_enrich"), False):
            cmd.append("--run-enrich")
        if inputs.get("fetch_days"):
            cmd.extend(["--fetch-days", str(inputs["fetch_days"])])
        if inputs.get("fetch_mode"):
            cmd.extend(["--fetch-mode", str(inputs["fetch_mode"])])
        if inputs.get("profile_tag"):
            cmd.extend(["--profile-tag", str(inputs["profile_tag"])])
        cmd.extend(["--embedding-device", "cpu", "--embedding-batch-size", "8"])
        return cmd

    if workflow_file == "conference-paper-retrieval.yml" or workflow_key == "conference-retrieval":
        run_date = datetime.now(timezone.utc).strftime("%Y%m%d")
        conference = str(inputs.get("conference") or "ICML")
        years = str(inputs.get("years") or "2025")
        conference_pairs = str(inputs.get("conference_pairs") or "")
        profile_tag = str(inputs.get("profile_tag") or "")
        run_rerank = as_bool(inputs.get("run_rerank"), True)
        run_llm_refine = as_bool(inputs.get("run_llm_refine"), True)
        params = json.dumps(
            {
                "conference": conference,
                "years": years,
                "conference_pairs": conference_pairs,
                "profile_tag": profile_tag,
                "run_date": run_date,
                "top_k": str(inputs.get("top_k") or "50"),
                "rrf_top_n": str(inputs.get("rrf_top_n") or "200"),
                "run_rerank": run_rerank,
                "run_llm_refine": run_llm_refine,
                "llm_min_star": str(inputs.get("llm_min_star") or "4"),
            },
            ensure_ascii=False,
        )
        # 跨平台编排器：算 token → 查 sidebar 是否已有词条 → 跑 conference_pipeline → 写 sidebar。
        # 不再依赖 bash（set -euo pipefail / heredoc / sed），Windows 与 Linux 均可运行。
        orchestrator = (
            "import json, os, subprocess, sys\n"
            "sys.path.insert(0, 'src')\n"
            "from conference_retrieval import build_years_token, parse_conference_pairs, parse_conferences, parse_years\n"
            "from conference_sidebar import build_conference_topic_marker, topic_from_profile_tag\n"
            "p = json.loads(sys.argv[1])\n"
            "conference, years, conference_pairs = p['conference'], p['years'], p['conference_pairs']\n"
            "profile_tag, run_date = p['profile_tag'], p['run_date']\n"
            "pairs = parse_conference_pairs(conference_pairs)\n"
            "confs, years_list = [], []\n"
            "for c, y in pairs:\n"
            "    if c not in confs: confs.append(c)\n"
            "    if y not in years_list: years_list.append(y)\n"
            "if not confs: confs = parse_conferences(conference)\n"
            "if not years_list: years_list = parse_years(years)\n"
            "conf_token = '-'.join(confs)\n"
            "year_token = build_years_token(years_list)\n"
            "kind, label = topic_from_profile_tag(profile_tag)\n"
            "topic_marker = build_conference_topic_marker(conf_token, year_token, kind, label)\n"
            "env = dict(os.environ)\n"
            "env['DPR_FILTER_PROFILE_TAG'] = profile_tag\n"
            "sidebar_path = 'docs/_sidebar.md'\n"
            "if os.path.isfile(sidebar_path):\n"
            "    with open(sidebar_path, encoding='utf-8') as fh:\n"
            "        if topic_marker in fh.read():\n"
            "            print('[INFO] 已存在会议词条，跳过重复检索：conference=' + conf_token + '-' + year_token + ' profile=' + (profile_tag or 'General'))\n"
            "            sys.exit(0)\n"
            "pipeline_cmd = [sys.executable, 'src/conference_pipeline.py', '--conferences', conference, '--years', years, '--top-k', str(p['top_k']), '--rrf-top-n', str(p['rrf_top_n']), '--output-dir', 'archive/' + run_date + '/filtered', '--embedding-device', 'cpu', '--embedding-batch-size', '8']\n"
            "if conference_pairs:\n"
            "    pipeline_cmd.extend(['--conference-pairs', conference_pairs])\n"
            "if p['run_rerank'] or p['run_llm_refine']:\n"
            "    pipeline_cmd.extend(['--run-rerank', '--rerank-device', 'cpu', '--rerank-batch-size', '4'])\n"
            "if p['run_llm_refine']:\n"
            "    pipeline_cmd.extend(['--run-llm-refine', '--llm-min-star', str(p['llm_min_star']), '--llm-filter-concurrency', '2'])\n"
            "subprocess.run(pipeline_cmd, check=True, env=env)\n"
            "base = 'conference-' + conf_token + '-' + year_token + '.supabase'\n"
            "sidebar_cmd = [sys.executable, 'src/conference_sidebar.py', '--result', 'archive/' + run_date + '/rank/' + base + '.llm.json', '--result', 'archive/' + run_date + '/rank/' + base + '.rerank.json', '--result', 'archive/' + run_date + '/filtered/' + base + '.rrf.json', '--sidebar', 'docs/_sidebar.md']\n"
            "subprocess.run(sidebar_cmd, check=True, env=env)\n"
        )
        return [python, "-c", orchestrator, params]

    if workflow_file == "reset-content.yml" or workflow_key == "reset-content":
        return [python, "-c", "import shutil, pathlib; root=pathlib.Path('.'); shutil.rmtree(root/'docs', ignore_errors=True); shutil.copytree(root/'docs_init', root/'docs'); print('docs reset from docs_init')"]

    if workflow_file == "sync.yml" or workflow_key == "sync":
        return ["git", "status", "--short"]

    raise ValueError(f"本地调试后端暂不支持 workflow: {workflow_key or workflow_file}")


def build_chat_request_payload(model: str, messages: list[dict], *, max_tokens: int | None = None) -> dict:
    payload: dict = {"model": model, "messages": messages, "stream": True}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    return payload


def _build_chat_endpoint(base_url: str) -> str:
    """Mirrors frontend buildChatCompletionsEndpoint: append /v1 if base_url has no /vN suffix."""
    base = base_url.rstrip("/")
    if not re.search(r"/v\d+$", base):
        base += "/v1"
    return base + "/chat/completions"


def _load_local_chat_config() -> dict:
    import yaml as _yaml
    cfg = _yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    chat = (cfg.get("local") or {}).get("chat") or {}
    return {
        "model": str(chat.get("model") or "").strip(),
        "base_url": str(chat.get("base_url") or "").strip().rstrip("/"),
        "api_key": str(chat.get("api_key") or "").strip(),
    }


def _load_subscriptions_section() -> dict | None:
    """返回 config.yaml 顶层的 subscriptions 段（含 intent_profiles 及其 embedding_cache），供前端编辑标签用。"""
    import yaml as _yaml
    cfg = _yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    cfg = cfg if isinstance(cfg, dict) else {}
    subs = cfg.get("subscriptions")
    return subs if isinstance(subs, dict) else None


def _normalize_recommend_setting(value) -> dict:
    """清洗 recommend_setting：只保留白名单字段，空值不写入（保持模式默认）。"""
    out: dict = {}
    if not isinstance(value, dict):
        return out
    for key in ("deep_dive_base", "quick_skim_base"):
        if key in value:
            raw = str(value.get(key) or "").strip()
            try:
                num = int(float(raw)) if raw else None
            except Exception:
                continue
            if num is not None and num > 0:
                out[key] = num
    if "deep_dive_unlimited" in value:
        out["deep_dive_unlimited"] = bool(value.get("deep_dive_unlimited"))
    return out


def _load_local_chat_full() -> dict:
    """返回 local 段的可编辑字段（chat + schedule）与顶层 subscriptions，供结构化读接口使用。"""
    import yaml as _yaml
    cfg = _yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    cfg = cfg if isinstance(cfg, dict) else {}
    loc = cfg.get("local") or {}
    chat = loc.get("chat") or {}
    sched = loc.get("schedule") or {}
    recommend = _normalize_recommend_setting(cfg.get("recommend_setting"))
    return {
        "chat": {
            "model": str(chat.get("model") or "").strip(),
            "base_url": str(chat.get("base_url") or "").strip().rstrip("/"),
            "api_key": str(chat.get("api_key") or "").strip(),
        },
        "rerank": {
            "profile": str((loc.get("rerank") or {}).get("profile") or "").strip(),
        },
        "recall": {
            "mode": str((loc.get("recall") or {}).get("mode") or "").strip().lower(),
        },
        "schedule": {
            "enabled": bool(sched.get("enabled", False)),
            "time": str(sched.get("time") or "").strip(),
        },
        "recommend_setting": {
            "deep_dive_base": str(recommend.get("deep_dive_base") or ""),
            "quick_skim_base": str(recommend.get("quick_skim_base") or ""),
            "deep_dive_unlimited": bool(recommend.get("deep_dive_unlimited", False)),
        },
        "subscriptions": _load_subscriptions_section(),
    }


def merge_local_section(existing: dict | None, incoming: dict | None) -> dict:
    """只把 incoming['local'] 深合并到 existing 的 local 段，保留其它段落与未传入字段。"""
    merged = dict(existing or {})
    loc = dict((existing or {}).get("local") or {})
    inc_loc = dict((incoming or {}).get("local") or {})
    for section in ("chat", "schedule", "rerank", "recall"):
        if section in inc_loc and isinstance(inc_loc[section], dict):
            base = dict(loc.get(section) or {})
            base.update({k: v for k, v in inc_loc[section].items() if v is not None})
            loc[section] = base
    merged["local"] = loc
    return merged


def merge_top_level_section(existing: dict | None, key: str, value: dict | None) -> dict:
    """整体替换 existing 的顶层 key 段，保留其它顶层键与未传入字段。"""
    merged = dict(existing or {})
    if isinstance(value, dict):
        merged[key] = value
    return merged


def _resolve_chat_api_key(cfg: dict) -> str:
    key = cfg.get("api_key") or ""
    if not key:
        key = os.environ.get("DEEPSEEK_API_KEY") or ""
    return key


# 设置面板可选的 reranker 预设（与 3.rank_papers.py RERANK_PROFILE_CONFIGS 对齐）；
# auto/未知值 → ''，表示不干预（跟随 .env；.env 也没配时 3.rank 内建默认即远程公益端点）。
RERANK_PROFILE_CHOICES = {
    "auto": "",
    "public-zwwen-rerank": "public-zwwen-rerank",
    "public-sinksilk-rerank": "public-sinksilk-rerank",
    "siliconflow-qwen3-0.6b": "siliconflow-qwen3-0.6b",
    "local-qwen3-0.6b": "local-qwen3-0.6b",
}


def _configured_rerank_profile() -> str:
    """设置面板选择的 reranker 后端；'' = 自动（跟随 .env）。"""
    import yaml as _yaml

    cfg = _yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    cfg = cfg if isinstance(cfg, dict) else {}
    raw = str(((cfg.get("local") or {}).get("rerank") or {}).get("profile") or "").strip().lower()
    return RERANK_PROFILE_CHOICES.get(raw, "")


def _env_file_rerank_profile() -> str:
    """读 .env 文件里的 RERANK_PROFILE（服务启动后文件可能被改过，不能信启动时的 env）。"""
    env_path = ROOT_DIR / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text.startswith("RERANK_PROFILE="):
                return text.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def _apply_rerank_profile_to_env() -> None:
    """把面板选择同步进当前进程 env（综述流水线在进程内跑，读 os.getenv）。

    auto 时回退 .env 文件的 RERANK_PROFILE；两者都没有则清空，交给
    3.rank_papers.py 的内建默认（远程公益端点）。选择非 auto 时清掉
    RERANK_PROVIDER/RERANK_MODEL，防止旧值覆盖 profile 推断
    （3.rank_papers.py 的 provider 解析里非空 RERANK_PROVIDER 优先于 profile）。
    """
    profile = _configured_rerank_profile() or _env_file_rerank_profile()
    if profile:
        os.environ["RERANK_PROFILE"] = profile
        # 这些键的优先级高于 profile 自带端点，不清掉会把远端 rerank 劫持回旧地址
        # （2026-08-29 实测：残留的 sinksilk base_url + zwwen profile → 401）。
        os.environ.pop("RERANK_PROVIDER", None)
        os.environ.pop("RERANK_MODEL", None)
        os.environ.pop("RERANK_API_BASE_URL", None)
        os.environ.pop("PUBLIC_RERANK_API_BASE_URL", None)
        os.environ.pop("SILICONFLOW_RERANK_URL", None)
    else:
        os.environ.pop("RERANK_PROFILE", None)
        os.environ.pop("RERANK_PROVIDER", None)
        os.environ.pop("RERANK_MODEL", None)


def _build_openai_models_url(base_url: str) -> str:
    """与 llm.py `_build_chat_completions_url` 同一套归一化：补 /v1、容忍粘错后缀。"""
    raw = str(base_url or "").strip().rstrip("/")
    if not raw:
        raise ValueError("缺少 API 端点，请先填写 OpenAI 兼容端点")
    if raw.lower().endswith("/chat/completions"):
        raw = raw[: -len("/chat/completions")].rstrip("/")
    if re.search(r"/v\d+$", raw, re.IGNORECASE):
        return f"{raw}/models"
    return f"{raw}/v1/models"


def _resolve_chat_credentials(base_url: str, api_key: str) -> tuple[str, str]:
    """模型列表/连通性测试共用：请求体优先，留空回退 config.yaml local.chat，再回退 .env。"""
    cfg = _load_local_chat_config()
    from llm import resolve_llm_api_key, resolve_llm_base_url

    url = str(base_url or "").strip() or cfg["base_url"] or resolve_llm_base_url()
    key = str(api_key or "").strip() or _resolve_chat_api_key(cfg) or resolve_llm_api_key()
    if not url:
        raise ValueError("缺少 API 端点：请填写端点或先在 config.yaml / .env 配置 base_url")
    if not key:
        raise ValueError("未配置 API Key：请在输入框填写，或先在 .env 配置 DEEPSEEK_API_KEY")
    return url, key


def _parse_model_ids(payload: bytes) -> list[str]:
    """解析 OpenAI /v1/models 响应；兼容 ollama 原生 {"models": [{"name": ...}]}。"""
    data = json.loads(payload.decode("utf-8", errors="replace"))
    models: list[str] = []
    raw_items = data.get("data") if isinstance(data, dict) else None
    if raw_items is None and isinstance(data, dict):
        raw_items = [
            {"id": str(m.get("name") or "")} for m in (data.get("models") or []) if isinstance(m, dict)
        ]
    for item in raw_items or []:
        mid = str(item.get("id") or "").strip() if isinstance(item, dict) else str(item).strip()
        if mid and mid not in models:
            models.append(mid)
    return sorted(models)


def _fetch_chat_model_list(base_url: str, api_key: str) -> list[str]:
    url = _build_openai_models_url(base_url)
    body = _http_get_with_retry(
        url,
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "daily-paper-reader/1.0"},
        timeout=20,
    )
    models = _parse_model_ids(body)
    if not models:
        raise ValueError("端点返回了空模型列表（响应不含 data[].id）")
    return models


def _probe_chat_completion(base_url: str, api_key: str, model: str) -> tuple[int, str]:
    """发一次最小 chat completion 测连通性。单次尝试不做重试，延迟才真实；异常向上抛。"""
    raw = str(base_url or "").strip().rstrip("/")
    if raw.lower().endswith("/chat/completions"):
        raw = raw[: -len("/chat/completions")].rstrip("/")
    if not re.search(r"/v\d+$", raw, re.IGNORECASE):
        raw += "/v1"
    client = create_openai_client(
        api_key=api_key,
        base_url=raw,
        timeout=30,
        max_concurrency=4,
        retry_policy=RetryPolicy(max_attempts=1),
        provider_name="daily-paper-probe",
    )
    t0 = time.time()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=8,
        temperature=0,
        stream=False,
    )
    latency_ms = int((time.time() - t0) * 1000)
    try:
        snippet = str(response.choices[0].message.content or "").strip()[:40]
    except (AttributeError, IndexError, TypeError):
        snippet = ""
    return latency_ms, snippet


def _get_schedule() -> dict:
    import yaml as _yaml
    cfg = _yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    return (cfg.get("local") or {}).get("schedule") or {}


def _schedule_enabled() -> bool:
    return as_bool(_get_schedule().get("enabled"), False)


def _schedule_time() -> str:
    return str(_get_schedule().get("time") or "").strip() or "18:30"


def _dispatch_daily_pipeline() -> None:
    python = sys.executable
    inputs = {"run_enrich": False}
    cmd = build_command("daily-now", "daily-paper-reader.yml", inputs)
    print(f"[local-server] 触发定时流水线: {' '.join(cmd)}", flush=True)
    RUN_STORE.create("daily-now", "daily-paper-reader.yml", inputs, cmd, config=None, secret=None)


# ---------------------------------------------------------------------------
# 论文总结（/api/paper/summarize）：复用日报流水线生成纸张页
# ---------------------------------------------------------------------------
def _http_get_with_retry(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    max_retries: int = 3,
    read_limit: int | None = None,
) -> bytes:
    """带重试的 HTTP GET。捕获 WinError 10054/10053 等连接重置异常，指数退避重试。

    Args:
        url: 完整 URL。
        headers: 请求头。
        timeout: 单次请求超时秒数。
        max_retries: 最大重试次数（含首次）。
        read_limit: 最多读取的字节数，None 表示不限。

    Returns:
        响应体 bytes。最终仍失败则抛最后一个异常。
    """
    import urllib.request
    import urllib.error

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if read_limit is not None:
                    return resp.read(read_limit + 1)
                return resp.read()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
            last_exc = exc
        except urllib.error.URLError as exc:
            last_exc = exc
            # 对被包裹的连接重置也重试；对 4xx 不重试
            reason = getattr(exc, "reason", "")
            if isinstance(reason, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
                pass
            elif isinstance(reason, OSError) and reason.errno in (10054, 10053, 10060, 54):
                pass
            else:
                raise
        except TimeoutError as exc:
            last_exc = exc
        if attempt < max_retries:
            time.sleep(min(2 ** (attempt - 1), 8))
    raise last_exc  # type: ignore[misc]


def _extract_arxiv_id(url: str) -> str | None:
    """从 arXiv 链接里提取论文 ID（含 abs / pdf / 裸 ID 形式）。"""
    text = url.strip()
    # 形如 arxiv.org/abs/2401.12345、arxiv.org/pdf/2401.12345、arxiv:2401.12345
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^/?#]+)", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?:^|\s)arXiv:\s*([^\s]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # 裸 ID，如 2401.12345 或 2401.12345v2
    if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", text):
        return text
    return None


def _fetch_arxiv_metadata(arxiv_id: str) -> dict[str, str]:
    """通过 arXiv Atom API 拉取标题与摘要。失败抛异常。"""
    import urllib.parse
    import xml.etree.ElementTree as ET

    url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode({"id_list": arxiv_id})
    data = _http_get_with_retry(
        url,
        headers={"User-Agent": "daily-paper-reader/1.0"},
        timeout=20,
    ).decode("utf-8", errors="replace")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(data)
    entry = root.find("a:entry", ns)
    if entry is None:
        raise ValueError(f"arXiv 未找到论文 {arxiv_id}")
    title = " ".join((entry.findtext("a:title", "", ns) or "").split())
    summary = " ".join((entry.findtext("a:summary", "", ns) or "").split())
    authors: list[str] = []
    for a in entry.findall("a:author", ns):
        name = " ".join((a.findtext("a:name", "", ns) or "").split())
        if name:
            authors.append(name)
    published = " ".join((entry.findtext("a:published", "", ns) or "").split())
    return {
        "title": title,
        "abstract": summary,
        "authors": authors,
        "published": published,
        "link": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
    }


def _arxiv_id_from_filename(filename: str) -> str | None:
    """从上传文件名里识别 arXiv id（2601.01234v1.pdf → 2601.01234v1），无则 None。"""
    stem = re.sub(r"\.pdf\s*$", "", str(filename or "").strip(), flags=re.IGNORECASE)
    match = re.search(r"\b(\d{4}\.\d{4,5}(?:v\d+)?)\b", stem)
    return match.group(1) if match else None


_SUMMARIZE_META_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "论文原标题（原文语言），无法判断则空串"},
        "authors": {"type": "array", "items": {"type": "string"}, "description": "作者列表（按署名顺序）"},
        "venue": {"type": "string", "description": "会议/期刊名（含年份更好），未标注则空串"},
        "source_kind": {
            "type": "string",
            "enum": ["arxiv", "biorxiv", "medrxiv", "chemrxiv", "conference", "web", "other"],
            "description": "论文来源类型，依据 arXiv 水印/期刊页眉/封面文字判断，无法判断用 other",
        },
    },
    "required": ["title", "authors", "venue", "source_kind"],
}


_SMART_QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tag": {"type": "string", "description": "订阅标签：简短英文/ASCII 标识，如 RAG、AHD-EA；用户已给标签则原样沿用"},
        "description": {"type": "string", "description": "该研究方向的中文一句话描述"},
        "keywords": {
            "type": "array",
            "description": "BM25 词法召回候选关键词：具体的技术名词/方法名/任务名（英文短语，不是完整句子）",
            "items": {
                "type": "object",
                "properties": {
                    "en": {"type": "string", "description": "英文关键词"},
                    "zh": {"type": "string", "description": "中文翻译"},
                },
                "required": ["en", "zh"],
            },
        },
        "queries": {
            "type": "array",
            "description": "向量语义召回候选意图查询：完整英文句子（如 Find recent papers on ...）",
            "items": {
                "type": "object",
                "properties": {
                    "en": {"type": "string", "description": "英文意图句"},
                    "zh": {"type": "string", "description": "中文翻译"},
                },
                "required": ["en", "zh"],
            },
        },
    },
    "required": ["tag", "description", "keywords", "queries"],
}


def _extract_paper_meta_with_llm(text: str) -> dict[str, Any]:
    """上传 PDF / 网页没有元数据来源，用 LLM 从正文开头抽 标题/作者/venue/来源。

    任何失败都返回 {} 并回退旧行为（标题=文件名、作者为空），不阻断总结主流程。
    """
    try:
        from llm import OpenAIClient, resolve_llm_api_key, resolve_llm_base_url, resolve_llm_model

        api_key = resolve_llm_api_key()
        if not api_key:
            return {}
        client = OpenAIClient(api_key=api_key, model=resolve_llm_model(), base_url=resolve_llm_base_url())
        resp = client.chat_structured(
            [
                {
                    "role": "system",
                    "content": (
                        "You extract academic paper metadata from the beginning of a paper's full text. "
                        "Return JSON with: title (in the original language), authors (ordered list, empty "
                        "if not discernible), venue (conference/journal name with year if stated, else "
                        "empty string), and source_kind (one of arxiv, biorxiv, medrxiv, chemrxiv, "
                        "conference, web, other — judge from arXiv watermark, journal header or cover "
                        "text; use 'other' when unclear)."
                    ),
                },
                {"role": "user", "content": f"论文开头内容：\n{text[:2500]}"},
            ],
            "summarize_paper_meta",
            _SUMMARIZE_META_SCHEMA,
        )
        parsed = resp.get("parsed") if isinstance(resp, dict) else None
        return parsed if isinstance(parsed, dict) else {}
    except Exception as exc:  # noqa: BLE001
        print(f"[SUMMARIZE][WARN] 元数据抽取失败（回退文件名标题）：{exc}", flush=True)
        return {}


def _merge_extracted_paper_meta(paper_meta: dict[str, Any], extracted: dict[str, Any]) -> None:
    """把 LLM 抽取结果就地并入 paper_meta；空值不覆盖已有字段。"""
    if not isinstance(extracted, dict):
        return
    title = str(extracted.get("title") or "").strip()
    if title:
        paper_meta["title"] = title
    authors = [str(a).strip() for a in (extracted.get("authors") or []) if str(a).strip()]
    if authors:
        paper_meta["authors"] = authors
    venue = str(extracted.get("venue") or "").strip()
    if venue:
        paper_meta["venue"] = venue
    source_kind = str(extracted.get("source_kind") or "").strip().lower()
    if source_kind:
        paper_meta["source_kind"] = source_kind


def _generate_subscription_candidates(intent: str, tag_hint: str = "") -> dict[str, Any]:
    """把用户的一句自然语言检索意图解析成订阅候选，供前端勾选后写入订阅词条。

    优先用 config.yaml local.chat 的问答模型配置，缺省回退 .env 的 LLM 解析。
    返回 {tag, description, keywords: [{en, zh}], queries: [{en, zh}]}；LLM 失败抛异常。
    """
    from llm import OpenAIClient, resolve_llm_api_key, resolve_llm_base_url, resolve_llm_model

    cfg = _load_local_chat_config()
    api_key = _resolve_chat_api_key(cfg) or resolve_llm_api_key()
    if not api_key:
        raise ValueError(
            "未配置 LLM API Key：请在「本地服务设置」里填 API Key，或在 .env 配置 DEEPSEEK_API_KEY"
        )
    model = cfg["model"] or resolve_llm_model()
    base_url = cfg["base_url"] or resolve_llm_base_url()
    client = OpenAIClient(api_key=api_key, model=model, base_url=base_url)
    system = (
        "You translate a researcher's information need into subscription candidates for a daily "
        "arXiv paper recommendation pipeline. Return JSON with: tag (short ASCII identifier for "
        "the research direction, e.g. 'RAG' or 'AHD-EA'; reuse the user's existing tag when given), "
        "description (one Chinese sentence summarizing the direction), keywords (8-12 lexical "
        "search phrases in English for BM25 keyword recall, each with a Chinese translation in "
        "zh; concrete technical terms, method names or task names, never full sentences), and "
        "queries (6-10 full English sentences describing the research intent for embedding "
        "semantic recall, each with a Chinese translation in zh, phrased like 'Find recent papers "
        "on ...')."
    )
    user = f"检索需求：{intent}"
    if tag_hint:
        user += f"\n已有订阅标签（沿用或参考）：{tag_hint}"
    resp = client.chat_structured(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "smart_query_candidates",
        _SMART_QUERY_SCHEMA,
    )
    parsed = resp.get("parsed") if isinstance(resp, dict) else None
    if not isinstance(parsed, dict):
        raise ValueError("LLM 未返回结构化候选")

    def _pairs(raw: Any, limit: int) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            en = str(item.get("en") or "").strip()
            if not en:
                continue
            out.append({"en": en, "zh": str(item.get("zh") or "").strip()})
            if len(out) >= limit:
                break
        return out

    tag = str(parsed.get("tag") or "").strip() or (tag_hint.strip() if tag_hint.strip() else "")
    return {
        "tag": tag,
        "description": str(parsed.get("description") or "").strip(),
        "keywords": _pairs(parsed.get("keywords"), 15),
        "queries": _pairs(parsed.get("queries"), 12),
    }


def _resolve_summarize_source(kind: str, paper_meta: dict[str, Any]) -> tuple[str, str]:
    """把总结输入 kind + 抽取到的元数据解析成 front matter 的 (source, venue)。

    source 只用稳定小写 token（同时作为图表资产目录名）；真实会议/期刊名放 venue，
    前端渲染成 venue chips。不再把非 arXiv 来源硬编码成 arxiv。
    """
    meta_kind = str(paper_meta.get("source_kind") or "").strip().lower()
    venue = str(paper_meta.get("venue") or "").strip()
    if kind in ("arxiv", "biorxiv"):
        return kind, venue
    if meta_kind in ("arxiv", "biorxiv", "medrxiv", "chemrxiv"):
        return meta_kind, venue
    if meta_kind == "conference":
        return "conference", venue
    return ("pdf" if kind == "pdf" else "web"), venue


def _fetch_web_text(url: str) -> tuple[str, str]:
    """抓取论文网页，尽力提取标题与正文纯文本（元组：标题, 正文）。失败抛异常。"""
    from html.parser import HTMLParser

    data = _http_get_with_retry(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; daily-paper-reader/1.0)"},
        timeout=25,
        read_limit=2 * 1024 * 1024,
    ).decode("utf-8", errors="replace")

    class _TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.title = ""
            self.h1 = ""
            self.parts: list[str] = []
            self._skip = 0
            self._buf = ""

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "noscript", "svg", "nav", "header", "footer"):
                self._skip += 1

        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript", "svg", "nav", "header", "footer") and self._skip > 0:
                self._skip -= 1
            if tag == "p" and self._buf.strip():
                self.parts.append(" ".join(self._buf.split()))
                self._buf = ""

        def handle_data(self, text):
            if self._skip:
                return
            self._buf += text

    parser = _TextExtractor()
    try:
        parser.feed(data)
    except Exception:
        pass
    for m in re.finditer(r"<title[^>]*>(.*?)</title>", data, re.I | re.S):
        title = " ".join(re.sub(r"<[^>]+>", "", m.group(1)).split())
        if title:
            parser.title = title
            break
    for m in re.finditer(r"<h1[^>]*>(.*?)</h1>", data, re.I | re.S):
        h1 = " ".join(re.sub(r"<[^>]+>", "", m.group(1)).split())
        if h1:
            parser.h1 = h1
            break

    title = parser.h1 or parser.title or url
    body = "\n\n".join(parser.parts).strip()
    if len(body) < 20:
        raise ValueError(f"无法从该网页提取到论文正文（{url}）。建议改为 arXiv 链接或直接上传 PDF。")
    return title, body


def _extract_pdf_text(data_b64: str) -> str:
    """解析 base64 PDF，返回纯文本。失败抛异常。"""
    import base64
    import tempfile
    import fitz

    raw = base64.b64decode(data_b64)
    if len(raw) > _pdf_max_bytes():
        raise ValueError(f"PDF 文件过大（上限 {_pdf_max_bytes() // (1024 * 1024)}MB）")
    tmp_path: str | None = None
    try:
        # Windows 上 NamedTemporaryFile 默认占用文件句柄，PyMuPDF 无法重新打开同一文件，
        # 因此先关闭句柄再交给 fitz.open（临时文件用完手动删除）。
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        with fitz.open(tmp_path) as doc:
            parts = []
            for page in doc:
                parts.append(page.get_text() or "")
            return "\n".join(parts).strip()
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _pdf_max_bytes() -> int:
    """PDF 上传/下载体积上限，默认 50MB，可用 DPR_PDF_MAX_MB（单位 MB）覆盖。"""
    try:
        return max(1, int(os.getenv("DPR_PDF_MAX_MB") or "50")) * 1024 * 1024
    except Exception:
        return 50 * 1024 * 1024


def _download_pdf_bytes_public(pdf_url: str, max_bytes: int | None = None) -> bytes:
    """下载 PDF 字节（复用日报的下载思路）。失败抛异常。"""
    if max_bytes is None:
        max_bytes = _pdf_max_bytes()
    data = _http_get_with_retry(
        pdf_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; daily-paper-reader/1.0)"},
        timeout=40,
        read_limit=max_bytes + 1,
    )
    if len(data) > max_bytes:
        raise ValueError(f"PDF 文件过大（上限 {max_bytes // (1024 * 1024)}MB）")
    return data


_SUMMARIZE_CACHE_FILE = RUNS_DIR / "paper_summarize_cache.json"


def _persist_summarize_as_daily_paper(
    *,
    paper_meta: dict[str, Any],
    title: str,
    text: str,
    kind: str,
    source: str,
    arxiv_id: str | None,
    pdf_bytes: bytes | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """把「论文总结」落地成一张与日报纸张页同构的 markdown，并注册进 Sidebar。

    复用 6.generate_docs 的 generate_external_paper_docs（速览 + 深度总结 + 图表解读 + 展示），
    使「总结」产出与日报纸张一致，可被 Sidebar / 未读等机制一并管理。

    Args:
        on_progress: 可选回调 (stage, message)，透传给 generate_external_paper_docs 发细粒度进度。

    返回 {"paper_id": ..., "md_path": ..., "registered": bool}。任何一步失败都返回
    {"paper_id": "", "md_path": "", "registered": False, "error": "..."}，不阻断上游 JSON 响应。
    """
    try:
        import importlib.util

        module_path = SRC_DIR / "6.generate_docs.py"
        spec = importlib.util.spec_from_file_location("dpr_generate_docs_for_summarize", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载日报生成模块：{module_path}")
        _gd = importlib.util.module_from_spec(spec)
        import sys as _sys
        _sys.modules[spec.name] = _gd
        spec.loader.exec_module(_gd)

        date_str = str(os.getenv("DPR_RUN_DATE") or "").strip() or datetime.now(timezone.utc).strftime("%Y%m%d")
        docs_dir = str(ROOT_DIR / "docs")

        authors = paper_meta.get("authors") or []
        published = str(paper_meta.get("published") or "").strip()[:10]
        link = str(paper_meta.get("link") or "").strip()
        pdf_url = str(paper_meta.get("pdf_url") or "").strip()

        # 摘要：arXiv 用真实 abstract；url/pdf 用预处理文本作为摘要（截断避免 prompt 过长）
        abstract = str(paper_meta.get("abstract") or "").strip()
        if not abstract:
            abstract = text[:6000]

        source_value, venue_value = _resolve_summarize_source(kind, paper_meta)
        paper = {
            "id": arxiv_id or "",
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "published": published,
            "link": link,
            "pdf_url": pdf_url or link,
            "source": source_value,
            "venue": venue_value,
        }

        md_path = ""
        try:
            pid, _title, md_path = _gd.generate_external_paper_docs(
                paper,
                date_str=date_str,
                docs_dir=docs_dir,
                section="deep",
                pdf_bytes=pdf_bytes,
                full_text=text,
                on_progress=on_progress,
            )
        except _CancelRequested:
            raise  # 用户取消必须传播到 worker，不能被当作落盘失败
        except Exception as exc:
            return {"paper_id": "", "md_path": "", "registered": False, "error": str(exc)}

        # 注册到 Sidebar（精读区）
        registered = False
        try:
            sidebar_path = os.path.join(docs_dir, "_sidebar.md")
            # 手动总结的论文打专属「手动总结」标签：侧边栏三级标签分组不再回退成「未标注」，
            # 且与日报论文（订阅 tag 分组）天然区分开。
            deep_entries: list[tuple[str, str, list]] = [(pid, title, [("manual", "手动总结")])]
            # 传入显式「原文链接」，避免非 arXiv 来源被拼成 arxiv.org/abs/<slug>。
            # link 为空（如上传 PDF）时指向纸张页自身（route_href），而不是错误 arxiv 链接。
            paper_link_map = {pid: link or "#" + "/" + pid}
            _gd.update_sidebar(
                sidebar_path,
                date_str,
                deep_entries,
                [],
                {pid: ""},
                paper_link_by_id=paper_link_map,
            )
            registered = True
        except Exception as exc:
            print(f"[SUMMARIZE][WARN] Sidebar 注册失败：{exc}", flush=True)

        return {"paper_id": pid, "md_path": md_path, "registered": registered}
    except _CancelRequested:
        raise  # 用户取消必须传播到 worker，不能被当作落盘失败
    except Exception as exc:
        return {"paper_id": "", "md_path": "", "registered": False, "error": str(exc)}


class SummarizeCache:
    """论文总结结果缓存：按论文身份（arXiv id / PDF 内容哈希）存 JSON 文件，重启仍有效。
    每个条目保存完整响应（含 base64 图），超出上限时按写入时间淘汰最旧条目。"""

    def __init__(self, path: Path, max_entries: int = 50) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._max = max_entries
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8") or "{}")
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._data.get(key)
            payload = entry.get("payload") if isinstance(entry, dict) else None
            return payload if isinstance(payload, dict) else None

    def put(self, key: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = {"at": utc_now(), "payload": payload}
            # 超出上限：按 at 淘汰最旧的
            if self._max and len(self._data) > self._max:
                for old_key in sorted(self._data, key=lambda k: self._data[k].get("at", ""))[: self._max * -1]:
                    self._data.pop(old_key, None)
            self._save()


SUMMARIZE_CACHE = SummarizeCache(
    _SUMMARIZE_CACHE_FILE,
    int(os.getenv("DPR_SUMMARIZE_CACHE_MAX") or "50"),
)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def _host_allowed(self) -> bool:
        """Reject requests whose Host header does not point to localhost.

        This is a DNS-rebinding defense: a malicious page tricking the browser
        into calling 127.0.0.1 with a foreign Host header.
        """
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return True  # HTTP/1.0 — no Host header
        if host.startswith("["):
            # IPv6 bracket notation: [::1] or [::1]:port
            try:
                hostname = host[1 : host.index("]")]
            except ValueError:
                return False
        else:
            hostname = host.split(":", 1)[0]
        return hostname in _ALLOWED_HOSTS

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        if not self._host_allowed():
            return self._json({"ok": False, "error": "forbidden"}, status=403)
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        if not self._host_allowed():
            return self._json({"ok": False, "error": "forbidden"}, status=403)
        parsed = urlparse(self.path)
        if parsed.path == "/api/local/health":
            return self._json({"ok": True, "mode": "local-server", "time": utc_now()})
        if parsed.path == "/api/local/system":
            from system_stats import system_snapshot

            return self._json({"ok": True, "snapshot": system_snapshot(
                ROOT_DIR,
                tracked_dirs=[RUNS_DIR, ROOT_DIR / "archive" / "survey_texts"],
                job_counts={
                    "survey_jobs": len(SURVEY_JOB_STORE.list()),
                    "summarize_jobs": len(SUMMARIZE_JOB_STORE.list()),
                    "workflow_runs": len(RUN_STORE.list()),
                },
            )})
        if parsed.path == "/api/local/config":
            return self._json({
                "ok": True,
                "path": str(CONFIG_PATH),
                "content": CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else "",
            })
        if parsed.path == "/api/local/config/structured":
            return self._json({"ok": True, "local": _load_local_chat_full()})
        if parsed.path == "/api/local/secret":
            return self._json({
                "ok": True,
                "exists": SECRET_PATH.exists(),
                "path": str(SECRET_PATH),
                "payload": json.loads(SECRET_PATH.read_text(encoding="utf-8")) if SECRET_PATH.exists() else None,
            })
        if parsed.path == "/api/local/runs":
            return self._json({"ok": True, "runs": RUN_STORE.list()})
        if parsed.path.startswith("/api/local/runs/"):
            parts = parsed.path.strip("/").split("/")
            run_id = parts[3] if len(parts) >= 4 else ""
            run = RUN_STORE.get(run_id)
            if not run:
                return self._json({"ok": False, "error": "run not found"}, status=404)
            if len(parts) >= 5 and parts[4] == "log":
                return self._json({"ok": True, "run": run, "log": RUN_STORE.log(run_id)})
            return self._json({"ok": True, "run": run})
        if parsed.path == "/api/chat/config":
            return self._chat_config()
        if parsed.path == "/api/paper/summarize":
            return self._json({"ok": True, "jobs": SUMMARIZE_JOB_STORE.list()})
        if parsed.path.startswith("/api/paper/summarize/"):
            parts = parsed.path.strip("/").split("/")
            job_id = parts[3] if len(parts) >= 4 else ""
            job = SUMMARIZE_JOB_STORE.get(job_id)
            if not job:
                return self._json({"ok": False, "error": "job not found"}, status=404)
            return self._json({"ok": True, "job": job})
        if parsed.path == "/api/survey":
            return self._json({"ok": True, "jobs": SURVEY_JOB_STORE.list()})
        if parsed.path.startswith("/api/survey/"):
            parts = parsed.path.strip("/").split("/")
            job_id = parts[2] if len(parts) >= 3 else ""
            job = SURVEY_JOB_STORE.get(job_id)
            if not job:
                return self._json({"ok": False, "error": "job not found"}, status=404)
            if len(parts) >= 4 and parts[3] == "log":
                return self._json({"ok": True, "job": job, "log": _survey_job_log(job_id)})
            return self._json({"ok": True, "job": job})
        return super().do_GET()

    def do_POST(self) -> None:
        if not self._host_allowed():
            return self._json({"ok": False, "error": "forbidden"}, status=403)
        parsed = urlparse(self.path)
        if parsed.path == "/api/chat":
            return self._proxy_chat()
        if parsed.path == "/api/paper/summarize":
            return self._paper_summarize_create_job()
        if parsed.path.startswith("/api/paper/summarize/") and parsed.path.rstrip("/").endswith("/cancel"):
            return self._paper_summarize_cancel(parsed.path)
        if parsed.path == "/api/survey":
            return self._survey_create_job()
        if parsed.path.startswith("/api/survey/") and parsed.path.rstrip("/").endswith("/cancel"):
            return self._survey_cancel(parsed.path)
        if parsed.path.startswith("/api/local/runs/") and parsed.path.rstrip("/").endswith("/cancel"):
            parts = parsed.path.strip("/").split("/")
            run_id = parts[3] if len(parts) >= 5 else ""
            if not run_id or not RUN_STORE.cancel(run_id):
                return self._json({"ok": False, "error": "run not found or already finished"}, status=404)
            return self._json({"ok": True, "run_id": run_id, "status": "cancelled"})
        if parsed.path == "/api/local/config":
            return self._save_local_config()
        if parsed.path == "/api/local/config/partial":
            return self._save_local_config_partial()
        if parsed.path == "/api/local/smart-query":
            return self._smart_query()
        if parsed.path == "/api/local/chat/models":
            return self._chat_models_fetch()
        if parsed.path == "/api/local/chat/test":
            return self._chat_connectivity_test()
        if parsed.path == "/api/local/secret":
            return self._save_local_secret()
        if parsed.path != "/api/local/workflows/dispatch":
            return self._json({"ok": False, "error": "not found"}, status=404)
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            workflow_key = str(payload.get("workflowKey") or "")
            workflow_file = str(payload.get("workflowFile") or "")
            inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
            inputs = {str(k): str(v) for k, v in inputs.items() if v is not None}
            config = payload.get("config") if isinstance(payload.get("config"), dict) else None
            secret = payload.get("secret") if isinstance(payload.get("secret"), dict) else None
            cmd = build_command(workflow_key, workflow_file, inputs)
            run = RUN_STORE.create(
                workflow_key,
                workflow_file,
                inputs,
                cmd,
                config=config,
                secret=secret,
                external_job_id=str(payload.get("externalJobId") or ""),
            )
            return self._json({"ok": True, "run": run})
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, status=400)

    def _paper_summarize_create_job(self) -> None:
        """POST /api/paper/summarize — 建异步 job，立即返回 job_id。

        旧同步行为（等待整个总结完成才返回）已废弃：耗时 3-10 分钟，前端 fetch
        会因 LLM 读超时（120s）而失败。现在改为立即返回 job_id，前端轮询
        GET /api/paper/summarize/<job_id> 获取进度与结果。
        """
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            source = str(payload.get("source") or "").strip()
            if source not in ("url", "pdf"):
                return self._json({"ok": False, "error": "source 必须是 url 或 pdf"}, status=400)
            job = SUMMARIZE_JOB_STORE.create(payload)
            return self._json({
                "ok": True,
                "schema_version": "connect.job.v1",
                "job_id": job["job_id"],
                "status": job["status"],
                "events": job["events"],
            })
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, status=500)

    def _paper_summarize_cancel(self, path: str) -> None:
        """POST /api/paper/summarize/<job_id>/cancel — 请求取消（best-effort）。"""
        parts = path.strip("/").split("/")
        job_id = parts[3] if len(parts) >= 4 else ""
        if not job_id:
            return self._json({"ok": False, "error": "missing job_id"}, status=400)
        ok = SUMMARIZE_JOB_STORE.request_cancel(job_id)
        if not ok:
            return self._json({"ok": False, "error": "job not found or already finished"}, status=404)
        return self._json({"ok": True, "job_id": job_id, "status": "cancelling"})

    def _survey_create_job(self) -> None:
        """POST /api/survey — 建异步综述 job，立即返回 job_id。

        综述含 PDF 深读与多轮 LLM 调用，耗时可达十几分钟，必须异步化。
        """
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            query = str(payload.get("query") or "").strip()
            if not query:
                return self._json({"ok": False, "error": "缺少综述主题 query"}, status=400)
            job = SURVEY_JOB_STORE.create(payload)
            return self._json({
                "ok": True,
                "schema_version": "connect.job.v1",
                "job_id": job["job_id"],
                "status": job["status"],
                "events": job["events"],
            })
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": str(exc)}, status=500)

    def _survey_cancel(self, path: str) -> None:
        """POST /api/survey/<job_id>/cancel — 请求取消（best-effort，协作式）。"""
        parts = path.strip("/").split("/")
        job_id = parts[2] if len(parts) >= 3 else ""
        if not job_id:
            return self._json({"ok": False, "error": "missing job_id"}, status=400)
        ok = SURVEY_JOB_STORE.request_cancel(job_id)
        if not ok:
            return self._json({"ok": False, "error": "job not found or already finished"}, status=404)
        return self._json({"ok": True, "job_id": job_id, "status": "cancelling"})

    def _save_local_secret(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            secret_payload = payload.get("payload")
            if not isinstance(secret_payload, dict):
                return self._json({"ok": False, "error": "payload must be an object"}, status=400)
            SECRET_PATH.write_text(
                json.dumps(secret_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            secret_plain = payload.get("secret") if isinstance(payload.get("secret"), dict) else None
            env_path = ""
            env_keys: list[str] = []
            if secret_plain:
                secret_env = build_secret_env(secret_plain)
                if secret_env:
                    update_env_file(ENV_PATH, secret_env)
                    env_path = str(ENV_PATH)
                    env_keys = sorted(secret_env.keys())
            return self._json({
                "ok": True,
                "path": str(SECRET_PATH),
                "envPath": env_path,
                "envKeys": env_keys,
                "savedAt": utc_now(),
            })
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, status=400)

    def _save_local_config(self) -> None:
        if yaml is None:
            return self._json({"ok": False, "error": "本地调试后端缺少 PyYAML，无法写入 config.yaml。"}, status=500)
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            config = payload.get("config")
            if not isinstance(config, dict):
                return self._json({"ok": False, "error": "config must be an object"}, status=400)
            content = yaml.safe_dump(config, allow_unicode=True, sort_keys=False, width=10**9)
            CONFIG_PATH.write_text(content, encoding="utf-8")
            return self._json({"ok": True, "path": str(CONFIG_PATH), "savedAt": utc_now()})
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, status=400)

    def _save_local_config_partial(self) -> None:
        """支持只更新 config.yaml 的指定顶层段，其余保持不变。

        当前可更新段：local（chat/schedule）、subscriptions、academic_news（前端会议速览勾选）。
        未涉及的段保持原样。
        """
        if yaml is None:
            return self._json({"ok": False, "error": "缺少 PyYAML，无法写入 config.yaml。"}, status=500)
        try:
            import yaml as _yaml
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            existing = _yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
            existing = existing if isinstance(existing, dict) else {}
            merged = existing

            incoming_local = payload.get("local")
            if isinstance(incoming_local, dict):
                merged = merge_local_section(merged, {"local": incoming_local})

            subscriptions = payload.get("subscriptions")
            if isinstance(subscriptions, dict):
                merged = merge_top_level_section(merged, "subscriptions", subscriptions)

            academic_news = payload.get("academic_news")
            if isinstance(academic_news, dict):
                merged = merge_top_level_section(merged, "academicNews_key", academic_news)
            # academic_news 顶层段
            academicNewsRoot = payload.get("academicNews")
            if isinstance(academicNewsRoot, dict):
                merged = merge_top_level_section(merged, "academic_news", academicNewsRoot)

            # 推荐名额设置：清洗后整体替换；payload 未携带则不动该段
            if "recommend_setting" in payload:
                merged = merge_top_level_section(
                    merged,
                    "recommend_setting",
                    _normalize_recommend_setting(payload.get("recommend_setting")),
                )

            content = _yaml.safe_dump(merged, allow_unicode=True, sort_keys=False, width=10**9)
            CONFIG_PATH.write_text(content, encoding="utf-8")
            # reranker 后端选择即时生效：综述在进程内跑（读 os.getenv），
            # 日报子进程另由 RunStore worker 注入，双路径都覆盖。
            _apply_rerank_profile_to_env()
            return self._json({"ok": True, "savedAt": utc_now()})
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, status=400)

    def _chat_config(self) -> None:
        cfg = _load_local_chat_config()
        return self._json({"ok": True, "model": cfg["model"], "base_url": cfg["base_url"]})

    def _smart_query(self) -> None:
        """POST /api/local/smart-query — 把一句检索意图解析成订阅候选（关键词/意图查询，英中成对）。

        同步调用 LLM（一次结构化生成，约几秒），不引入异步 Job。
        """
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": f"请求体解析失败：{exc}"}, status=400)
        intent = str(payload.get("intent") or "").strip()
        if not intent:
            return self._json({"ok": False, "error": "请先填写检索需求（一句自然语言描述）"}, status=400)
        tag_hint = str(payload.get("tag") or "").strip()
        try:
            result = _generate_subscription_candidates(intent, tag_hint)
        except Exception as exc:  # noqa: BLE001
            print(f"[SMART-QUERY][WARN] 候选生成失败：{exc}", flush=True)
            return self._json({"ok": False, "error": f"候选生成失败：{exc}"}, status=502)
        return self._json({"ok": True, **result})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        return payload if isinstance(payload, dict) else {}

    def _chat_models_fetch(self) -> None:
        """POST /api/local/chat/models — 按端点+密钥拉取 OpenAI 兼容模型列表。

        密钥留空时回退 config.yaml local.chat，再回退 .env，与问答代理同一套解析。
        """
        try:
            payload = self._read_json_body()
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": f"请求体解析失败：{exc}"}, status=400)
        try:
            base_url, api_key = _resolve_chat_credentials(
                str(payload.get("base_url") or ""), str(payload.get("api_key") or "")
            )
            models = _fetch_chat_model_list(base_url, api_key)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": f"拉取模型列表失败：{exc}"}, status=502)
        return self._json({"ok": True, "models": models, "count": len(models)})

    def _chat_connectivity_test(self) -> None:
        """POST /api/local/chat/test — 用最小 chat completion 测端点+模型连通性。"""
        try:
            payload = self._read_json_body()
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": f"请求体解析失败：{exc}"}, status=400)
        model = str(payload.get("model") or "").strip()
        if not model:
            return self._json({"ok": False, "error": "请先填写模型名称"}, status=400)
        try:
            base_url, api_key = _resolve_chat_credentials(
                str(payload.get("base_url") or ""), str(payload.get("api_key") or "")
            )
            latency_ms, snippet = _probe_chat_completion(base_url, api_key, model)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            import urllib.error

            reason = str(exc)
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    reason = exc.read(512).decode("utf-8", errors="replace") or reason
                except Exception:  # noqa: BLE001
                    pass
            return self._json({"ok": False, "error": f"连接失败：{reason}"}, status=502)
        return self._json({
            "ok": True,
            "model": model,
            "latency_ms": latency_ms,
            "reply": snippet,
        })

    def _proxy_chat(self) -> None:
        import json, urllib.request, urllib.error
        length = int(self.headers.get("Content-Length") or "0")
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        cfg = _load_local_chat_config()
        api_key = _resolve_chat_api_key(cfg)
        body = build_chat_request_payload(cfg["model"], payload.get("messages") or [], max_tokens=payload.get("max_tokens"))
        endpoint = _build_chat_endpoint(cfg["base_url"])
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            upstream = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as exc:
            upstream_status = getattr(exc, "code", 502)
            return self._json({"ok": False, "error": f"upstream returned {upstream_status}"}, status=502)
        except urllib.error.URLError as exc:
            return self._json({"ok": False, "error": f"upstream unavailable: {exc.reason}"}, status=502)
        upstream_status = upstream.getcode() or 200
        self.send_response(upstream_status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                chunk = upstream.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            upstream.close()

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8567)
    parser.add_argument("--no-schedule", action="store_true", help="不启动定时器")
    parser.add_argument("--serve", action="store_true", help="启动本地服务（默认行为）")
    args = parser.parse_args()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    # 启动时把面板保存的 reranker 后端同步进进程 env（综述在进程内读 os.getenv）
    _apply_rerank_profile_to_env()
    server = ThreadingHTTPServer((args.host, args.port), Handler)

    sched_thread = None
    if not args.no_schedule:
        schedule = _get_schedule()
        if as_bool(schedule.get("enabled"), False):
            sched_time = str(schedule.get("time") or "").strip() or "18:30"
            on_fire = lambda: _dispatch_daily_pipeline()
            sched_thread = SchedulerThread(sched_time, on_fire)
            sched_thread.start()
            print(f"[local-server] 定时器已启动，每天 {sched_time} 自动跑流水线", flush=True)

    print(f"[local-server] serving http://{args.host}:{args.port}", flush=True)
    print("[local-server] AI 问答通过 /api/chat 本地代理", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if sched_thread:
            sched_thread.stop()


if __name__ == "__main__":
    main()
