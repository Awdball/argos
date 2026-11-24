"""FastAPI REST API and dashboard routes for Argos."""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from argos import __version__
from argos.companion import build_dashboard_payload, build_hermes_status_payload
from argos.config import Settings, get_settings
from argos.db import check_db_health, get_session, init_db
from argos.image_fetcher import fallback_svg_for_category, fetch_product_image
from argos.inductor import induct_item
from argos.models import (
    ApiUsageLog,
    HermesActivity,
    Listing,
    PriceBenchmark,
    ProviderGap,
    ProviderRunState,
    WatchItem,
    utcnow,
)
from argos.providers.base import RawListing
from argos.providers.registry import list_providers, load_all_providers
from argos.scheduler import (
    SchedulerService,
    apply_listing_feedback,
    ingest_external_listing,
    run_item_scan_now,
)
from argos.telegram_bot import ArgosTelegramBot

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _BASE_DIR / "templates"
_STATIC_DIR = _BASE_DIR / "static"


class InductRequest(BaseModel):
    raw_request: str = Field(..., min_length=2)
    is_hot: bool = False
    auto_activate: bool = False
    notify_telegram: bool = True
    fetch_image: bool = True
    radius_miles: Optional[int] = Field(default=None, ge=5, le=250)
    override_payload: Optional[Dict[str, Any]] = None


class RefreshImageRequest(BaseModel):
    image_url: Optional[str] = None
    alternative: bool = False
    exclude_urls: List[str] = Field(default_factory=list)


class WatchPatchRequest(BaseModel):
    name: Optional[str] = None
    is_hot: Optional[bool] = None
    status: Optional[str] = None
    radius_miles: Optional[int] = None
    retail_baseline_usd: Optional[float] = None
    new_deal_target_usd: Optional[float] = None
    used_deal_target_usd: Optional[float] = None
    search_queries: Optional[List[str]] = None
    required_tokens: Optional[List[Any]] = None
    negative_keywords: Optional[List[str]] = None
    trusted_retailers: Optional[List[Dict[str, Any]]] = None
    deal_likelihood: Optional[str] = None
    active_providers: Optional[List[str]] = None


class BenchmarkRequest(BaseModel):
    source: str = Field(..., min_length=1)
    price_usd: float = Field(..., gt=0)
    market_type: str = "new"
    url: str = ""
    title: str = ""


class HermesActivityRequest(BaseModel):
    operation: str = Field(..., min_length=1)
    status: str = "running"
    detail: str = ""
    target_domain: str = ""
    watch_item_id: Optional[int] = None
    activity_id: Optional[int] = None
    duration_seconds: Optional[float] = None


class FeedbackRequest(BaseModel):
    action: str
    notes: str = ""


class IngestRequest(BaseModel):
    title: str = Field(..., min_length=2)
    price_usd: float = Field(..., gt=0)
    shipping_usd: float = 0.0
    url: str = Field(..., min_length=4)
    source: str = "facebook_saved_search"
    condition: str = "used"
    market_type: str = "secondhand"
    location_text: str = ""
    distance_miles: Optional[float] = None
    description: str = ""
    image_url: str = ""
    external_id: str = ""
    watch_item_id: Optional[int] = None


def _serialize_item(item: WatchItem, include_details: bool = False) -> dict:
    ts = int(item.updated_at.timestamp()) if item.updated_at else 0
    data = {
        "id": item.id,
        "name": item.name,
        "raw_request": item.raw_request,
        "category": item.category,
        "search_queries": item.search_queries,
        "required_tokens": item.required_tokens,
        "negative_keywords": item.negative_keywords,
        "bundle_components": item.bundle_components,
        "retail_baseline_usd": item.retail_baseline_usd,
        "new_deal_target_usd": item.new_deal_target_usd,
        "used_deal_target_usd": item.used_deal_target_usd,
        "deal_likelihood": item.deal_likelihood,
        "trusted_retailers": item.trusted_retailers,
        "active_providers": item.active_providers,
        "radius_miles": item.radius_miles,
        "is_hot": item.is_hot,
        "status": item.status,
        "induction_summary": item.induction_summary,
        "image_url": item.image_url,
        "has_image": bool(item.image_data or item.image_url),
        "image_endpoint": f"/api/items/{item.id}/image?v={ts}",
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }
    if include_details:
        data["benchmarks"] = [
            {
                "id": b.id,
                "source": b.source,
                "market_type": b.market_type,
                "price_usd": b.price_usd,
                "url": b.url,
                "title": b.title,
            }
            for b in item.benchmarks
        ]
        data["run_states"] = [
            {
                "provider": rs.provider,
                "last_run_at": rs.last_run_at.isoformat() if rs.last_run_at else None,
                "last_status": rs.last_status,
                "last_error": rs.last_error,
                "items_found": rs.items_found,
                "new_alerts": rs.new_alerts,
            }
            for rs in item.run_states
        ]
    return data


