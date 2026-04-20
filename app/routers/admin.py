from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
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
from app.models import (
    Domain,
    DomainStatus,
    Post,
    PostStatus,
    Server,
    ServerStatus,
    Site,
    SiteStatus,
    Tier,
)
from app.queue import content_q, domains_q, publish_q, redis_conn
from app.categories import all_categories, FEATURED_KEYS
from app.models import Expense, KeywordCluster
from app.services import budget
from app.services.domains import (
    DEFAULT_TLDS,
    PurchaseItem,
    check_availability,
    expand_candidates,
    queue_purchases,
)
from app.services.footprint import analyse as footprint_analyse
from app.services.servers import fleet_capacity
from app.services.site_health import compute as compute_site_health

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()

TIER_CHOICES = [(int(t), t.name) for t in Tier]


async def _pipeline_stats():
    from rq import Queue

    out = {}
    for name in ("domains", "content", "publish"):
        q = Queue(name, connection=redis_conn)
        out[name] = {
            "queued": q.count,
            "failed": q.failed_job_registry.count,
            "started": q.started_job_registry.count,
        }
    return out


def _severity(ratio: float) -> str:
    if ratio >= 0.9:
        return "red"
    if ratio >= 0.7:
        return "yellow"
    return "green"


# ─── Dashboard ─────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from app.services.gsc import is_configured as gsc_on

    # Tier breakdown
    by_tier = {}
    for tier in Tier:
        total = await session.scalar(
            select(func.count()).select_from(Domain).where(Domain.tier == tier)
        ) or 0
        active = await session.scalar(
            select(func.count())
            .select_from(Domain)
            .where(Domain.tier == tier, Domain.status == DomainStatus.ACTIVE)
        ) or 0
        live_sites = await session.scalar(
            select(func.count())
            .select_from(Site)
            .join(Domain, Domain.id == Site.domain_id)
            .where(Domain.tier == tier, Site.status == SiteStatus.LIVE)
        ) or 0
        by_tier[tier.name] = {"total": total, "active": active, "live": live_sites, "value": int(tier)}

    sites_count = await session.scalar(select(func.count()).select_from(Site)) or 0
    posts_total = await session.scalar(select(func.count()).select_from(Post)) or 0
    posts_published = await session.scalar(
        select(func.count()).select_from(Post).where(Post.status == PostStatus.PUBLISHED)
    ) or 0

    fc = await fleet_capacity(session)

    # Expiry alerts: domains that expire within 30 days
    soon = datetime.now(timezone.utc) + timedelta(days=30)
    expiring = (
        await session.execute(
            select(Domain)
            .where(Domain.expires_at.is_not(None), Domain.expires_at <= soon)
            .order_by(Domain.expires_at)
            .limit(20)
        )
    ).scalars().all()

    alerts: list[dict] = []
    if fc.needs_new_server:
        alerts.append({
            "severity": "red",
            "title": "Server-Auslastung kritisch",
            "body": f"{fc.total_used}/{fc.total_limit} Slots genutzt. Neuen Server anlegen bevor der nächste Bulk-Kauf läuft.",
            "link": "/servers",
        })
    elif fc.total_limit == 0:
        alerts.append({
            "severity": "yellow",
            "title": "Kein Server-Inventar gepflegt",
            "body": "Pflege mindestens einen Server-Eintrag (IP + Hostname) für sinnvolle Footprint-Alerts.",
            "link": "/servers/new",
        })

    # Same-IP warning when >=3 sites on one IP
    site_ips = [s[0] for s in (await session.execute(
        select(Server.ip).select_from(Site).join(Server, Server.id == Site.server_id, isouter=True)
    )).all() if s[0]]
    if site_ips:
        from collections import Counter
        counts = Counter(site_ips)
        top_ip, top_n = counts.most_common(1)[0]
        if top_n >= 3 and top_n / len(site_ips) >= 0.7:
            alerts.append({
                "severity": "yellow",
                "title": "IP-Cluster zu dicht",
                "body": f"{top_n}/{len(site_ips)} Sites liegen auf {top_ip}. Zweiter Server oder Cloudflare-Proxy einplanen.",
                "link": "/footprint",
            })

    if expiring:
        alerts.append({
            "severity": "yellow",
            "title": f"{len(expiring)} Domain(s) laufen in <30 Tagen aus",
            "body": "Renew planen / automatisieren bevor Sites offline gehen.",
            "link": "/domains",
        })

    pipeline = await _pipeline_stats()
    pipeline_fail_total = sum(v["failed"] for v in pipeline.values())
    if pipeline_fail_total:
        alerts.append({
            "severity": "red",
            "title": f"{pipeline_fail_total} fehlgeschlagene Jobs",
            "body": "Worker-Logs checken. Details: docker logs threelayerseo-worker",
        })

    # Health pillar overview
    pillars = {
        "infra": {
            "label": "Infrastruktur",
            "severity": fc.severity if fc.total_limit else "yellow",
            "summary": (
                f"{len(fc.servers)} Server · {fc.total_used}/{fc.total_limit} Slots"
                if fc.total_limit else "kein Inventar"
            ),
        },
        "content": {
            "label": "Content",
            "severity": "green" if posts_published >= 3 else ("yellow" if posts_published else "gray"),
            "summary": f"{posts_published} Posts live · {sites_count} Sites",
        },
        "indexing": {
            "label": "Indexing",
            "severity": "green" if gsc_on() else "yellow",
            "summary": "GSC + IndexNow aktiv" if gsc_on() else "nur IndexNow (GSC fehlt)",
        },
        "pipeline": {
            "label": "Pipeline",
            "severity": "red" if pipeline_fail_total else (
                "yellow" if any(v["queued"] or v["started"] for v in pipeline.values()) else "green"
            ),
            "summary": (
                f"{sum(v['started'] for v in pipeline.values())} running · "
                f"{sum(v['queued'] for v in pipeline.values())} queued · "
                f"{pipeline_fail_total} failed"
            ),
        },
    }

    # Recent activity: newest 10 domains
    recent_domains = (
        await session.execute(
            select(Domain).order_by(Domain.created_at.desc()).limit(10)
        )
    ).scalars().all()

    return templates.TemplateResponse(
        "admin/index.html",
        {
            "request": request,
            "pillars": pillars,
            "alerts": alerts,
            "by_tier": by_tier,
            "sites_count": sites_count,
            "posts_total": posts_total,
            "posts_published": posts_published,
            "pipeline": pipeline,
            "fleet": fc,
            "expiring": expiring,
            "recent_domains": recent_domains,
        },
    )


