"""Renowned scholar local database (SQLite).

Pre-populated with academicians, Changjiang Scholars, IEEE/ACM/AAAI Fellows,
etc.  Provides fast name-based lookup to avoid LLM calls during Phase 2
renowned-scholar filtering.

Usage:
    from citationclaw.core.scholar_db import ScholarDB
    db = ScholarDB()                     # opens ~/.citationclaw/scholars.db
    db.build()                           # scrape + populate (run once)
    hit = db.lookup("何恺明")             # → dict or None
    hit = db.lookup("Kaiming He")        # name_en also indexed
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
from research_connect_core import DataPaths

DB_PATH = DataPaths.for_module("citationclaw").state / "scholars.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS renowned_scholars (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    name_en       TEXT DEFAULT '',
    name_aliases  TEXT DEFAULT '[]',
    affiliation   TEXT DEFAULT '',
    country       TEXT DEFAULT '中国',
    title         TEXT DEFAULT '',
    honors        TEXT DEFAULT '[]',
    field         TEXT DEFAULT '',
    sub_field     TEXT DEFAULT '',
    source        TEXT DEFAULT '',
    updated_at    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_name      ON renowned_scholars(name);
CREATE INDEX IF NOT EXISTS idx_name_en   ON renowned_scholars(name_en);
CREATE INDEX IF NOT EXISTS idx_field     ON renowned_scholars(field);
"""


