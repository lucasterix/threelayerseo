from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

import markdown as md_lib

from app.auth import require_admin
from app.config import settings
from app.db import get_session
from app.models import Domain, DomainStatus, Post, PostStatus, Site, SiteStatus, Tier
from app.queue import content_q, domains_q, publish_q
from app.services.domains import DEFAULT_TLDS, check_availability, expand_candidates, queue_purchases

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()

TIER_CHOICES = [(int(t), t.name) for t in Tier]


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    by_tier = {}
    for tier in Tier:
        total = await session.scalar(
            select(func.count()).select_from(Domain).where(Domain.tier == tier)
        )
        active = await session.scalar(
            select(func.count())
            .select_from(Domain)
            .where(Domain.tier == tier, Domain.status == DomainStatus.ACTIVE)
        )
        by_tier[tier.name] = {"total": total or 0, "active": active or 0}

    sites_count = await session.scalar(select(func.count()).select_from(Site)) or 0
    posts_total = await session.scalar(select(func.count()).select_from(Post)) or 0
    posts_published = (
        await session.scalar(
            select(func.count()).select_from(Post).where(Post.status == PostStatus.PUBLISHED)
        )
        or 0
    )

    return templates.TemplateResponse(
        "admin/index.html",
        {
            "request": request,
            "by_tier": by_tier,
            "sites_count": sites_count,
            "posts_total": posts_total,
            "posts_published": posts_published,
        },
    )


# ─── Domains ────────────────────────────────────────────────────────────────

@router.get("/domains", response_class=HTMLResponse)
async def domains_list(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(select(Domain).order_by(Domain.created_at.desc()))).scalars().all()
    return templates.TemplateResponse(
        "admin/domains.html", {"request": request, "domains": rows, "tiers": list(Tier)}
    )


@router.get("/domains/search", response_class=HTMLResponse)
async def domains_search_form(
    request: Request,
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/domains_search.html",
        {"request": request, "default_tlds": DEFAULT_TLDS, "tier_choices": TIER_CHOICES},
    )


@router.post("/domains/search", response_class=HTMLResponse)
async def domains_search(
    request: Request,
    names: str = Form(""),
    tlds: list[str] = Form(default_factory=list),
    _: str = Depends(require_admin),
):
    tlds = tlds or list(DEFAULT_TLDS)
    candidates = expand_candidates(names, tlds)
    if not candidates:
        return HTMLResponse(
            '<div class="p-4 text-slate-500">Keine gültigen Kandidaten erkannt.</div>'
        )
    if len(candidates) > 300:
        return HTMLResponse(
            f'<div class="p-4 text-red-700">Zu viele Kandidaten ({len(candidates)}). Maximum 300.</div>',
            status_code=400,
        )
    try:
        results = check_availability(candidates)
    except Exception as e:  # noqa: BLE001
        log.exception("INWX check failed")
        return HTMLResponse(
            f'<div class="p-4 text-red-700">INWX-Check fehlgeschlagen: {e}</div>',
            status_code=502,
        )
    return templates.TemplateResponse(
        "admin/domains_results.html",
        {"request": request, "results": results, "tier_choices": TIER_CHOICES},
    )


