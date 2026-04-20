"""Google Search Console + Site Verification via a service account.

No OAuth dance. The admin creates one GCP project, one service account,
downloads the JSON key, and scps it onto the host. The SA email is added
as owner to every GSC property we manage — but since we also run the
verification flow programmatically (DNS TXT via INWX), the SA can claim
new domains without the user clicking through GSC.

This beats OAuth for our use-case: service-account credentials never
expire, `webmasters` is a sensitive scope that would otherwise need
Google verification or a 7-day testing-mode lifetime on refresh tokens.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import httpx

try:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import service_account
except ImportError:  # pragma: no cover
    service_account = None  # type: ignore
    GoogleAuthRequest = None  # type: ignore

from app.config import settings

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/webmasters",
    "https://www.googleapis.com/auth/siteverification",
]


class GscError(RuntimeError):
    pass


def _credentials():
    if service_account is None:
        raise GscError("google-auth not installed")
    path = settings.google_credentials_path
    if not path or not os.path.exists(path):
        raise GscError(f"google SA key not found at {path}")
    return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)


def service_account_email() -> str | None:
    try:
        return _credentials().service_account_email
    except GscError:
        return None


def _access_token() -> str:
    creds = _credentials()
    creds.refresh(GoogleAuthRequest())
    return creds.token


def is_configured() -> bool:
    return bool(
        settings.google_credentials_path
        and os.path.exists(settings.google_credentials_path)
        and service_account is not None
    )


# ─── Site Verification ─────────────────────────────────────────────────────

def dns_verification_token(domain: str) -> str | None:
    """Return Google's DNS TXT verification string for ``domain``.

    Google returns a token like ``google-site-verification=ABC...`` — that
    full string goes as the value of a TXT record at the apex.
    """
    try:
        token = _access_token()
    except GscError as e:
        log.warning("GSC token fetch skipped: %s", e)
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
        log.warning("verification token request failed %s: %s", r.status_code, r.text[:200])
        return None
    return r.json().get("token")


def verify_domain(domain: str) -> bool:
    """Ask Google to check the TXT record for ``domain`` and claim
    ownership by the service account.
    """
    try:
        token = _access_token()
    except GscError:
        return False
    r = httpx.post(
        "https://www.googleapis.com/siteVerification/v1/webResource",
        headers={"Authorization": f"Bearer {token}"},
        params={"verificationMethod": "DNS_TXT"},
        json={
            "site": {"type": "INET_DOMAIN", "identifier": domain},
            "verificationMethod": "DNS_TXT",
        },
        timeout=30,
    )
    if r.status_code in (200, 201):
        log.info("GSC verified %s", domain)
        return True
    log.warning("GSC verify failed for %s: %s %s", domain, r.status_code, r.text[:300])
    return False


# ─── Search Console ────────────────────────────────────────────────────────

def add_site_property(site_url: str) -> bool:
    """Register a site in Search Console. ``site_url`` needs the trailing
    slash: ``https://example.de/``.
    """
    try:
        token = _access_token()
    except GscError:
        return False
    url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{quote(site_url, safe='')}"
    r = httpx.put(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if r.status_code in (200, 204):
        log.info("added GSC site %s", site_url)
        return True
    log.warning("GSC add-site failed %s: %s %s", site_url, r.status_code, r.text[:200])
    return False


def submit_sitemap(site_url: str, sitemap_url: str) -> bool:
    try:
        token = _access_token()
    except GscError as e:
        log.debug("GSC sitemap submit skipped: %s", e)
        return False
    url = (
        f"https://searchconsole.googleapis.com/webmasters/v3/sites/"
        f"{quote(site_url, safe='')}/sitemaps/{quote(sitemap_url, safe='')}"
    )
    r = httpx.put(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if r.status_code in (200, 204):
        log.info("sitemap %s submitted for %s", sitemap_url, site_url)
        return True
    log.warning("GSC sitemap submit failed %s: %s %s", site_url, r.status_code, r.text[:200])
    return False


# ─── Convenience ────────────────────────────────────────────────────────────

def onboard_domain_in_gsc(domain: str, inwx_set_txt) -> dict[str, Any]:
    """End-to-end: fetch token -> add DNS TXT via INWX -> verify -> add
    both a domain property (``sc-domain:example.de``) and a URL-prefix
    property (``https://example.de/``) -> submit sitemap against the
    URL-prefix property.

    ``inwx_set_txt(domain, name, value)`` is a callable. Returns a dict
    summarising each step's outcome for logging.
    """
    out: dict[str, Any] = {"domain": domain}
    token = dns_verification_token(domain)
    out["token_fetched"] = bool(token)
    if not token:
        return out
    out["dns_txt_set"] = bool(inwx_set_txt(domain, domain, token))
    out["verified"] = verify_domain(domain)
    if not out["verified"]:
        return out
    sc_domain = f"sc-domain:{domain}"
    url_prefix = f"https://{domain}/"
    out["sc_domain_added"] = add_site_property(sc_domain)
    out["url_prefix_added"] = add_site_property(url_prefix)
    out["sitemap_submitted"] = submit_sitemap(url_prefix, f"https://{domain}/sitemap.xml")
    return out
