"""Local honor list (academicians, Fellows, 杰青 ...) with name + affiliation/email matching.

The list lives outside git in a SQLite file (Docker: ``/honor-list/honors.sqlite3``,
``$CITATIONCLAW_HONOR_DB``) and is mounted read-only. A hit always requires the
name key to match AND at least one of:

  * email      the author's full address equals a listed address
  * email域名   the author's email domain equals a listed domain, or names the
               listed institution (``cs.tsinghua.edu.cn`` → Tsinghua)
  * 单位        normalized affiliations match exactly, or one is a contiguous
               token run of the other and that run carries a distinctive token

Name-only matches are counted as candidates but never reported as hits, so two
people sharing a name at different institutions are not confused.

Build (host side, needs network for scraped sources; pypinyin adds pinyin keys
for Chinese-only records):

    PYTHONPATH=packages/research-connect-core/src:modules/citationclaw \\
    python -m citationclaw.core.honor_list build --db deploy/tool/state/honor-list/honors.sqlite3 \\
        --wikidata-csv /path/to/scholars.csv --sources aaai changjiang ieee_cs

    python -m citationclaw.core.honor_list stats --db ...
    python -m citationclaw.core.honor_list match --db ... "Wei Wang" "Tsinghua University"
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Iterable, List, Optional

ENV_DB = "CITATIONCLAW_HONOR_DB"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS honorees (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    name_zh        TEXT DEFAULT '',
    honor          TEXT NOT NULL,
    source         TEXT NOT NULL,
    affiliation    TEXT DEFAULT '',
    affiliation_zh TEXT DEFAULT '',
    email          TEXT DEFAULT '',
    email_domain   TEXT DEFAULT '',
    source_ref     TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS name_keys (
    key        TEXT NOT NULL,
    honoree_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_name_keys ON name_keys(key);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# ── normalization ────────────────────────────────────────────────────────

_STOP = {"of", "the", "and", "at", "for", "de", "du", "der", "la", "le", "in", "on"}
_GENERIC = {
    "university", "univ", "universite", "universitat", "universidad", "institute",
    "inst", "college", "school", "department", "dept", "faculty", "lab", "labs",
    "laboratory", "laboratories", "key", "state", "national", "center", "centre",
    "research", "science", "sciences", "technology", "technologies", "tech",
    "engineering", "academy", "graduate", "division", "group", "computer",
    "computing", "information", "electrical", "electronic", "electronics",
    "mathematics", "physics", "chemistry", "biology", "medical", "medicine",
    "artificial", "intelligence", "ai", "cs", "ee", "inc", "ltd", "co", "corp",
    "corporation", "company", "foundation",
}
_LOCATION = {
    "china", "beijing", "shanghai", "hong", "kong", "hongkong", "taiwan", "usa",
    "us", "uk", "united", "states", "kingdom", "america", "california", "new",
    "york", "texas", "japan", "tokyo", "korea", "seoul", "singapore", "germany",
    "france", "paris", "london", "canada", "toronto", "australia", "sydney",
    "india", "italy", "spain", "netherlands", "switzerland", "zurich", "israel",
    "nanjing", "hangzhou", "wuhan", "guangzhou", "shenzhen", "tianjin", "xian",
    "chengdu", "hefei", "harbin", "north", "south", "east", "west", "central",
    "southern", "northern", "eastern", "western", "pr", "prc",
}
_ABBREV = {
    "mit": "massachusetts institute of technology",
    "cmu": "carnegie mellon university",
    "uc": "university of california",
    "ucb": "university of california berkeley",
    "ucla": "university of california los angeles",
    "ucsd": "university of california san diego",
    "uiuc": "university of illinois urbana champaign",
    "usc": "university of southern california",
    "nyu": "new york university",
    "upenn": "university of pennsylvania",
    "gatech": "georgia institute of technology",
    "caltech": "california institute of technology",
    "ucl": "university college london",
    "eth": "eth zurich",
    "epfl": "ecole polytechnique federale de lausanne",
    "kaist": "korea advanced institute of science and technology",
    "nus": "national university of singapore",
    "ntu": "nanyang technological university",
    "hku": "university of hong kong",
    "cuhk": "chinese university of hong kong",
    "hkust": "hong kong university of science and technology",
    "thu": "tsinghua university",
    "pku": "peking university",
    "zju": "zhejiang university",
    "sjtu": "shanghai jiao tong university",
    "ustc": "university of science and technology of china",
    "nju": "nanjing university",
    "hit": "harbin institute of technology",
    "buaa": "beihang university",
    "hust": "huazhong university of science and technology",
    "whu": "wuhan university",
    "xjtu": "xi an jiaotong university",
    "uestc": "university of electronic science and technology of china",
    "bupt": "beijing university of posts and telecommunications",
    "nudt": "national university of defense technology",
    "sustech": "southern university of science and technology",
    "ucas": "university of chinese academy of sciences",
    "cas": "chinese academy of sciences",
    "msra": "microsoft research asia",
    "fair": "facebook ai research",
}
_WORD_CANON = {
    "univ": "university", "uni": "university", "universite": "university",
    "universitat": "university", "universidad": "university", "universita": "university",
    "inst": "institute", "tech": "technology", "technol": "technology",
    "dept": "department", "lab": "laboratory", "labs": "laboratory",
    "laboratories": "laboratory", "sci": "science", "natl": "national",
    "acad": "academy", "coll": "college", "eng": "engineering", "engn": "engineering",
    "ctr": "center", "centre": "center",
}
# Chinese institution names → English, matched as substrings (longest first).
_ZH_AFFIL = {
    "清华大学": "tsinghua university", "北京大学": "peking university",
    "浙江大学": "zhejiang university", "上海交通大学": "shanghai jiao tong university",
    "复旦大学": "fudan university", "中国科学技术大学": "university of science and technology of china",
    "南京大学": "nanjing university", "哈尔滨工业大学": "harbin institute of technology",
    "西安交通大学": "xi an jiaotong university", "华中科技大学": "huazhong university of science and technology",
    "武汉大学": "wuhan university", "中山大学": "sun yat sen university",
    "北京航空航天大学": "beihang university", "同济大学": "tongji university",
    "南开大学": "nankai university", "天津大学": "tianjin university",
    "东南大学": "southeast university", "厦门大学": "xiamen university",
    "山东大学": "shandong university", "四川大学": "sichuan university",
    "电子科技大学": "university of electronic science and technology of china",
    "北京理工大学": "beijing institute of technology", "中国人民大学": "renmin university of china",
    "中国科学院大学": "university of chinese academy of sciences",
    "中国科学院自动化研究所": "institute of automation chinese academy of sciences",
    "中国科学院计算技术研究所": "institute of computing technology chinese academy of sciences",
    "中国科学院": "chinese academy of sciences", "西北工业大学": "northwestern polytechnical university",
    "大连理工大学": "dalian university of technology", "吉林大学": "jilin university",
    "湖南大学": "hunan university", "中南大学": "central south university",
    "华南理工大学": "south china university of technology",
    "国防科技大学": "national university of defense technology",
    "国防科学技术大学": "national university of defense technology",
    "北京邮电大学": "beijing university of posts and telecommunications",
    "西安电子科技大学": "xidian university", "香港大学": "university of hong kong",
    "香港中文大学": "chinese university of hong kong",
    "香港科技大学": "hong kong university of science and technology",
    "南方科技大学": "southern university of science and technology",
    "上海科技大学": "shanghaitech university", "北京师范大学": "beijing normal university",
    "华东师范大学": "east china normal university", "苏州大学": "soochow university",
    "深圳大学": "shenzhen university", "重庆大学": "chongqing university",
    "兰州大学": "lanzhou university", "东北大学": "northeastern university",
    "北京交通大学": "beijing jiaotong university", "北京科技大学": "university of science and technology beijing",
    "华东理工大学": "east china university of science and technology",
    "中国农业大学": "china agricultural university", "北京工业大学": "beijing university of technology",
    "南京航空航天大学": "nanjing university of aeronautics and astronautics",
    "南京理工大学": "nanjing university of science and technology",
    "微软亚洲研究院": "microsoft research asia", "阿里巴巴": "alibaba", "腾讯": "tencent",
    "华为": "huawei", "百度": "baidu", "字节跳动": "bytedance",
}
_ZH_KEYS = sorted(_ZH_AFFIL, key=len, reverse=True)
_EMAIL_GENERIC = {
    "edu", "ac", "com", "org", "net", "gov", "cn", "uk", "us", "jp", "kr", "de",
    "fr", "sg", "hk", "tw", "ca", "au", "in", "it", "ch", "nl", "il", "mail",
    "email", "www", "cs", "ee", "ece", "eecs", "math", "stu", "student", "alumni",
    "gmail", "outlook", "hotmail", "qq", "163", "126", "yahoo", "foxmail", "live",
    "icloud", "sina", "mails", "connect", "my", "staff", "ieee", "acm",
}
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _ascii_fold(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def name_key(name: str) -> str:
    """Order-insensitive key for a person name; '' when too weak to match on.

    Initials are dropped and hyphens/apostrophes joined, so "Kai-Ming He",
    "He, Kaiming" and "Kaiming He" share ``"he kaiming"``. Chinese-script names
    get a ``zh:`` key on the raw characters.
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    if _CJK_RE.search(raw) and not re.search(r"[A-Za-z]", raw):
        return "zh:" + re.sub(r"[\s·•・]", "", raw)
    text = _ascii_fold(raw).lower()
    text = re.sub(r"[\-'’`´]", "", text)
    text = re.sub(r"[^a-z\s]", " ", text)
    tokens = [t for t in text.split() if len(t) > 1]
    if len(tokens) < 2:
        return ""
    return " ".join(sorted(tokens))


