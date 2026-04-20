"""Multi-tenant blog renderer.

Serves per-domain blog sites. Dispatches based on the incoming Host header
to the matching ``Site`` row in the database.
"""
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routers import blog

app = FastAPI(title="ThreeLayerSEO — Renderer", docs_url=None, openapi_url=None)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Featured images for every post live on a shared volume that the worker
# writes to. Mount the same directory read-only here so every blog domain
# can serve /media/<filename>.png regardless of its Host header.
if os.path.isdir(settings.images_dir):
    app.mount("/media", StaticFiles(directory=settings.images_dir), name="media")

app.include_router(blog.router)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return JSONResponse({"ok": True, "env": settings.env, "service": "renderer"})