@router.post("/domains/buy", response_class=HTMLResponse)
async def domains_buy(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    selected: list[str] = form.getlist("selected")
    if not selected:
        return HTMLResponse(
            '<div class="p-3 text-amber-700">Nichts ausgewählt.</div>', status_code=400
        )
    items: list[tuple[str, Tier, int | None]] = []
    for name in selected:
        tier_raw = form.get(f"tier[{name}]")
        price_raw = form.get(f"price[{name}]")
        try:
            tier = Tier(int(tier_raw)) if tier_raw else Tier.BAD
        except (ValueError, TypeError):
            tier = Tier.BAD
        try:
            price_cents = int(price_raw) if price_raw else None
        except (ValueError, TypeError):
            price_cents = None
        items.append((name, tier, price_cents))

    domain_ids = await queue_purchases(session, items)
    for did in domain_ids:
        domains_q.enqueue(
            "app.jobs.domains.register_domain_job",
            did,
            job_timeout=300,
            retry=None,
        )
    resp = RedirectResponse(url="/domains", status_code=303)
    resp.headers["HX-Redirect"] = "/domains"
    return resp


# ─── Sites ──────────────────────────────────────────────────────────────────

@router.get("/sites", response_class=HTMLResponse)
async def sites_list(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Site)
        .options(joinedload(Site.domain))
        .order_by(Site.created_at.desc())
    )
    sites = list((await session.execute(stmt)).unique().scalars().all())
    return templates.TemplateResponse(
        "admin/sites.html", {"request": request, "sites": sites}
    )


@router.get("/sites/{site_id}", response_class=HTMLResponse)
async def site_detail(
    site_id: int,
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Site)
        .options(joinedload(Site.domain), joinedload(Site.posts))
        .where(Site.id == site_id)
    )
    site = (await session.execute(stmt)).unique().scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404)
    posts = sorted(site.posts, key=lambda p: p.created_at, reverse=True)
    return templates.TemplateResponse(
        "admin/site_detail.html",
        {"request": request, "site": site, "posts": posts, "site_status_enum": SiteStatus},
    )


@router.post("/sites/{site_id}/update")
async def site_update(
    site_id: int,
    title: str = Form(""),
    topic: str = Form(""),
    language: str = Form("de"),
    status: str = Form(""),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404)
    if title:
        site.title = title
    if topic:
        site.topic = topic
    if language:
        site.language = language
    if status and status in {s.value for s in SiteStatus}:
        site.status = SiteStatus(status)
    await session.commit()
    return RedirectResponse(url=f"/sites/{site_id}", status_code=303)


@router.post("/sites/{site_id}/homepage")
async def site_homepage_generate(
    site_id: int,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404)
    content_q.enqueue("app.jobs.content.generate_homepage_job", site_id, job_timeout=600)
    return RedirectResponse(url=f"/sites/{site_id}", status_code=303)


@router.post("/sites/{site_id}/posts")
async def post_create(
    site_id: int,
    topic: str = Form(...),
    primary_keyword: str = Form(...),
    scheduled_at: str = Form(""),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Site).options(joinedload(Site.domain)).where(Site.id == site_id)
    site = (await session.execute(stmt)).scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404)
    sched = _parse_datetime(scheduled_at)
    post = Post(
        site_id=site.id,
        slug=slugify(primary_keyword)[:200],
        title=topic[:500],
        primary_keyword=primary_keyword,
        status=PostStatus.PENDING,
        scheduled_at=sched,
    )
    session.add(post)
    await session.commit()
    content_q.enqueue(
        "app.jobs.content.generate_post_job",
        post.id,
        topic,
        job_timeout=900,
    )
    return RedirectResponse(url=f"/sites/{site_id}", status_code=303)


def _parse_datetime(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


@router.post("/posts/{post_id}/publish")
async def post_publish(
    post_id: int,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    post = await session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404)
    if post.status not in {PostStatus.READY, PostStatus.FAILED, PostStatus.PUBLISHED}:
        raise HTTPException(status_code=409, detail=f"post status is {post.status.value}")
    publish_q.enqueue("app.jobs.content.publish_post_job", post.id, job_timeout=60)
    return RedirectResponse(url=f"/sites/{post.site_id}", status_code=303)


@router.post("/posts/{post_id}/regenerate")
async def post_regenerate(
    post_id: int,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    post = await session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404)
    post.status = PostStatus.PENDING
    await session.commit()
    content_q.enqueue(
        "app.jobs.content.generate_post_job",
        post.id,
        post.title,
        job_timeout=900,
    )
    return RedirectResponse(url=f"/posts/{post_id}", status_code=303)


@router.post("/posts/{post_id}/edit")
async def post_edit(
    post_id: int,
    body_markdown: str = Form(""),
    title: str = Form(""),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    post = await session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404)
    if title.strip():
        post.title = title.strip()
    post.body_markdown = body_markdown
    post.body_html = md_lib.markdown(body_markdown, extensions=["extra", "toc", "sane_lists"])
    await session.commit()
    return RedirectResponse(url=f"/posts/{post_id}", status_code=303)


@router.get("/posts/{post_id}", response_class=HTMLResponse)
async def post_detail(
    post_id: int,
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Post)
        .options(joinedload(Post.site).joinedload(Site.domain))
        .where(Post.id == post_id)
    )
    post = (await session.execute(stmt)).scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        "admin/post_detail.html", {"request": request, "post": post}
    )