def affiliation_tokens(text: str) -> List[str]:
    """Normalized token list: CJK institutions translated, abbreviations expanded."""
    raw = str(text or "")
    extra = []
    for zh in _ZH_KEYS:
        if zh in raw:
            extra.append(_ZH_AFFIL[zh])
            raw = raw.replace(zh, " ")
    text = _ascii_fold(" ".join([raw] + extra)).lower().replace("&", " and ")
    text = re.sub(r"[\-'’`´.]", "", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    out: List[str] = []
    for tok in text.split():
        tok = _WORD_CANON.get(tok, tok)
        if tok in _ABBREV:
            out.extend(t for t in _ABBREV[tok].split() if t not in _STOP)
        elif tok not in _STOP:
            out.append(tok)
    return out


def _contains_run(longer: List[str], shorter: List[str]) -> bool:
    n = len(shorter)
    return any(longer[i:i + n] == shorter for i in range(len(longer) - n + 1))


def _distinctive(tokens: Iterable[str]) -> bool:
    return any(t not in _GENERIC and t not in _LOCATION and not t.isdigit() for t in tokens)


def affiliation_match(a: str, b: str) -> bool:
    ta, tb = affiliation_tokens(a), affiliation_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not _contains_run(longer, shorter):
        return False
    return _distinctive(shorter) or len(shorter) >= 3


def email_domain_labels(domain: str) -> List[str]:
    labels = [l for l in str(domain or "").lower().split(".") if l]
    return [l for l in labels if l not in _EMAIL_GENERIC and not l.isdigit()]


def email_matches_affiliation(email: str, affiliation: str) -> bool:
    """``x@cs.tsinghua.edu.cn`` names Tsinghua; ``mit.edu`` expands to MIT."""
    domain = email.rsplit("@", 1)[-1] if "@" in email else email
    aff = affiliation_tokens(affiliation)
    if not aff:
        return False
    for label in email_domain_labels(domain):
        expanded = [t for t in _ABBREV.get(label, label).split() if t not in _STOP]
        if _distinctive(expanded) and _contains_run(aff, expanded):
            return True
    return False


def split_affiliations(text: str) -> List[str]:
    return [p.strip() for p in re.split(r"[;；\n]", str(text or "")) if p.strip()]


# ── lookup ───────────────────────────────────────────────────────────────

class HonorList:
    """Read-only matcher over ``honors.sqlite3``."""

    def __init__(self, db_path: Optional[Path] = None):
        env = os.getenv(ENV_DB, "").strip()
        self.db_path = Path(db_path) if db_path else (Path(env) if env else None)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.db_path and self.db_path.is_file())

    def _db(self) -> Optional[sqlite3.Connection]:
        if self._conn is None and self.available:
            uri = f"file:{self.db_path}?mode=ro&immutable=1"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def stats(self) -> dict:
        db = self._db()
        if db is None:
            return {"available": False, "path": str(self.db_path or ""), "total": 0, "sources": {}}
        with self._lock:
            total = db.execute("SELECT COUNT(*) FROM honorees").fetchone()[0]
            sources = {r[0]: r[1] for r in db.execute(
                "SELECT source, COUNT(*) FROM honorees GROUP BY source")}
            with_aff = db.execute(
                "SELECT COUNT(*) FROM honorees WHERE affiliation <> '' OR affiliation_zh <> ''"
            ).fetchone()[0]
            with_email = db.execute(
                "SELECT COUNT(*) FROM honorees WHERE email <> '' OR email_domain <> ''"
            ).fetchone()[0]
            meta = {r[0]: r[1] for r in db.execute("SELECT key, value FROM meta")}
        return {"available": True, "path": str(self.db_path), "total": total,
                "sources": sources, "with_affiliation": with_aff,
                "with_email": with_email, "built_at": meta.get("built_at", "")}

    def candidates(self, name: str) -> List[dict]:
        key = name_key(name)
        db = self._db()
        if not key or db is None:
            return []
        with self._lock:
            rows = db.execute(
                "SELECT h.* FROM name_keys k JOIN honorees h ON h.id = k.honoree_id "
                "WHERE k.key = ?", (key,)).fetchall()
        return [dict(r) for r in rows]

    def match(self, name: str, affiliation: str = "", email: str = "") -> dict:
        """Return ``{"hits": [...], "name_only": n}`` for one author.

        Each hit: name, honor, source, basis (邮箱/邮箱域名/单位), matched_value,
        listed_affiliation.
        """
        cands = self.candidates(name)
        if not cands:
            return {"hits": [], "name_only": 0}
        email = str(email or "").strip().lower()
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        author_affs = split_affiliations(affiliation)
        hits, name_only = [], 0
        for c in cands:
            listed_affs = split_affiliations(c.get("affiliation")) + split_affiliations(c.get("affiliation_zh"))
            basis = value = listed = ""
            if email and c.get("email") and email == c["email"].lower():
                basis, value = "邮箱", email
            elif domain and c.get("email_domain") and (
                    domain == c["email_domain"].lower() or domain.endswith("." + c["email_domain"].lower())):
                basis, value = "邮箱域名", domain
            else:
                for la in listed_affs:
                    if domain and email_matches_affiliation(email, la):
                        basis, value, listed = "邮箱域名", domain, la
                        break
                    hit_aff = next((aa for aa in author_affs if affiliation_match(aa, la)), "")
                    if hit_aff:
                        basis, value, listed = "单位", hit_aff, la
                        break
            if basis:
                hits.append({
                    "name": c["name"], "name_zh": c.get("name_zh", ""), "honor": c["honor"],
                    "source": c["source"], "basis": basis, "matched_value": value,
                    "listed_affiliation": listed or c.get("affiliation", ""),
                })
            else:
                name_only += 1
        return {"hits": hits, "name_only": name_only}

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None


