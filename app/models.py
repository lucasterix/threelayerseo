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
    title: Mapped[str] = mapped_column(String(255))
    topic: Mapped[str] = mapped_column(Text)           # seed topic / niche
    language: Mapped[str] = mapped_column(String(8), default="de")
    theme: Mapped[str] = mapped_column(String(32), default="default")
    status: Mapped[SiteStatus] = mapped_column(
        Enum(SiteStatus, native_enum=False, length=16), default=SiteStatus.DRAFT
    )
    homepage_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    domain: Mapped[Domain] = relationship(back_populates="site")
    posts: Mapped[list[Post]] = relationship(back_populates="site", cascade="all, delete-orphan")


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


class MoneySite(Base):
    """Target site that tier-3 backlinks ultimately point at."""

    __tablename__ = "money_sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(2048))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
