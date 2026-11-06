from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import __version__
from .config import settings
from .db import SessionLocal, init_db
from .models import GenerationRun, utcnow
from .routers import auth, generate, llm, projects, requirements, testcases
from .security import get_current_user
from .seed import seed
from .services.agents import REQ_TYPES, TEST_TYPES

STATIC_DIR = Path(__file__).parent / "static"
ROADMAP_FILE = Path(__file__).parent / "roadmap.json"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if settings.seed_demo:
        seed()
    # a run still marked "running" belongs to a process that no longer exists
    with SessionLocal() as db:
        for run in db.scalars(select(GenerationRun).where(GenerationRun.status == "running")):
            run.status = "failed"
            run.error = "Interrupted by a server restart"
            run.finished_at = utcnow()
        db.commit()
    yield


app = FastAPI(title="TRACE", version=__version__, lifespan=lifespan)

for module in (auth, projects, requirements, generate, testcases, llm):
    app.include_router(module.router)


@app.get("/api/meta")
def meta(_=Depends(get_current_user)):
    return {
        "version": __version__,
        "test_types": TEST_TYPES,
        "requirement_types": REQ_TYPES,
        "generation_delay_ms": settings.generation_delay_ms,
    }


@app.get("/api/roadmap")
def roadmap():
    return json.loads(ROADMAP_FILE.read_text(encoding="utf-8"))


@app.get("/api/health")
def health():
    return {"status": "ok", "version": __version__}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