# ─── Domains ────────────────────────────────────────────────────────────────

@router.get("/domains", response_class=HTMLResponse)
async def domains_list(
    request: Request,
    category: str = "",
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Domain).order_by(Domain.created_at.desc())
    if category:
        stmt = stmt.where(Domain.category == category)
    rows = (await session.execute(stmt)).scalars().all()
    return templates.TemplateResponse(
        "admin/domains.html",
        {
            "request": request,
            "domains": rows,
            "tiers": list(Tier),
            "categories": all_categories(),
            "selected_category": category,
        },
    )


@router.get("/domains/search", response_class=HTMLResponse)
async def domains_search_form(
    request: Request,
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/domains_search.html",
        {
            "request": request,
            "default_tlds": DEFAULT_TLDS,
            "tier_choices": TIER_CHOICES,
            "categories": all_categories(),
        },
    )


@router.post("/domains/search", response_class=HTMLResponse)
async def domains_search(
    request: Request,
    names: str = Form(""),
    tlds: list[str] = Form(default_factory=list),
    default_category: str = Form(""),
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
        {
            "request": request,
            "results": results,
            "tier_choices": TIER_CHOICES,
            "categories": all_categories(),
            "default_category": default_category,
        },
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
    items: list[PurchaseItem] = []
    for name in selected:
        tier_raw = form.get(f"tier[{name}]")
        price_raw = form.get(f"price[{name}]")
        category = form.get(f"category[{name}]") or None
        is_expired = form.get(f"expired[{name}]") == "1"
        try:
            tier = Tier(int(tier_raw)) if tier_raw else Tier.BAD
        except (ValueError, TypeError):
            tier = Tier.BAD
        try:
            price_cents = int(price_raw) if price_raw else None
        except (ValueError, TypeError):
            price_cents = None
        items.append(
            PurchaseItem(
                name=name,
                tier=tier,
                price_cents=price_cents,
                category=category,
                is_expired_purchase=is_expired,
            )
        )

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


# ─── Expired-domain finder ─────────────────────────────────────────────────

@router.get("/expired", response_class=HTMLResponse)
async def expired_form(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(
        "admin/expired.html",
        {"request": request},
    )


@router.post("/expired/analyse", response_class=HTMLResponse)
async def expired_analyse(
    request: Request,
    domains_raw: str = Form(""),
    _: str = Depends(require_admin),
):
    candidates = []
    seen: set[str] = set()
    for line in domains_raw.splitlines():
        s = line.strip().lower()
        if not s or s.startswith("#"):
            continue
        # strip scheme/path/www
        import re
        s = re.sub(r"^https?://", "", s)
        s = s.split("/")[0].lstrip("www.")
        if "." not in s:
            continue
        if s not in seen:
            seen.add(s)
            candidates.append(s)
    if not candidates:
        return HTMLResponse('<div class="p-3 text-slate-500">Keine gültigen Domains erkannt.</div>')
    if len(candidates) > 60:
        return HTMLResponse(
            f'<div class="p-3 text-red-700">Zu viele ({len(candidates)}). Maximum 60 pro Batch.</div>',
            status_code=400,
        )
    try:
        from app.services.expired import analyse

        results = analyse(candidates)
    except Exception as e:  # noqa: BLE001
        log.exception("expired analyse failed")
        return HTMLResponse(
            f'<div class="p-3 text-red-700">Analyse fehlgeschlagen: {e}</div>',
            status_code=502,
        )
    return templates.TemplateResponse(
        "admin/expired_results.html",
        {
            "request": request,
            "results": results,
            "tier_choices": TIER_CHOICES,
            "categories": all_categories(),
        },
    )


# ─── Sites ──────────────────────────────────────────────────────────────────

@router.get("/sites", response_class=HTMLResponse)
async def sites_list(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Site)
        .options(joinedload(Site.domain), joinedload(Site.server))
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
        .options(
            joinedload(Site.domain),
            joinedload(Site.server),
            joinedload(Site.posts),
        )
        .where(Site.id == site_id)
    )
    site = (await session.execute(stmt)).unique().scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404)
    health = await compute_site_health(session, site, site.domain)
    posts = sorted(site.posts, key=lambda p: p.created_at, reverse=True)

    servers = list((
        await session.execute(select(Server).where(Server.status == ServerStatus.ACTIVE))
    ).scalars().all())

    # GSC metrics (best-effort, never blocks rendering)
    gsc_perf = None
    gsc_top = []
    try:
        from app.services.gsc_metrics import query_performance, top_queries

        gsc_perf = query_performance(f"sc-domain:{site.domain.name}", days=28)
        if gsc_perf is None:
            gsc_perf = query_performance(f"https://{site.domain.name}/", days=28)
        gsc_top = top_queries(f"sc-domain:{site.domain.name}", days=28, limit=10)
    except Exception:  # noqa: BLE001
        log.debug("gsc metrics fetch skipped", exc_info=True)

    return templates.TemplateResponse(
        "admin/site_detail.html",
        {
            "request": request,
            "site": site,
            "posts": posts,
            "health": health,
            "servers": servers,
            "site_status_enum": SiteStatus,
            "gsc_perf": gsc_perf,
            "gsc_top": gsc_top,
        },
    )


