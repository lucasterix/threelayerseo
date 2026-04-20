"""Wayback Machine client.

Free public API. Three useful endpoints:

- /wayback/available — single closest snapshot to a timestamp
- /cdx/search/cdx  — list all snapshots (great for counting history)
- /web/<ts>id_/<url> — raw snapshot content (no Wayback UI frame)

We use (1) for quick live-check, (2) for a "this domain has 487
snapshots going back to 2011" signal, and (3) to feed the content
pipeline with the text of the last pre-expiry snapshot.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

AVAIL_URL = "https://archive.org/wayback/available"
CDX_URL = "http://web.archive.org/cdx/search/cdx"


@dataclass
class Snapshot:
    url: str
    timestamp: str   # YYYYMMDDhhmmss


def _timestamp_to_date(ts: str) -> str:
    if len(ts) < 8:
        return ts
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"


def last_snapshot(domain: str) -> Snapshot | None:
    try:
        r = httpx.get(AVAIL_URL, params={"url": domain}, timeout=15)
        r.raise_for_status()
        j: dict[str, Any] = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("wayback avail %s: %s", domain, e)
        return None
    closest = (j.get("archived_snapshots") or {}).get("closest")
    if not closest or not closest.get("available"):
        return None
    return Snapshot(url=closest["url"], timestamp=closest.get("timestamp", ""))


def snapshot_count(domain: str, cap: int = 2000) -> tuple[int, str | None, str | None]:
    """Return (count, first_date, last_date) from the CDX index.

    ``collapse=timestamp:8`` de-duplicates same-day captures so the number
    represents distinct days seen — a better signal for "this was a real
    site" than raw snapshot count.
    """
    try:
        r = httpx.get(
            CDX_URL,
            params={
                "url": domain,
                "output": "json",
                "limit": cap,
                "collapse": "timestamp:8",
            },
            timeout=25,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("wayback cdx %s: %s", domain, e)
        return 0, None, None
    if not rows or len(rows) < 2:
        return 0, None, None
    # header is rows[0]; data starts at rows[1]
    header = rows[0]
    try:
        ts_idx = header.index("timestamp")
    except ValueError:
        return 0, None, None
    first_ts = rows[1][ts_idx]
    last_ts = rows[-1][ts_idx]
    return (
        len(rows) - 1,
        _timestamp_to_date(first_ts),
        _timestamp_to_date(last_ts),
    )


def fetch_snapshot_text(domain: str, max_chars: int = 8000) -> str | None:
    """Grab the closest available snapshot's text content.

    Useful for feeding the homepage/post generator with "this is what the
    site used to be about" context on expired-domain buys.
    """
    snap = last_snapshot(domain)
    if not snap:
        return None
    # Insert "id_" after the timestamp so Wayback serves the raw response
    # without its wrapper chrome. Snap URL is
    # http://web.archive.org/web/<ts>/<original>.
    raw_url = snap.url.replace(f"/web/{snap.timestamp}/", f"/web/{snap.timestamp}id_/")
    try:
        r = httpx.get(raw_url, timeout=30, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("wayback raw fetch %s: %s", domain, e)
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for s in soup(["script", "style", "noscript"]):
        s.decompose()
    text = soup.get_text(separator=" ", strip=True)
    if not text:
        return None
    return text[:max_chars]
