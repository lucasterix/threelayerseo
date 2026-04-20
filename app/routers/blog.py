from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config import settings
from app.db import get_session
from app.models import Domain, DomainStatus, Post, PostStatus, Site, SiteStatus

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()


def _host(request: Request) -> str:
    raw = request.headers.get("host", "").split(":")[0].lower()
    return raw[4:] if raw.startswith("www.") else raw


async def _site_for_host(host: str, session: AsyncSession) -> Site:
    stmt = (
        select(Site)
        .join(Domain, Domain.id == Site.domain_id)
        .where(Domain.name == host)
        .options(joinedload(Site.domain))
    )
    site = (await session.execute(stmt)).scalar_one_or_none()
    if not site or site.status not in {SiteStatus.LIVE, SiteStatus.DRAFT}:
        raise HTTPException(status_code=404, detail="site not found")
    return site


# ─── On-demand TLS gate ─────────────────────────────────────────────────────

@router.get("/_/caddy-ask", response_class=PlainTextResponse, include_in_schema=False)
async def caddy_ask(
    domain: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """Caddy calls this before issuing a cert for an unknown hostname.

    200 = allow, 4xx = deny. We allow any domain that's ACTIVE in our DB.
    """
    candidate = domain.lower().lstrip("www.")
    stmt = select(Domain).where(
        Domain.name == candidate, Domain.status == DomainStatus.ACTIVE
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="not managed")
    return "ok"


# ─── robots.txt / sitemap.xml / IndexNow key ───────────────────────────────

@router.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
async def robots(request: Request, session: AsyncSession = Depends(get_session)):
    host = _host(request)
    try:
        site = await _site_for_host(host, session)
    except HTTPException:
        return "User-agent: *\nDisallow: /\n"
    sitemap = f"https://{site.domain.name}/sitemap.xml"
    return f"User-agent: *\nAllow: /\nSitemap: {sitemap}\n"


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap(request: Request, session: AsyncSession = Depends(get_session)):
    host = _host(request)
    site = await _site_for_host(host, session)
    stmt = (
        select(Post)
        .where(Post.site_id == site.id, Post.status == PostStatus.PUBLISHED)
        .order_by(Post.published_at.desc().nullslast())
    )
    posts = (await session.execute(stmt)).scalars().all()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    urls = [
        f"<url><loc>https://{host}/</loc><lastmod>{now}</lastmod><changefreq>daily</changefreq></url>"
    ]
    for p in posts:
        lastmod = (p.published_at or p.updated_at or datetime.now(timezone.utc)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
        urls.append(
            f"<url><loc>https://{host}/{p.slug}</loc><lastmod>{lastmod}</lastmod></url>"
        )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")


@router.get("/{key}.txt", response_class=PlainTextResponse, include_in_schema=False)
async def indexnow_key_file(key: str):
    """Serve <INDEXNOW_KEY>.txt at every domain root so IndexNow can verify
    ownership before accepting submissions."""
    if not settings.indexnow_key or key != settings.indexnow_key:
        raise HTTPException(status_code=404)
    return settings.indexnow_key


@router.get("/healthz/renderer", response_class=PlainTextResponse, include_in_schema=False)
async def ping():
    return "ok"


# ─── Blog ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def home(request: Request, session: AsyncSession = Depends(get_session)):
    host = _host(request)
    site = await _site_for_host(host, session)
    if site.status != SiteStatus.LIVE:
        raise HTTPException(status_code=404, detail="site not live yet")
    posts_stmt = (
        select(Post)
        .where(Post.site_id == site.id, Post.status == PostStatus.PUBLISHED)
        .order_by(Post.published_at.desc().nullslast())
        .limit(20)
    )
    posts = (await session.execute(posts_stmt)).scalars().all()
    template = f"blog/tier_{site.domain.tier.name.lower()}/index.html"
    return templates.TemplateResponse(template, {"request": request, "site": site, "posts": posts})


@router.get("/{slug}", response_class=HTMLResponse)
async def post_detail(slug: str, request: Request, session: AsyncSession = Depends(get_session)):
    host = _host(request)
    site = await _site_for_host(host, session)
    if site.status != SiteStatus.LIVE:
        raise HTTPException(status_code=404)
    stmt = select(Post).where(
        Post.site_id == site.id, Post.slug == slug, Post.status == PostStatus.PUBLISHED
    )
    post = (await session.execute(stmt)).scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="post not found")
    template = f"blog/tier_{site.domain.tier.name.lower()}/post.html"
    return templates.TemplateResponse(template, {"request": request, "site": site, "post": post})
