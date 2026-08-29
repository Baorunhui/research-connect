from __future__ import annotations

import time

from citationclaw.app.config_manager import AppConfig
from citationclaw.skills.base import SkillContext, SkillResult
from citationclaw.skills.registry import build_default_registry


class SkillsRuntime:
    """Skills execution runtime for phase-based pipelines."""

    def __init__(self):
        self.registry = build_default_registry()

    async def run(
        self,
        skill_name: str,
        *,
        config: AppConfig,
        log,
        progress=None,
        cancel_check=None,
        extras=None,
        **kwargs,
    ):
        ctx = SkillContext(
            config=config,
            log=log,
            progress=progress,
            cancel_check=cancel_check,
            extras=extras or {},
        )
        skill = self.registry.get(skill_name)

        start = time.monotonic()
        result = await skill.run(ctx, **kwargs)
        elapsed = time.monotonic() - start
        log(f"[SkillsRuntime] {skill_name} completed in {elapsed:.1f}s")

        # Validate result
        if not isinstance(result, SkillResult):
            raise TypeError(
                f"Skill {skill_name!r} returned {type(result).__name__} instead of SkillResult"
            )
        if result.data is None:
            raise ValueError(f"Skill {skill_name!r} returned SkillResult with data=None")

        return result.data
