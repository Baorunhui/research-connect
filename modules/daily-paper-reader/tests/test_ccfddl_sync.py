"""sync_ccfddl_deadlines.py 测试：时区换算、TBD、多轮、override 合并、date_text 解析、容错、schema。

全部使用本地 fixture（--local-dir），不访问网络。
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.sync_ccfddl_deadlines as scd

BEIJING_TZ = timezone(timedelta(hours=8))


def _beijing_ts(y, mo, d, h, mi, s):
    """北京时区下 y-mo-d h:mi:s 对应的 epoch 毫秒。"""
    return int(
        datetime(y, mo, d, h, mi, s, tzinfo=BEIJING_TZ).timestamp() * 1000
    )


TYPES_YML = """\
- name: 计算机体系结构/并行与分布计算/存储系统
  name_en: Computer Architecture/Parallel Programming/Storage Technology
  sub: DS
- name: 计算机网络
  name_en: Network System
  sub: NW
- name: 网络与信息安全
  name_en: Network and System Security
  sub: SC
- name: 软件工程/系统软件/程序设计语言
  name_en: Software Engineering/Operating System/Programming Language Design
  sub: SE
- name: 数据库/数据挖掘/内容检索
  name_en: Database/Data Mining/Information Retrieval
  sub: DB
- name: 计算机科学理论
  name_en: Computing Theory
  sub: CT
- name: 计算机图形学与多媒体
  name_en: Graphics
  sub: CG
- name: 人工智能
  name_en: Artificial Intelligence
  sub: AI
- name: 人机交互与普适计算
  name_en: Computer-Human Interaction
  sub: HI
- name: 交叉/综合/新兴
  name_en: Interdiscipline/Mixture/Emerging
  sub: MX
"""

NEURIPS_YML = """\
- title: NeurIPS
  description: Conference on Neural Information Processing Systems
  sub: AI
  rank:
    ccf: A
    core: A*
    thcpl: A
  dblp: nips
  confs:
    - year: 2025
      id: nips25
      link: https://neurips.cc/Conferences/2025
      timeline:
        - abstract_deadline: '2025-05-11 23:59:59'
          deadline: '2025-05-15 23:59:59'
      timezone: AoE
      date: December 2-7, 2025
      place: San Diego Convention Center, USA
    - year: 2026
      id: nips26
      link: https://neurips.cc/Conferences/2026
      timeline:
        - abstract_deadline: '2026-05-05 11:59:00'
          deadline: '2026-05-07 11:59:00'
      timezone: UTC+0
      date: December 6, 2026
      place: Sydney, Australia
"""

ACL_YML = """\
- title: ACL
  description: Annual Meeting of the Association for Computational Linguistics
  sub: AI
  rank:
    ccf: A
    core: A*
    thcpl: A
  dblp: acl
  confs:
    - year: 2027
      id: acl27
      link: https://2027.aclweb.org/
      timeline:
        - deadline: 'TBD'
          comment: 'ARR Submission'
      timezone: UTC-12
      date: August 17-22, 2027
      place: Kyoto, Japan
"""

SIGMOD_YML = """\
- title: SIGMOD
  description: ACM Conference on Management of Data
  sub: DB
  rank:
    ccf: A
    core: A*
    thcpl: A
  dblp: sigmod
  confs:
    - year: 2025
      id: sigmod25
      link: https://2025.sigmod.org/
      timeline:
        - abstract_deadline: '2024-01-10 23:59:00'
          deadline: '2024-01-17 23:59:00'
          comment: round 1
        - abstract_deadline: '2024-04-10 23:59:00'
          deadline: '2024-04-17 23:59:00'
          comment: round 2
        - abstract_deadline: '2024-07-10 23:59:00'
          deadline: '2024-07-17 23:59:00'
          comment: round 3
        - abstract_deadline: '2024-10-10 23:59:00'
          deadline: '2024-10-17 23:59:00'
          comment: round 4
      timezone: AoE
      date: June 22-27, 2025
      place: Berlin, Germany
