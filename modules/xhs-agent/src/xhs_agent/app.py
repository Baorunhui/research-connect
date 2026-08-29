from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException

from .package import write_package
from .pipeline import XHSPipeline
from .schemas import SocialContentRequest, SocialContentResponse


app = FastAPI(title="xhs_agent", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/xhs/packages", response_model=SocialContentResponse)
def create_package(request: SocialContentRequest) -> SocialContentResponse:
    output_root = Path(os.getenv("XHS_AGENT_OUTPUT_DIR", "outputs"))
    offline = os.getenv("XHS_AGENT_OFFLINE", "false").lower() in {"1", "true", "yes"}
    pipeline = XHSPipeline.offline() if offline else XHSPipeline()
    try:
        result = pipeline.run(request)
        return write_package(result, output_root)
    except Exception as exc:
        return SocialContentResponse(
            request_id=request.request_id,
            status="failed",
            error=str(exc),
        )


@app.get("/v1/xhs/packages/{package_id}", response_model=SocialContentResponse)
def get_package(package_id: str) -> SocialContentResponse:
    output_root = Path(os.getenv("XHS_AGENT_OUTPUT_DIR", "outputs"))
    response_path = output_root / package_id / "response.json"
    if not response_path.exists():
        raise HTTPException(status_code=404, detail="package not found")
    return SocialContentResponse.model_validate_json(response_path.read_text(encoding="utf-8"))
