import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "5.select_papers.py"


def load_module():
    spec = importlib.util.spec_from_file_location("select_papers_mod", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def patch_setting(monkeypatch, mod, setting):
    monkeypatch.setattr(mod, "load_recommend_setting", lambda: setting)


def test_no_setting_keeps_mode_defaults(monkeypatch):
    mod = load_module()
    patch_setting(monkeypatch, mod, {})
    cfg = mod.apply_recommend_setting(dict(mod.MODES["standard"]), "standard")
    assert cfg["deep_base"] == 5
    assert cfg["quick_base"] == 10
    assert not cfg.get("deep_unlimited")


def test_deep_base_override_with_min_clamp(monkeypatch):
    mod = load_module()
    # 用户填 1 → 夹到下限 3
    patch_setting(monkeypatch, mod, {"deep_dive_base": "1", "quick_skim_base": ""})
    cfg = mod.apply_recommend_setting(dict(mod.MODES["standard"]), "standard")
    assert cfg["deep_base"] == mod.MIN_DEEP_DIVE_BASE == 3
    # 正常覆盖：7 生效
    patch_setting(monkeypatch, mod, {"deep_dive_base": 7})
    cfg = mod.apply_recommend_setting(dict(mod.MODES["standard"]), "standard")
    assert cfg["deep_base"] == 7


def test_unlimited_flag(monkeypatch):
    mod = load_module()
    patch_setting(monkeypatch, mod, {"deep_dive_unlimited": True})
    cfg = mod.apply_recommend_setting(dict(mod.MODES["extend"]), "extend")
    assert cfg.get("deep_unlimited") is True
    assert cfg["deep_base"] == 10  # unlimited 不改写 base，仅在 process 分支生效


def test_quick_base_override_and_skims(monkeypatch):
    mod = load_module()
    patch_setting(
        monkeypatch,
        mod,
        {"quick_skim_base": "25", "deep_dive_base": "9"},
    )
    cfg = mod.apply_recommend_setting(dict(mod.MODES["skims"]), "skims")
    # skims 无精读区：deep_dive_base 不得写入
    assert "deep_base" not in cfg
    assert cfg["quick_base"] == 25


def test_invalid_values_ignored(monkeypatch):
    mod = load_module()
    patch_setting(
        monkeypatch,
        mod,
        {"deep_dive_base": "-5", "quick_skim_base": "abc", "deep_dive_unlimited": False},
    )
    cfg = mod.apply_recommend_setting(dict(mod.MODES["standard"]), "standard")
    assert cfg["deep_base"] == 5
    assert cfg["quick_base"] == 10
