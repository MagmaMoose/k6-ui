import asyncio
import os
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .archive import ArchiveError, archive


class Bundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str = Field(min_length=1, max_length=300)
    variables: dict[Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")], Annotated[str, Field(max_length=2048)]] = Field(max_length=64)


def create_archiver(root: Path, builder=archive):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    slots = asyncio.Semaphore(2)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.post("/archive")
    async def bundle(body: Bundle):
        try:
            async with slots:
                content = await builder(root, body.script, body.variables)
        except (ArchiveError, TimeoutError):
            return Response(status_code=422)
        return Response(content, media_type="application/octet-stream")

    return app


if __name__ == "__main__":
    uvicorn.run(create_archiver(Path(os.environ.get("K6_UI_SCRIPTS_DIR", "/scripts"))), host="127.0.0.1", port=8081, access_log=False)  # DevSkim: ignore DS162092 - production API accepts only pod-local requests