"""

UTC12_YML = """\
- title: UTC12Conf
  description: UTC-12 identical to AoE
  sub: AI
  rank:
    ccf: B
  dblp: utc12conf
  confs:
    - year: 2025
      id: utc12conf25
      link: https://example.com/utc12conf25
      timeline:
        - deadline: '2025-05-15 23:59:59'
      timezone: UTC-12
      date: October 1-3, 2025
      place: Test
"""

PT_YML = """\
- title: PTConf
  description: PT timezone with DST
  sub: AI
  rank:
    ccf: C
  dblp: ptconf
  confs:
    - year: 2025
      id: ptconf25
      link: https://example.com/ptconf25
      timeline:
        - deadline: '2025-07-15 23:59:59'
      timezone: PT
      date: October 1-3, 2025
      place: Test
    - year: 2026
      id: ptconf26
      link: https://example.com/ptconf26
      timeline:
        - deadline: '2025-12-15 23:59:59'
      timezone: PT
      date: March 1-3, 2026
      place: Test
"""

CROSSYEAR_YML = """\
- title: CrossYear
  description: AoE cross-year deadline
  sub: AI
  rank:
    ccf: B
  dblp: crossyear
  confs:
    - year: 2026
      id: crossyear26
      link: https://example.com/crossyear26
      timeline:
        - deadline: '2025-12-31 23:59:59'
      timezone: AoE
      date: June 1-3, 2026
      place: Test
"""

DATECONF_YML = """\
- title: DateConf
  description: date_text parsing
  sub: AI
  rank:
    ccf: C
  dblp: dateconf
  confs:
    - year: 2025
      id: dateconf25
      link: https://example.com/dateconf25
      timeline:
        - deadline: '2025-01-15 23:59:59'
      timezone: UTC
      date: December 2-7, 2025
      place: Test
    - year: 2026
      id: dateconf26
      link: https://example.com/dateconf26
      timeline:
        - deadline: '2026-01-15 23:59:59'
      timezone: UTC
      date: TBD
      place: Test
"""

OVERRIDECONF_YML = """\
- title: OverrideConf
  description: override merge test
  sub: AI
  rank:
    ccf: B
    core: B
  dblp: overrideconf
  confs:
    - year: 2026
      id: overrideconf26
      link: https://example.com/overrideconf26
      timeline:
        - deadline: '2026-01-15 23:59:59'
      timezone: UTC
      date: June 1-5, 2026
      place: Test
