"""DataForSEO Backlinks summary.

Gives us a rough link-juice score per domain: DataForSEO's ``rank`` is a
0-1000 PageRank-like number, plus counts for total backlinks and
referring (main) domains. Perfect for ranking expired-domain candidates.

Requires a DataForSEO plan that includes Backlinks endpoints. On 403 or
similar, we return ``None`` and the expired-finder UI just shows "—".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import settings

log = logging.getLogger(__name__)

BASE = "https://api.dataforseo.com"


@dataclass
class BacklinkSummary:
    rank: int | None                   # 0-1000 DataForSEO rank
    backlinks: int | None
    referring_domains: int | None
    referring_main_domains: int | None
    first_seen: str | None
    last_visited: str | None


def _auth():
    if not (settings.dataforseo_login and settings.dataforseo_password):
        return None
    return settings.dataforseo_login, settings.dataforseo_password


def summary(target: str) -> BacklinkSummary | None:
    auth = _auth()
    if not auth:
        return None
    try:
        r = httpx.post(
            f"{BASE}/v3/backlinks/summary/live",
            auth=auth,
            json=[{"target": target, "include_subdomains": True}],
            timeout=30,
        )
        if r.status_code == 403:
            log.warning("DataForSEO backlinks: account lacks subscription / not verified")
            return None
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("DataForSEO backlinks error for %s: %s", target, e)
        return None

    tasks = data.get("tasks") or []
    if not tasks:
        return None
    result = (tasks[0].get("result") or [None])[0]
    if not result:
        return None
    return BacklinkSummary(
        rank=result.get("rank"),
        backlinks=result.get("backlinks"),
        referring_domains=result.get("referring_domains"),
        referring_main_domains=result.get("referring_main_domains"),
        first_seen=result.get("first_seen"),
        last_visited=result.get("last_visited"),
    )
