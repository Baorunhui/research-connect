from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from .package import write_package
from .pipeline import DEFAULT_MODELS, PipelineConfig, XHSPipeline
from .schemas import SocialContentRequest, SocialContentResponse
from .webui import INDEX_HTML


app = FastAPI(title="xhs_agent", version="0.1.0")


def _output_root() -> Path:
    return Path(os.getenv("XHS_AGENT_OUTPUT_DIR", "outputs"))


def _pipeline_config() -> PipelineConfig:
    model = os.getenv("XHS_AGENT_MODEL", "").strip()
    return PipelineConfig(models={step: model for step in DEFAULT_MODELS} if model else None)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/xhs/packages", response_model=SocialContentResponse)
def create_package(request: SocialContentRequest) -> SocialContentResponse:
    output_root = _output_root()
    offline = os.getenv("XHS_AGENT_OFFLINE", "false").lower() in {"1", "true", "yes"}
    try:
        pipeline = XHSPipeline.offline() if offline else XHSPipeline()
        pipeline.config = _pipeline_config()
        result = pipeline.run(request)
        return write_package(result, output_root)
    except Exception as exc:
        return SocialContentResponse(
            request_id=request.request_id,
            status="failed",
            error=str(exc),
        )


@app.get("/v1/xhs/packages")
def list_packages() -> dict[str, list[dict[str, str]]]:
    output_root = _output_root()
    packages = []
    if output_root.is_dir():
        for response_path in sorted(output_root.glob("*/response.json"), reverse=True):
            try:
                response = SocialContentResponse.model_validate_json(
                    response_path.read_text(encoding="utf-8")
                )
            except ValueError:
                continue
            if response.data is None:
                continue
            packages.append({
                "package_id": response.data.package_id,
                "title": response.data.xhs_payload.title,
            })
    return {"packages": packages}


@app.get("/v1/xhs/packages/{package_id}", response_model=SocialContentResponse)
def get_package(package_id: str) -> SocialContentResponse:
    response_path = _output_root() / package_id / "response.json"
    if not response_path.exists():
        raise HTTPException(status_code=404, detail="package not found")
    return SocialContentResponse.model_validate_json(response_path.read_text(encoding="utf-8"))


@app.get("/v1/xhs/packages/{package_id}/files/{file_path:path}")
def get_package_file(package_id: str, file_path: str) -> FileResponse:
    output_root = _output_root().resolve()
    package_dir = (output_root / package_id).resolve()
    target = (package_dir / file_path).resolve()
    if package_dir.parent != output_root or package_dir not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target)
