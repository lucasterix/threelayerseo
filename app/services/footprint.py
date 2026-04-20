"""Footprint-analysis helpers.

Given the network of registered domains + sites + servers, surface the
signals Google correlates when deciding whether a cluster of sites
is one operator. Returns structured data the dashboard renders as a
traffic-light matrix with concrete recommendations.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Domain, DomainStatus, Server, ServerStatus, Site, SiteStatus, Tier


@dataclass
class Signal:
    key: str
    label: str
    severity: str           # "red" | "yellow" | "green" | "gray"
    summary: str
    detail: str | None = None


@dataclass
class FootprintReport:
    signals: list[Signal]
    by_tier: dict[int, dict] = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)


async def analyse(session: AsyncSession) -> FootprintReport:
    # Pull live active domains + their sites + the server they're on.
    stmt = (
        select(Domain)
        .options(joinedload(Domain.site).joinedload(Site.server))
        .where(Domain.status == DomainStatus.ACTIVE)
    )
    domains = list((await session.execute(stmt)).unique().scalars().all())
    servers = list(
        (
            await session.execute(
                select(Server).where(Server.status.in_([ServerStatus.ACTIVE, ServerStatus.FULL]))
            )
        )
        .scalars()
        .all()
    )

    n = len(domains)
    signals: list[Signal] = []
    recs: list[str] = []

    # ─── IP diversity ───────────────────────────────────────────────────────
    site_ips: list[str] = []
    for d in domains:
        if d.site and d.site.server:
            site_ips.append(d.site.server.ip)
        else:
            # Fallback: if not assigned, we assume everyone lands on the
            # legacy single-server IP configured in settings.
            from app.config import settings
            site_ips.append(settings.server_ip)
    ip_counter = Counter(site_ips)
    unique_ips = len(ip_counter)

    if n == 0:
        signals.append(
            Signal("ip", "IP-Diversität", "gray", "Noch keine aktiven Domains.")
        )
    else:
        top_ip, top_count = ip_counter.most_common(1)[0]
        ratio = top_count / n
        if ratio >= 0.9 and n >= 3:
            sev = "red"
            recs.append(
                f"🔴 {top_count}/{n} Sites liegen auf {top_ip}. Zweiten Hetzner-Server anlegen oder Cloudflare-Proxy vor mindestens Tier-1 schieben."
            )
        elif ratio >= 0.6:
            sev = "yellow"
        else:
            sev = "green"
        signals.append(
            Signal(
                "ip",
                "IP-Diversität",
                sev,
                f"{unique_ips} unique IP(s) für {n} Sites · größter Cluster: {top_count} auf {top_ip}",
                detail=", ".join(f"{ip}: {c}" for ip, c in ip_counter.most_common()),
            )
        )

    # ─── Server capacity ─────────────────────────────────────────────────────
    from app.services.servers import fleet_capacity

    fc = await fleet_capacity(session)
    if not fc.servers:
        signals.append(
            Signal(
                "servers",
                "Server-Inventar",
                "yellow",
                "Keine Server gepflegt — alles läuft implizit auf 46.224.7.46.",
            )
        )
        recs.append(
            "⚠ Leg wenigstens einen Server-Eintrag an, damit Footprint-Alerts sinnvoll werden."
        )
    else:
        sev = fc.severity
        msg = (
            f"{fc.total_used}/{fc.total_limit} Site-Slots genutzt "
            f"({int(fc.total_pct * 100)} %) · Headroom {fc.headroom}"
        )
        if fc.needs_new_server:
            recs.append(
                f"🔴 Server-Auslastung {int(fc.total_pct * 100)} % — neuen Hetzner-Host buchen, bevor der nächste Bulk-Kauf läuft."
            )
        signals.append(Signal("servers", "Server-Kapazität", sev, msg))

    # ─── Tier coverage ───────────────────────────────────────────────────────
    by_tier: dict[int, dict] = {}
    for tier in Tier:
        tier_domains = [d for d in domains if d.tier == tier]
        by_tier[int(tier)] = {
            "name": tier.name,
            "count": len(tier_domains),
            "live_sites": sum(
                1 for d in tier_domains if d.site and d.site.status == SiteStatus.LIVE
            ),
            "unique_ips": len({
                d.site.server.ip for d in tier_domains
                if d.site and d.site.server
            }) or (1 if tier_domains else 0),
        }

    # If any tier is empty, mark as gray info signal.
    empty = [t.name for t in Tier if by_tier[int(t)]["count"] == 0]
    if empty:
        signals.append(
            Signal(
                "tier-coverage",
                "Tier-Abdeckung",
                "yellow" if len(empty) < 3 else "gray",
                f"Noch keine Domains in Tier: {', '.join(empty)}",
            )
        )
    else:
        signals.append(
            Signal("tier-coverage", "Tier-Abdeckung", "green", "Alle drei Tiers bestückt.")
        )

    # ─── Nameserver diversity ────────────────────────────────────────────────
    # Every INWX-registered domain uses ns.inwx.de by default. For real
    # diversity we'd need to rotate to other NS (Cloudflare, own bind, ...).
    ns_distinct = len({d.registrar for d in domains if d.registrar}) or 0
    if n and ns_distinct <= 1:
        signals.append(
            Signal(
                "nameserver",
                "Nameserver-Rotation",
                "yellow" if n < 5 else "red",
                f"Alle {n} Domains nutzen {domains[0].registrar if domains else '—'}-Nameserver.",
            )
        )
        if n >= 5:
            recs.append(
                "⚠ Tier-3-Sites auf Cloudflare umziehen — das ändert auch die NS und verwässert den Registrar-Fingerprint."
            )
    else:
        signals.append(
            Signal(
                "nameserver",
                "Nameserver-Rotation",
                "green",
                f"{ns_distinct} Nameserver-Provider",
            )
        )

    # ─── GSC account ─────────────────────────────────────────────────────────
    from app.services.gsc import is_configured as gsc_on

    if gsc_on():
        if n >= 5:
            signals.append(
                Signal(
                    "gsc",
                    "GSC-Account-Verteilung",
                    "yellow",
                    "1 Service-Account managed alle Properties — ok für Tier-1/2, "
                    "bei Tier-3 eigenen SA erwägen.",
                )
            )
        else:
            signals.append(
                Signal(
                    "gsc",
                    "GSC-Account-Verteilung",
                    "green",
                    "1 Service-Account reicht bei diesem Umfang.",
                )
            )
    else:
        signals.append(Signal("gsc", "Search Console", "gray", "Nicht angebunden."))

    # ─── WHOIS privacy (placeholder — INWX API call would populate) ────────
    signals.append(
        Signal(
            "whois",
            "WHOIS-Privacy",
            "yellow",
            "Status unbekannt — INWX-Status-Abruf noch nicht integriert.",
            detail="Bei INWX standardmäßig aus. Über das Kundenportal oder API einschaltbar.",
        )
    )

    return FootprintReport(signals=signals, by_tier=by_tier, recommendations=recs)
