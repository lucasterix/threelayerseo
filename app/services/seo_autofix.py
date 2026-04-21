"""Auto-fixer for known SEO/Lighthouse issue codes.

Runs after every audit. For each `Issue.code` in the latest audit we
check the registry — if a fixer exists, we enqueue the corresponding
job. A per-(site, code) cooldown table in ``Site.meta['autofix']``
prevents infinite loops when a fix doesn't actually move the score.

New fixers go in ``FIXERS`` and emit one or more queue.enqueue calls.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SeoAudit, Site

log = logging.getLogger(__name__)

# How long a given (site, code) is locked after we attempt a fix. The
# next audit cycle should reflect the change before we try again.
COOLDOWN = timedelta(hours=6)


def _enqueue_homepage(site_id: int, _post_id: int | None) -> str:
    from app.queue import content_q

    content_q.enqueue("app.jobs.content.generate_homepage_job", site_id, job_timeout=600)
    return "homepage regenerated"


def _enqueue_favicon(site_id: int, _post_id: int | None) -> str:
    from app.queue import content_q

    content_q.enqueue("app.jobs.content.generate_favicon_job", site_id, job_timeout=120)
    return "favicon (re)generated"


def _enqueue_image(site_id: int, post_id: int | None) -> str:
    if not post_id:
        return ""
    from app.queue import content_q

    content_q.enqueue("app.jobs.content.generate_image_job", post_id, "", job_timeout=600)
    return "featured image generated"


def _enqueue_legal(site_id: int, _post_id: int | None) -> str:
    from app.queue import content_q

    content_q.enqueue("app.jobs.content.generate_legal_job", site_id, job_timeout=300)
    return "legal pages regenerated"


# code → (handler, applies_to: 'site' | 'post' | 'any')
FIXERS: dict[str, tuple[Callable[[int, int | None], str], str]] = {
    "favicon-missing": (_enqueue_favicon, "site"),
    "meta-description": (_enqueue_homepage, "site"),
    "meta-description-length": (_enqueue_homepage, "site"),
    "og-image": (_enqueue_homepage, "site"),
    "imprint-missing": (_enqueue_legal, "site"),
    "img-alt": (_enqueue_image, "post"),
}


async def apply_autofixes(session: AsyncSession, audit: SeoAudit) -> list[str]:
    """Look at the audit's issues, fire fixers for known codes (subject
    to cooldown), and return human-readable notes about what we did.
    """
    if not audit or not audit.issues:
        return []
    site = await session.get(Site, audit.site_id)
    if not site:
        return []
    cooldowns: dict = ((site.meta or {}).get("autofix") or {})
    now = datetime.now(timezone.utc)

    notes: list[str] = []
    updated_cooldowns = dict(cooldowns)
    for issue in audit.issues:
        code = issue.get("code") if isinstance(issue, dict) else getattr(issue, "code", None)
        if not code or code not in FIXERS:
            continue
        last = cooldowns.get(code)
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
            except ValueError:
                last_dt = None
            if last_dt and now - last_dt < COOLDOWN:
                continue
        handler, scope = FIXERS[code]
        if scope == "post" and not audit.post_id:
            continue
        try:
            note = handler(audit.site_id, audit.post_id)
        except Exception:  # noqa: BLE001
            log.warning("autofix %s failed for site=%s post=%s", code, audit.site_id, audit.post_id, exc_info=True)
            continue
        if note:
            notes.append(f"{code} → {note}")
            updated_cooldowns[code] = now.isoformat()

    if notes:
        meta = dict(site.meta or {})
        meta["autofix"] = updated_cooldowns
        site.meta = meta
        await session.commit()
        log.info("autofix site=%s applied: %s", site.id, "; ".join(notes))
    return notes
