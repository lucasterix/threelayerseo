"""Expense tracking + budget aggregates.

Every content-generation step that hits a paid API (OpenAI, Anthropic,
DataForSEO, INWX) drops an Expense row. The dashboard aggregates by
month and category for the budget view.

Unit costs are rough estimates — we log what we charge ourselves, not
what the provider actually bills, so these numbers are for planning
not bookkeeping.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import Expense

log = logging.getLogger(__name__)

# Rough unit costs in EUR cents (approximate — use to budget, not bill).
# Updated when OpenAI/Anthropic pricing changes.
UNIT_COSTS = {
    # OpenAI paid calls
    ("openai", "research"): 3,          # o4-mini-deep-research per call
    ("openai", "image-dalle-3"): 4,     # dall-e-3 standard 1024x1024
    ("openai", "image-dalle-2"): 2,     # dall-e-2 1024x1024
    ("openai", "image-gpt-image-1"): 2,
    # OpenAI simple tasks — covered by the 1M free tokens/day grant,
    # zero effective cost up to that cap.
    ("openai", "homepage"): 0,
    ("openai", "legal"): 0,
    ("openai", "clustering"): 0,
    ("openai", "category-fit"): 0,
    ("openai", "chart-planner"): 0,
    ("openai", "brainstorm"): 0,
    ("openai", "deep-research"): 0,
    # Anthropic — only writer stays here now
    ("anthropic", "writer"): 5,         # Claude Opus per post
    # DataForSEO
    ("dataforseo", "keyword-volumes"): 1,
    ("dataforseo", "keyword-ideas"): 1,
    ("dataforseo", "backlinks-summary"): 2,
    ("dataforseo", "serp"): 1,
    # INWX
    ("inwx", "domain.create"): 600,     # typical .de price
    # Hetzner
    ("hetzner", "cx22-monthly"): 590,
}


@dataclass
class MonthTotals:
    year: int
    month: int
    by_category: dict[str, int]  # cents
    total_cents: int


async def track(
    category: str,
    kind: str,
    *,
    amount_cents: int | None = None,
    site_id: int | None = None,
    post_id: int | None = None,
    note: str | None = None,
) -> None:
    """Write an Expense row. Falls back to UNIT_COSTS[(cat,kind)] when
    amount_cents is omitted. Swallows DB errors — never blocks the caller.
    """
    if amount_cents is None:
        amount_cents = UNIT_COSTS.get((category, kind), 0)
    try:
        async with SessionLocal() as session:
            session.add(
                Expense(
                    category=category,
                    kind=kind,
                    amount_cents=amount_cents,
                    site_id=site_id,
                    post_id=post_id,
                    note=note,
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        log.warning("expense track failed (%s/%s)", category, kind, exc_info=True)


async def month_totals(session: AsyncSession, year: int, month: int) -> MonthTotals:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=timezone.utc)

    rows = await session.execute(
        select(Expense.category, func.sum(Expense.amount_cents))
        .where(Expense.created_at >= start, Expense.created_at < end)
        .group_by(Expense.category)
    )
    by_cat = {r[0]: int(r[1] or 0) for r in rows.all()}
    return MonthTotals(
        year=year, month=month, by_category=by_cat, total_cents=sum(by_cat.values())
    )


async def recent_expenses(session: AsyncSession, limit: int = 50) -> list[Expense]:
    stmt = select(Expense).order_by(Expense.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def last_6_months(session: AsyncSession) -> list[MonthTotals]:
    now = datetime.now(timezone.utc)
    out = []
    y, m = now.year, now.month
    for _ in range(6):
        out.append(await month_totals(session, y, m))
        m -= 1
        if m < 1:
            m = 12
            y -= 1
    return list(reversed(out))


def track_sync(
    category: str,
    kind: str,
    *,
    amount_cents: int | None = None,
    site_id: int | None = None,
    post_id: int | None = None,
    note: str | None = None,
) -> None:
    """Blocking version usable from sync contexts (e.g. sync portions of
    jobs). Spins up its own event loop.
    """
    import asyncio

    try:
        asyncio.run(
            track(
                category,
                kind,
                amount_cents=amount_cents,
                site_id=site_id,
                post_id=post_id,
                note=note,
            )
        )
    except Exception:  # noqa: BLE001
        log.warning("sync expense track failed", exc_info=True)
