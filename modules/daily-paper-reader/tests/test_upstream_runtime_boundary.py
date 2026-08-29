import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REPOSITORY = "ziwenhahaha/daily-paper-reader"


def test_canonical_repository_workflows_do_not_commit_runtime_outputs():
    guarded_steps = {
        ROOT / ".github" / "workflows" / "daily-paper-reader.yml": "Commit results",
        ROOT / ".github" / "workflows" / "conference-paper-retrieval.yml": "Commit conference retrieval results",
        ROOT / ".github" / "workflows" / "reset-content.yml": "Commit reset result",
    }

    for path, step_name in guarded_steps.items():
        workflow = path.read_text(encoding="utf-8")
        pattern = re.compile(
            rf"- name: {re.escape(step_name)}\n\s+if: github\.repository != '{re.escape(CANONICAL_REPOSITORY)}'"
        )
        assert pattern.search(workflow), path