@router.post("/sites/{site_id}/update")
async def site_update(
    site_id: int,
    title: str = Form(""),
    topic: str = Form(""),
    language: str = Form("de"),
    status: str = Form(""),
    server_id: str = Form(""),
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
    if server_id == "":
        pass
    elif server_id == "none":
        site.server_id = None
    else:
        try:
            site.server_id = int(server_id)
        except ValueError:
            pass
    await session.commit()
    return RedirectResponse(url=f"/sites/{site_id}", status_code=303)


@router.post("/sites/{site_id}/cloudflare/onboard")
async def site_cloudflare_onboard(
    site_id: int,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Create a CF zone for this site's domain, proxy apex+www to the
    origin IP, and switch the INWX nameservers to the ones CF returns.
    Stores the result in Domain.meta["cloudflare"] for later reference.
    """
    stmt = (
        select(Site)
        .options(joinedload(Site.domain), joinedload(Site.server))
        .where(Site.id == site_id)
    )
    site = (await session.execute(stmt)).scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404)
    if site.domain.status != DomainStatus.ACTIVE:
        raise HTTPException(status_code=409, detail="domain muss ACTIVE sein")

    from app.services.cloudflare import account_id as cf_account_id
    from app.services.cloudflare import is_configured as cf_configured
    from app.services.cloudflare import onboard_domain

    if not cf_configured():
        raise HTTPException(status_code=400, detail="Cloudflare nicht konfiguriert")
    acct = cf_account_id()
    if not acct:
        raise HTTPException(status_code=400, detail="CLOUDFLARE_ACCOUNT_ID nicht gesetzt")

    origin_ip = site.server.ip if site.server else settings.server_ip

    try:
        zone = onboard_domain(site.domain.name, origin_ip, acct)
    except Exception as e:  # noqa: BLE001
        log.exception("CF onboard failed for %s", site.domain.name)
        raise HTTPException(status_code=502, detail=f"Cloudflare-Onboarding fehlgeschlagen: {e}")

    nameservers = zone.get("name_servers") or []
    ns_switched = False
    if nameservers:
        from app.registrars.inwx import InwxRegistrar

        try:
            inwx = InwxRegistrar()
            ns_switched = inwx.set_nameservers(site.domain.name, list(nameservers))
        except Exception:  # noqa: BLE001
            log.warning("INWX NS switch failed for %s", site.domain.name, exc_info=True)

    meta = dict(site.domain.meta or {})
    meta["cloudflare"] = {
        "zone_id": zone.get("id"),
        "name": zone.get("name"),
        "status": zone.get("status"),
        "name_servers": list(nameservers),
        "account_id": acct,
        "origin_ip": origin_ip,
        "ns_switched_at_inwx": ns_switched,
    }
    site.domain.meta = meta
    await session.commit()
    log.info(
        "CF onboarded %s: zone=%s status=%s NS-switched=%s",
        site.domain.name,
        zone.get("id"),
        zone.get("status"),
        ns_switched,
    )
    return RedirectResponse(url=f"/sites/{site_id}", status_code=303)


@router.post("/sites/{site_id}/launch")
async def site_launch(
    site_id: int,
    keywords_raw: str = Form(""),
    interval_hours: int = Form(24),
    auto_publish: bool = Form(True),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """One-click: homepage + legal + N posts drip-fed + images + schema."""
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404)
    keywords = [k.strip() for k in keywords_raw.splitlines() if k.strip()]
    if not keywords:
        raise HTTPException(status_code=400, detail="Mindestens ein Keyword angeben")
    if len(keywords) > 40:
        raise HTTPException(status_code=400, detail="Maximal 40 Keywords pro Launch")
    content_q.enqueue(
        "app.jobs.content.launch_site_job",
        site_id,
        keywords,
        None,
        interval_hours,
        bool(auto_publish),
        job_timeout=1800,
    )
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


@router.post("/sites/{site_id}/legal/generate")
async def site_legal_generate(
    site_id: int,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404)
    if not (settings.operator_name and settings.operator_email):
        raise HTTPException(
            status_code=400,
            detail="OPERATOR_NAME und OPERATOR_EMAIL müssen in der Server-.env gesetzt sein",
        )
    content_q.enqueue("app.jobs.content.generate_legal_job", site_id, job_timeout=300)
    return RedirectResponse(url=f"/sites/{site_id}", status_code=303)


@router.post("/sites/{site_id}/legal/imprint")
async def site_legal_imprint_edit(
    site_id: int,
    imprint_markdown: str = Form(""),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404)
    from app.services.legal import to_html

    meta = dict(site.meta or {})
    meta["imprint_markdown"] = imprint_markdown
    site.meta = meta
    site.imprint_html = to_html(imprint_markdown)
    await session.commit()
    return RedirectResponse(url=f"/sites/{site_id}#legal", status_code=303)


@router.post("/sites/{site_id}/legal/privacy")
async def site_legal_privacy_edit(
    site_id: int,
    privacy_markdown: str = Form(""),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404)
    from app.services.legal import to_html

    meta = dict(site.meta or {})
    meta["privacy_markdown"] = privacy_markdown
    site.meta = meta
    site.privacy_html = to_html(privacy_markdown)
    await session.commit()
    return RedirectResponse(url=f"/sites/{site_id}#legal", status_code=303)


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


@router.post("/posts/{post_id}/image")
async def post_image_regenerate(
    post_id: int,
    style: str = Form(""),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    post = await session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404)
    content_q.enqueue(
        "app.jobs.content.generate_image_job", post.id, style, job_timeout=180
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


# ─── Servers ────────────────────────────────────────────────────────────────

@router.get("/servers", response_class=HTMLResponse)
async def servers_list(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    fc = await fleet_capacity(session)
    all_servers = list((
        await session.execute(select(Server).order_by(Server.created_at))
    ).scalars().all())
    return templates.TemplateResponse(
        "admin/servers.html",
        {
            "request": request,
            "fleet": fc,
            "all_servers": all_servers,
            "statuses": [s.value for s in ServerStatus],
        },
    )


@router.get("/servers/new", response_class=HTMLResponse)
async def server_new_form(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(
        "admin/server_new.html",
        {"request": request, "statuses": [s.value for s in ServerStatus]},
    )


@router.post("/servers")
async def server_create(
    provider: str = Form("hetzner"),
    hostname: str = Form(...),
    ip: str = Form(...),
    ipv6: str = Form(""),
    location: str = Form(""),
    server_type: str = Form(""),
    capacity_limit: int = Form(25),
    monthly_cost_cents: str = Form(""),
    status: str = Form("active"),
    notes: str = Form(""),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    cost = None
    if monthly_cost_cents.strip():
        try:
            cost = int(monthly_cost_cents)
        except ValueError:
            pass
    srv = Server(
        provider=provider,
        hostname=hostname,
        ip=ip,
        ipv6=ipv6 or None,
        location=location,
        server_type=server_type,
        capacity_limit=capacity_limit,
        monthly_cost_cents=cost,
        status=ServerStatus(status) if status in {s.value for s in ServerStatus} else ServerStatus.ACTIVE,
        notes=notes or None,
    )
    session.add(srv)
    await session.commit()
    return RedirectResponse(url="/servers", status_code=303)


@router.post("/servers/{server_id}/update")
async def server_update(
    server_id: int,
    hostname: str = Form(""),
    capacity_limit: int = Form(0),
    status: str = Form(""),
    notes: str = Form(""),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    srv = await session.get(Server, server_id)
    if not srv:
        raise HTTPException(status_code=404)
    if hostname:
        srv.hostname = hostname
    if capacity_limit:
        srv.capacity_limit = capacity_limit
    if status and status in {s.value for s in ServerStatus}:
        srv.status = ServerStatus(status)
    if notes:
        srv.notes = notes
    await session.commit()
    return RedirectResponse(url="/servers", status_code=303)


# ─── Footprint ──────────────────────────────────────────────────────────────

@router.get("/footprint", response_class=HTMLResponse)
async def footprint(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    report = await footprint_analyse(session)
    return templates.TemplateResponse(
        "admin/footprint.html", {"request": request, "report": report}
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


# ─── Calendar ──────────────────────────────────────────────────────────────

@router.get("/calendar", response_class=HTMLResponse)
async def calendar(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Week-centric publishing calendar: upcoming scheduled posts +
    recently published. Lets the operator see at a glance when the
    drip-feed of a fresh site lands.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    end = now + timedelta(days=28)
    stmt = (
        select(Post)
        .options(joinedload(Post.site).joinedload(Site.domain))
        .where(
            (Post.scheduled_at.between(start, end))
            | (Post.published_at.between(start, end))
        )
        .order_by(Post.scheduled_at.nulls_last(), Post.published_at.nulls_last())
    )
    posts = list((await session.execute(stmt)).scalars().all())
    days: dict[str, list[Post]] = {}
    for p in posts:
        when = p.scheduled_at or p.published_at
        if not when:
            continue
        key = when.strftime("%Y-%m-%d")
        days.setdefault(key, []).append(p)
    return templates.TemplateResponse(
        "admin/calendar.html",
        {"request": request, "days": days, "now": now},
    )


# ─── Budget ────────────────────────────────────────────────────────────────

@router.get("/budget", response_class=HTMLResponse)
async def budget_view(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    six = await budget.last_6_months(session)
    recent = await budget.recent_expenses(session, limit=80)
    return templates.TemplateResponse(
        "admin/budget.html",
        {"request": request, "six_months": six, "recent": recent},
    )


# ─── Keyword cluster view ──────────────────────────────────────────────────

@router.get("/keywords/cluster", response_class=HTMLResponse)
async def cluster_form(
    request: Request,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    existing = list(
        (
            await session.execute(
                select(KeywordCluster).order_by(KeywordCluster.created_at.desc()).limit(40)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        "admin/cluster.html",
        {"request": request, "existing": existing, "categories": all_categories()},
    )


@router.post("/keywords/cluster")
async def cluster_submit(
    request: Request,
    keywords_raw: str = Form(""),
    focus_category: str = Form(""),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    keywords = list({k.strip().lower() for k in keywords_raw.splitlines() if k.strip()})
    if not keywords:
        raise HTTPException(status_code=400, detail="Leere Keyword-Liste")
    from app.services.clustering import cluster_keywords

    try:
        clusters = cluster_keywords(keywords, focus_category=focus_category or None)
    except Exception as e:  # noqa: BLE001
        log.exception("clustering failed")
        raise HTTPException(status_code=502, detail=str(e))
    for c in clusters:
        row = KeywordCluster(
            name=c.get("name") or "Unbenannt",
            category=focus_category or None,
            keywords=c.get("keywords") or [],
            intent=c.get("intent"),
        )
        session.add(row)
    await session.commit()
    await budget.track("anthropic", "clustering", note=f"{len(keywords)} keywords -> {len(clusters)}")
    return RedirectResponse(url="/keywords/cluster", status_code=303)


# ─── Competitor SERP ───────────────────────────────────────────────────────

@router.get("/keywords/serp", response_class=HTMLResponse)
async def serp_form(
    request: Request,
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse("admin/serp.html", {"request": request})


@router.post("/keywords/serp", response_class=HTMLResponse)
async def serp_analyse(
    request: Request,
    keyword: str = Form(...),
    location_code: int = Form(2276),
    _: str = Depends(require_admin),
):
    from app.services.competitor import top_results

    try:
        results = top_results(keyword, location_code=location_code)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f'<div class="p-3 text-red-700">{e}</div>', status_code=502)
    await budget.track("dataforseo", "serp", note=keyword)
    return templates.TemplateResponse(
        "admin/serp_results.html",
        {"request": request, "results": results, "keyword": keyword},
    )


# ─── Content refresh (self-rescheduling cron) ──────────────────────────────

@router.post("/refresh/start")
async def refresh_start(
    _: str = Depends(require_admin),
):
    """Kick off the refresh-scheduler chain. Safe to call multiple times
    (worst case a few duplicate runs — they're idempotent)."""
    content_q.enqueue("app.jobs.content.refresh_stale_job", 20, job_timeout=3600)
    return RedirectResponse(url="/", status_code=303)


# ─── Integrations ──────────────────────────────────────────────────────────

@router.get("/integrations", response_class=HTMLResponse)
async def integrations(
    request: Request,
    _: str = Depends(require_admin),
):
    from app.services.cloudflare import (
        is_configured as cf_configured,
        list_accounts as cf_list_accounts,
        list_zones as cf_list_zones,
        verify_token,
    )
    from app.services.gsc import is_configured as gsc_configured, service_account_email

    cf_info = verify_token() if cf_configured() else None
    cf_accounts: list[dict] = []
    cf_zones: list[dict] = []
    if cf_info:
        try:
            cf_accounts = cf_list_accounts()
        except Exception:  # noqa: BLE001
            log.warning("cloudflare list_accounts failed", exc_info=True)
        try:
            cf_zones = cf_list_zones(
                account_id_=settings.cloudflare_account_id or None
            )
        except Exception:  # noqa: BLE001
            log.warning("cloudflare list_zones failed", exc_info=True)

    return templates.TemplateResponse(
        "admin/integrations.html",
        {
            "request": request,
            "indexnow_key": settings.indexnow_key,
            "gsc_configured": gsc_configured(),
            "gsc_sa_email": service_account_email(),
            "gsc_key_path": settings.google_credentials_path,
            "dataforseo_set": bool(settings.dataforseo_login and settings.dataforseo_password),
            "cf_configured": cf_configured(),
            "cf_info": cf_info,
            "cf_accounts": cf_accounts,
            "cf_zones": cf_zones,
            "cf_account_id": settings.cloudflare_account_id,
        },
    )