def _get_db_path() -> Path:
    p = DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class ScholarDB:
    """SQLite-backed renowned scholar lookup."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or _get_db_path()
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_CREATE_SQL)
        return self._conn

    # ── public API ──────────────────────────────────────────────

    def lookup(self, name: str) -> Optional[dict]:
        """Look up a scholar by Chinese name, English name, or alias.

        Returns a dict with keys: name, name_en, affiliation, country,
        title, honors, field, sub_field — or None if not found.
        """
        if not name or not name.strip():
            return None
        name = name.strip()
        row = self.conn.execute(
            "SELECT * FROM renowned_scholars WHERE name = ? LIMIT 1", (name,)
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT * FROM renowned_scholars WHERE name_en = ? LIMIT 1", (name,)
            ).fetchone()
        if row is None:
            rows = self.conn.execute(
                "SELECT * FROM renowned_scholars WHERE name_aliases LIKE ?",
                (f'%"{name}"%',),
            ).fetchall()
            if rows:
                row = rows[0]
        if row is None:
            return None
        return {
            "name": row["name"],
            "name_en": row["name_en"],
            "affiliation": row["affiliation"],
            "country": row["country"],
            "title": row["title"],
            "honors": json.loads(row["honors"] or "[]"),
            "field": row["field"],
            "sub_field": row["sub_field"],
        }

    def count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM renowned_scholars"
        ).fetchone()[0]

    def add(self, scholar: dict) -> bool:
        """Insert or update a scholar record. Returns True if added/updated."""
        name = scholar.get("name", "").strip()
        if not name:
            return False
        existing = self.conn.execute(
            "SELECT id FROM renowned_scholars WHERE name = ?", (name,)
        ).fetchone()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        honors = scholar.get("honors", [])
        if isinstance(honors, list):
            honors = json.dumps(honors, ensure_ascii=False)
        aliases = scholar.get("name_aliases", [])
        if isinstance(aliases, list):
            aliases = json.dumps(aliases, ensure_ascii=False)
        if existing:
            self.conn.execute(
                """UPDATE renowned_scholars SET
                   name_en=?, name_aliases=?, affiliation=?, country=?,
                   title=?, honors=?, field=?, sub_field=?, source=?, updated_at=?
                   WHERE id=?""",
                (
                    scholar.get("name_en", ""),
                    aliases,
                    scholar.get("affiliation", ""),
                    scholar.get("country", "中国"),
                    scholar.get("title", ""),
                    honors,
                    scholar.get("field", ""),
                    scholar.get("sub_field", ""),
                    scholar.get("source", ""),
                    now,
                    existing["id"],
                ),
            )
        else:
            self.conn.execute(
                """INSERT INTO renowned_scholars
                   (name, name_en, name_aliases, affiliation, country,
                    title, honors, field, sub_field, source, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    name,
                    scholar.get("name_en", ""),
                    aliases,
                    scholar.get("affiliation", ""),
                    scholar.get("country", "中国"),
                    scholar.get("title", ""),
                    honors,
                    scholar.get("field", ""),
                    scholar.get("sub_field", ""),
                    scholar.get("source", ""),
                    now,
                ),
            )
        self.conn.commit()
        return True

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── build from sources ──────────────────────────────────────

    def build(self, sources: list[str] | None = None, log=print):
        """Populate the database from public sources.

        Args:
            sources: list of source names to scrape. Default: all.
            log: log callback function.
        """
        all_sources = ["cas", "cae", "aaai", "changjiang", "jieqing", "ieee_cs", "manual"]
        sources = sources or all_sources
        total = 0
        for src in sources:
            try:
                fn = getattr(self, f"_load_{src}", None) or getattr(self, f"_scrape_{src}")
                count = fn(log=log)
                total += count
                log(f"  [{src}] {count} 条记录")
            except Exception as e:
                log(f"  [{src}] 失败: {e}")
        log(f"建库完成: 共 {total} 条记录 (数据库总 {self.count()} 条)")
        return total

    # ── manual seed data ─────────────────────────────────────────

    def _load_manual(self, log=print) -> int:
        """Load manually curated scholars from ``data/manual_scholars.json``."""
        # Find data dir: repo root / data / manual_scholars.json
        repo_root = Path(__file__).resolve().parent.parent.parent
        json_path = repo_root / "data" / "manual_scholars.json"
        if not json_path.exists():
            log(f"    [manual] {json_path} 不存在，跳过")
            return 0
        with open(json_path, "r", encoding="utf-8") as f:
            scholars = json.load(f)
        count = 0
        for s in scholars:
            s["source"] = "manual"
            if self.add(s):
                count += 1
        return count

    # ── CAS 院士 ────────────────────────────────────────────────

    def _scrape_cas(self, log=print) -> int:
        """Scrape 中国科学院院士 from casad.cas.cn."""
        divisions = [
            ("sxwl", "数学物理学部"),
            ("hxb", "化学部"),
            ("smkx", "生命科学和医学学部"),
            ("dxb", "地学部"),
            ("xxjs", "信息技术科学部"),
            ("jskx", "技术科学部"),
        ]
        count = 0
        client = httpx.Client(timeout=30.0, follow_redirects=True)
        try:
            for div_code, div_name in divisions:
                url = f"https://casad.cas.cn/ysxx2022/ysmd/{div_code}/"
                try:
                    resp = client.get(url)
                    if resp.status_code != 200:
                        log(f"    CAS {div_name}: HTTP {resp.status_code}")
                        continue
                    names = re.findall(
                        r'<a[^>]*href="[^"]*"[^>]*>([^\x00-\x7f][^<]{2,5})</a>',
                        resp.text,
                    )
                    names = [n.strip() for n in names if 2 <= len(n.strip()) <= 5
                             and not any(c in n for c in "学部委员关于")]
                    for name in names:
                        self.add({
                            "name": name,
                            "country": "中国",
                            "title": "中国科学院院士",
                            "honors": ["中国科学院院士"],
                            "field": div_name,
                            "source": "cas",
                        })
                        count += 1
                except Exception as e:
                    log(f"    CAS {div_name}: {e}")
        finally:
            client.close()
        return count

    # ── CAE 院士 ────────────────────────────────────────────────

    def _scrape_cae(self, log=print) -> int:
        """Scrape 中国工程院院士 from cae.cn."""
        count = 0
        client = httpx.Client(timeout=30.0, follow_redirects=True)
        try:
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                url = f"https://www.cae.cn/cae/html/main/col48/column_48_{letter}.html"
                try:
                    resp = client.get(url)
                    if resp.status_code != 200:
                        continue
                    names = re.findall(
                        r'<li class="name_list"><a[^>]*>([^<]+)</a></li>',
                        resp.text,
                    )
                    names = [n.strip() for n in names if n.strip()]
                    for name in names:
                        self.add({
                            "name": name,
                            "country": "中国",
                            "title": "中国工程院院士",
                            "honors": ["中国工程院院士"],
                            "source": "cae",
                        })
                        count += 1
                except Exception:
                    continue
        finally:
            client.close()
        return count

    # ── AAAI Fellows ────────────────────────────────────────────

    def _scrape_aaai(self, log=print) -> int:
        """Scrape AAAI Fellows from aaai.org."""
        url = ("https://aaai.org/about-aaai/aaai-awards/"
               "the-aaai-fellows-program/elected-aaai-fellows/")
        count = 0
        client = httpx.Client(timeout=30.0, follow_redirects=True)
        try:
            resp = client.get(url)
            if resp.status_code != 200:
                log(f"    AAAI: HTTP {resp.status_code}")
                return 0
            text = resp.text
            entries = re.findall(
                r'<p class="wp-block-paragraph">([^<]+)<em>([^<]*)</em><br>([^<]*)</p>',
                text,
            )
            for name_raw, affil, _citation in entries:
                name = name_raw.strip()
                if not name or not re.search(r"[A-Za-z]", name):
                    continue
                affiliation = affil.strip() if affil else ""
                self.add({
                    "name": name,
                    "name_en": name,
                    "affiliation": affiliation,
                    "country": "国际",
                    "title": "AAAI Fellow",
                    "honors": ["AAAI Fellow"],
                    "field": "人工智能",
                    "source": "aaai",
                })
                count += 1
        except Exception as e:
            log(f"    AAAI: {e}")
        finally:
            client.close()
        return count

    # ── 长江学者 (from GitHub) ──────────────────────────────────

    def _scrape_changjiang(self, log=print) -> int:
        """Download Changjiang Scholar data from GitHub repo."""
        zip_url = ("https://codeload.github.com/ming66/"
                   "Data-analysis-of-changjiang-scholars/zip/refs/heads/master")
        count = 0
        client = httpx.Client(timeout=60.0, follow_redirects=True)
        try:
            resp = client.get(zip_url)
            if resp.status_code != 200:
                log(f"    长江学者: HTTP {resp.status_code}")
                return 0
            import zipfile
            tmp_zip = Path("/tmp/_changjiang_repo.zip")
            tmp_zip.write_bytes(resp.content)
            try:
                import openpyxl
                with zipfile.ZipFile(str(tmp_zip)) as z:
                    xlsx_name = next(
                        n for n in z.namelist()
                        if n.endswith("所有长江学者名单.xlsx")
                    )
                    z.extract(xlsx_name, "/tmp/cj/")
                wb = openpyxl.load_workbook(
                    f"/tmp/cj/{xlsx_name}", read_only=True
                )
                ws = wb.active
                headers = None
                for row in ws.iter_rows(values_only=True):
                    if headers is None:
                        headers = [str(c or "").strip() for c in row]
                        continue
                    row_dict = dict(zip(headers, row))
                    name = str(row_dict.get("教授姓名", "") or "").strip()
                    if not name or len(name) > 20:
                        continue
                    affiliation = str(
                        row_dict.get("聘任学校/推荐单位", "") or ""
                    ).strip()
                    discipline = str(
                        row_dict.get("设岗学科/岗位名称", "") or ""
                    ).strip()
                    scholar_type = str(
                        row_dict.get("聘任类型", "") or ""
                    ).strip()
                    self.add({
                        "name": name,
                        "affiliation": affiliation,
                        "country": "中国",
                        "title": f"长江学者({scholar_type})" if scholar_type else "长江学者",
                        "honors": ["长江学者"],
                        "field": discipline,
                        "source": "changjiang",
                    })
                    count += 1
                wb.close()
            except ImportError:
                log("    长江学者: 需要 openpyxl (pip install openpyxl)")
            finally:
                tmp_zip.unlink(missing_ok=True)
        except Exception as e:
            log(f"    长江学者: {e}")
        finally:
            client.close()
        return count

    # ── 杰青 (LetPub NSFC) ──────────────────────────────────────

    def _scrape_jieqing(self, log=print) -> int:
        """Scrape 国家杰出青年基金获得者 from LetPub (1994-2021)."""
        base_url = "https://www.letpub.com.cn/nsfcfund_search.php"
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.letpub.com.cn/index.php?page=grant",
        }
        count = 0
        client = httpx.Client(timeout=30.0, follow_redirects=True, headers=headers)
        try:
            for year in range(1994, 2022):
                year_count = 0
                for page in range(1, 21):
                    params = {
                        "mode": "advanced",
                        "datakind": "list",
                        "currentpage": str(page),
                    }
                    data = {
                        "searchsubmit": "true",
                        "submit": "advSearch",
                        "subcategory": "国家杰出青年科学基金",
                        "startTime": str(year),
                        "endTime": str(year),
                    }
                    text = None
                    for attempt in range(3):
                        try:
                            resp = client.post(base_url, params=params, data=data)
                            if resp.status_code != 200:
                                time.sleep(5.0)
                                continue
                            text = resp.text
                            if "过于频繁" in text:
                                log(f"    杰青 {year} p{page}: 被限流，等待 30s...")
                                time.sleep(30.0)
                                text = None
                                continue
                            if "需要先注册登录" in text:
                                text = None
                                break
                            break
                        except Exception:
                            time.sleep(5.0)
                    if text is None:
                        break
                    tds = re.findall(r"<td[^>]*>(.*?)</td>", text, re.DOTALL)
                    if not tds:
                        break
                    records_on_page = 0
                    for i in range(0, len(tds), 15):
                        if i + 1 >= len(tds):
                            break
                        name = re.sub(r"<[^>]+>", "", tds[i]).strip()
                        affil = re.sub(r"<[^>]+>", "", tds[i + 1]).strip()
                        if not name or len(name) > 10 or not affil:
                            continue
                        if any(w in name for w in ("题目", "学科", "执行", "搜索")):
                            continue
                        self.add({
                            "name": name,
                            "affiliation": affil,
                            "country": "中国",
                            "title": "国家杰青",
                            "honors": ["国家杰出青年基金"],
                            "source": "jieqing",
                        })
                        count += 1
                        records_on_page += 1
                        year_count += 1
                    if records_on_page == 0:
                        break
                    if "下一页" not in text:
                        break
                    time.sleep(5.0)
                if year_count > 0:
                    log(f"    杰青 {year}: {year_count} 条")
                time.sleep(3.0)
        except Exception as e:
            log(f"    杰青: {e}")
        finally:
            client.close()
        return count

    # ── IEEE CS Fellows (computer.org) ──────────────────────────

    def _scrape_ieee_cs(self, log=print) -> int:
        """Scrape IEEE Computer Society Fellows from computer.org (2016-2026)."""
        year_pages = [
            (2016, "/press-room/2015-news/cs-fellows-2016"),
            (2017, "/press-room/2016-news/cs-fellows-2017"),
            (2018, "/press-room/2017-news/cs-fellows-2018"),
            (2019, "/press-room/2018-news/ieee-cs-2019-fellows"),
            (2020, "/press-room/2019-news/ieee-computer-society-announces-2020-fellows"),
            (2021, "/press-room/2020-news/ieee-computer-society-announces-2021-fellows"),
            (2022, "/press-room/2021-news/ieee-computer-society-announces-2022-fellows"),
            (2023, "/press-room/2022-news/ieee-computer-society-announces-2023-class-of-fellows"),
            (2024, "/press-room/2024-fellows-announced"),
            (2025, "/press-room/2025-class-fellows"),
            (2026, "/press-room/2026-class-fellows"),
        ]
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        count = 0
        import subprocess
        import codecs
        for year, path in year_pages:
            url = f"https://www.computer.org{path}"
            try:
                result = subprocess.run(
                    ["curl", "-sL", "-H", f"User-Agent: {headers['User-Agent']}",
                     "-H", "Accept: text/html,application/xhtml+xml",
                     "-H", "Accept-Language: en-US,en;q=0.9",
                     "--max-time", "45", url],
                    capture_output=True, timeout=60,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                if len(text) < 1000:
                    log(f"    IEEE CS {year}: 响应过短 ({len(text)} chars)")
                    continue
                chunks = re.findall(
                    r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)',
                    text,
                )
                if not chunks:
                    log(f"    IEEE CS {year}: 无 RSC 数据")
                    continue
                blob = codecs.decode(
                    "".join(chunks).encode(), "unicode_escape"
                )
                new_fmt = re.findall(
                    r'"children":"([^"]+)"}\]," - for ', blob
                )
                if new_fmt:
                    for name in new_fmt:
                        name = name.strip()
                        if not name or len(name) < 3 or len(name) > 60:
                            continue
                        if not re.search(r"[A-Za-z]", name):
                            continue
                        self.add({
                            "name": name,
                            "name_en": name,
                            "country": "国际",
                            "title": "IEEE Fellow",
                            "honors": [f"IEEE Fellow ({year})"],
                            "field": "计算机",
                            "source": "ieee_cs",
                        })
                        count += 1
                else:
                    old_fmt = re.findall(
                        r'"children":"([^"]+?)\u2014([^"]+)"', blob
                    )
                    if not old_fmt:
                        old_fmt = re.findall(
                            r'"children":"([^"]+?)—([^"]+)"', blob
                        )
                    for name, affil in old_fmt:
                        name = name.strip()
                        if not name or len(name) < 3 or len(name) > 60:
                            continue
                        if not re.search(r"[A-Za-z]", name):
                            continue
                        self.add({
                            "name": name,
                            "name_en": name,
                            "affiliation": affil.strip(),
                            "country": "国际",
                            "title": "IEEE Fellow",
                            "honors": [f"IEEE Fellow ({year})"],
                            "field": "计算机",
                            "source": "ieee_cs",
                        })
                        count += 1
            except Exception as e:
                log(f"    IEEE CS {year}: {e}")
        return count


