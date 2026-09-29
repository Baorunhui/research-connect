"""Kaggle/Cornell arXiv 元数据快照：下载 + SQLite FTS5 索引 + 词法粗筛。

综述大规模粗筛主路：Cornell-University/arxiv 官方快照（~250 万篇全量元数据）落在本地，
FTS5 bm25 词法粗筛上万候选（零网络、无限流、可重复），后续由 survey_pipeline 的
本地语义粗排收窄到 rerank 池。快照由 Cornell 持续同步（周级滞后），建议每周
`python scripts/build_kaggle_arxiv_index.py --download` 重建；最新论文由 DeepXiv 路补位。

认证：优先 KAGGLE_API_TOKEN（KGAT 新式单 token，Bearer）；回退 KAGGLE_USERNAME +
KAGGLE_KEY（传统 kaggle.json，Basic）。均只从环境变量读取，不落代码。
"""

from __future__ import annotations

import email.utils
import json
import os
import re
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
KAGGLE_API_BASE_URL = (os.getenv("KAGGLE_API_BASE_URL") or "https://www.kaggle.com").rstrip("/")
KAGGLE_DATASET = "Cornell-University/arxiv"
DEFAULT_DATA_DIR = ROOT_DIR / "archive" / "kaggle_arxiv"
SNAPSHOT_JSON_NAME = "arxiv-metadata-oai-snapshot.json"
_BATCH_ROWS = 50000
# 词法粗筛的查询阶梯：AND 词数逐级放宽（每级匹配行可控，亚秒级），末级 1 兜住
# 「6D位姿估计」这类中英混排主题只提出孤词（如 6d）的场景；
# 宽 OR 在 314 万行上匹配失控（实测 30s/查询），已弃用
_FTS_TERM_LADDER = (12, 6, 3, 2, 1)
_FTS_OR_MAX_TERMS = 24

_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in into is it its of on or that the their there "
    "these this to via was were which while with without how what when where who whose can could "
    "should would may might will shall do does did not no nor but if then than so such about above "
    "after again against all also am any because before below between both during each few more "
    "most other some until up down out off over under only own same too very just we you they he "
    "she his her our your their its my me us them i".split()
)


def _default_log(message: str) -> None:
    print(f"[kaggle-arxiv] {message}", flush=True)


def resolve_default_db_path() -> Path:
    """索引库路径：DPR_SURVEY_KAGGLE_INDEX / KAGGLE_ARXIV_INDEX 可指到任意位置，默认 archive/kaggle_arxiv/。"""
    env_path = str(os.getenv("DPR_SURVEY_KAGGLE_INDEX") or os.getenv("KAGGLE_ARXIV_INDEX") or "").strip()
    return Path(env_path) if env_path else DEFAULT_DATA_DIR / "index.sqlite3"


class KaggleArxivError(RuntimeError):
    """Kaggle 快照下载/建库/检索错误。"""


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #


