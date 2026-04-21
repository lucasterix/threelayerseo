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
    "ALTER TABLE domains ADD COLUMN IF NOT EXISTS category VARCHAR(32)",
    "ALTER TABLE domains ADD COLUMN IF NOT EXISTS is_expired_purchase BOOLEAN DEFAULT false NOT NULL",
    "ALTER TABLE domains ADD COLUMN IF NOT EXISTS wayback_snapshots INTEGER",
    "ALTER TABLE domains ADD COLUMN IF NOT EXISTS backlink_score INTEGER",
    "CREATE INDEX IF NOT EXISTS ix_domains_category ON domains(category)",
    "CREATE INDEX IF NOT EXISTS ix_domains_is_expired_purchase ON domains(is_expired_purchase)",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS featured_image_path VARCHAR(500)",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS featured_image_prompt TEXT",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS meta_description VARCHAR(500)",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS schema_json JSON",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS stylometric_profile VARCHAR(64)",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS refresh_due_at TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS ix_posts_refresh_due_at ON posts(refresh_due_at)",
    "CREATE INDEX IF NOT EXISTS ix_research_runs_status ON research_runs(status)",
    "ALTER TABLE money_sites ADD COLUMN IF NOT EXISTS category VARCHAR(32)",
    "ALTER TABLE money_sites ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT true NOT NULL",
    "ALTER TABLE money_sites ADD COLUMN IF NOT EXISTS anchor_hints JSON",
    "ALTER TABLE money_sites ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now() NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_money_sites_category ON money_sites(category)",
    "CREATE INDEX IF NOT EXISTS ix_money_sites_active ON money_sites(active)",
    "CREATE INDEX IF NOT EXISTS ix_pageviews_site_created ON pageviews(site_id, created_at DESC)",
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
