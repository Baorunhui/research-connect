#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
图表抽取方案对照评估：现有 caption-render 路径 vs MinerU 云端 API。

对指定文件夹下所有 PDF 各跑两路，输出对比报告：
  - figure / table 数量
  - caption 命中率（有非空 caption 的占比）
  - 表格结构化成功率（有 table_html 的占比，仅 MinerU）
  - 单篇耗时

抽出的图片分别存到 compare_output/<extractor>/<pdf_stem>/{figures,tables}/。

用法（从仓库根目录）：
    python scripts/compare_figure_extractors.py "C:\\Users\\zykj\\Desktop\\论文\\ltsf"
    python scripts/compare_figure_extractors.py "C:\\path\\to\\pdfs" --output compare_output

前置条件：在 .env 中配置 MINERU_API_TOKEN（在 https://mineru.net/apiManage 生成）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from local_env import load_local_env  # noqa: E402

load_local_env()


def _stem(path: str) -> str:
    return Path(path).stem


def _caption_hit_rate(items: List[Dict[str, Any]]) -> float:
    if not items:
        return 0.0
    hit = sum(1 for it in items if str(it.get("caption") or "").strip())
    return hit / len(items)


def _table_structured_rate(items: List[Dict[str, Any]]) -> float:
    if not items:
        return 0.0
    hit = sum(1 for it in items if str(it.get("table_html") or "").strip())
    return hit / len(items)


def run_caption_render(
    pdf_path: str,
    output_dir: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float]:
    """跑现有 caption-render 主路径，返回 (figures, tables, elapsed_seconds)。"""
    import paper_figures

    fig_dir = os.path.join(output_dir, "figures")
    tbl_dir = os.path.join(output_dir, "tables")
    os.makedirs(fig_dir, exist_ok=True)
    os.makedirs(tbl_dir, exist_ok=True)

    start = time.time()
    # 直接调内部函数，和 ensure_paper_media 的主路径一致
    figs, tbls = paper_figures._extract_media_with_caption_render(
        pdf_path,
        fig_dir,
        "figures",
        tbl_dir,
        "tables",
    )
    elapsed = time.time() - start
    return figs, tbls, elapsed


def run_mineru_batch(
    pdf_paths: List[str],
    output_root: str,
) -> Dict[str, Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float, str]]:
    """批量提交所有 PDF 到 MinerU，返回 {pdf_path: (figures, tables, elapsed, status)}。"""
    from mineru_api import MinerUClient, parse_content_list

    out: Dict[str, Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float, str]] = {}
    try:
        client = MinerUClient()
    except Exception as e:
        print(f"[ERROR] 无法初始化 MinerU 客户端：{e}", flush=True)
        for p in pdf_paths:
            out[p] = ([], [], 0.0, f"init_failed: {e}")
        return out

    start = time.time()
    try:
        batch_id = client.submit_batch(pdf_paths)
        results = client.poll_batch(batch_id)
    except Exception as e:
        print(f"[ERROR] MinerU 批量提交/轮询失败：{e}", flush=True)
        for p in pdf_paths:
            out[p] = ([], [], time.time() - start, f"submit_failed: {e}")
        return out

    # 用 file_name 匹配回原 PDF
    result_by_name = {str(r.get("file_name") or ""): r for r in results}

    for pdf_path in pdf_paths:
        name = os.path.basename(pdf_path)
        result = result_by_name.get(name)
        if not result:
            out[pdf_path] = ([], [], 0.0, "no_result")
            continue
        state = str(result.get("state") or "")
        if state != "done":
            out[pdf_path] = ([], [], 0.0, f"state={state} err={result.get('err_msg')}")
            continue
        zip_url = str(result.get("full_zip_url") or "").strip()
        if not zip_url:
            out[pdf_path] = ([], [], 0.0, "no_zip_url")
            continue

        pdf_out = os.path.join(output_root, _stem(pdf_path))
        extract_dir = os.path.join(pdf_out, "_mineru_raw")
        try:
            doc_dir = client.download_zip(zip_url, extract_dir)
            content_list_path = client.find_content_list(doc_dir)
            if not content_list_path:
                out[pdf_path] = ([], [], 0.0, "no_content_list")
                continue
            figs, tbls = parse_content_list(
                content_list_path,
                doc_dir=doc_dir,
                output_dir=os.path.join(pdf_out, "figures"),
                relative_prefix="figures",
                table_output_dir=os.path.join(pdf_out, "tables"),
                table_relative_prefix="tables",
            )
            out[pdf_path] = (figs, tbls, time.time() - start, "ok")
        except Exception as e:
            out[pdf_path] = ([], [], 0.0, f"download_failed: {e}")

    return out


