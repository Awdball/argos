"""Background scan scheduler, deduplication persistence, and feedback transitions."""

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from argos.config import Settings, get_settings
from argos.db import get_session
from argos.inductor import upsert_provider_gap
from argos.models import (
    ApiUsageLog,
    FeedbackEvent,
    Listing,
    ProviderGap,
    ProviderRunState,
    WatchItem,
    utcnow,
)
from argos.providers.base import BaseProvider, RawListing
from argos.providers.registry import get_provider, load_all_providers
from argos.scoring import EvaluationResult, evaluate_listing, should_notify, vet_deal_with_ai

logger = logging.getLogger(__name__)

AlertCallback = Callable[[int, int, str], Awaitable[Optional[int]]]


def effective_interval_minutes(provider: BaseProvider, is_hot: bool) -> int:
    """Return provider check cadence capped at 24h (1440m) maximum."""
    cap = provider.capabilities
    raw = cap.hot_interval_minutes if is_hot else cap.normal_interval_minutes
    return min(1440, max(5, raw))


def is_due(
    run_state: Optional[ProviderRunState], interval_minutes: int, now: Optional[datetime] = None
) -> bool:
    """Return True if a provider check is due for an item."""
    if run_state is None or run_state.last_run_at is None:
        return True
    current_now = now or utcnow()
    return (current_now - run_state.last_run_at) >= timedelta(minutes=interval_minutes)


def apply_listing_feedback(
    session: Session,
    listing_id: int,
    action: str,
    settings: Settings,
    notes: str = "",
) -> Optional[dict]:
    """Transition listing state from user quiesce/bought feedback."""
    listing = session.get(Listing, listing_id)
    if not listing:
        return None
    item = session.get(WatchItem, listing.watch_item_id)
    action_clean = action.strip().lower()
    now = utcnow()

    state_map = {
        "price": "quiesced_price",
        "not_good_enough": "quiesced_price",
        "distance": "quiesced_distance",
        "too_far": "quiesced_distance",
        "timing": "quiesced_timing",
        "bad_timing": "quiesced_timing",
        "wrong": "quiesced_wrong_item",
        "wrong_item": "quiesced_wrong_item",
        "expired": "quiesced_expired",
        "oos": "quiesced_expired",
        "false_positive": "quiesced_false_positive",
        "fp": "quiesced_false_positive",
        "bought": "bought",
    }
    new_state = state_map.get(action_clean)
    if not new_state:
        raise ValueError(f"Unknown feedback action: {action}")

    listing.state = new_state
    if listing.last_notified_price_usd is None:
        listing.last_notified_price_usd = listing.landed_cost_usd

    if new_state == "quiesced_timing":
        listing.snooze_until = now + timedelta(days=settings.timing_snooze_days)
    elif new_state == "bought" and item is not None:
        item.status = "archived"

    session.add(
        FeedbackEvent(
            listing_id=listing.id,
            watch_item_id=listing.watch_item_id,
            action=action_clean,
            notes=notes[:500],
        )
    )
    session.flush()
    return {
        "listing_id": listing.id,
        "watch_item_id": listing.watch_item_id,
        "state": listing.state,
        "snooze_until": listing.snooze_until.isoformat() if listing.snooze_until else None,
        "watch_status": item.status if item else None,
    }


def _current_month() -> str:
    return utcnow().strftime("%Y-%m")


def _can_afford_provider(session: Session, provider: BaseProvider, settings: Settings) -> bool:
    cost = provider.capabilities.estimated_cost_per_call_usd
    if cost <= 0:
        return True
    return True


def _record_api_usage(session: Session, provider: BaseProvider, api_calls: int) -> None:
    if api_calls <= 0:
        return
    month = _current_month()
    name = provider.capabilities.name
    cost = round(provider.capabilities.estimated_cost_per_call_usd * api_calls, 4)
    usage = session.scalar(
        select(ApiUsageLog).where(
            ApiUsageLog.provider == name,
            ApiUsageLog.period_month == month,
        )
    )
    if usage:
        usage.calls_count += api_calls
        usage.estimated_cost_usd = round(usage.estimated_cost_usd + cost, 4)
        usage.updated_at = utcnow()
    else:
        session.add(
            ApiUsageLog(
                provider=name,
                period_month=month,
                calls_count=api_calls,
                estimated_cost_usd=cost,
            )
        )


