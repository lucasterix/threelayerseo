"""Google Search Console integration.

The only reliable way to ask Google to crawl our sites programmatically.
Flow:

1. User creates a Google Cloud project, enables Search Console API, makes
   an OAuth client (Web application) with redirect URI
   https://seo.zdkg.de/integrations/gsc/callback — puts
   GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in .env.
2. Admin clicks "Connect Search Console" -> this module's build_auth_url
   redirects to Google -> callback handler exchanges the code for a
   refresh token -> operator pastes it into .env as GOOGLE_REFRESH_TOKEN.
3. Per-site verification (TXT record via INWX) + sitemap submission happen
   from the worker on site activation.

For domains to be usable: each domain needs a verified GSC property. This
module provides the API calls — verification itself is one-time per domain
(DNS TXT record) and runs right after registrar setup.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/webmasters"


class GscError(RuntimeError):
    pass


def build_auth_url(redirect_uri: str, state: str = "") -> str:
    if not settings.google_client_id:
        raise GscError("GOOGLE_CLIENT_ID not set")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    """Trade the authorization code for tokens. Returns the Google
    response body — the caller decides whether to persist the refresh
    token to .env.
    """
    if not (settings.google_client_id and settings.google_client_secret):
        raise GscError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set")
    r = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def _access_token() -> str:
    if not settings.google_refresh_token:
        raise GscError("GOOGLE_REFRESH_TOKEN not set")
    r = httpx.post(
        TOKEN_URL,
        data={
            "refresh_token": settings.google_refresh_token,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def submit_sitemap(site_url: str, sitemap_url: str) -> bool:
    """Submit/update a sitemap for an already-verified GSC property.

    ``site_url`` is the exact property URL as registered in GSC, e.g.
    ``https://example.de/``. Returns True on 200/204.
    """
    try:
        token = _access_token()
    except GscError as e:
        log.warning("GSC sitemap submit skipped: %s", e)
        return False
    headers = {"Authorization": f"Bearer {token}"}
    from urllib.parse import quote

    url = (
        f"https://searchconsole.googleapis.com/webmasters/v3/sites/"
        f"{quote(site_url, safe='')}/sitemaps/{quote(sitemap_url, safe='')}"
    )
    r = httpx.put(url, headers=headers, timeout=20)
    if r.status_code in (200, 204):
        log.info("sitemap %s submitted for %s", sitemap_url, site_url)
        return True
    log.warning("GSC submit failed %s: %s", r.status_code, r.text[:200])
    return False


def add_site_property(site_url: str) -> bool:
    """Add a new site to Search Console. After this, the property shows
    up in GSC as UNVERIFIED — use ``verify_by_dns`` to finalize.
    """
    try:
        token = _access_token()
    except GscError:
        return False
    from urllib.parse import quote

    url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{quote(site_url, safe='')}"
    r = httpx.put(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    return r.status_code in (200, 204)


def dns_verification_token(domain: str) -> str | None:
    """Fetch the DNS TXT verification string Google wants added to the zone.

    Google's Site Verification API returns the token we write as TXT; the
    admin then triggers INWX to create the record. This is a distinct API
    (``siteverification``) from Search Console.
    """
    try:
        token = _access_token()
    except GscError:
        return None
    r = httpx.post(
        "https://www.googleapis.com/siteVerification/v1/token",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "site": {"type": "INET_DOMAIN", "identifier": domain},
            "verificationMethod": "DNS_TXT",
        },
        timeout=20,
    )
    if r.status_code != 200:
        log.warning("GSC verification token failed %s: %s", r.status_code, r.text[:200])
        return None
    return r.json().get("token")