def build_report(
    pdf_paths: List[str],
    local_results: Dict[str, Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float]],
    mineru_results: Dict[str, Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float, str]],
) -> Dict[str, Any]:
    per_pdf: List[Dict[str, Any]] = []
    for pdf_path in pdf_paths:
        stem = _stem(pdf_path)
        l_figs, l_tbls, l_time = local_results.get(pdf_path, ([], [], 0.0))
        m_tuple = mineru_results.get(pdf_path, ([], [], 0.0, "missing"))
        m_figs, m_tbls, m_time, m_status = m_tuple[0], m_tuple[1], m_tuple[2], m_tuple[3]

        per_pdf.append(
            {
                "pdf": stem,
                "caption_render": {
                    "figures": len(l_figs),
                    "tables": len(l_tbls),
                    "caption_hit": round(_caption_hit_rate(l_figs + l_tbls), 3),
                    "seconds": round(l_time, 2),
                },
                "mineru": {
                    "figures": len(m_figs),
                    "tables": len(m_tbls),
                    "caption_hit": round(_caption_hit_rate(m_figs + m_tbls), 3),
                    "table_structured": round(_table_structured_rate(m_tbls), 3),
                    "seconds": round(m_time, 2),
                    "status": m_status,
                },
            }
        )

    total_l_figs = sum(p["caption_render"]["figures"] for p in per_pdf)
    total_l_tbls = sum(p["caption_render"]["tables"] for p in per_pdf)
    total_m_figs = sum(p["mineru"]["figures"] for p in per_pdf)
    total_m_tbls = sum(p["mineru"]["tables"] for p in per_pdf)
    l_caption_hits = sum(
        1
        for pdf_path in pdf_paths
        for it in (local_results.get(pdf_path, ([], [], 0.0))[0] + local_results.get(pdf_path, ([], [], 0.0))[1])
        if str(it.get("caption") or "").strip()
    )
    m_caption_hits = sum(
        1
        for pdf_path in pdf_paths
        for it in (mineru_results.get(pdf_path, ([], [], 0.0, ""))[0] + mineru_results.get(pdf_path, ([], [], 0.0, ""))[1])
        if str(it.get("caption") or "").strip()
    )
    m_structured = sum(
        1
        for pdf_path in pdf_paths
        for it in mineru_results.get(pdf_path, ([], [], 0.0, ""))[1]
        if str(it.get("table_html") or "").strip()
    )

    return {
        "summary": {
            "pdf_count": len(pdf_paths),
            "caption_render": {
                "total_figures": total_l_figs,
                "total_tables": total_l_tbls,
                "caption_hit_rate": round(l_caption_hits / max(total_l_figs + total_l_tbls, 1), 3),
            },
            "mineru": {
                "total_figures": total_m_figs,
                "total_tables": total_m_tbls,
                "caption_hit_rate": round(m_caption_hits / max(total_m_figs + total_m_tbls, 1), 3),
                "table_structured_rate": round(m_structured / max(total_m_tbls, 1), 3),
            },
        },
        "per_pdf": per_pdf,
    }