def _kaggle_auth() -> Tuple[Dict[str, str], Optional[Tuple[str, str]]]:
    """认证形态：KGAT 单 token（Bearer）优先，回退 kaggle.json 的 username/key（Basic）。"""
    token = str(os.getenv("KAGGLE_API_TOKEN") or "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}, None
    username = str(os.getenv("KAGGLE_USERNAME") or "").strip()
    key = str(os.getenv("KAGGLE_KEY") or "").strip()
    if username and key:
        return {}, (username, key)
    raise KaggleArxivError(
        "缺少 Kaggle 凭据：请在 .env 配置 KAGGLE_API_TOKEN（KGAT 单 token）或 "
        "KAGGLE_USERNAME + KAGGLE_KEY（kaggle.json）。免费账号即可，在 Kaggle → Account → "
        "Settings → API 处创建。"
    )


def download_snapshot(dest_dir: Optional[Path] = None, *, log: Callable[[str], None] = _default_log) -> Path:
    """下载官方快照 zip 并解压出 arxiv-metadata-oai-snapshot.json（已存在则直接复用）。"""
    dest_dir = Path(dest_dir) if dest_dir else DEFAULT_DATA_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    json_path = dest_dir / SNAPSHOT_JSON_NAME
    if json_path.exists() and json_path.stat().st_size > 1024 * 1024:
        log(f"快照已存在，跳过下载：{json_path}（{json_path.stat().st_size / 1e9:.1f} GB）")
        return json_path

    headers, basic = _kaggle_auth()
    session = requests.Session()
    # 本机代理会劫持外网请求（trust_env 教训与 DeepXiv 相同）；可用 KAGGLE_TRUST_ENV=1 打开
    session.trust_env = str(os.getenv("KAGGLE_TRUST_ENV") or "").strip().lower() in {"1", "true", "yes"}

    api_url = f"{KAGGLE_API_BASE_URL}/api/v1/datasets/download/{KAGGLE_DATASET}"
    resp = session.get(api_url, headers=headers, auth=basic, allow_redirects=False, timeout=60)
    if resp.status_code == 401:
        raise KaggleArxivError("Kaggle 认证失败（401）：请检查 KAGGLE_API_TOKEN / KAGGLE_USERNAME+KAGGLE_KEY")
    if resp.status_code != 302 or not resp.headers.get("Location"):
        raise KaggleArxivError(f"Kaggle 下载端点异常：HTTP {resp.status_code}")
    signed_url = resp.headers["Location"]

    zip_path = dest_dir / "archive.zip"
    log(f"开始下载快照（~4GB，视带宽 10-30 分钟）：{KAGGLE_DATASET}")
    started = time.time()
    last_note = 0.0
    with session.get(signed_url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(zip_path, "wb") as fh:
            total = 0
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                total += len(chunk)
                if total - last_note >= 512 * 1024 * 1024:
                    last_note = total
                    speed = total / 1e6 / max(time.time() - started, 1)
                    log(f"已下载 {total / 1e9:.1f} GB（{speed:.0f} MB/s）")
    log(f"下载完成：{zip_path.stat().st_size / 1e9:.2f} GB，耗时 {time.time() - started:.0f}s，解压中")

    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".json")]
        target = SNAPSHOT_JSON_NAME if SNAPSHOT_JSON_NAME in names else (names[0] if names else "")
        if not target:
            raise KaggleArxivError(f"快照 zip 内未找到 JSON（成员：{zf.namelist()[:5]}）")
        zf.extract(target, dest_dir)
        if target != SNAPSHOT_JSON_NAME:
            extracted = dest_dir / target
            extracted.rename(json_path)
    zip_path.unlink(missing_ok=True)
    log(f"快照就绪：{json_path}")
    return json_path


# --------------------------------------------------------------------------- #
# 建库
# --------------------------------------------------------------------------- #


def _parse_published(versions: List[Any]) -> str:
    """versions[0].created（RFC2822，如 'Sat, 02 Jan 2021 00:00:00 GMT'）→ 'YYYY-MM-DD'。"""
    try:
        raw = str((versions or [{}])[0].get("created") or "")
        return email.utils.parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return ""


