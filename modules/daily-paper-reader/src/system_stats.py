"""只读系统资源快照：供 /api/local/system 端点排障使用。

设计边界：每次调用现查现返回，不做后台采样、不落时序、不新增常驻线程。
覆盖三块：本进程+子进程树（内存大头在哪一 eyeball 可见）、GPU 显存（nvidia-smi 存在时）、
运行产物目录的磁盘占用（.local-runs 日志 / 综述 PDF 缓存等可清理项）。
psutil 缺失时优雅降级为提示信息，不影响服务其他功能。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

_CMDLINE_MAX_CHARS = 200
_WALK_MAX_FILES = 20000


def _process_tree() -> Dict[str, Any]:
    import psutil

    proc = psutil.Process()
    mem = proc.memory_info()

    def _entry(p: Any) -> Dict[str, Any]:
        try:
            cmdline = " ".join(p.cmdline() or [])
        except Exception:  # noqa: BLE001 - 权限/竞态时进程可能刚退出
            cmdline = ""
        rss = 0
        try:
            rss = p.memory_info().rss
        except Exception:  # noqa: BLE001
            pass
        return {
            "pid": p.pid,
            "name": p.name(),
            "rss_mb": round(rss / 1e6, 1),
            "cmdline": cmdline[:_CMDLINE_MAX_CHARS],
        }

    children: List[Dict[str, Any]] = []
    try:
        children = [_entry(child) for child in proc.children(recursive=True)]
    except Exception:  # noqa: BLE001
        children = []
    children.sort(key=lambda item: -item["rss_mb"])
    return {
        "self": {**_entry(proc), "rss_mb": round(mem.rss / 1e6, 1)},
        "children": children,
    }


def _gpu() -> List[Dict[str, Any]] | None:
    """nvidia-smi 存在时返回每卡显存/利用率；否则 None。不 import torch（服务进程不背大依赖）。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        gpus: List[Dict[str, Any]] = []
        for line in (out.stdout or "").strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            gpus.append({
                "name": parts[0],
                "vram_used_mb": float(parts[1]),
                "vram_total_mb": float(parts[2]),
                "util_percent": float(parts[3]),
            })
        return gpus
    except Exception:  # noqa: BLE001 - 驱动缺失/超时都不致命
        return None


def _directory_size(path: Path) -> Dict[str, Any]:
    total = 0
    files = 0
    try:
        for root, _dirs, names in os.walk(path):
            for name in names:
                files += 1
                if files > _WALK_MAX_FILES:
                    break
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
            if files > _WALK_MAX_FILES:
                break
    except OSError:
        pass
    return {"path": str(path), "mb": round(total / 1e6, 1), "files": files}


def system_snapshot(root_dir: Path, *, tracked_dirs: List[Path] | None = None, job_counts: Dict[str, int] | None = None) -> Dict[str, Any]:
    """组装只读快照。job_counts 由调用方（local_server）传入各 Store 的驻留数量。"""
    payload: Dict[str, Any] = {"ok": True}
    try:
        payload["process"] = _process_tree()
    except ImportError:
        return {
            "ok": False,
            "error": "psutil 未安装：请先 pip install psutil（已列入 requirements.txt）",
        }
    except Exception as exc:  # noqa: BLE001
        payload["process"] = {"error": str(exc)}

    payload["gpu"] = _gpu()

    dirs = list(tracked_dirs or [])
    payload["disk"] = [_directory_size(Path(d)) for d in dirs]

    payload["jobs"] = dict(job_counts or {})
    return payload
