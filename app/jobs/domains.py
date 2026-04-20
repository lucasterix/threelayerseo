"""Domain purchase job.

Registers the domain at INWX, stores the registrar response, sets A records
for apex + www, creates a draft Site row, and — if GSC is configured —
auto-onboards the domain into Search Console (DNS TXT verification +
sitemap submit).
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

            try:
                inwx.set_a_record(domain.name, domain.name, settings.server_ip)
                inwx.set_a_record(domain.name, f"www.{domain.name}", settings.server_ip)
            except Exception:  # noqa: BLE001
                log.warning("DNS A-record setup partial for %s", domain.name, exc_info=True)

            # Auto-assign to the least-full active server, if any exists.
            from app.services.servers import pick_least_full_server

            srv = await pick_least_full_server(session)
            site = Site(
                domain_id=domain.id,
                server_id=srv.id if srv else None,
                title=domain.name,
                topic=domain.name,
                status=SiteStatus.DRAFT,
            )
            session.add(site)
            domain.status = DomainStatus.ACTIVE
            meta = dict(domain.meta or {})

            # Google Search Console onboarding. Skipped cleanly if not configured.
            try:
                from app.services.gsc import is_configured, onboard_domain_in_gsc

                if is_configured():
                    gsc_result = onboard_domain_in_gsc(
                        domain.name,
                        lambda d, host, value: inwx.set_txt_record(d, host, value),
                    )
                    meta["gsc"] = gsc_result
                    log.info("GSC onboarding for %s: %s", domain.name, gsc_result)
            except Exception:  # noqa: BLE001
                log.warning("GSC onboarding failed for %s", domain.name, exc_info=True)

            domain.meta = meta
            await session.commit()
            log.info("registered %s tier=%s", domain.name, domain.tier.name)
        except Exception as e:  # noqa: BLE001
            domain.status = DomainStatus.FAILED
            domain.notes = str(e)[:500]
            await session.commit()
            log.exception("registration failed for %s", domain.name)


def register_domain_job(domain_id: int) -> None:
    asyncio.run(_register(domain_id))
