"""INWX registrar client.

Uses the official ``inwx-domrobot`` SDK (JSON-RPC under the hood). Test mode
targets api.ote.inwx.com (OT&E sandbox); production targets api.inwx.com.

Docs: https://www.inwx.de/en/help/apidoc
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from INWX.Domrobot import ApiClient  # type: ignore[import-untyped]

from app.config import settings
from app.registrars.base import DomainAvailability, RegistrationResult

log = logging.getLogger(__name__)


class InwxError(RuntimeError):
    pass


class InwxRegistrar:
    name = "inwx"

    def __init__(
        self,
        user: str | None = None,
        password: str | None = None,
        shared_secret: str | None = None,
        test_mode: bool | None = None,
    ) -> None:
        self.user = user or settings.inwx_user
        self.password = password or settings.inwx_password
        self.shared_secret = shared_secret or settings.inwx_shared_secret
        self.test_mode = settings.inwx_test_mode if test_mode is None else test_mode
        if not self.user or not self.password:
            raise InwxError("INWX credentials missing — set INWX_USER and INWX_PASSWORD")

    @contextmanager
    def _session(self):
        api_url = (
            ApiClient.API_OTE_URL if self.test_mode else ApiClient.API_LIVE_URL
        )
        client = ApiClient(api_url=api_url, debug_mode=False)
        res = client.login(self.user, self.password, self.shared_secret or None)
        if res.get("code") != 1000:
            raise InwxError(f"INWX login failed: {res}")
        try:
            yield client
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                log.warning("INWX logout failed", exc_info=True)

    def check(self, domains: list[str]) -> list[DomainAvailability]:
        if not domains:
            return []
        with self._session() as c:
            res = c.call_api(api_method="domain.check", method_params={"domain": domains, "wide": 2})
        if res.get("code") != 1000:
            raise InwxError(f"INWX check failed: {res}")
        out: list[DomainAvailability] = []
        for item in res["resData"].get("domain", []):
            avail = item.get("avail") == 1
            price = item.get("price")
            price_cents = int(round(float(price) * 100)) if price is not None else None
            out.append(
                DomainAvailability(
                    name=item["domain"],
                    available=avail,
                    price_cents=price_cents,
                    currency=item.get("currency", "EUR"),
                    reason=item.get("status") if not avail else None,
                )
            )
        return out

    def register(
        self,
        domain: str,
        period_years: int = 1,
        nameservers: list[str] | None = None,
    ) -> RegistrationResult:
        params: dict = {"domain": domain, "period": f"{period_years}Y"}
        if nameservers:
            params["ns"] = nameservers
        with self._session() as c:
            res = c.call_api(api_method="domain.create", method_params=params)
        if res.get("code") not in (1000, 1001):
            return RegistrationResult(name=domain, success=False, error=str(res))
        data = res.get("resData", {}) or {}
        return RegistrationResult(
            name=domain,
            success=True,
            registrar_domain_id=str(data.get("roId", "")) or None,
            expires_at=data.get("exDate"),
        )

    def set_nameservers(self, domain: str, nameservers: list[str]) -> bool:
        with self._session() as c:
            res = c.call_api(
                api_method="domain.update",
                method_params={"domain": domain, "ns": nameservers},
            )
        return res.get("code") == 1000

    def set_a_record(self, domain: str, hostname: str, ip: str) -> bool:
        """Set an A record via INWX DNS (nameserver.*). ``hostname`` is FQDN."""
        with self._session() as c:
            res = c.call_api(
                api_method="nameserver.createRecord",
                method_params={
                    "domain": domain,
                    "type": "A",
                    "name": hostname,
                    "content": ip,
                    "ttl": 3600,
                },
            )
        return res.get("code") == 1000

    def initiate_transfer(
        self,
        domain: str,
        authinfo: str,
        *,
        period_years: int = 1,
        nameservers: list[str] | None = None,
    ) -> dict:
        """Start a domain transfer from another registrar to INWX.

        Returns the raw INWX response dict. Success code is 1000 or 1001
        (1001 = waiting for losing-registrar confirmation). The transfer
        completes server-side 5-7 days later; the caller should persist
        a Domain row with status=PURCHASING and re-check later.
        """
        params: dict = {
            "domain": domain,
            "authinfo": authinfo,
            "period": f"{period_years}Y",
        }
        if nameservers:
            params["ns"] = nameservers
        with self._session() as c:
            res = c.call_api(api_method="domain.transfer", method_params=params)
        return res

    def set_txt_record(self, domain: str, hostname: str, value: str) -> bool:
        """Add a TXT record (e.g. Google Site Verification)."""
        with self._session() as c:
            res = c.call_api(
                api_method="nameserver.createRecord",
                method_params={
                    "domain": domain,
                    "type": "TXT",
                    "name": hostname,
                    "content": value,
                    "ttl": 300,
                },
            )
        return res.get("code") == 1000
