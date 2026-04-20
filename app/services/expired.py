"""Expired-domain analyser.

Takes a list of candidate domains and enriches each in parallel with:

- INWX availability + price (one bulk call covers them all)
- Wayback Machine snapshot count + first/last dates (per-domain)
- DataForSEO backlinks summary — rank, referring domains (per-domain)

The orchestrator is sync because the underlying SDKs are sync; we run
the per-domain enrichment in a ThreadPoolExecutor so 50 candidates
finish in seconds, not minutes.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.services.backlinks import BacklinkSummary, summary as backlink_summary
from app.services.domains import check_availability
from app.services.wayback import snapshot_count

log = logging.getLogger(__name__)


@dataclass
class ExpiredCandidate:
    name: str
    available: bool
    price_cents: int | None
    currency: str
    wayback_count: int
    wayback_first: str | None
    wayback_last: str | None
    backlinks: BacklinkSummary | None
    reason: str | None = None

    @property
    def score(self) -> int:
        """Heuristic ranking score. Higher = more tempting to buy."""
        s = 0
        if self.available:
            s += 10
        s += min(self.wayback_count, 400) // 5         # up to +80
        if self.backlinks:
            s += min(self.backlinks.rank or 0, 600) // 10  # up to +60
            s += min((self.backlinks.referring_main_domains or 0), 200) // 2  # up to +100
        return s


def analyse(candidates: list[str]) -> list[ExpiredCandidate]:
    """Return candidates sorted by descending score."""
    if not candidates:
        return []

    try:
        avail = check_availability(candidates)
    except Exception as e:  # noqa: BLE001
        log.exception("INWX availability bulk call failed")
        raise RuntimeError(f"INWX check failed: {e}") from e
    avail_map = {r.name: r for r in avail}

    def enrich(name: str) -> ExpiredCandidate:
        a = avail_map.get(name)
        count, first, last = snapshot_count(name)
        bl = backlink_summary(name)
        return ExpiredCandidate(
            name=name,
            available=bool(a and a.available),
            price_cents=a.price_cents if a else None,
            currency=a.currency if a else "EUR",
            wayback_count=count,
            wayback_first=first,
            wayback_last=last,
            backlinks=bl,
            reason=a.reason if a and not a.available else None,
        )

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(enrich, candidates))

    results.sort(key=lambda c: c.score, reverse=True)
    return results
