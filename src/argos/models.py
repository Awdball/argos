"""SQLAlchemy ORM models for Argos."""

from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Return naive UTC datetime compatible across SQLite and PostgreSQL."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Base ORM class."""


class WatchItem(Base):
    """An inducted item or bundle watched across secondhand and retail sources."""

    __tablename__ = "watch_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_request: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(100), default="general", nullable=False)

    search_queries: Mapped[List[str]] = mapped_column(JSON, default=list, nullable=False)
    required_tokens: Mapped[List[Any]] = mapped_column(JSON, default=list, nullable=False)
    negative_keywords: Mapped[List[str]] = mapped_column(JSON, default=list, nullable=False)
    bundle_components: Mapped[List[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )

    retail_baseline_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    new_deal_target_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    used_deal_target_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    deal_likelihood: Mapped[str] = mapped_column(Text, default="", nullable=False)

    trusted_retailers: Mapped[List[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    active_providers: Mapped[List[str]] = mapped_column(JSON, default=list, nullable=False)
    radius_miles: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    is_hot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending_approval", nullable=False)
    induction_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    image_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    image_data: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    image_mime: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    benchmarks: Mapped[List["PriceBenchmark"]] = relationship(
        back_populates="watch_item", cascade="all, delete-orphan"
    )
    listings: Mapped[List["Listing"]] = relationship(
        back_populates="watch_item", cascade="all, delete-orphan"
    )
    run_states: Mapped[List["ProviderRunState"]] = relationship(
        back_populates="watch_item", cascade="all, delete-orphan"
    )


class PriceBenchmark(Base):
    """Historical or observed price benchmark captured during induction or scans."""

    __tablename__ = "price_benchmarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_item_id: Mapped[int] = mapped_column(
        ForeignKey("watch_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), default="new", nullable=False)
    price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    shipping_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    watch_item: Mapped[WatchItem] = relationship(back_populates="benchmarks")


class Listing(Base):
    """A deduplicated candidate listing discovered by a provider."""

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint(
            "watch_item_id", "provider", "external_id", name="uq_listing_item_provider_ext"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_item_id: Mapped[int] = mapped_column(
        ForeignKey("watch_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    shipping_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    landed_cost_usd: Mapped[float] = mapped_column(Float, nullable=False)

    condition: Mapped[str] = mapped_column(String(64), default="unknown", nullable=False)
    location_text: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    distance_miles: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    image_url: Mapped[str] = mapped_column(Text, default="", nullable=False)

    matched_bundle: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    matched_bundle_names: Mapped[List[str]] = mapped_column(JSON, default=list, nullable=False)

    deal_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    deal_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    state: Mapped[str] = mapped_column(String(32), default="new", nullable=False, index=True)
    last_notified_price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    snooze_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    telegram_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    watch_item: Mapped[WatchItem] = relationship(back_populates="listings")
    feedback_events: Mapped[List["FeedbackEvent"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )


class FeedbackEvent(Base):
    """User quiesce or purchase feedback recorded from Telegram or API."""

    __tablename__ = "feedback_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    watch_item_id: Mapped[int] = mapped_column(
        ForeignKey("watch_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    listing: Mapped[Listing] = relationship(back_populates="feedback_events")


class ProviderGap(Base):
    """Recommended or bot-blocked retailer without an automated dedicated scraper."""

    __tablename__ = "provider_gaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    retailer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    recommended_by: Mapped[str] = mapped_column(String(128), default="research", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sample_item: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ProviderRunState(Base):
    """Tracks per-item per-provider check cadence and last execution status."""

    __tablename__ = "provider_run_state"
    __table_args__ = (
        UniqueConstraint("watch_item_id", "provider", name="uq_run_state_item_provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_item_id: Mapped[int] = mapped_column(
        ForeignKey("watch_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    items_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_alerts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    watch_item: Mapped[WatchItem] = relationship(back_populates="run_states")


class ApiUsageLog(Base):
    """Monthly metered API usage and spend guardrail tracking."""

    __tablename__ = "api_usage_log"
    __table_args__ = (
        UniqueConstraint("provider", "period_month", name="uq_usage_provider_month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    period_month: Mapped[str] = mapped_column(String(7), nullable=False)
    calls_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class HermesActivity(Base):
    """Tracks live and historical Hermes agent operations for Argos."""

    __tablename__ = "hermes_activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation: Mapped[str] = mapped_column(String(64), default="idle", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    target_domain: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    watch_item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
