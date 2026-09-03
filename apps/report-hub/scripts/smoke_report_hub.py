from __future__ import annotations

import io
import json
import os
import time
import urllib.request
import zipfile


BASE_URL = os.environ["REPORT_HUB_API_URL"].rstrip("/")
TOKEN = os.environ["REPORT_HUB_AGENT_TOKEN"]
SITE_ID = f"smoke-{int(time.time())}"


def call(
    path: str,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str = "application/json",
) -> dict[str, object]:
    request = urllib.request.Request(
        BASE_URL + path,
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": content_type},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


created = call(
    "/api/v1/sites",
    "POST",
    json.dumps(
        {
            "site_id": SITE_ID,
            "module_name": "other",
            "title": "Report Hub 冒烟测试",
            "command_policy": [],
        }
    ).encode(),
)
archive_bytes = io.BytesIO()
with zipfile.ZipFile(archive_bytes, "w") as archive:
    archive.writestr(
        "index.html",
        "<!doctype html><meta name='viewport' content='width=device-width'>"
        "<h1>Report Hub 站点上传已跑通</h1>",
    )
uploaded = call(
    f"/api/v1/sites/{SITE_ID}/report",
    "PUT",
    archive_bytes.getvalue(),
    "application/zip",
)
print(uploaded.get("public_url") or created.get("public_url") or "")