def main():
    """CLI entry: python -m citationclaw.core.scholar_db build"""
    if len(sys.argv) < 2:
        print(f"Usage: python -m citationclaw.core.scholar_db build [source...]")
        print(f"  Sources: cas, cae, aaai, changjiang, jieqing, ieee_cs, all")
        print(f"  DB path: {_get_db_path()}")
        db = ScholarDB()
        print(f"  Current records: {db.count()}")
        db.close()
        return
    cmd = sys.argv[1]
    if cmd == "build":
        sources = sys.argv[2:] if len(sys.argv) > 2 else None
        if sources and "all" in sources:
            sources = None
        db = ScholarDB()
        print(f"数据库: {db.db_path}")
        print(f"当前记录: {db.count()}")
        print(f"开始建库 (sources: {sources or 'all'})...")
        db.build(sources=sources)
        print(f"完成! 总记录: {db.count()}")
        db.close()
    elif cmd == "count":
        db = ScholarDB()
        print(f"数据库: {db.db_path}")
        print(f"总记录: {db.count()}")
        db.close()
    elif cmd == "lookup":
        if len(sys.argv) < 3:
            print("Usage: lookup <name>")
            return
        db = ScholarDB()
        result = db.lookup(sys.argv[2])
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("未找到")
        db.close()
    elif cmd == "export":
        out = sys.argv[2] if len(sys.argv) > 2 else "scholars.json"
        db = ScholarDB()
        rows = db.conn.execute("SELECT * FROM renowned_scholars").fetchall()
        data = [dict(r) for r in rows]
        Path(out).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"导出 {len(data)} 条到 {out}")
        db.close()


if __name__ == "__main__":
    main()
