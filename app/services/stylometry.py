"""Stylometric variation for post generation.

We rotate between a small set of profiles when queueing posts. Each
profile tweaks Claude temperature, target length, tone modifier, and
structural preferences. Goal: make a site's N posts look like N
different writers rather than 50 variations of the same prompt.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass


@dataclass
class StylometricProfile:
    name: str
    temperature: float
    words_min: int
    words_max: int
    tone_hint: str


_PROFILES: tuple[StylometricProfile, ...] = (
    StylometricProfile(
        "neutral-expert",
        0.7,
        900,
        1400,
        "sachlich, präzise, mit konkreten Zahlen wenn möglich; kein Marketing-Sprech.",
    ),
    StylometricProfile(
        "persoenlich-erfahrungsbasiert",
        0.9,
        700,
        1100,
        "ich-persektivisch und alltagsnah, gelegentliche Erfahrungsberichte, aber keine Übertreibungen.",
    ),
    StylometricProfile(
        "journalistisch-recherchierend",
        0.8,
        1200,
        1800,
        "reportagenhaft mit eingebetteten Quellen-Hinweisen, Zwischenüberschriften klar faktisch.",
    ),
    StylometricProfile(
        "kurz-praktisch",
        0.75,
        500,
        800,
        "direkt und knapp, klare Handlungsempfehlungen, viele Bullet-Lists.",
    ),
    StylometricProfile(
        "wissenschaftlich-vorsichtig",
        0.65,
        1400,
        2200,
        "formell, viele 'unter Umständen' / 'Studien legen nahe', Quellenhinweise, wenige Superlative.",
    ),
)

_PROFILE_BY_NAME = {p.name: p for p in _PROFILES}


def pick_profile(post_id: int | None = None, site_id: int | None = None) -> StylometricProfile:
    """Deterministic selection from (post_id, site_id) if provided (so
    a post always uses the same profile even when regenerated), else random.
    """
    if post_id is None:
        return random.choice(_PROFILES)
    seed = f"{site_id}-{post_id}"
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    return _PROFILES[h % len(_PROFILES)]


def by_name(name: str | None) -> StylometricProfile | None:
    if not name:
        return None
    return _PROFILE_BY_NAME.get(name)
