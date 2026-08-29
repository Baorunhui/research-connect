#!/usr/bin/env python3
"""Build the renowned scholar local database.

Usage:
    python scripts/build_scholar_db.py           # build all sources
    python scripts/build_scholar_db.py cas cae   # build specific sources
    python scripts/build_scholar_db.py --count   # show current count
    python scripts/build_scholar_db.py --lookup "高文"  # test lookup
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from citationclaw.core.scholar_db import ScholarDB, _get_db_path


def main():
    db_path = _get_db_path()
    db = ScholarDB()

    if "--count" in sys.argv:
        print(f"数据库: {db_path}")
        print(f"总记录: {db.count()}")
        rows = db.conn.execute(
            "SELECT source, COUNT(*) as cnt FROM renowned_scholars GROUP BY source ORDER BY cnt DESC"
        ).fetchall()
        for r in rows:
            print(f"  {r['source']}: {r['cnt']}")
        db.close()
        return

    if "--lookup" in sys.argv:
        idx = sys.argv.index("--lookup")
        if idx + 1 < len(sys.argv):
            result = db.lookup(sys.argv[idx + 1])
            if result:
                import json
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print("未找到")
        db.close()
        return

    if "--export" in sys.argv:
        idx = sys.argv.index("--export")
        out = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "scholars.json"
        rows = db.conn.execute("SELECT * FROM renowned_scholars").fetchall()
        import json
        data = [dict(r) for r in rows]
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"导出 {len(data)} 条到 {out}")
        db.close()
        return

    sources = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not sources:
        sources = None
    elif "all" in sources:
        sources = None

    print(f"数据库: {db_path}")
    print(f"当前记录: {db.count()}")
    print(f"开始建库 (sources: {sources or 'all'})...")
    db.build(sources=sources)
    print(f"完成! 总记录: {db.count()}")
    db.close()


if __name__ == "__main__":
    main()
