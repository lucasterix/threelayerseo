"""Cloudflare API client (v4).

Scoped to zone + DNS edits. Used to add domains as CF zones (which gives
us CF nameservers to switch to at INWX) and flip the "orange cloud" proxy
so origin IP stays hidden — the single biggest IP-diversity win since
every CF-proxied domain shares CF's anycast IP pool, not our one Hetzner.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

BASE = "https://api.cloudflare.com/client/v4"


class CloudflareError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    if not settings.cloudflare_api_token:
        raise CloudflareError("CLOUDFLARE_API_TOKEN not set")
    return {
        "Authorization": f"Bearer {settings.cloudflare_api_token}",
        "Content-Type": "application/json",
    }


def is_configured() -> bool:
    return bool(settings.cloudflare_api_token)


def verify_token() -> dict[str, Any] | None:
    """Returns the verification result dict or None on failure."""
    if not is_configured():
        return None
    try:
        r = httpx.get(f"{BASE}/user/tokens/verify", headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:  # noqa: BLE001
        log.warning("cloudflare verify_token: %s", e)
        return None


def list_accounts() -> list[dict[str, Any]]:
    r = httpx.get(f"{BASE}/accounts", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json().get("result", [])


def list_zones(per_page: int = 50) -> list[dict[str, Any]]:
    r = httpx.get(
        f"{BASE}/zones",
        headers=_headers(),
        params={"per_page": per_page},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("result", [])


def get_zone(domain: str) -> dict[str, Any] | None:
    r = httpx.get(
        f"{BASE}/zones",
        headers=_headers(),
        params={"name": domain},
        timeout=20,
    )
    r.raise_for_status()
    zones = r.json().get("result", [])
    return zones[0] if zones else None


def add_zone(domain: str, account_id: str) -> dict[str, Any]:
    r = httpx.post(
        f"{BASE}/zones",
        headers=_headers(),
        json={"name": domain, "account": {"id": account_id}, "type": "full"},
        timeout=30,
    )
    # Cloudflare returns 200 with success:true, or 4xx with errors
    if r.status_code >= 400:
        raise CloudflareError(f"add_zone {domain}: {r.status_code} {r.text[:300]}")
    j = r.json()
    if not j.get("success"):
        raise CloudflareError(f"add_zone {domain}: {j.get('errors')}")
    return j["result"]


def add_dns_record(
    zone_id: str,
    type_: str,
    name: str,
    content: str,
    proxied: bool = True,
    ttl: int = 1,
) -> dict[str, Any]:
    """``ttl=1`` means "automatic" in Cloudflare parlance — required when proxied."""
    r = httpx.post(
        f"{BASE}/zones/{zone_id}/dns_records",
        headers=_headers(),
        json={
            "type": type_,
            "name": name,
            "content": content,
            "ttl": ttl,
            "proxied": proxied,
        },
        timeout=20,
    )
    if r.status_code >= 400:
        raise CloudflareError(f"add_dns {name}: {r.status_code} {r.text[:300]}")
    return r.json().get("result") or {}


def onboard_domain(domain: str, origin_ip: str, account_id: str) -> dict[str, Any]:
    """Add ``domain`` as a CF zone and prepopulate A-records (apex + www)
    with the proxy enabled. Returns the zone record (includes the
    ``name_servers`` tuple the user must set at the registrar).
    """
    zone = get_zone(domain) or add_zone(domain, account_id)
    zone_id = zone["id"]
    add_dns_record(zone_id, "A", domain, origin_ip, proxied=True)
    add_dns_record(zone_id, "A", f"www.{domain}", origin_ip, proxied=True)
    return zone
