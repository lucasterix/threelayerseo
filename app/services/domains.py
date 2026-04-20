"""Domain inventory service.

Expands user input (bare names + TLDs) into candidate domains, calls the
registrar for availability + price, and stores buy requests. Actual purchase
happens asynchronously via ``app.jobs.domains.register_domain_job``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Domain, DomainStatus, Tier
from app.registrars.base import DomainAvailability
from app.registrars.inwx import InwxRegistrar

# Conservative list used when a user enters a bare name without a TLD.
DEFAULT_TLDS = ("de", "com", "net", "org", "info", "shop", "online", "tips")

_DOMAIN_RE = re.compile(r"^(?=.{1,253}\b)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z]{2,})+$")


@dataclass
class SearchResult:
    name: str
    available: bool
    price_cents: int | None
    currency: str
    reason: str | None = None


def _parse_line(line: str) -> str | None:
    line = line.strip().lower().lstrip("*").lstrip(".")
    if not line or line.startswith("#"):
        return None
    line = re.sub(r"^https?://", "", line)
    line = line.split("/")[0]
    line = line.lstrip("www.")
    return line or None


def expand_candidates(raw: str, tlds: list[str]) -> list[str]:
    """Build a de-duplicated list of candidate FQDNs from user input.

    * Lines that already look like a full domain (contain a dot) are kept.
    * Lines without a dot get combined with each selected TLD.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        parsed = _parse_line(line)
        if not parsed:
            continue
        if "." in parsed:
            candidate = parsed
            if candidate not in seen and _DOMAIN_RE.match(candidate):
                seen.add(candidate)
                out.append(candidate)
        else:
            base = re.sub(r"[^a-z0-9-]", "-", parsed).strip("-")
            if not base:
                continue
            for tld in tlds:
                candidate = f"{base}.{tld.lower().lstrip('.')}"
                if candidate in seen:
                    continue
                if _DOMAIN_RE.match(candidate):
                    seen.add(candidate)
                    out.append(candidate)
    return out


def check_availability(names: list[str]) -> list[SearchResult]:
    """Synchronously query INWX. INWX caps domain.check at 50 names per call."""
    if not names:
        return []
    inwx = InwxRegistrar()
    out: list[SearchResult] = []
    for i in range(0, len(names), 50):
        batch = names[i : i + 50]
        for r in inwx.check(batch):
            out.append(
                SearchResult(
                    name=r.name,
                    available=r.available,
                    price_cents=r.price_cents,
                    currency=r.currency,
                    reason=r.reason,
                )
            )
    return out


async def queue_purchases(session: AsyncSession, items: list[tuple[str, Tier, int | None]]) -> list[int]:
    """Create Domain rows in PENDING status for each (name, tier, price_cents).

    Returns list of Domain.id values the caller can hand to the worker queue.
    Duplicates (already in DB) are skipped silently.
    """
    created_ids: list[int] = []
    for name, tier, price_cents in items:
        existing = await session.scalar(select(Domain).where(Domain.name == name))
        if existing:
            continue
        tld = name.rsplit(".", 1)[-1]
        d = Domain(
            name=name,
            tld=tld,
            tier=tier,
            status=DomainStatus.PENDING,
            registrar="inwx",
            price_cents=price_cents,
            currency="EUR",
        )
        session.add(d)
        await session.flush()
        created_ids.append(d.id)
    await session.commit()
    return created_ids
