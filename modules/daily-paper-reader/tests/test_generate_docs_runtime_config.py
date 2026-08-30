from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_generate_docs():
    root = Path(__file__).resolve().parents[1]
    path = root / "src" / "6.generate_docs.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("generate_docs_runtime_config", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_explicit_skims_mode_still_loads_config(monkeypatch):
    module = _load_generate_docs()
    expected = {"arxiv_paper_setting": {"mode": "standard"}, "venue_enrichment": {}}
    monkeypatch.setattr(module, "load_config", lambda: expected)

    config, mode = module.resolve_run_config_and_mode("skims")

    assert config is expected
    assert mode == "skims"


def test_loads_effective_per_run_config_file(tmp_path, monkeypatch):
    config_path = tmp_path / "run-config.yaml"
    config_path.write_text(
        "arxiv_paper_setting:\n  mode: skims\nsubscriptions:\n  intent_profiles: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DPR_CONFIG_FILE", str(config_path))
    module = _load_generate_docs()

    config, mode = module.resolve_run_config_and_mode(None)

    assert module.CONFIG_FILE == str(config_path)
    assert config["arxiv_paper_setting"]["mode"] == "skims"
    assert mode == "skims"


def test_pipeline_scripts_honor_per_run_config_environment():
    root = Path(__file__).resolve().parents[1]
    for name in ("0.enrich_config_queries.py", "5.select_papers.py", "6.generate_docs.py"):
        source = (root / "src" / name).read_text(encoding="utf-8")
        assert 'os.getenv("DPR_CONFIG_FILE")' in source