_shared: Optional[HonorList] = None
_shared_lock = threading.Lock()


def get_honor_list() -> HonorList:
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = HonorList()
        return _shared


# ── build ────────────────────────────────────────────────────────────────

_COMPOUND_SURNAMES = {"欧阳", "司马", "诸葛", "上官", "皇甫", "东方", "慕容", "夏侯",
                      "公孙", "令狐", "长孙", "宇文", "尉迟", "司徒", "澹台", "端木"}
_SURNAME_PINYIN = {"曾": "zeng", "单": "shan", "解": "xie", "仇": "qiu", "区": "ou",
                   "查": "zha", "翟": "zhai", "覃": "qin", "盖": "ge", "朴": "piao",
                   "乐": "yue", "缪": "miao", "尉": "yu", "长": "zhang", "重": "chong",
                   "种": "chong", "秘": "bi", "万俟": "moqi"}


def pinyin_name_keys(name_zh: str) -> List[str]:
    """Pinyin keys for a Chinese name (needs pypinyin; build-time only)."""
    name = re.sub(r"[\s·•・]", "", str(name_zh or ""))
    if not (2 <= len(name) <= 4) or not all(_CJK_RE.match(c) for c in name):
        return []
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return []
    split = 2 if name[:2] in _COMPOUND_SURNAMES and len(name) > 2 else 1
    surname, given = name[:split], name[split:]
    family = _SURNAME_PINYIN.get(surname) or "".join(lazy_pinyin(surname))
    given_py = "".join(lazy_pinyin(given))
    key = name_key(f"{given_py} {family}")
    return [key] if key else []


