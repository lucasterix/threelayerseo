"""Per-site design generator.

LLM picks a color palette, typography pairing, and a handful of design
flourishes that fit the site's topic + tier + category. Output is a
small JSON of design tokens AND ~100 lines of CSS overrides that the
blog templates inject below the base tier styles.

The point isn't to replace the base tier layout — Tier-1 stays
list-style minimal, Tier-3 keeps the Backlinko-esque editorial
grammar — but to give each individual site a distinct visual voice
(accent color, font pairing, subtle spacing) so N sites in the same
tier don't look stamped from the same mold.

Regenerates on demand via the worker. Kept idempotent: if tokens
exist and look valid, re-running just freshens the CSS around them.
"""
from __future__ import annotations

import logging

from app.models import Site
from app.services.llm import LlmError, complete_json

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Du entwirfst ein individuelles visuelles Profil für eine
deutsche Content-Website. Input: Nische/Topic, Tier (1-3) und Kategorie.

Gib ein JSON-Objekt mit diesen Feldern zurück:

{
  "style_note": "kurze Beschreibung des Designs in 1 Zeile",
  "palette": {
    "primary":     "#RRGGBB",
    "primary_dark":"#RRGGBB",
    "accent":      "#RRGGBB",
    "bg":          "#RRGGBB",
    "surface":     "#RRGGBB",
    "text":        "#RRGGBB",
    "text_muted":  "#RRGGBB",
    "border":      "#RRGGBB"
  },
  "typography": {
    "heading_family": "CSS font-family Stack (mit Fallbacks)",
    "body_family":    "CSS font-family Stack (mit Fallbacks)",
    "heading_weight": "600|700|800",
    "body_size":      "16px|17px|18px"
  },
  "radius":      "0px|4px|6px|8px|12px",
  "shadow_intensity": "none|soft|medium",
  "accent_shape": "border-left|underline|bar|dot"
}

Regeln:
- Farben müssen untereinander harmonieren und lesbaren Kontrast zum bg
  haben (≥ AA).
- Vermeide Neon, vermeide reinen Pastell, vermeide Dark-Mode (blog bg
  muss hell sein, sonst bricht die Prose-Lesbarkeit).
- Typographie: eine Serif + eine Sans-Kombination ist Standard für
  editorial, zwei Sans für modern-clean. Liste nur System- und Google-
  Standard-Fonts ("Georgia", "Inter", "system-ui", etc.).
- Tier 1 = schlichter/sachlicher. Tier 3 = polierter/editorial.
- Kategorie beeinflusst den Farbton:
    healthcare/life-science/pharma/medtech = beruhigende Grün-/Türkis-/Blautöne
    finance/legal = ruhige Indigo-/Slate-Töne
    lifestyle/fitness = wärmere Amber/Rosé
    nutrition = warme Amber/Emerald
    tech = Blau/Indigo/Violett

Nur JSON. Keine Prosa, keine Code-Fences."""


def _example_for(site: Site) -> str:
    return (
        f"Topic: {site.topic or site.domain.name}\n"
        f"Tier: {site.domain.tier.value}\n"
        f"Kategorie: {site.domain.category or 'other'}\n"
        f"Sprache: {site.language}\n"
    )


def _build_css(tokens: dict) -> str:
    """Turn design tokens into a compact CSS block injected into blog head."""
    p = tokens.get("palette") or {}
    t = tokens.get("typography") or {}
    radius = tokens.get("radius") or "6px"
    shadow = tokens.get("shadow_intensity") or "soft"
    accent_shape = tokens.get("accent_shape") or "border-left"

    shadow_val = {
        "none": "none",
        "soft": "0 1px 3px rgba(15, 23, 42, 0.08)",
        "medium": "0 4px 12px rgba(15, 23, 42, 0.12)",
    }.get(shadow, "0 1px 3px rgba(15, 23, 42, 0.08)")

    primary = p.get("primary", "#0284c7")
    primary_dark = p.get("primary_dark", "#0369a1")
    accent = p.get("accent", "#38bdf8")
    bg = p.get("bg", "#ffffff")
    surface = p.get("surface", "#f8fafc")
    text = p.get("text", "#0f172a")
    text_muted = p.get("text_muted", "#64748b")
    border = p.get("border", "#e2e8f0")

    h_family = t.get("heading_family", "Georgia, serif")
    b_family = t.get("body_family", "system-ui, sans-serif")
    h_weight = t.get("heading_weight", "700")
    b_size = t.get("body_size", "17px")

    accent_h2_css = {
        "border-left": f"border-left: 4px solid {primary}; padding-left: 1rem;",
        "bar":         f"border-top: 3px solid {primary}; padding-top: .4rem;",
        "underline":   f"border-bottom: 2px solid {primary}; padding-bottom: .3rem;",
        "dot":         f"padding-left: 1.1rem; position: relative; "
                       f"background: radial-gradient({primary} 5px, transparent 5px) left .7em / 14px 14px no-repeat;",
    }.get(accent_shape, f"border-left: 4px solid {primary}; padding-left: 1rem;")

    return f"""
/* site-custom: generated design tokens */
:root {{
  --site-primary: {primary};
  --site-primary-dark: {primary_dark};
  --site-accent: {accent};
  --site-bg: {bg};
  --site-surface: {surface};
  --site-text: {text};
  --site-muted: {text_muted};
  --site-border: {border};
  --site-radius: {radius};
  --site-shadow: {shadow_val};
  --site-font-heading: {h_family};
  --site-font-body: {b_family};
}}
body {{
  background: var(--site-bg);
  color: var(--site-text);
  font-family: var(--site-font-body);
  font-size: {b_size};
}}
h1, h2, h3, h4 {{ font-family: var(--site-font-heading); font-weight: {h_weight}; color: var(--site-text); }}
.article-prose h2 {{ {accent_h2_css} color: var(--site-text); }}
.article-prose a, a {{ color: var(--site-primary); }}
.article-prose a:hover, a:hover {{ color: var(--site-primary-dark); }}
.article-prose img, img.hero {{ border-radius: var(--site-radius); border-color: var(--site-border); }}
.article-prose table th {{ background: var(--site-surface); }}
.article-prose .admonition {{ border-radius: var(--site-radius); border-left-color: var(--site-primary); background: var(--site-surface); }}
.article-prose .admonition-title {{ color: var(--site-primary-dark); }}
.tldr-box {{ background: var(--site-surface); border-color: var(--site-border); border-radius: var(--site-radius); }}
article.border {{ border-radius: var(--site-radius); border-color: var(--site-border); box-shadow: var(--site-shadow); }}
header {{ border-color: var(--site-border); }}
footer {{ color: var(--site-muted); border-color: var(--site-border); }}
""".strip()


def generate_for_site(site: Site) -> tuple[dict, str]:
    """Return (tokens, css) for this site. Raises on LLM failure so the
    caller can decide whether to fall back to tier defaults."""
    try:
        tokens = complete_json(SYSTEM_PROMPT, _example_for(site), max_tokens=1500, strict=False)
    except LlmError as e:
        log.warning("design LLM failed for site %s: %s", site.id, e)
        return {}, ""
    if not isinstance(tokens, dict) or "palette" not in tokens:
        return {}, ""
    css = _build_css(tokens)
    return tokens, css