def print_report(report: Dict[str, Any]) -> None:
    s = report["summary"]
    print("\n" + "=" * 88)
    print("图表抽取对照评估汇总")
    print("=" * 88)
    print(f"PDF 数量：{s['pdf_count']}")
    print()
    print(f"{'指标':<24}{'caption-render':>20}{'MinerU':>20}")
    print("-" * 64)
    print(f"{'总 figure 数':<24}{s['caption_render']['total_figures']:>20}{s['mineru']['total_figures']:>20}")
    print(f"{'总 table 数':<24}{s['caption_render']['total_tables']:>20}{s['mineru']['total_tables']:>20}")
    print(f"{'caption 命中率':<24}{s['caption_render']['caption_hit_rate']:>20}{s['mineru']['caption_hit_rate']:>20}")
    print(f"{'表格结构化率':<24}{'—':>20}{s['mineru']['table_structured_rate']:>20}")
    print()
    print("逐篇对比：")
    print(f"{'PDF':<36}{'CR-fig':>8}{'CR-tbl':>8}{'MU-fig':>8}{'MU-tbl':>8}{'MU-cap':>8}{'MU-tbl-s':>9}")
    print("-" * 88)
    for p in report["per_pdf"]:
        cr = p["caption_render"]
        mu = p["mineru"]
        name = p["pdf"]
        if len(name) > 34:
            name = name[:31] + "..."
        print(
            f"{name:<36}{cr['figures']:>8}{cr['tables']:>8}{mu['figures']:>8}{mu['tables']:>8}"
            f"{mu['caption_hit']:>8}{mu['table_structured']:>9}"
        )
        if mu["status"] not in ("ok", "missing"):
            print(f"  └ MinerU 状态：{mu['status']}")
    print("=" * 88)


def main() -> None:
    parser = argparse.ArgumentParser(description="图表抽取方案对照评估")
    parser.add_argument("pdf_dir", help="PDF 所在文件夹")
    parser.add_argument(
        "--output",
        default="compare_output",
        help="输出根目录（默认 compare_output）",
    )
    parser.add_argument(
        "--skip-mineru",
        action="store_true",
        help="只跑 caption-render，跳过 MinerU（调试用）",
    )
    args = parser.parse_args()

    pdf_dir = Path(args.pdf_dir).expanduser().resolve()
    if not pdf_dir.is_dir():
        print(f"[ERROR] 文件夹不存在：{pdf_dir}", file=sys.stderr)
        sys.exit(1)

    pdf_paths = sorted(str(p) for p in pdf_dir.glob("*.pdf"))
    if not pdf_paths:
        print(f"[ERROR] 文件夹内没有 PDF：{pdf_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"找到 {len(pdf_paths)} 个 PDF：{pdf_dir}", flush=True)

    output_root = Path(args.output).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    # ---- 1. 本地 caption-render ----
    print("\n[1/2] 运行 caption-render（本地）...", flush=True)
    local_results: Dict[str, Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float]] = {}
    for pdf_path in pdf_paths:
        stem = _stem(pdf_path)
        print(f"  - {stem}", flush=True)
        out_dir = str(output_root / "caption_render" / stem)
        figs, tbls, elapsed = run_caption_render(pdf_path, out_dir)
        local_results[pdf_path] = (figs, tbls, elapsed)
        print(f"      figures={len(figs)} tables={len(tbls)} ({elapsed:.1f}s)", flush=True)

    # ---- 2. MinerU 云端 ----
    mineru_results: Dict[str, Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float, str]] = {}
    if args.skip_mineru:
        print("\n[2/2] 跳过 MinerU（--skip-mineru）", flush=True)
        for p in pdf_paths:
            mineru_results[p] = ([], [], 0.0, "skipped")
    else:
        print(f"\n[2/2] 运行 MinerU 云端 API（批量 {len(pdf_paths)} 篇）...", flush=True)
        mineru_out_root = str(output_root / "mineru")
        os.makedirs(mineru_out_root, exist_ok=True)
        mineru_results = run_mineru_batch(pdf_paths, mineru_out_root)
        for pdf_path in pdf_paths:
            m = mineru_results.get(pdf_path, ([], [], 0.0, "missing"))
            print(f"  - {_stem(pdf_path)}: figures={len(m[0])} tables={len(m[1])} status={m[3]}", flush=True)

    # ---- 3. 汇总报告 ----
    report = build_report(pdf_paths, local_results, mineru_results)
    print_report(report)

    report_path = output_root / "report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已写入：{report_path}", flush=True)
    print(f"图片产物目录：{output_root / 'caption_render'} 和 {output_root / 'mineru'}", flush=True)


if __name__ == "__main__":
    main()
