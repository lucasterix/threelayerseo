from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import settings
from app.db import Base, engine
from app import models  # noqa: F401 — register models
from app.routers import admin

# Idempotent ALTER TABLEs for columns added after the first deploy. Postgres
# supports ADD COLUMN IF NOT EXISTS, so this is safe to re-run on every boot.
# When the schema stabilises we'll switch to alembic; for now schema churns.
POST_CREATE_MIGRATIONS = [
    "ALTER TABLE sites ADD COLUMN IF NOT EXISTS server_id INTEGER REFERENCES servers(id) ON DELETE SET NULL",
    "ALTER TABLE sites ADD COLUMN IF NOT EXISTS imprint_html TEXT",
    "ALTER TABLE sites ADD COLUMN IF NOT EXISTS privacy_html TEXT",
    "CREATE INDEX IF NOT EXISTS ix_sites_server_id ON sites(server_id)",
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in POST_CREATE_MIGRATIONS:
            await conn.execute(text(stmt))
    yield


app = FastAPI(
    title="ThreeLayerSEO — Admin",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(admin.router)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return JSONResponse({"ok": True, "env": settings.env})
