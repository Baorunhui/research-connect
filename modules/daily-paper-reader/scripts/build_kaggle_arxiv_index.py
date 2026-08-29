#!/usr/bin/env python
"""构建 Kaggle arXiv 快照索引（综述大规模粗筛路的本地数据源）。

用法（在仓库根目录）：
  python scripts/build_kaggle_arxiv_index.py --download          # 下载快照（~4GB）+ 建索引
  python scripts/build_kaggle_arxiv_index.py --no-download       # 用已有 JSON 重建索引
  python scripts/build_kaggle_arxiv_index.py --db-path D:/x.sqlite3 --download

产物：archive/kaggle_arxiv/arxiv-metadata-oai-snapshot.json（~4GB，建完可手动删）
      archive/kaggle_arxiv/index.sqlite3（~6-8GB，检索用）
认证：.env 的 KAGGLE_API_TOKEN（KGAT）或 KAGGLE_USERNAME+KAGGLE_KEY。
建议每周重跑一次 --download 刷新（快照由 Cornell 官方持续同步，周级滞后）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))

from kaggle_arxiv import (  # noqa: E402
    DEFAULT_DATA_DIR,
    SNAPSHOT_JSON_NAME,
    build_index,
    download_snapshot,
    resolve_default_db_path,
)

try:
    from local_env import load_local_env  # noqa: E402
except ImportError:
    from src.local_env import load_local_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 Kaggle arXiv 快照 SQLite FTS 索引")
    parser.add_argument("--download", action="store_true", help="先从 Kaggle 下载最新快照（默认关闭，用本地已有 JSON）")
    parser.add_argument("--no-download", dest="download", action="store_false")
    parser.add_argument("--json-path", default="", help="快照 JSON 路径（默认 archive/kaggle_arxiv/%s）" % SNAPSHOT_JSON_NAME)
    parser.add_argument("--db-path", default="", help="索引库输出路径（默认 DPR_SURVEY_KAGGLE_INDEX 或 archive/kaggle_arxiv/index.sqlite3）")
    parser.add_argument("--keep-json", action="store_true", help="保留下载的 zip/JSON（默认建完自动清理 zip）")
    args = parser.parse_args()
    load_local_env()

    started = time.time()
    json_path = Path(args.json_path) if args.json_path else DEFAULT_DATA_DIR / SNAPSHOT_JSON_NAME
    db_path = Path(args.db_path) if args.db_path else resolve_default_db_path()

    if args.download or not json_path.exists():
        json_path = download_snapshot(DEFAULT_DATA_DIR)
    if not json_path.exists():
        print(f"[error] 快照不存在：{json_path}（先跑 --download）", file=sys.stderr)
        return 1

    stats = build_index(json_path, db_path)
    print(
        f"完成：{stats['row_count']} 行 → {stats['db_path']}"
        f"（建库 {stats['build_seconds']}s，总耗时 {time.time() - started:.0f}s）"
    )
    print("综述流水线下次运行将自动启用 Kaggle 粗筛路（可在前端关闭）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
