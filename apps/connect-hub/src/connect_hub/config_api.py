from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Mapping

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from connect_hub.provider_config import CredentialStore
from connect_hub.provider_catalog import (
    catalog_payload,
    merge_public_update,
    merged_defaults,
    probe_provider,
    public_config,
)


NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def create_config_app(config_path: str | Path) -> FastAPI:
    store = CredentialStore(config_path)
    app = FastAPI(title="Research Connect Local Configuration", version="0.1.0")

    def current() -> dict[str, Any]:
        return merged_defaults(store.load())

    @app.get("/api/config/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "connect-config"}

    @app.get("/api/config/catalog")
    def catalog() -> JSONResponse:
        return JSONResponse(catalog_payload(), headers=NO_CACHE)

    @app.get("/api/config/value")
    def read_config() -> JSONResponse:
        return JSONResponse(public_config(current()), headers=NO_CACHE)

    @app.post("/api/config/value")
    def save_config(body: dict[str, Any]) -> dict[str, Any]:
        merged = merge_public_update(current(), body)
        store.save(merged)
        return {"ok": True, "configured": True, "config": public_config(merged)}

    @app.post("/api/config/probe/{provider_id:path}")
    def probe(provider_id: str, body: dict[str, Any]) -> Any:
        provider = body.get("provider") if isinstance(body.get("provider"), Mapping) else {}
        config = merge_public_update(
            current(), {"providers": {provider_id: dict(provider)}}
        )
        try:
            return probe_provider(provider_id, config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Research Connect local configuration API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--config-path", required=True)
    args = parser.parse_args(argv)
    uvicorn.run(
        create_config_app(args.config_path),
        host=args.host,
        port=args.port,
        workers=1,
    )


if __name__ == "__main__":
    main()