"""

BAD_YML = """\
- title: BadConf
  description: [unclosed
  sub: AI
"""

OVERRIDES_JSON = """\
{
  "overrideconf26": [
    {"type": "review", "date": "2026-03-01", "label": "审稿阶段"},
    {"type": "notification", "date": "2026-04-15", "label": "录取通知"},
    {"type": "camera_ready", "date": "2026-05-10", "label": "Camera Ready"}
  ]
}
"""


@pytest.fixture()
def fixture_dir(tmp_path):
    conf = tmp_path / "conference"
    (conf / "AI").mkdir(parents=True)
    (conf / "DB").mkdir(parents=True)
    (conf / "types.yml").write_text(TYPES_YML, encoding="utf-8")
    (conf / "AI" / "neurips.yml").write_text(NEURIPS_YML, encoding="utf-8")
    (conf / "AI" / "acl.yml").write_text(ACL_YML, encoding="utf-8")
    (conf / "DB" / "sigmod.yml").write_text(SIGMOD_YML, encoding="utf-8")
    (conf / "AI" / "utc12conf.yml").write_text(UTC12_YML, encoding="utf-8")
    (conf / "AI" / "ptconf.yml").write_text(PT_YML, encoding="utf-8")
    (conf / "AI" / "crossyear.yml").write_text(CROSSYEAR_YML, encoding="utf-8")
    (conf / "AI" / "dateconf.yml").write_text(DATECONF_YML, encoding="utf-8")
    (conf / "AI" / "overrideconf.yml").write_text(OVERRIDECONF_YML, encoding="utf-8")
    (conf / "AI" / "bad.yml").write_text(BAD_YML, encoding="utf-8")
    (tmp_path / "overrides.json").write_text(OVERRIDES_JSON, encoding="utf-8")
    return tmp_path


def _run(fixture_dir, tmp_path):
    out = tmp_path / "out.json"
    rc = scd.main([
        "--local-dir", str(fixture_dir),
        "--output", str(out),
        "--overrides", str(fixture_dir / "overrides.json"),
        "--generated-at", "2026-08-18T00:00:00Z",
    ])
    assert rc == 0
    return json.loads(out.read_text(encoding="utf-8"))


def _find(snapshot, key, year):
    conf = next(c for c in snapshot["conferences"] if c["key"] == key)
    return next(y for y in conf["years"] if y["year"] == year)


def _milestone(year_entry, mtype):
    return next(m for m in year_entry["milestones"] if m["type"] == mtype)


# --- 1. AoE 换算 ---
def test_aoe_conversion(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    y = _find(snap, "neurips", 2025)
    fp = _milestone(y, "full_paper")
    assert fp["date"] == "2025-05-15"
    assert fp["ts"] == _beijing_ts(2025, 5, 16, 19, 59, 59)
    assert fp["time_text"] == "23:59:59 AoE · 北京 5/16 19:59"
    assert fp["round"] is None  # 单轮时间线 round 为 null
    assert fp["source"] == "ccfddl"


# --- 2. UTC+0 ---
def test_utc0_conversion(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    y = _find(snap, "neurips", 2026)
    fp = _milestone(y, "full_paper")
    assert fp["ts"] == _beijing_ts(2026, 5, 7, 19, 59, 0)
    assert fp["time_text"] == "11:59:00 UTC+0 · 北京 5/7 19:59"


# --- 3. UTC-12 与 AoE 相同 ---
def test_utc12_identical_to_aoe(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    y = _find(snap, "utc12conf", 2025)
    fp = _milestone(y, "full_paper")
    assert fp["ts"] == _beijing_ts(2025, 5, 16, 19, 59, 59)
    assert fp["time_text"] == "23:59:59 UTC-12 · 北京 5/16 19:59"


# --- 4. PT 夏令时（-7）---
def test_pt_summer_dst(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    y = _find(snap, "ptconf", 2025)
    fp = _milestone(y, "full_paper")
    assert fp["ts"] == _beijing_ts(2025, 7, 16, 14, 59, 59)
    assert fp["time_text"] == "23:59:59 PT · 北京 7/16 14:59"


# --- 5. PT 冬令时（-8）---
def test_pt_winter_std(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    y = _find(snap, "ptconf", 2026)
    fp = _milestone(y, "full_paper")
    assert fp["ts"] == _beijing_ts(2025, 12, 16, 15, 59, 59)
    assert fp["time_text"] == "23:59:59 PT · 北京 12/16 15:59"


# --- 6. AoE 跨年 ---
def test_aoe_cross_year(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    y = _find(snap, "crossyear", 2026)
    fp = _milestone(y, "full_paper")
    assert fp["ts"] == _beijing_ts(2026, 1, 1, 19, 59, 59)
    assert fp["time_text"] == "23:59:59 AoE · 北京 1/1 19:59"


# --- 7. TBD ---
def test_tbd(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    y = _find(snap, "acl", 2027)
    assert y["is_tbd"] is True
    fp = _milestone(y, "full_paper")
    assert fp["is_tbd"] is True
    assert fp["ts"] is None
    assert fp["round"] is None
    assert fp["date"] == ""


# --- 8. 多轮（SIGMOD 风格）---
def test_multi_round(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    y = _find(snap, "sigmod", 2025)
    fps = [m for m in y["milestones"] if m["type"] == "full_paper"]
    assert len(fps) == 1
    fp = fps[0]
    assert fp["round"] == 1
    assert fp["date"] == "2024-01-17"
    assert fp["ts"] == _beijing_ts(2024, 1, 18, 19, 59, 0)
    assert fp["is_tbd"] is False


# --- 9. abstract_deadline 独立里程碑且更早 ---
def test_abstract_deadline_separate(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    y = _find(snap, "neurips", 2025)
    ab = _milestone(y, "abstract_deadline")
    fp = _milestone(y, "full_paper")
    assert ab["type"] == "abstract_deadline"
    assert ab["ts"] < fp["ts"]
    assert ab["ts"] == _beijing_ts(2025, 5, 12, 19, 59, 59)
    assert ab["label"] == "摘要投稿截止"


# --- 10. override 合并 ---
def test_override_merge(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    y = _find(snap, "overrideconf", 2026)
    types = {m["type"]: m for m in y["milestones"]}
    assert types["review"]["source"] == "override"
    assert types["review"]["date"] == "2026-03-01"
    assert types["review"]["label"] == "审稿阶段"
    assert types["notification"]["source"] == "override"
    assert types["camera_ready"]["source"] == "override"
    assert types["full_paper"]["source"] == "ccfddl"
    assert types["full_paper"]["ts"] == _beijing_ts(2026, 1, 16, 7, 59, 59)
    assert types["review"]["ts"] == _beijing_ts(2026, 3, 1, 0, 0, 0)


# --- 11. date_text 解析 ---
def test_date_text_parse(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    y = _find(snap, "dateconf", 2025)
    conf_m = _milestone(y, "conference")
    assert conf_m["date"] == "2025-12-02"
    assert conf_m["source"] == "ccfddl:date_text"
    assert conf_m["time_text"] == "December 2-7, 2025"
    assert conf_m["ts"] == _beijing_ts(2025, 12, 2, 0, 0, 0)


def test_date_text_unparseable(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    y = _find(snap, "dateconf", 2026)
    conf_m = _milestone(y, "conference")
    assert conf_m["ts"] is None
    assert conf_m["date"] == ""
    assert conf_m["source"] == "ccfddl:date_text"


# --- 12. 坏 YAML 跳过，运行仍成功 ---
def test_bad_yaml_skipped(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    keys = [c["key"] for c in snap["conferences"]]
    assert "badconf" not in keys
    assert "neurips" in keys
    assert "sigmod" in keys


# --- 13. schema 版本 / fields 顺序 / years DESC ---
def test_schema_and_fields(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    assert snap["schema_version"] == 3
    assert snap["source"] == "ccfddl/ccf-deadlines"
    assert snap["license"] == "MIT"
    subs = [f["sub"] for f in snap["fields"]]
    assert subs == ["DS", "NW", "SC", "SE", "DB", "CT", "CG", "AI", "HI", "MX"]
    assert len(snap["fields"]) == 10
    for conf in snap["conferences"]:
        years = [y["year"] for y in conf["years"]]
        assert years == sorted(years, reverse=True)


# --- 14. rank.ccf 恒存在；core/thcpl 可空 ---
def test_rank_fields(fixture_dir, tmp_path):
    snap = _run(fixture_dir, tmp_path)
    for conf in snap["conferences"]:
        assert "ccf" in conf["rank"]
        assert conf["rank"]["ccf"] in ("A", "B", "C", "N")
    pt = next(c for c in snap["conferences"] if c["key"] == "ptconf")
    assert pt["rank"]["ccf"] == "C"
    assert pt["rank"]["core"] is None
    assert pt["rank"]["thcpl"] is None
    neu = next(c for c in snap["conferences"] if c["key"] == "neurips")
    assert neu["rank"] == {"ccf": "A", "core": "A*", "thcpl": "A"}