from __future__ import annotations

import asyncio
from pathlib import Path

from citationclaw.skills.base import SkillContext, SkillResult
from citationclaw.core.exporter import ResultExporter


class ExportSkill:
    name = "phase3_export"

    async def run(self, ctx: SkillContext, **kwargs) -> SkillResult:
        try:
            return await self._run_inner(ctx, **kwargs)
        except Exception as e:
            ctx.log(f"[Phase3] fatal error: {e}")
            raise

    async def _run_inner(self, ctx: SkillContext, **kwargs) -> SkillResult:
        input_file = Path(kwargs["input_file"])
        excel_output = Path(kwargs["excel_output"])
        json_output = Path(kwargs["json_output"])

        exporter = ResultExporter(log_callback=ctx.log)
        # exporter.export() is synchronous; run in a thread to avoid blocking
        await asyncio.to_thread(
            exporter.export,
            input_file=input_file,
            excel_output=excel_output,
            json_output=json_output,
        )
        return SkillResult(
            name=self.name,
            data={
                "excel_output": str(excel_output),
                "json_output": str(json_output),
            },
        )
