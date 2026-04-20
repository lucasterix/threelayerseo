"""Competitor SERP analysis via DataForSEO SERP API.

Gives us the top-10 organic results for a keyword — URLs + titles +
snippets. Fed into the content generator so Claude can "beat what
ranks" rather than writing in a vacuum. Costs ~$0.003 per call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import settings

log = logging.getLogger(__name__)

BASE = "https://api.dataforseo.com"


@dataclass
class SerpResult:
    position: int
    title: str
    url: str
    domain: str
    description: str


def _auth():
    if not (settings.dataforseo_login and settings.dataforseo_password):
        return None
    return settings.dataforseo_login, settings.dataforseo_password


def top_results(keyword: str, location_code: int = 2276, language: str = "de", limit: int = 10) -> list[SerpResult]:
    auth = _auth()
    if not auth:
        return []
    try:
        r = httpx.post(
            f"{BASE}/v3/serp/google/organic/live/regular",
            auth=auth,
            json=[{
                "keyword": keyword,
                "location_code": location_code,
                "language_code": language,
                "depth": limit,
            }],
            timeout=45,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("DataForSEO SERP for %s: %s", keyword, e)
        return []

    tasks = data.get("tasks") or []
    if not tasks:
        return []
    items = ((tasks[0].get("result") or [{}])[0]).get("items") or []
    out: list[SerpResult] = []
    for item in items:
        if item.get("type") != "organic":
            continue
        out.append(
            SerpResult(
                position=item.get("rank_group") or item.get("rank_absolute") or 0,
                title=item.get("title", ""),
                url=item.get("url", ""),
                domain=item.get("domain", ""),
                description=item.get("description", "") or "",
            )
        )
        if len(out) >= limit:
            break
    return out


def compact_brief(results: list[SerpResult]) -> str:
    """Tight text summary suitable for a prompt. LLM-friendly."""
    if not results:
        return ""
    lines = ["Top-ranking competitors for this keyword (Google DE):"]
    for r in results:
        lines.append(f"#{r.position} {r.domain} — {r.title}")
        if r.description:
            lines.append(f"    > {r.description[:180]}")
    return "\n".join(lines)