# ─── Keyword research (DataForSEO) ─────────────────────────────────────────

@router.get("/keywords", response_class=HTMLResponse)
async def keywords_form(
    request: Request,
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/keywords.html",
        {
            "request": request,
            "configured": bool(settings.dataforseo_login and settings.dataforseo_password),
        },
    )


@router.post("/keywords/search", response_class=HTMLResponse)
async def keywords_search(
    request: Request,
    mode: str = Form("volumes"),
    seeds: str = Form(""),
    location_code: int = Form(2276),
    _: str = Depends(require_admin),
):
    keywords = [line.strip() for line in seeds.splitlines() if line.strip()]
    if not keywords:
        return HTMLResponse('<div class="p-3 text-slate-500">Keine Keywords.</div>')
    try:
        from app.services.keywords import keyword_ideas, keyword_volumes

        if mode == "ideas":
            results = keyword_ideas(keywords[0], limit=80, location_code=location_code)
        else:
            results = keyword_volumes(keywords[:200], location_code=location_code)
    except Exception as e:  # noqa: BLE001
        log.exception("DataForSEO call failed")
        return HTMLResponse(
            f'<div class="p-3 text-red-700">DataForSEO Fehler: {e}</div>',
            status_code=502,
        )
    return templates.TemplateResponse(
        "admin/keywords_results.html",
        {"request": request, "results": results, "mode": mode},
    )


# ─── Integrations (Google Search Console) ──────────────────────────────────

@router.get("/integrations", response_class=HTMLResponse)
async def integrations(
    request: Request,
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/integrations.html",
        {
            "request": request,
            "indexnow_key": settings.indexnow_key,
            "gsc_client_set": bool(settings.google_client_id and settings.google_client_secret),
            "gsc_connected": bool(settings.google_refresh_token),
            "dataforseo_set": bool(settings.dataforseo_login and settings.dataforseo_password),
        },
    )


@router.get("/integrations/gsc/connect")
async def gsc_connect(request: Request, _: str = Depends(require_admin)):
    from app.services.gsc import GscError, build_auth_url

    redirect_uri = f"https://{settings.admin_host}/integrations/gsc/callback"
    try:
        url = build_auth_url(redirect_uri, state="admin")
    except GscError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=url)


@router.get("/integrations/gsc/callback", response_class=HTMLResponse)
async def gsc_callback(
    request: Request,
    code: str = Query(""),
    error: str = Query(""),
    _: str = Depends(require_admin),
):
    from app.services.gsc import exchange_code

    if error:
        return HTMLResponse(f"<pre>OAuth error: {error}</pre>", status_code=400)
    if not code:
        return HTMLResponse("<pre>missing code</pre>", status_code=400)
    redirect_uri = f"https://{settings.admin_host}/integrations/gsc/callback"
    try:
        tokens = exchange_code(code, redirect_uri)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f"<pre>token exchange failed: {e}</pre>", status_code=502)
    refresh = tokens.get("refresh_token")
    return templates.TemplateResponse(
        "admin/integrations_gsc_done.html",
        {
            "request": request,
            "tokens": tokens,
            "refresh_token": refresh,
        },
    )
