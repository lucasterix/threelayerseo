"""DataForSEO client (Google Ads Keywords API + Labs endpoints).

Authentication: HTTP Basic with (login, password). Most endpoints return
a ``tasks[]`` list — we post a task and then the same sync endpoints
resolve immediately for the live endpoints (``*_live``).

Docs: https://dataforseo.com/apis
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

BASE = "https://api.dataforseo.com"


@dataclass
class KeywordIdea:
    keyword: str
    search_volume: int | None
    cpc: float | None
    competition: float | None
    difficulty: int | None = None


class DataForSeoError(RuntimeError):
    pass


def _auth() -> tuple[str, str]:
    if not (settings.dataforseo_login and settings.dataforseo_password):
        raise DataForSeoError("DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD must be set")
    return settings.dataforseo_login, settings.dataforseo_password


def _post(path: str, payload: list[dict[str, Any]]) -> dict:
    with httpx.Client(timeout=60, auth=_auth()) as c:
        r = c.post(f"{BASE}{path}", json=payload)
        r.raise_for_status()
        return r.json()


def keyword_volumes(keywords: list[str], location_code: int = 2276) -> list[KeywordIdea]:
    """Return search volume + CPC + competition for each keyword.

    ``location_code`` follows DataForSEO's location catalog — 2276 = Germany.
    """
    if not keywords:
        return []
    payload = [{"keywords": keywords, "location_code": location_code, "language_code": "de"}]
    data = _post("/v3/keywords_data/google_ads/search_volume/live", payload)
    out: list[KeywordIdea] = []
    for task in data.get("tasks", []):
        for item in (task.get("result") or []):
            out.append(
                KeywordIdea(
                    keyword=item.get("keyword", ""),
                    search_volume=item.get("search_volume"),
                    cpc=item.get("cpc"),
                    competition=item.get("competition"),
                )
            )
    return out


def keyword_ideas(seed: str, limit: int = 50, location_code: int = 2276) -> list[KeywordIdea]:
    """Suggestions around a seed keyword from DataForSEO Labs."""
    payload = [
        {
            "keywords": [seed],
            "location_code": location_code,
            "language_code": "de",
            "limit": limit,
        }
    ]
    data = _post("/v3/dataforseo_labs/google/keyword_ideas/live", payload)
    out: list[KeywordIdea] = []
    for task in data.get("tasks", []):
        for result in (task.get("result") or []):
            for item in (result.get("items") or []):
                info = item.get("keyword_info") or {}
                out.append(
                    KeywordIdea(
                        keyword=item.get("keyword", ""),
                        search_volume=info.get("search_volume"),
                        cpc=info.get("cpc"),
                        competition=info.get("competition"),
                        difficulty=(item.get("keyword_properties") or {}).get(
                            "keyword_difficulty"
                        ),
                    )
                )
    return out
