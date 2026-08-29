from __future__ import annotations

import io
import json
import os
import time
import urllib.request
import zipfile


BASE_URL = os.environ["REPORT_HUB_API_URL"].rstrip("/")
TOKEN = os.environ["REPORT_HUB_AGENT_TOKEN"]
JOB_ID = f"smoke-{int(time.time())}"


def call(path: str, method: str = "GET", body: bytes | None = None, content_type: str = "application/json"):
    request = urllib.request.Request(
        BASE_URL + path,
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": content_type},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


created = call(
    "/api/v1/jobs",
    "POST",
    json.dumps({"job_id": JOB_ID, "module_name": "daily-paper", "title": "Report Hub 冒烟测试"}).encode(),
)
for index, message in enumerate(["正在检索论文", "正在生成摘要", "正在整理网页"], 1):
    call(
        f"/api/v1/jobs/{JOB_ID}/events",
        "POST",
        json.dumps(
            {
                "event_id": f"smoke-{index}",
                "event_type": "job.progress",
                "stage": "smoke",
                "message": message,
                "current": index,
                "total": 3,
            }
        ).encode(),
    )

archive_bytes = io.BytesIO()
with zipfile.ZipFile(archive_bytes, "w") as archive:
    archive.writestr(
        "index.html",
        "<!doctype html><meta name='viewport' content='width=device-width'><style>body{font-family:sans-serif;padding:2rem;max-width:700px;margin:auto}</style><h1>Report Hub 已跑通</h1><p>这份页面由本地任务上传，任务进程退出后仍由公网服务器保存。</p>",
    )
call(
    f"/api/v1/jobs/{JOB_ID}/report",
    "PUT",
    archive_bytes.getvalue(),
    "application/zip",
)
print(created["public_url"])

