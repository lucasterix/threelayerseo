"""Domain categorisation for portfolio organisation + UI color-coding.

Featured categories surface first in dropdowns and get prominent treatment
in the UI. Healthcare + life-science is Daniel's primary focus.
"""
from __future__ import annotations

from dataclasses import dataclass

FEATURED_KEYS = ("healthcare", "life-science", "pharma", "medtech")


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    color: str  # Tailwind color name (emerald, teal, ...)


_CATEGORIES: tuple[Category, ...] = (
    Category("healthcare", "Healthcare", "emerald"),
    Category("life-science", "Life Science", "teal"),
    Category("pharma", "Pharma", "cyan"),
    Category("medtech", "MedTech", "sky"),
    Category("nutrition", "Ernährung", "amber"),
    Category("fitness", "Fitness / Wellness", "lime"),
    Category("psychology", "Psychologie", "violet"),
    Category("finance", "Finance", "indigo"),
    Category("legal", "Legal", "slate"),
    Category("tech", "Tech / SaaS", "blue"),
    Category("ecommerce", "E-Commerce", "rose"),
    Category("lifestyle", "Lifestyle", "pink"),
    Category("other", "Sonstige", "gray"),
)


def all_categories() -> list[Category]:
    """Featured first, then the rest in declared order."""
    featured = [c for c in _CATEGORIES if c.key in FEATURED_KEYS]
    rest = [c for c in _CATEGORIES if c.key not in FEATURED_KEYS]
    return featured + rest


def get(key: str | None) -> Category | None:
    if not key:
        return None
    for c in _CATEGORIES:
        if c.key == key:
            return c
    return None


def label(key: str | None) -> str:
    c = get(key)
    return c.label if c else "—"


def color(key: str | None) -> str:
    c = get(key)
    return c.color if c else "gray"


# Hidden in base.html so Tailwind CDN JIT ships the dynamic color classes.
TAILWIND_CATEGORY_CLASSES = " ".join(
    f"bg-{c.color}-100 text-{c.color}-800 border-{c.color}-300"
    for c in _CATEGORIES
)