async def run_provider_for_item(
    watch_item_id: int,
    provider_name: str,
    settings: Settings,
    alert_cb: Optional[AlertCallback] = None,
) -> dict:
    """Execute one provider check for a watch item, persist results, and trigger alerts."""
    provider = get_provider(provider_name)
    if provider is None or not provider.is_configured(settings):
        return {"provider": provider_name, "skipped": True, "reason": "unconfigured"}

    with get_session() as session:
        item = session.get(WatchItem, watch_item_id)
        if not item:
            return {"provider": provider_name, "skipped": True, "reason": "missing_item"}
        if not _can_afford_provider(session, provider, settings):
            return {"provider": provider_name, "skipped": True, "reason": "budget_exhausted"}
        # Detach snapshot values needed for async HTTP search outside session lock.
        item_snapshot = WatchItem(
            id=item.id,
            name=item.name,
            raw_request=item.raw_request,
            category=item.category,
            search_queries=list(item.search_queries or []),
            required_tokens=list(item.required_tokens or []),
            negative_keywords=list(item.negative_keywords or []),
            bundle_components=list(item.bundle_components or []),
            retail_baseline_usd=item.retail_baseline_usd,
            new_deal_target_usd=item.new_deal_target_usd,
            used_deal_target_usd=item.used_deal_target_usd,
            trusted_retailers=list(item.trusted_retailers or []),
            radius_miles=item.radius_miles,
            is_hot=item.is_hot,
            status=item.status,
        )

    result = await provider.search(item_snapshot, settings)

    # Evaluate candidates (calling optional Ollama vet outside DB transaction).
    evaluated: List[tuple] = []
    for raw in result.listings:
        ev = evaluate_listing(raw, item_snapshot, settings)
        if ev.passed:
            ev = await vet_deal_with_ai(raw, item_snapshot, ev, settings)
        evaluated.append((raw, ev))

    pending_alerts: List[tuple[int, str]] = []
    now = utcnow()
    with get_session() as session:
        item = session.get(WatchItem, watch_item_id)
        if not item:
            return {"provider": provider_name, "skipped": True, "reason": "deleted"}

        for gap in result.gaps:
            upsert_provider_gap(
                session=session,
                domain=gap.get("domain", ""),
                retailer_name=gap.get("retailer_name", ""),
                recommended_by=gap.get("recommended_by", provider_name),
                reason=gap.get("reason", ""),
                sample_item=gap.get("sample_item", item.name),
            )

        _record_api_usage(session, provider, result.api_calls)

        for raw, ev in evaluated:
            existing = session.scalar(
                select(Listing).where(
                    Listing.watch_item_id == item.id,
                    Listing.provider == raw.provider,
                    Listing.external_id == raw.external_id,
                )
            )
            notify, notify_reason = should_notify(
                existing, ev.landed_cost_usd, ev, settings, now=now
            )
            price_val = float(raw.price_usd) if raw.price_usd is not None else 0.0
            ship_val = float(raw.shipping_usd or 0.0)

            if existing:
                existing.url = raw.url
                existing.title = raw.title
                existing.description = raw.description[:2000]
                existing.price_usd = price_val
                existing.shipping_usd = ship_val
                existing.landed_cost_usd = ev.landed_cost_usd
                existing.condition = raw.condition
                existing.location_text = raw.location_text[:255]
                existing.distance_miles = raw.distance_miles
                existing.image_url = raw.image_url
                existing.matched_bundle = ev.matched_bundle
                existing.matched_bundle_names = ev.matched_bundle_names
                existing.deal_score = ev.deal_score
                existing.deal_reason = ev.reason
                existing.last_seen_at = now
                if existing.state == "new" and not ev.passed:
                    existing.state = "ignored"
                target_listing = existing
            else:
                target_listing = Listing(
                    watch_item_id=item.id,
                    provider=raw.provider,
                    external_id=raw.external_id,
                    url=raw.url,
                    title=raw.title,
                    description=raw.description[:2000],
                    price_usd=price_val,
                    shipping_usd=ship_val,
                    landed_cost_usd=ev.landed_cost_usd,
                    condition=raw.condition,
                    location_text=raw.location_text[:255],
                    distance_miles=raw.distance_miles,
                    image_url=raw.image_url,
                    matched_bundle=ev.matched_bundle,
                    matched_bundle_names=ev.matched_bundle_names,
                    deal_score=ev.deal_score,
                    deal_reason=ev.reason,
                    state="new" if ev.passed else "ignored",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(target_listing)
                session.flush()

            if notify:
                pending_alerts.append((target_listing.id, notify_reason))

            # Auto-resolve any open ProviderGap if this URL yielded a real price.
            if price_val > 0 and raw.url:
                dom = urlparse(raw.url).netloc.lower().lstrip("www.")
                if dom:
                    gap = session.scalar(
                        select(ProviderGap).where(
                            ProviderGap.domain == dom, ProviderGap.status == "open"
                        )
                    )
                    if gap:
                        gap.status = "resolved"
                        gap.last_seen_at = now

        run_state = session.scalar(
            select(ProviderRunState).where(
                ProviderRunState.watch_item_id == item.id,
                ProviderRunState.provider == provider_name,
            )
        )
        if not run_state:
            run_state = ProviderRunState(watch_item_id=item.id, provider=provider_name)
            session.add(run_state)
        run_state.last_run_at = now
        run_state.last_status = "error" if result.error else "ok"
        run_state.last_error = (result.error or "")[:500]
        run_state.items_found = len(result.listings)
        run_state.new_alerts = len(pending_alerts)

    # Dispatch Telegram alerts outside DB transaction, then mark notified.
    sent_count = 0
    for listing_id, alert_reason in pending_alerts:
        msg_id: Optional[int] = None
        if alert_cb is not None:
            try:
                msg_id = await alert_cb(watch_item_id, listing_id, alert_reason)
            except Exception as exc:
                logger.warning("Alert callback failed for listing %s: %s", listing_id, exc)
        with get_session() as session:
            listing = session.get(Listing, listing_id)
            if listing:
                listing.state = "notified"
                listing.last_notified_price_usd = listing.landed_cost_usd
                listing.snooze_until = None
                if msg_id is not None:
                    listing.telegram_message_id = msg_id
        sent_count += 1

    return {
        "provider": provider_name,
        "skipped": False,
        "items_found": len(result.listings),
        "new_alerts": sent_count,
        "gaps_logged": len(result.gaps),
        "error": result.error,
    }


async def run_item_scan_now(
    watch_item_id: int,
    settings: Optional[Settings] = None,
    alert_cb: Optional[AlertCallback] = None,
) -> dict:
    """Immediately scan all configured providers for a watch item."""
    cfg = settings or get_settings()
    load_all_providers()

    with get_session() as session:
        item = session.get(WatchItem, watch_item_id)
        if not item:
            raise ValueError(f"WatchItem {watch_item_id} not found")
        provider_names = list(item.active_providers or [])

    results: List[dict] = []
    total_found = 0
    total_alerts = 0
    errors: List[str] = []

    for pname in provider_names:
        res = await run_provider_for_item(watch_item_id, pname, cfg, alert_cb=alert_cb)
        results.append(res)
        if not res.get("skipped"):
            total_found += int(res.get("items_found", 0))
            total_alerts += int(res.get("new_alerts", 0))
            if res.get("error"):
                errors.append(f"{pname}: {res['error']}")

    return {
        "watch_item_id": watch_item_id,
        "providers": results,
        "items_found": total_found,
        "new_alerts": total_alerts,
        "errors": errors,
    }


async def ingest_external_listing(
    raw: RawListing,
    settings: Settings,
    watch_item_id: Optional[int] = None,
    alert_cb: Optional[AlertCallback] = None,
) -> dict:
    """Evaluate and persist an externally pushed listing (FB saved-search email, shortcut, etc.)."""
    if not raw.external_id:
        raw.external_id = hashlib.sha1((raw.url or raw.title).encode("utf-8")).hexdigest()[:20]

    with get_session() as session:
        if watch_item_id is not None:
            it = session.get(WatchItem, watch_item_id)
            items = [it] if it else []
        else:
            items = session.scalars(select(WatchItem).where(WatchItem.status == "active")).all()
        snapshots = [
            WatchItem(
                id=it.id,
                name=it.name,
                raw_request=it.raw_request,
                category=it.category,
                search_queries=list(it.search_queries or []),
                required_tokens=list(it.required_tokens or []),
                negative_keywords=list(it.negative_keywords or []),
                bundle_components=list(it.bundle_components or []),
                retail_baseline_usd=it.retail_baseline_usd,
                new_deal_target_usd=it.new_deal_target_usd,
                used_deal_target_usd=it.used_deal_target_usd,
                radius_miles=it.radius_miles,
                is_hot=it.is_hot,
                status=it.status,
            )
            for it in items
        ]

    matched_evals: List[tuple[WatchItem, EvaluationResult]] = []
    for snap in snapshots:
        ev = evaluate_listing(raw, snap, settings)
        if ev.passed:
            ev = await vet_deal_with_ai(raw, snap, ev, settings)
            if ev.passed:
                matched_evals.append((snap, ev))

    now = utcnow()
    pending_alerts: List[tuple[int, int, str]] = []
    results: List[dict] = []

    with get_session() as session:
        for snap, ev in matched_evals:
            existing = session.scalar(
                select(Listing).where(
                    Listing.watch_item_id == snap.id,
                    Listing.provider == raw.provider,
                    Listing.external_id == raw.external_id,
                )
            )
            notify, notify_reason = should_notify(
                existing, ev.landed_cost_usd, ev, settings, now=now
            )
            price_val = float(raw.price_usd or 0.0)
            ship_val = float(raw.shipping_usd or 0.0)
            if existing:
                existing.price_usd = price_val
                existing.shipping_usd = ship_val
                existing.landed_cost_usd = ev.landed_cost_usd
                existing.deal_score = ev.deal_score
                existing.deal_reason = ev.reason
                existing.last_seen_at = now
                target_listing = existing
            else:
                target_listing = Listing(
                    watch_item_id=snap.id,
                    provider=raw.provider,
                    external_id=raw.external_id,
                    url=raw.url,
                    title=raw.title,
                    description=raw.description[:2000],
                    price_usd=price_val,
                    shipping_usd=ship_val,
                    landed_cost_usd=ev.landed_cost_usd,
                    condition=raw.condition,
                    location_text=raw.location_text[:255],
                    distance_miles=raw.distance_miles,
                    image_url=raw.image_url,
                    matched_bundle=ev.matched_bundle,
                    matched_bundle_names=ev.matched_bundle_names,
                    deal_score=ev.deal_score,
                    deal_reason=ev.reason,
                    state="new",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(target_listing)
                session.flush()
            if notify:
                pending_alerts.append((snap.id, target_listing.id, notify_reason))
            results.append(
                {
                    "watch_item_id": snap.id,
                    "listing_id": target_listing.id,
                    "deal_score": ev.deal_score,
                    "will_notify": notify,
                    "reason": notify_reason,
                }
            )

    alerts_sent = 0
    for wid, lid, reason in pending_alerts:
        msg_id: Optional[int] = None
        if alert_cb is not None:
            try:
                msg_id = await alert_cb(wid, lid, reason)
            except Exception as exc:
                logger.warning("Ingest alert callback failed for listing %s: %s", lid, exc)
        with get_session() as session:
            lst = session.get(Listing, lid)
            if lst:
                lst.state = "notified"
                lst.last_notified_price_usd = lst.landed_cost_usd
                lst.snooze_until = None
                if msg_id is not None:
                    lst.telegram_message_id = msg_id
        alerts_sent += 1

    return {
        "matched_watches": len(results),
        "alerts_sent": alerts_sent,
        "results": results,
    }


class SchedulerService:
    """Periodic background loop checking due (watch_item, provider) pairs."""

    def __init__(self, settings: Settings, alert_cb: Optional[AlertCallback] = None) -> None:
        self.settings = settings
        self.alert_cb = alert_cb
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._loop(), name="argos-scheduler")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        load_all_providers()
        logger.info("Argos scheduler started (tick=%ss)", self.settings.scheduler_tick_seconds)
        # Short initial delay to allow FastAPI & Telegram startup to settle.
        await asyncio.sleep(5)
        while not self._stop_event.is_set():
            try:
                await self.run_due_checks()
            except Exception as exc:
                logger.exception("Scheduler tick error: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=float(self.settings.scheduler_tick_seconds)
                )
            except asyncio.TimeoutError:
                continue

    async def run_due_checks(self) -> List[dict]:
        """Run all (watch_item, provider) checks whose interval has elapsed."""
        now = utcnow()
        due_pairs: List[tuple[int, str]] = []

        with get_session() as session:
            items = session.scalars(select(WatchItem).where(WatchItem.status == "active")).all()
            for item in items:
                states = {
                    rs.provider: rs
                    for rs in session.scalars(
                        select(ProviderRunState).where(ProviderRunState.watch_item_id == item.id)
                    ).all()
                }
                for pname in item.active_providers or []:
                    prov = get_provider(pname)
                    if prov is None or not prov.is_configured(self.settings):
                        continue
                    interval = effective_interval_minutes(prov, item.is_hot)
                    if is_due(states.get(pname), interval, now=now):
                        due_pairs.append((item.id, pname))

        outcomes: List[dict] = []
        for item_id, pname in due_pairs:
            if self._stop_event.is_set():
                break
            res = await run_provider_for_item(item_id, pname, self.settings, alert_cb=self.alert_cb)
            outcomes.append(res)
        return outcomes
