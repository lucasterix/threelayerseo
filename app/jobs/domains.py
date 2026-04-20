"""Domain purchase job.

Registers the domain at INWX, stores the registrar response, sets A records
for apex + www pointing at the server IP, and creates a draft Site row.

No Caddy config touch required — the catchall site block with on-demand TLS
handles any hostname whose ACTIVE status in our DB the renderer's
/_/caddy-ask endpoint confirms.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import settings
from app.db import SessionLocal
from app.models import Domain, DomainStatus, Site, SiteStatus
from app.registrars.inwx import InwxRegistrar

log = logging.getLogger(__name__)


async def _register(domain_id: int) -> None:
    async with SessionLocal() as session:
        domain = await session.get(Domain, domain_id)
        if not domain:
            log.error("domain %s not found", domain_id)
            return
        domain.status = DomainStatus.PURCHASING
        await session.commit()

        inwx = InwxRegistrar()
        try:
            result = inwx.register(domain.name, period_years=1)
            if not result.success:
                domain.status = DomainStatus.FAILED
                domain.notes = result.error or "unknown registrar error"
                await session.commit()
                log.error("registrar rejected %s: %s", domain.name, result.error)
                return

            domain.registrar_domain_id = result.registrar_domain_id
            if result.expires_at:
                try:
                    domain.expires_at = datetime.fromisoformat(result.expires_at).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    log.warning("could not parse exDate %s", result.expires_at)
            domain.registered_at = datetime.now(timezone.utc)

            # DNS: apex + www. The INWX default zone may already contain
            # a few records from registration, so these calls are best-effort.
            try:
                inwx.set_a_record(domain.name, domain.name, settings.server_ip)
                inwx.set_a_record(domain.name, f"www.{domain.name}", settings.server_ip)
            except Exception:  # noqa: BLE001
                log.warning("DNS setup partial for %s", domain.name, exc_info=True)

            site = Site(
                domain_id=domain.id,
                title=domain.name,
                topic=domain.name,
                status=SiteStatus.DRAFT,
            )
            session.add(site)
            domain.status = DomainStatus.ACTIVE
            await session.commit()
            log.info("registered %s tier=%s", domain.name, domain.tier.name)
        except Exception as e:  # noqa: BLE001
            domain.status = DomainStatus.FAILED
            domain.notes = str(e)[:500]
            await session.commit()
            log.exception("registration failed for %s", domain.name)


def register_domain_job(domain_id: int) -> None:
    """RQ entry-point. Sync wrapper around the async impl."""
    asyncio.run(_register(domain_id))