def normalize_title(title: str) -> str:
    """精确标题键：小写、标点→空格、压缩空白（与 CitationClaw arxiv_db 同规则）。"""
    t = str(title or "").lower()
    t = re.sub(r"[^\w\u4e00-\u9fff\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_arxiv_id(arxiv_id: str) -> str:
    """'arXiv:2401.00001v2' / abs URL → '2401.00001'。"""
    aid = str(arxiv_id or "").strip()
    aid = re.sub(r"^(?:https?://)?(?:www\.)?arxiv\.org/(?:abs|pdf)/", "", aid, flags=re.IGNORECASE)
    aid = re.sub(r"^arxiv:", "", aid, flags=re.IGNORECASE)
    aid = re.sub(r"\.pdf$", "", aid, flags=re.IGNORECASE)
    return re.sub(r"v\d+$", "", aid).strip()


def _parsed_author_names(item: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for parts in item.get("authors_parsed") or []:
        if not isinstance(parts, list) or not parts:
            continue
        last = str(parts[0] or "").strip()
        first = str(parts[1] or "").strip() if len(parts) > 1 else ""
        suffix = str(parts[2] or "").strip() if len(parts) > 2 else ""
        name = " ".join(p for p in (first, last, suffix) if p)
        if name:
            names.append(name)
    return names


def iter_snapshot_rows(json_path: Path):
    """流式逐行产出规范化 paper 行（250 万行不吃内存）。

    前 7 列为综述粗筛用的原始列；其后追加单篇 metadata 查询用的
    doi / journal_ref / authors_json / title_norm。
    """
    with open(json_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            arxiv_id = str(item.get("id") or "").strip()
            title = str(item.get("title") or "").strip()
            if not arxiv_id or not title:
                continue
            abstract = str(item.get("abstract") or "").strip().replace("\n", " ")
            yield (
                arxiv_id,
                title,
                abstract,
                str(item.get("authors") or "").strip(),
                str(item.get("categories") or "").strip(),
                _parse_published(item.get("versions") or []),
                str(item.get("update_date") or "").strip(),
                str(item.get("doi") or "").strip().lower(),
                str(item.get("journal-ref") or "").strip(),
                json.dumps(_parsed_author_names(item), ensure_ascii=False),
                normalize_title(title),
            )


def build_index(
    json_path: Path,
    db_path: Optional[Path] = None,
    *,
    log: Callable[[str], None] = _default_log,
    vacuum: bool = True,
) -> Dict[str, Any]:
    """把快照 JSON 建成 SQLite + FTS5 索引（原子替换：先建 .tmp 再 rename）。

    vacuum=False 省掉 VACUUM 的整库临时副本（磁盘吃紧时用；新建库碎片很少）。
    """
    db_path = Path(db_path) if db_path else resolve_default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    tmp_path.unlink(missing_ok=True)

    started = time.time()
    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(
            """
            CREATE TABLE papers (
                arxiv_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                abstract TEXT NOT NULL,
                authors TEXT,
                categories TEXT,
                published TEXT,
                update_date TEXT,
                doi TEXT,
                journal_ref TEXT,
                authors_json TEXT,
                title_norm TEXT
            )
            """
        )
        conn.execute(
            "CREATE VIRTUAL TABLE papers_fts USING fts5("
            "title, abstract, content='papers', content_rowid='rowid')"
        )
        batch: List[Tuple[str, ...]] = []
        rows = 0
        for row in iter_snapshot_rows(json_path):
            batch.append(row)
            if len(batch) >= _BATCH_ROWS:
                conn.executemany(
                    "INSERT OR IGNORE INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
                )
                rows += len(batch)
                batch.clear()
                if rows % 500_000 < _BATCH_ROWS:
                    log(f"已入库 {rows} 行（{time.time() - started:.0f}s）")
        if batch:
            conn.executemany("INSERT OR IGNORE INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
            rows += len(batch)
        log(f"入库完成 {rows} 行，构建 title/doi 查询索引")
        conn.execute("CREATE INDEX idx_papers_title_norm ON papers(title_norm)")
        conn.execute("CREATE INDEX idx_papers_doi ON papers(doi) WHERE doi <> ''")
        log("构建 FTS 索引（数分钟）")
        conn.execute("INSERT INTO papers_fts(papers_fts) VALUES('rebuild')")
        conn.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        for key, value in {
            "schema_version": "2",
            "built_at_utc": datetime.now(timezone.utc).isoformat(),
            "row_count": str(rows),
            "snapshot_json": str(json_path),
            "build_seconds": f"{time.time() - started:.0f}",
        }.items():
            conn.execute("INSERT INTO meta VALUES (?,?)", (key, value))
        conn.commit()
        if vacuum:
            conn.execute("VACUUM")
    finally:
        conn.close()
    os.replace(tmp_path, db_path)
    stats = {"row_count": rows, "db_path": str(db_path), "build_seconds": round(time.time() - started)}
    log(f"索引就绪：{db_path}（{rows} 行，总耗时 {stats['build_seconds']}s）")
    return stats


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9.-]*[a-z0-9]|[a-z0-9]", re.IGNORECASE)


def _extract_terms(text: str) -> List[str]:
    terms = []
    for word in _WORD_RE.findall(str(text or "").lower()):
        word = word.strip(".-")
        if len(word) >= 2 and word not in _STOPWORDS and word not in terms:
            terms.append(word)
    return terms


def extractable_terms(query: str) -> List[str]:
    """对外暴露的可检索词判断：综述 lane 用它区分「无英文词」与「无命中」。"""
    return _extract_terms(query)


def _query_to_fts(text: str) -> Tuple[str, str]:
    """英文查询 → (AND 精确式, OR 宽查式)。逐词双引号包裹，隔离 FTS5 语法字符。

    供调试/测试展示清洗形态；search 内部走递减 AND 阶梯（性能原因）。
    """
    terms = _extract_terms(text)[:_FTS_OR_MAX_TERMS]
    if not terms:
        return "", ""
    quoted = [f'"{t}"' for t in terms]
    return " ".join(quoted[:_FTS_TERM_LADDER[0]]), " OR ".join(quoted)


class KaggleArxivIndex:
    """只读检索接口：FTS5 bm25 词法粗筛 + 日期/类别过滤，top_k 支持上万。"""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else resolve_default_db_path()
        if not self.db_path.exists():
            raise KaggleArxivError(f"Kaggle 索引不存在：{self.db_path}")
        uri = f"file:{quote(self.db_path.as_posix())}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "KaggleArxivIndex":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def meta(self) -> Dict[str, str]:
        try:
            rows = self._conn.execute("SELECT key, value FROM meta").fetchall()
            return {str(k): str(v) for k, v in rows}
        except sqlite3.Error:
            return {}

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])

    def _run_fts(
        self,
        fts_query: str,
        *,
        top_k: int,
        date_start: str,
        date_end: str,
        categories: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        cat_clause = ""
        params: Dict[str, Any] = {"fts": fts_query, "ds": date_start, "de": date_end, "top": int(top_k)}
        cats = [str(c).strip() for c in (categories or []) if str(c).strip()]
        if cats:
            cat_clause = "AND (" + " OR ".join(["(' ' || p.categories || ' ') LIKE :cat%d" % i for i in range(len(cats))]) + ")"
            for i, cat in enumerate(cats):
                params[f"cat{i}"] = f"% {cat} %"
        sql = (
            "SELECT p.arxiv_id, p.title, p.abstract, p.authors, p.categories, p.published, "
            "bm25(papers_fts, 10.0, 1.0) AS rank_score "
            "FROM papers_fts JOIN papers AS p ON p.rowid = papers_fts.rowid "
            "WHERE papers_fts MATCH :fts "
            "AND (:ds = '' OR p.published >= :ds) AND (:de = '' OR p.published <= :de) "
            f"{cat_clause} "
            "ORDER BY rank_score LIMIT :top"
        )
        out: List[Dict[str, Any]] = []
        for arxiv_id, title, abstract, authors, cats_raw, published, score in self._conn.execute(sql, params):
            out.append(
                {
                    "paper_id": arxiv_id,
                    "title": title,
                    "abstract": abstract,
                    "authors": [authors] if authors else [],
                    "published": published,
                    "link": f"https://arxiv.org/abs/{arxiv_id}",
                    "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
                    "source": "kaggle",
                    "citation_count": 0,
                    "bm25_score": round(float(score), 4),
                }
            )
        return out

    def search(
        self,
        query: str,
        *,
        top_k: int = 200,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        categories: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """递减 AND 阶梯：12→6→3→2 词逐级放宽，命中达 top_k 一半即收。

        不用宽 OR 兜底：24 词 OR 在 314 万行上匹配行数失控，实测 30 秒/查询；
        每级 AND 匹配行可控（亚秒级），逐级放宽同样能捞到稀疏领域的候选。
        bm25() 越小越相关（ORDER BY 升序）。输出与 survey_pipeline._paper_to_dict 同构。
        """
        terms = _extract_terms(query)[:_FTS_OR_MAX_TERMS]
        if not terms:
            return []
        ds = str(date_start or "").strip()[:10]
        de = str(date_end or "").strip()[:10]
        best: List[Dict[str, Any]] = []
        for size in _FTS_TERM_LADDER:
            if size > len(terms):
                continue
            fts_query = " ".join(f'"{t}"' for t in terms[:size])
            hits = self._run_fts(fts_query, top_k=top_k, date_start=ds, date_end=de, categories=categories)
            if len(hits) > len(best):
                best = hits
            if len(hits) >= max(int(top_k) // 2, 1):
                return best
        # 终极兜底：前 2 词 OR。仅当各级 AND 全空（词形变化如 robot/robots、
        # 极小候选域）时触发；真实全量库罕见，一次性代价可接受。
        if not best and len(terms) >= 2:
            or_query = " OR ".join(f'"{t}"' for t in terms[:2])
            best = self._run_fts(or_query, top_k=top_k, date_start=ds, date_end=de, categories=categories)
        return best


def is_kaggle_ready() -> Tuple[bool, str]:
    """探测本地索引可用性；不可用时返回用户可读的构建指引。"""
    db_path = resolve_default_db_path()
    if not db_path.exists():
        return False, (
            "未检测到 Kaggle arXiv 快照索引（" + str(db_path) + "），Kaggle 粗筛路已跳过；"
            "构建方式：python scripts/build_kaggle_arxiv_index.py --download（首次需下载 ~4GB）"
        )
    return True, ""


# --------------------------------------------------------------------------- #
# 单篇 metadata 精确查询（列表/详情/对话前的论文元信息优先走本地快照）
# --------------------------------------------------------------------------- #

_LOOKUP_COLS = (
    "arxiv_id, title, abstract, authors, categories, published, update_date, "
    "doi, journal_ref, authors_json"
)
_lookup_lock = threading.Lock()
_lookup_conn: Optional[sqlite3.Connection] = None
_lookup_path: Optional[Path] = None
_lookup_ok: Optional[bool] = None


def _lookup_connection() -> Optional[sqlite3.Connection]:
    """懒打开只读连接；索引缺失或是旧 schema（无 title_norm/doi）时返回 None。"""
    global _lookup_conn, _lookup_path, _lookup_ok
    db_path = resolve_default_db_path()
    if _lookup_path != db_path:
        _lookup_conn, _lookup_path, _lookup_ok = None, db_path, None
    if _lookup_ok is None:
        _lookup_ok = False
        if db_path.is_file():
            try:
                uri = f"file:{quote(db_path.as_posix())}?mode=ro&immutable=1"
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
                if {"title_norm", "doi", "journal_ref", "authors_json"} <= cols:
                    _lookup_conn, _lookup_ok = conn, True
                else:
                    conn.close()
                    _default_log(f"索引缺少 title_norm/doi 列，单篇查询停用（需重建）：{db_path}")
            except sqlite3.Error as exc:
                _default_log(f"打开索引失败 {db_path}: {exc}")
    return _lookup_conn if _lookup_ok else None


def _lookup_row_to_meta(row: Tuple[Any, ...]) -> Dict[str, Any]:
    (aid, title, abstract, authors_raw, categories, published, update_date,
     doi, journal_ref, authors_json) = row
    try:
        authors = json.loads(authors_json or "[]")
    except json.JSONDecodeError:
        authors = []
    if not authors and authors_raw:
        authors = [a.strip() for a in re.split(r",\s*|\s+and\s+", authors_raw) if a.strip()]
    return {
        "paper_id": aid,
        "arxiv_id": aid,
        "title": " ".join(str(title or "").split()),
        "abstract": " ".join(str(abstract or "").split()),
        "authors": authors,
        "published": published or update_date or "",
        "categories": categories or "",
        "doi": doi or "",
        "venue": journal_ref or "",
        "link": f"https://arxiv.org/abs/{aid}",
        "pdf_url": f"https://arxiv.org/pdf/{aid}",
        "metadata_source": "kaggle_arxiv",
    }


def lookup_paper_meta(arxiv_id: str = "", *, title: str = "", doi: str = "") -> Optional[Dict[str, Any]]:
    """本地快照精确查询：arxiv_id → doi → 规范化标题。未命中/索引不可用返回 None。"""
    with _lookup_lock:
        conn = _lookup_connection()
        if conn is None:
            return None
        row = None
        aid = normalize_arxiv_id(arxiv_id)
        if aid:
            row = conn.execute(f"SELECT {_LOOKUP_COLS} FROM papers WHERE arxiv_id = ?", (aid,)).fetchone()
        d = str(doi or "").strip().lower()
        if row is None and d:
            row = conn.execute(
                f"SELECT {_LOOKUP_COLS} FROM papers WHERE doi = ? AND doi <> '' LIMIT 1", (d,)
            ).fetchone()
        norm = normalize_title(title)
        if row is None and len(norm) >= 8:
            row = conn.execute(
                f"SELECT {_LOOKUP_COLS} FROM papers WHERE title_norm = ? ORDER BY published DESC LIMIT 1",
                (norm,),
            ).fetchone()
    return _lookup_row_to_meta(row) if row else None
