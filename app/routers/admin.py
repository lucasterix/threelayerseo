from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.auth import require_admin
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
    # Expect fields: selected (multi), tier[<name>], price[<name>]
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
    if status and status in SiteStatus.__members__.values():
        site.status = SiteStatus(status)
    await session.commit()
    return RedirectResponse(url=f"/sites/{site_id}", status_code=303)


@router.post("/sites/{site_id}/posts")
async def post_create(
    site_id: int,
    topic: str = Form(...),
    primary_keyword: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Site).options(joinedload(Site.domain)).where(Site.id == site_id)
    site = (await session.execute(stmt)).scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404)
    post = Post(
        site_id=site.id,
        slug=slugify(primary_keyword)[:200],
        title=topic[:500],
        primary_keyword=primary_keyword,
        status=PostStatus.PENDING,
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


@router.post("/posts/{post_id}/publish")
async def post_publish(
    post_id: int,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    post = await session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404)
    if post.status not in {PostStatus.READY, PostStatus.FAILED}:
        raise HTTPException(status_code=409, detail=f"post status is {post.status.value}")
    publish_q.enqueue("app.jobs.content.publish_post_job", post.id, job_timeout=60)
    return RedirectResponse(url=f"/sites/{post.site_id}", status_code=303)


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
