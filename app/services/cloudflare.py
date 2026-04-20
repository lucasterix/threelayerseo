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


def _token(*, write: bool) -> str:
    """Return the appropriate token. For writes the edit-scoped token
    is required; for reads we prefer the explicit read token if set,
    otherwise reuse the edit token (which often has read too).
    """
    if write:
        if not settings.cloudflare_api_token:
            raise CloudflareError("CLOUDFLARE_API_TOKEN not set")
        return settings.cloudflare_api_token
    token = settings.cloudflare_read_token or settings.cloudflare_api_token
    if not token:
        raise CloudflareError("no Cloudflare token configured")
    return token


def _headers(*, write: bool = False) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token(write=write)}",
        "Content-Type": "application/json",
    }


def is_configured() -> bool:
    return bool(settings.cloudflare_api_token or settings.cloudflare_read_token)


def account_id() -> str | None:
    return settings.cloudflare_account_id or None


def verify_token() -> dict[str, Any] | None:
    """Verify whichever read-capable token we have."""
    try:
        headers = _headers(write=False)
    except CloudflareError:
        return None
    try:
        r = httpx.get(f"{BASE}/user/tokens/verify", headers=headers, timeout=10)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:  # noqa: BLE001
        log.warning("cloudflare verify_token: %s", e)
        return None


def list_accounts() -> list[dict[str, Any]]:
    r = httpx.get(f"{BASE}/accounts", headers=_headers(write=False), timeout=15)
    r.raise_for_status()
    return r.json().get("result", [])


def list_zones(per_page: int = 50, account_id_: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"per_page": per_page}
    if account_id_:
        params["account.id"] = account_id_
    r = httpx.get(
        f"{BASE}/zones",
        headers=_headers(write=False),
        params=params,
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("result", [])


def get_zone(domain: str) -> dict[str, Any] | None:
    r = httpx.get(
        f"{BASE}/zones",
        headers=_headers(write=False),
        params={"name": domain},
        timeout=20,
    )
    r.raise_for_status()
    zones = r.json().get("result", [])
    return zones[0] if zones else None


def add_zone(domain: str, account_id_: str) -> dict[str, Any]:
    r = httpx.post(
        f"{BASE}/zones",
        headers=_headers(write=True),
        json={"name": domain, "account": {"id": account_id_}, "type": "full"},
        timeout=30,
    )
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
        headers=_headers(write=True),
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


def onboard_domain(domain: str, origin_ip: str, account_id_: str) -> dict[str, Any]:
    """Add ``domain`` as a CF zone and prepopulate A-records (apex + www)
    with the proxy enabled. Returns the zone record (includes the
    ``name_servers`` tuple the user must set at the registrar).
    """
    zone = get_zone(domain) or add_zone(domain, account_id_)
    zone_id = zone["id"]
    # Best-effort: if the records exist already (re-onboard), CF returns
    # 409. Swallow so the overall flow still reports the zone.
    for name in (domain, f"www.{domain}"):
        try:
            add_dns_record(zone_id, "A", name, origin_ip, proxied=True)
        except CloudflareError as e:
            if "already exists" in str(e).lower() or "dns record" in str(e).lower():
                log.info("CF record %s already exists, skipping", name)
            else:
                raise
    return zone