def _serialize_listing(l: Listing) -> dict:
    return {
        "id": l.id,
        "watch_item_id": l.watch_item_id,
        "provider": l.provider,
        "external_id": l.external_id,
        "url": l.url,
        "title": l.title,
        "price_usd": l.price_usd,
        "shipping_usd": l.shipping_usd,
        "landed_cost_usd": l.landed_cost_usd,
        "condition": l.condition,
        "location_text": l.location_text,
        "distance_miles": l.distance_miles,
        "matched_bundle": l.matched_bundle,
        "matched_bundle_names": l.matched_bundle_names,
        "deal_score": l.deal_score,
        "deal_reason": l.deal_reason,
        "state": l.state,
        "last_notified_price_usd": l.last_notified_price_usd,
        "first_seen_at": l.first_seen_at.isoformat() if l.first_seen_at else None,
        "last_seen_at": l.last_seen_at.isoformat() if l.last_seen_at else None,
    }


def create_app(
    settings: Optional[Settings] = None,
    enable_background: bool = True,
) -> FastAPI:
    """Create and configure the Argos FastAPI application."""
    cfg = settings or get_settings()
    bot = ArgosTelegramBot(cfg)
    scheduler = SchedulerService(
        cfg, alert_cb=bot.send_deal_alert if bot.is_enabled else None
    )
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        load_all_providers()
        init_db(cfg.database_url)
        if enable_background:
            if bot.is_enabled:
                await bot.start()
            if cfg.enable_scheduler:
                scheduler.start()
        try:
            yield
        finally:
            if enable_background:
                await scheduler.stop()
                await bot.stop()

    app = FastAPI(title="Argos Deal Intelligence", version=__version__, lifespan=lifespan)
    app.state.settings = cfg
    app.state.bot = bot
    app.state.scheduler = scheduler
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/api/health")
    def health() -> dict:
        try:
            check_db_health()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"DB unhealthy: {exc}") from exc
        provs = list_providers(cfg)
        configured_count = sum(1 for p in provs if p["configured"])
        with get_session() as session:
            active_watches = session.scalar(
                select(func.count(WatchItem.id)).where(WatchItem.status == "active")
            ) or 0
        return {
            "status": "ok",
            "version": __version__,
            "providers_configured": configured_count,
            "active_watches": int(active_watches),
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with get_session() as session:
            payload = build_dashboard_payload(session, cfg)
        safe_json = (
            json.dumps(payload)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "version": __version__,
                "home_postal": cfg.home_postal,
                "initial_json": safe_json,
                "summary": payload["summary"],
            },
        )

    @app.get("/api/dashboard")
    def get_dashboard() -> dict:
        with get_session() as session:
            return build_dashboard_payload(session, cfg)

    @app.get("/api/items/{item_id}/image")
    def get_item_image(item_id: int) -> Response:
        with get_session() as session:
            item = session.get(WatchItem, item_id)
            if not item:
                raise HTTPException(status_code=404, detail="Watch item not found")
            if item.image_data:
                return Response(
                    content=item.image_data,
                    media_type=item.image_mime or "image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"},
                )
            if item.image_url:
                return RedirectResponse(item.image_url, status_code=302)
            svg = fallback_svg_for_category(item.category, item.name)
            return Response(
                content=svg,
                media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=3600"},
            )

    @app.post("/api/items/{item_id}/refresh-image")
    async def refresh_item_image(
        item_id: int, body: Optional[RefreshImageRequest] = None
    ) -> dict:
        with get_session() as session:
            item = session.get(WatchItem, item_id)
            if not item:
                raise HTTPException(status_code=404, detail="Watch item not found")
            name = item.name
            queries = list(item.search_queries or [])
            current_img_url = item.image_url or ""
        preferred = body.image_url if body else None
        exclude_urls: List[str] = []
        if body and body.alternative and not preferred:
            exclude_urls = [u for u in [*body.exclude_urls, current_img_url] if u]
        meta = await fetch_product_image(
            item_name=name,
            search_queries=queries,
            preferred_url=preferred,
            exclude_urls=exclude_urls,
        )
        with get_session() as session:
            item = session.get(WatchItem, item_id)
            if not item:
                raise HTTPException(status_code=404, detail="Watch item not found")
            if meta.get("image_url"):
                item.image_url = str(meta["image_url"])
                item.image_data = meta.get("image_data")
                item.image_mime = str(meta.get("image_mime") or "")
                item.updated_at = utcnow()
                session.flush()
            ts = int(item.updated_at.timestamp()) if item.updated_at else 0
            return {
                "id": item.id,
                "has_image": bool(item.image_data or item.image_url),
                "image_url": item.image_url,
                "image_endpoint": f"/api/items/{item.id}/image?v={ts}",
                "image_bytes": len(item.image_data) if item.image_data else 0,
            }

    @app.get("/api/items")
    def get_items() -> List[dict]:
        with get_session() as session:
            items = session.scalars(select(WatchItem).order_by(WatchItem.id.asc())).all()
            return [_serialize_item(it, include_details=True) for it in items]

    @app.post("/api/items/induct")
    async def api_induct_item(req: InductRequest) -> dict:
        override = dict(req.override_payload) if req.override_payload else None
        item_id = await induct_item(
            raw_request=req.raw_request,
            settings=cfg,
            is_hot=req.is_hot,
            auto_activate=req.auto_activate,
            override_payload=override,
            fetch_image=req.fetch_image,
        )
        with get_session() as session:
            item = session.get(WatchItem, item_id)
            assert item is not None
            if req.radius_miles is not None:
                item.radius_miles = int(req.radius_miles)
                session.flush()
            payload = _serialize_item(item, include_details=True)
            card_item = item if (req.notify_telegram and bot.is_enabled) else None
        if card_item is not None:
            await bot.send_induction_card(card_item)
        return payload

    @app.patch("/api/items/{item_id}")
    def patch_item(item_id: int, body: WatchPatchRequest) -> dict:
        with get_session() as session:
            item = session.get(WatchItem, item_id)
            if not item:
                raise HTTPException(status_code=404, detail="Watch item not found")
            updates = body.model_dump(exclude_unset=True)
            for field, val in updates.items():
                setattr(item, field, val)
            item.updated_at = utcnow()
            session.flush()
            return _serialize_item(item, include_details=True)

    @app.post("/api/items/{item_id}/benchmarks")
    def add_item_benchmark(item_id: int, body: BenchmarkRequest) -> dict:
        with get_session() as session:
            item = session.get(WatchItem, item_id)
            if not item:
                raise HTTPException(status_code=404, detail="Watch item not found")
            bench = PriceBenchmark(
                watch_item_id=item.id,
                source=body.source[:120],
                market_type=body.market_type[:20],
                price_usd=round(float(body.price_usd), 2),
                url=body.url[:1000],
                title=body.title[:250],
            )
            session.add(bench)
            item.updated_at = utcnow()
            session.flush()
            return {
                "id": bench.id,
                "watch_item_id": item.id,
                "source": bench.source,
                "market_type": bench.market_type,
                "price_usd": bench.price_usd,
                "url": bench.url,
            }

    @app.delete("/api/items/{item_id}")
    def delete_item(item_id: int) -> dict:
        with get_session() as session:
            item = session.get(WatchItem, item_id)
            if not item:
                raise HTTPException(status_code=404, detail="Watch item not found")
            session.delete(item)
            return {"deleted": item_id}

    @app.post("/api/items/{item_id}/check")
    async def check_item(item_id: int) -> dict:
        try:
            return await run_item_scan_now(
                item_id,
                settings=cfg,
                alert_cb=bot.send_deal_alert if bot.is_enabled else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/listings")
    def get_listings(
        watch_item_id: Optional[int] = Query(default=None),
        state: Optional[str] = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> List[dict]:
        with get_session() as session:
            stmt = select(Listing).order_by(Listing.last_seen_at.desc()).limit(limit)
            if watch_item_id is not None:
                stmt = stmt.where(Listing.watch_item_id == watch_item_id)
            if state:
                stmt = stmt.where(Listing.state == state)
            rows = session.scalars(stmt).all()
            return [_serialize_listing(r) for r in rows]

    @app.post("/api/listings/{listing_id}/feedback")
    def post_feedback(listing_id: int, body: FeedbackRequest) -> dict:
        with get_session() as session:
            try:
                res = apply_listing_feedback(
                    session, listing_id, body.action, cfg, notes=body.notes
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not res:
                raise HTTPException(status_code=404, detail="Listing not found")
            return res

    @app.post("/api/ingest")
    async def ingest_listing(body: IngestRequest) -> dict:
        raw = RawListing(
            provider=body.source,
            external_id=body.external_id,
            url=body.url,
            title=body.title,
            price_usd=body.price_usd,
            shipping_usd=body.shipping_usd,
            condition=body.condition,
            description=body.description,
            location_text=body.location_text,
            distance_miles=body.distance_miles,
            image_url=body.image_url,
            market_type=body.market_type,
        )
        return await ingest_external_listing(
            raw=raw,
            settings=cfg,
            watch_item_id=body.watch_item_id,
            alert_cb=bot.send_deal_alert if bot.is_enabled else None,
        )

    @app.get("/api/providers")
    def get_providers() -> dict:
        with get_session() as session:
            usages = session.scalars(select(ApiUsageLog)).all()
            usage_rows = [
                {
                    "provider": u.provider,
                    "period_month": u.period_month,
                    "calls_count": u.calls_count,
                    "estimated_cost_usd": u.estimated_cost_usd,
                }
                for u in usages
            ]
        return {"providers": list_providers(cfg), "usage": usage_rows}

    @app.get("/api/provider-gaps")
    def get_provider_gaps() -> List[dict]:
        with get_session() as session:
            gaps = session.scalars(
                select(ProviderGap).order_by(ProviderGap.occurrences.desc())
            ).all()
            return [
                {
                    "id": g.id,
                    "domain": g.domain,
                    "retailer_name": g.retailer_name,
                    "recommended_by": g.recommended_by,
                    "reason": g.reason,
                    "sample_item": g.sample_item,
                    "status": g.status,
                    "occurrences": g.occurrences,
                    "first_seen_at": g.first_seen_at.isoformat() if g.first_seen_at else None,
                    "last_seen_at": g.last_seen_at.isoformat() if g.last_seen_at else None,
                }
                for g in gaps
            ]

    @app.get("/api/hermes/status")
    def get_hermes_status() -> dict:
        with get_session() as session:
            return build_hermes_status_payload(session)

    @app.post("/api/hermes/activity")
    def post_hermes_activity(body: HermesActivityRequest) -> dict:
        now_dt = utcnow()
        op = body.operation.strip().lower().replace(" ", "_")[:64]
        st = body.status.strip().lower()[:32]
        with get_session() as session:
            if body.activity_id is not None and st in ("completed", "failed", "idle"):
                row = session.get(HermesActivity, body.activity_id)
                if row:
                    row.status = "completed" if st == "idle" else st
                    row.finished_at = now_dt
                    if body.detail:
                        row.detail = body.detail[:1000]
                    row.duration_seconds = (
                        round(body.duration_seconds, 1)
                        if body.duration_seconds is not None
                        else max(0.0, round((now_dt - row.started_at).total_seconds(), 1))
                    )
                    session.flush()
                return build_hermes_status_payload(session)

            if st in ("idle", "completed", "failed"):
                open_rows = session.scalars(
                    select(HermesActivity).where(HermesActivity.status == "running")
                ).all()
                matched_open = None
                for r in open_rows:
                    if st != "idle" and r.operation == op and matched_open is None:
                        matched_open = r
                    r.status = "completed" if st == "idle" else st
                    r.finished_at = now_dt
                    r.duration_seconds = (
                        round(body.duration_seconds, 1)
                        if (body.duration_seconds is not None and r is matched_open)
                        else max(0.0, round((now_dt - r.started_at).total_seconds(), 1))
                    )
                    if r is matched_open and body.detail:
                        r.detail = body.detail[:1000]
                if st in ("completed", "failed") and matched_open is None:
                    session.add(
                        HermesActivity(
                            operation=op,
                            status=st,
                            detail=body.detail[:1000],
                            target_domain=body.target_domain[:160],
                            watch_item_id=body.watch_item_id,
                            started_at=now_dt,
                            finished_at=now_dt,
                            duration_seconds=round(body.duration_seconds or 0.0, 1),
                        )
                    )
                session.flush()
                return build_hermes_status_payload(session)

            # New running operation: close any stale running rows cleanly first
            for stale in session.scalars(
                select(HermesActivity).where(HermesActivity.status == "running")
            ).all():
                stale.status = "completed"
                stale.finished_at = now_dt
                stale.duration_seconds = max(
                    0.0, round((now_dt - stale.started_at).total_seconds(), 1)
                )
            act = HermesActivity(
                operation=op,
                status="running",
                detail=body.detail[:1000],
                target_domain=body.target_domain[:160],
                watch_item_id=body.watch_item_id,
                started_at=now_dt,
            )
            session.add(act)
            session.flush()
            payload = build_hermes_status_payload(session)
            payload["activity_id"] = act.id
            return payload

    @app.get("/api/stats")
    def get_stats() -> dict:
        with get_session() as session:
            watches_total = session.scalar(select(func.count(WatchItem.id))) or 0
            watches_active = (
                session.scalar(
                    select(func.count(WatchItem.id)).where(WatchItem.status == "active")
                )
                or 0
            )
            listings_total = session.scalar(select(func.count(Listing.id))) or 0
            alerts_sent = (
                session.scalar(
                    select(func.count(Listing.id)).where(Listing.state == "notified")
                )
                or 0
            )
            gaps_open = (
                session.scalar(
                    select(func.count(ProviderGap.id)).where(ProviderGap.status == "open")
                )
                or 0
            )
        return {
            "watches_total": int(watches_total),
            "watches_active": int(watches_active),
            "listings_total": int(listings_total),
            "alerts_sent": int(alerts_sent),
            "open_provider_gaps": int(gaps_open),
        }

    return app
