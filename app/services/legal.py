"""Auto-generate Impressum + Datenschutzerklärung via OpenAI.

These are legal documents in Germany:
- Impressum per §5 TMG + §18(2) MStV (contact data, liable person, tax ID)
- Datenschutzerklärung per DSGVO/GDPR (processing, rights, contact)

The generator is AI-assisted — the admin MUST review the output before a
site goes live. We bake legal disclaimers into the system prompt so the
output is conservative (no claims, no guarantees, clear liability clauses)
but this is not a substitute for a lawyer.
"""
from __future__ import annotations

import logging

import markdown as md_lib

from app.config import settings
from app.services.llm import complete_text

log = logging.getLogger(__name__)

SYSTEM_IMPRINT = """Du bist ein präziser Rechtstexter für deutsche
Websites. Erstelle ein vollständiges Impressum nach §5 TMG und §18(2)
MStV in Markdown. Pflichtfelder:

1. Überschrift "# Impressum"
2. Abschnitt "## Angaben gemäß §5 TMG": Name, Anschrift, Kontakt
3. Abschnitt "## Verantwortlich für den Inhalt nach §18 Abs. 2 MStV"
   (derselbe wie oben, wenn nicht anders angegeben)
4. "## Haftungsausschluss" mit Unterabschnitten:
   - Haftung für Inhalte (§§ 8-10 TMG-konform formuliert)
   - Haftung für Links
   - Urheberrecht
5. "## Streitschlichtung" mit EU-ODR-Link (https://ec.europa.eu/consumers/odr)
   und Hinweis, dass Betreiber nicht zur Teilnahme verpflichtet ist
6. Falls USt-ID übergeben: Abschnitt USt-ID

Regeln:
- Verwende NUR die übergebenen Daten. Erfinde nichts.
- Keine Platzhalter ("[Name]") im Output.
- Keine Markenrechte Dritter erwähnen.
- Konservative Haftungsklauseln, keine Werbesprache.
- Nur Markdown, kein YAML, keine Code-Fences."""


SYSTEM_PRIVACY = """Du bist ein präziser Rechtstexter für deutsche
Websites. Erstelle eine DSGVO-konforme Datenschutzerklärung in Markdown
für eine Blog-Website, die NICHT folgendes nutzt:
- Google Analytics / Google Tag Manager
- Cookies über reine Session-Cookies hinaus
- Externe Tracker / Pixel / Social Plugins
- Newsletter-Systeme
- Kommentarfunktionen

Das, was tatsächlich passiert:
- Server-Zugriffs-Logs (IP, Timestamp, User-Agent, Referer) für
  technischen Betrieb (Art. 6(1)(f) DSGVO berechtigtes Interesse)
- TLS-Zertifikat-Provisionierung via Let's Encrypt (Caddy)

Inhalt:
1. "# Datenschutzerklärung"
2. "## Verantwortlicher" mit Kontaktdaten
3. "## Erhobene Daten und Zwecke" (Server-Logs, technische Stabilität)
4. "## Rechtsgrundlage" (Art. 6(1)(f) DSGVO für Logs)
5. "## Speicherdauer" (typisch 7-14 Tage für Logs)
6. "## Empfänger der Daten" (keine Weitergabe an Dritte außer Hosting-Provider)
7. "## Betroffenenrechte" (Art. 15-22 DSGVO: Auskunft, Berichtigung,
   Löschung, Einschränkung, Widerspruch, Datenübertragbarkeit, Beschwerde
   bei Aufsichtsbehörde)
8. "## Kontakt für Datenschutzanfragen" (die zu Verfügung gestellte E-Mail)
9. "## Hosting" (kurz, Hetzner in Deutschland)

Regeln:
- Nur Markdown, keine Code-Fences, kein YAML.
- Verwende die übergebenen Daten exakt.
- Keine Erwähnung von Analytics/Tracking/Cookies, die es nicht gibt.
- Konservativer Ton, keine Werbesprache."""


def _operator_block() -> str:
    if not settings.operator_name or not settings.operator_email:
        raise RuntimeError(
            "OPERATOR_NAME and OPERATOR_EMAIL must be set in .env before "
            "generating Impressum or Datenschutz."
        )
    lines = [
        f"Name: {settings.operator_name}",
        f"Anschrift: {settings.operator_address or '(nicht angegeben)'}",
        f"E-Mail: {settings.operator_email}",
    ]
    if settings.operator_phone:
        lines.append(f"Telefon: {settings.operator_phone}")
    if settings.operator_tax_id:
        lines.append(f"USt-ID: {settings.operator_tax_id}")
    return "\n".join(lines)


def generate_imprint_markdown(site_title: str, site_domain: str) -> str:
    user = (
        f"Betreiberdaten:\n{_operator_block()}\n\n"
        f"Website: {site_title} ({site_domain})\n"
        "Erstelle jetzt das Impressum."
    )
    return complete_text(SYSTEM_IMPRINT, user, max_tokens=2000).strip()


def generate_privacy_markdown(site_title: str, site_domain: str) -> str:
    user = (
        f"Betreiberdaten:\n{_operator_block()}\n\n"
        f"Website: {site_title} ({site_domain})\n"
        "Erstelle jetzt die Datenschutzerklärung."
    )
    return complete_text(SYSTEM_PRIVACY, user, max_tokens=2000).strip()


def to_html(markdown_text: str) -> str:
    return md_lib.markdown(markdown_text, extensions=["extra", "sane_lists"])