class _Builder:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.tmp = db_path.with_suffix(db_path.suffix + ".tmp")
        self.tmp.unlink(missing_ok=True)
        self.conn = sqlite3.connect(str(self.tmp))
        self.conn.executescript(_SCHEMA)
        self._seen: set = set()

    def add(self, *, name: str, honor: str, source: str, name_zh: str = "",
            affiliation: str = "", affiliation_zh: str = "", email: str = "",
            email_domain: str = "", source_ref: str = "") -> bool:
        name, name_zh = (name or "").strip(), (name_zh or "").strip()
        if not name and not name_zh:
            return False
        dedup = (name.lower(), name_zh, honor, source, affiliation.strip().lower())
        if dedup in self._seen:
            return False
        self._seen.add(dedup)
        if email and not email_domain and "@" in email:
            email_domain = email.rsplit("@", 1)[-1]
        cur = self.conn.execute(
            "INSERT INTO honorees (name, name_zh, honor, source, affiliation, affiliation_zh,"
            " email, email_domain, source_ref) VALUES (?,?,?,?,?,?,?,?,?)",
            (name or name_zh, name_zh, honor, source, affiliation.strip(),
             affiliation_zh.strip(), email.strip().lower(), email_domain.strip().lower(), source_ref))
        hid = cur.lastrowid
        keys = {k for k in (name_key(name), name_key(name_zh)) if k}
        for zh in (name_zh, name if _CJK_RE.search(name) else ""):
            keys.update(pinyin_name_keys(zh))
        self.conn.executemany("INSERT INTO name_keys (key, honoree_id) VALUES (?, ?)",
                              [(k, hid) for k in keys])
        return True

    def finish(self, notes: dict):
        self.conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", [
            ("built_at", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("notes", json.dumps(notes, ensure_ascii=False)),
        ])
        self.conn.commit()
        self.conn.close()
        os.replace(self.tmp, self.db_path)


