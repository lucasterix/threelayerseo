from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Tier(enum.IntEnum):
    """Three-layer SEO tiers. Links flow 1 -> 2 -> 3 -> money."""

    BAD = 1
    MEDIUM = 2
    GOOD = 3


class DomainStatus(enum.StrEnum):
    PENDING = "pending"       # queued for purchase
    PURCHASING = "purchasing"
    ACTIVE = "active"
    FAILED = "failed"
    EXPIRED = "expired"
    TRANSFERRED_OUT = "transferred_out"


class SiteStatus(enum.StrEnum):
    DRAFT = "draft"
    BUILDING = "building"     # initial homepage generation in progress
    LIVE = "live"
    DISABLED = "disabled"


class PostStatus(enum.StrEnum):
    PENDING = "pending"         # queued
    RESEARCHING = "researching"
    WRITING = "writing"
    READY = "ready"
    PUBLISHED = "published"
    FAILED = "failed"


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(253), unique=True, index=True)
    tld: Mapped[str] = mapped_column(String(63), index=True)
    tier: Mapped[Tier] = mapped_column(Enum(Tier), index=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[DomainStatus] = mapped_column(
        Enum(DomainStatus, native_enum=False, length=32),
        default=DomainStatus.PENDING,
        index=True,
    )
    registrar: Mapped[str] = mapped_column(String(32), default="inwx")
    registrar_domain_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    is_expired_purchase: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    wayback_snapshots: Mapped[int | None] = mapped_column(Integer, nullable=True)
    backlink_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    site: Mapped[Site | None] = relationship(back_populates="domain", uselist=False)


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id", ondelete="CASCADE"), unique=True)
    server_id: Mapped[int | None] = mapped_column(
        ForeignKey("servers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    topic: Mapped[str] = mapped_column(Text)           # seed topic / niche
    language: Mapped[str] = mapped_column(String(8), default="de")
    theme: Mapped[str] = mapped_column(String(32), default="default")
    status: Mapped[SiteStatus] = mapped_column(
        Enum(SiteStatus, native_enum=False, length=16), default=SiteStatus.DRAFT
    )
    homepage_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    imprint_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    privacy_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    domain: Mapped[Domain] = relationship(back_populates="site")
    server: Mapped[Server | None] = relationship(back_populates="sites")
    posts: Mapped[list[Post]] = relationship(back_populates="site", cascade="all, delete-orphan")


class ServerStatus(enum.StrEnum):
    PLANNED = "planned"       # we intend to provision, not live yet
    ACTIVE = "active"
    FULL = "full"             # at soft capacity, new sites shouldn't land here
    RETIRED = "retired"


class Server(Base):
    """A host we route blog sites through.

    We track both infra we own (Hetzner) and external hops (Cloudflare proxy
    IPs, dedicated money-site hosts). ``capacity_limit`` is a soft cap for
    how many sites we deliberately land on one box — tweak per tier.
    """

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="hetzner", index=True)
    hostname: Mapped[str] = mapped_column(String(128), index=True)
    ip: Mapped[str] = mapped_column(String(45), unique=True, index=True)
    ipv6: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location: Mapped[str] = mapped_column(String(32), default="")   # fsn1 / nbg1 / hel1 / ash / other
    server_type: Mapped[str] = mapped_column(String(32), default="")  # cx22, cx32, etc.
    capacity_limit: Mapped[int] = mapped_column(Integer, default=25)
    monthly_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[ServerStatus] = mapped_column(
        Enum(ServerStatus, native_enum=False, length=16), default=ServerStatus.ACTIVE
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sites: Mapped[list[Site]] = relationship(back_populates="server")


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(500))
    primary_keyword: Mapped[str] = mapped_column(String(255))
    secondary_keywords: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    research_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    body_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    featured_image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    featured_image_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    stylometric_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refresh_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    status: Mapped[PostStatus] = mapped_column(
        Enum(PostStatus, native_enum=False, length=16), default=PostStatus.PENDING
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    site: Mapped[Site] = relationship(back_populates="posts")
    outgoing_links: Mapped[list[Backlink]] = relationship(
        foreign_keys="Backlink.source_post_id",
        back_populates="source_post",
        cascade="all, delete-orphan",
    )

    __table_args__ = (UniqueConstraint("site_id", "slug", name="uq_posts_site_slug"),)


class Backlink(Base):
    """An internal (between our sites) or external (to money-site) link.

    Direction constraint: source.tier <= target.tier (links only flow up the stack).
    Validation is enforced at insert time in the service layer, not via DB check.
    """

    __tablename__ = "backlinks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), index=True
    )
    target_site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    external_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    anchor_text: Mapped[str] = mapped_column(String(512))
    rel: Mapped[str] = mapped_column(String(32), default="")     # e.g. "nofollow" or ""
    placed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source_post: Mapped[Post] = relationship(
        foreign_keys=[source_post_id], back_populates="outgoing_links"
    )


class Expense(Base):
    """Running tally of operational spend for the budget dashboard.

    Each row is a single API call or periodic cost (domain renewal, server
    lease). `category` is coarse (openai / anthropic / dataforseo / inwx /
    hetzner / cloudflare) so the dashboard can aggregate by month.
    """

    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(64))         # e.g. "research", "writer", "image", "domain.create"
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True, index=True
    )
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class KeywordCluster(Base):
    """Topical grouping of keywords for content planning.

    Produced either manually from the admin or by Claude when a user
    pastes a big keyword list on /keywords/cluster.
    """

    __tablename__ = "keyword_clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)   # info / commercial / transactional / nav
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchRun(Base):
    """A long-running deep-research search for purchase candidates.

    Worker fills progress_label/progress_pct as it works through the
    phases. When done, candidates (JSON blob) holds the ranked list
    that the admin reviews + approves.
    """

    __tablename__ = "research_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seed: Mapped[str] = mapped_column(String(255))
    category_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    depth: Mapped[str] = mapped_column(String(16), default="normal")    # quick / normal / deep
    tlds: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    progress_label: Mapped[str] = mapped_column(String(128), default="queued")
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    total_checked: Mapped[int] = mapped_column(Integer, default=0)
    total_available: Mapped[int] = mapped_column(Integer, default=0)
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    keywords: Mapped[list | None] = mapped_column(JSON, nullable=True)
    clusters: Mapped[list | None] = mapped_column(JSON, nullable=True)
    candidates: Mapped[list | None] = mapped_column(JSON, nullable=True)
    expired_opportunities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MoneySite(Base):
    """Target site that tier-3 backlinks ultimately point at.

    Lives OUTSIDE our INWX portfolio — hosted anywhere, registered
    anywhere. We only store the URL + metadata so the tier-3 linker
    has a menu to pick from. Category lets us prefer matching
    niche (healthcare tier-3 post → healthcare money site).
    """

    __tablename__ = "money_sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(2048))
    category: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    anchor_hints: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
