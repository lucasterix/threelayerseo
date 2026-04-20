from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class DomainAvailability:
    name: str
    available: bool
    price_cents: int | None = None
    currency: str = "EUR"
    reason: str | None = None


@dataclass
class RegistrationResult:
    name: str
    success: bool
    registrar_domain_id: str | None = None
    expires_at: str | None = None
    error: str | None = None


class Registrar(Protocol):
    name: str

    def check(self, domains: list[str]) -> list[DomainAvailability]: ...

    def register(self, domain: str, period_years: int = 1, nameservers: list[str] | None = None) -> RegistrationResult: ...

    def set_nameservers(self, domain: str, nameservers: list[str]) -> bool: ...

    def set_a_record(self, domain: str, hostname: str, ip: str) -> bool: ...