def _load_wikidata_csv(builder: _Builder, csv_path: Path, log=print) -> int:
    """Wikidata-derived table: title_zh,name_en,name_zh,affiliation_en,affiliation_zh."""
    count = 0
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if builder.add(name=row.get("name_en", ""), name_zh=row.get("name_zh", ""),
                           honor=row.get("title_zh", "").strip() or "unknown",
                           source="wikidata", affiliation=row.get("affiliation_en", ""),
                           affiliation_zh=row.get("affiliation_zh", ""),
                           source_ref=str(csv_path.name)):
                count += 1
    log(f"  [wikidata] {count} 条 ← {csv_path}")
    return count


def _load_scraped(builder: _Builder, sources: List[str], log=print) -> dict:
    """Reuse ScholarDB's public-page scrapers, keeping every (name, affiliation) row."""
    from citationclaw.core.scholar_db import ScholarDB

    counts: dict = {}

    class _Collector(ScholarDB):
        current = ""

        def add(self, scholar: dict) -> bool:  # type: ignore[override]
            name = scholar.get("name_en") or scholar.get("name", "")
            is_zh = bool(_CJK_RE.search(scholar.get("name", "")))
            aff = scholar.get("affiliation", "")
            ok = builder.add(
                name=name, name_zh=scholar.get("name", "") if is_zh else "",
                honor=scholar.get("title") or ", ".join(scholar.get("honors") or []) or self.current,
                source=self.current,
                affiliation="" if _CJK_RE.search(aff) else aff,
                affiliation_zh=aff if _CJK_RE.search(aff) else "",
            )
            counts[self.current] = counts.get(self.current, 0) + int(ok)
            return ok

    collector = _Collector(db_path=Path("/nonexistent"))
    for src in sources:
        collector.current = src
        fn = getattr(collector, f"_scrape_{src}", None)
        if fn is None:
            log(f"  [{src}] 未知来源，跳过")
            continue
        try:
            fn(log=log)
        except Exception as e:  # noqa: BLE001 - report and continue with other sources
            log(f"  [{src}] 失败: {e}")
        log(f"  [{src}] {counts.get(src, 0)} 条")
    return counts


def build(db_path: Path, wikidata_csv: Optional[Path], sources: List[str], log=print) -> dict:
    builder = _Builder(db_path)
    notes: dict = {}
    if wikidata_csv:
        notes["wikidata"] = _load_wikidata_csv(builder, wikidata_csv, log=log)
    if sources:
        notes.update(_load_scraped(builder, sources, log=log))
    builder.finish(notes)
    log(f"完成: {db_path} {notes}")
    return notes


def main(argv: Optional[List[str]] = None):
    ap = argparse.ArgumentParser(prog="python -m citationclaw.core.honor_list")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--db", required=True, type=Path)
    b.add_argument("--wikidata-csv", type=Path)
    b.add_argument("--sources", nargs="*", default=[],
                   help="ScholarDB scrapers: aaai changjiang ieee_cs jieqing cas cae")
    s = sub.add_parser("stats")
    s.add_argument("--db", type=Path)
    m = sub.add_parser("match")
    m.add_argument("--db", type=Path)
    m.add_argument("name")
    m.add_argument("affiliation", nargs="?", default="")
    m.add_argument("--email", default="")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        build(args.db, args.wikidata_csv, args.sources)
    elif args.cmd == "stats":
        print(json.dumps(HonorList(args.db).stats(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(HonorList(args.db).match(args.name, args.affiliation, args.email),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
