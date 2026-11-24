"""Enriched read-model builder for the Argos web companion app."""

from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from argos.config import Settings
from argos.models import ApiUsageLog, HermesActivity, Listing, ProviderGap, WatchItem, utcnow
from argos.providers.registry import get_provider, list_providers
from argos.scheduler import effective_interval_minutes

_SECONDHAND_PROVIDERS = {"craigslist", "ebay", "facebook_apify", "facebook_browser"}
_RETAIL_PROVIDERS = {"universal_retail", "woot", "slickdeals", "bestbuy_browser"}
_SUPPRESSED_STATES = {
    "quiesced_wrong_item",
    "quiesced_distance",
    "quiesced_expired",
    "quiesced_false_positive",
    "bought",
}
_HARD_REJECT_PREFIXES = (
    "missing_price",
    "placeholder_or_scam_price",
    "missing_title",
    "universal_negative:",
    "negative_keyword:",
    "missing_required_token",
    "outside_radius",
    "ai_rejected:",
)


def _domain_of(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _pct_off(price: Optional[float], baseline: Optional[float]) -> Optional[float]:
    if price is None or not baseline or baseline <= 0:
        return None
    return round(((baseline - price) / baseline) * 100.0, 1)


def _is_valid_benchmark_price(price: float, baseline: Optional[float]) -> bool:
    if price <= 1.0:
        return False
    if baseline and baseline >= 50.0 and price < baseline * 0.25:
        return False
    return True


def build_item_companion_view(
    item: WatchItem,
    gap_map: Dict[str, ProviderGap],
    settings: Settings,
) -> Dict[str, Any]:
    """Assemble visual card + modal telemetry for a single WatchItem."""
    ts = int(item.updated_at.timestamp()) if item.updated_at else 0
    image_endpoint = f"/api/items/{item.id}/image?v={ts}"
    has_image = bool(item.image_data or item.image_url)

    # 1. Retail listings & benchmarks by domain
    retail_by_domain: Dict[str, Dict[str, Any]] = {}
    for lst in item.listings:
        if lst.provider not in _RETAIL_PROVIDERS and lst.condition != "new":
            continue
        if lst.state in _SUPPRESSED_STATES or lst.deal_reason.startswith(_HARD_REJECT_PREFIXES):
            continue
        dom = _domain_of(lst.url) or lst.provider
        cur = retail_by_domain.get(dom)
        if cur is None or lst.landed_cost_usd < cur["price_usd"]:
            retail_by_domain[dom] = {
                "domain": dom,
                "name": lst.location_text or dom,
                "price_usd": lst.landed_cost_usd,
                "url": lst.url,
                "title": lst.title,
                "provider": lst.provider,
                "is_live": True,
                "last_seen_at": lst.last_seen_at.isoformat() if lst.last_seen_at else None,
            }

    bench_by_domain: Dict[str, Dict[str, Any]] = {}
    for b in item.benchmarks:
        if b.market_type != "new" or not _is_valid_benchmark_price(b.price_usd, item.retail_baseline_usd):
            continue
        dom = _domain_of(b.url) or b.source.lower().lstrip("www.")
        cur = bench_by_domain.get(dom)
        if cur is None or b.price_usd < cur["price_usd"]:
            bench_by_domain[dom] = {
                "domain": dom,
                "name": b.source,
                "price_usd": b.price_usd,
                "url": b.url,
                "title": b.title,
                "is_live": False,
            }

    # Merge trusted_retailers + live retail domains + valid benchmark domains
    retailer_matrix: List[Dict[str, Any]] = []
    seen_domains: set[str] = set()

    def _push_retailer(
        dom: str,
        name: str,
        notes: str = "",
        uncommon: bool = False,
    ) -> None:
        clean_dom = dom.lower().lstrip("www.")
        if not clean_dom or clean_dom in seen_domains:
            return
        seen_domains.add(clean_dom)
        live = retail_by_domain.get(clean_dom)
        bench = bench_by_domain.get(clean_dom)
        gap = gap_map.get(clean_dom)

        if live:
            meets = bool(
                item.new_deal_target_usd is not None
                and live["price_usd"] <= item.new_deal_target_usd
            )
            status = "below_target" if meets else "live_price"
            status_label = "Below New Target" if meets else "Live Price"
            price = live["price_usd"]
            url = live["url"]
            title = live["title"]
            is_live = True
        elif gap and any(code in (gap.reason or "") for code in ("403", "429", "503", "anti-bot")):
            status = "blocked"
            status_label = "Anti-Bot Blocked"
            price = bench["price_usd"] if bench else None
            url = bench["url"] if bench else f"https://{clean_dom}"
            title = gap.reason
            is_live = False
        elif gap:
            status = "unstructured"
            status_label = "Needs Custom Scraper"
            price = bench["price_usd"] if bench else None
            url = bench["url"] if bench else f"https://{clean_dom}"
            title = gap.reason
            is_live = False
        elif bench:
            meets = bool(
                item.new_deal_target_usd is not None
                and bench["price_usd"] <= item.new_deal_target_usd
            )
            status = "benchmark"
            status_label = "Researched Ref"
            price = bench["price_usd"]
            url = bench["url"]
            title = bench["title"]
            is_live = False
        else:
            status = "awaiting_scan"
            status_label = "Awaiting Scan"
            price = None
            url = f"https://{clean_dom}"
            title = ""
            is_live = False

        retailer_matrix.append(
            {
                "name": name or clean_dom,
                "domain": clean_dom,
                "price_usd": price,
                "pct_vs_baseline": _pct_off(price, item.retail_baseline_usd),
                "meets_target": bool(
                    price is not None
                    and item.new_deal_target_usd is not None
                    and price <= item.new_deal_target_usd
                ),
                "url": url,
                "title": title,
                "status": status,
                "status_label": status_label,
                "is_live": is_live,
                "notes": notes,
                "uncommon": uncommon,
            }
        )

    for tr in item.trusted_retailers or []:
        if isinstance(tr, dict):
            _push_retailer(
                str(tr.get("domain") or ""),
                str(tr.get("name") or tr.get("domain") or ""),
                notes=str(tr.get("notes") or ""),
                uncommon=bool(tr.get("uncommon", False)),
            )
    for dom, info in retail_by_domain.items():
        _push_retailer(dom, str(info.get("name") or dom))
    for dom, info in bench_by_domain.items():
        _push_retailer(dom, str(info.get("name") or dom))

    _status_rank = {
        "below_target": 0,
        "live_price": 1,
        "benchmark": 2,
        "awaiting_scan": 3,
        "unstructured": 4,
        "blocked": 5,
    }
    retailer_matrix.sort(
        key=lambda r: (
            0 if r["price_usd"] is not None else 1,
            r["price_usd"] if r["price_usd"] is not None else 1e9,
            _status_rank.get(r["status"], 9),
        )
    )

    priced_retail = [r for r in retailer_matrix if r["price_usd"] is not None]
    live_retail = [r for r in priced_retail if r["is_live"]]
    best_retail = live_retail[0] if live_retail else (priced_retail[0] if priced_retail else None)

    # Historical retail best price across valid new benchmarks + non-suppressed retail listings
    historical_candidates: List[Dict[str, Any]] = []
    for b in item.benchmarks:
        if b.market_type != "new" or not _is_valid_benchmark_price(b.price_usd, item.retail_baseline_usd):
            continue
        dom = _domain_of(b.url) or b.source.lower().lstrip("www.")
        historical_candidates.append(
            {
                "price_usd": b.price_usd,
                "domain": dom,
                "name": b.source or dom,
                "url": b.url,
                "title": b.title,
                "observed_at": b.observed_at.isoformat() if b.observed_at else None,
                "is_live": False,
            }
        )
    for r in priced_retail:
        historical_candidates.append(
            {
                "price_usd": r["price_usd"],
                "domain": r["domain"],
                "name": r["name"],
                "url": r["url"],
                "title": r["title"],
                "observed_at": None,
                "is_live": r["is_live"],
            }
        )
    historical_best_retail = (
        min(historical_candidates, key=lambda c: c["price_usd"])
        if historical_candidates
        else None
    )

    # 2. Viable 2nd-hand deals
    viable_secondhand: List[Dict[str, Any]] = []
    for lst in item.listings:
        is_secondhand = lst.provider in _SECONDHAND_PROVIDERS or (
            lst.condition != "new" and lst.provider not in _RETAIL_PROVIDERS
        )
        if not is_secondhand:
            continue
        if lst.state in _SUPPRESSED_STATES:
            continue
        if lst.deal_reason.startswith(_HARD_REJECT_PREFIXES):
            continue

        target = item.used_deal_target_usd or item.retail_baseline_usd
        meets_target = bool(target is not None and lst.landed_cost_usd <= target)
        within_watch_band = bool(
            meets_target
            or (target is not None and lst.landed_cost_usd <= target * 1.25)
            or lst.state in ("new", "notified")
        )
        if not within_watch_band:
            continue

        viable_secondhand.append(
            {
                "id": lst.id,
                "watch_item_id": item.id,
                "watch_item_name": item.name,
                "provider": lst.provider,
                "url": lst.url,
                "title": lst.title,
                "price_usd": lst.price_usd,
                "shipping_usd": lst.shipping_usd,
                "landed_cost_usd": lst.landed_cost_usd,
                "condition": lst.condition,
                "location_text": lst.location_text,
                "distance_miles": lst.distance_miles,
                "matched_bundle": lst.matched_bundle,
                "matched_bundle_names": lst.matched_bundle_names or [],
                "deal_score": lst.deal_score,
                "deal_reason": lst.deal_reason,
                "state": lst.state,
                "meets_target": meets_target,
                "pct_vs_baseline": _pct_off(lst.landed_cost_usd, item.retail_baseline_usd),
                "last_seen_at": lst.last_seen_at.isoformat() if lst.last_seen_at else None,
            }
        )

    viable_secondhand.sort(key=lambda d: (0 if d["meets_target"] else 1, d["landed_cost_usd"]))
    best_secondhand = viable_secondhand[0] if viable_secondhand else None

    # 3. Provider search frequency & run health
    rs_map = {rs.provider: rs for rs in item.run_states}
    cadence: List[Dict[str, Any]] = []
    sh_intervals: List[int] = []
    rt_intervals: List[int] = []
    last_checked_dt = None

    for pname in item.active_providers or []:
        prov = get_provider(pname)
        if prov is None:
            continue
        cap = prov.capabilities
        configured = prov.is_configured(settings)
        interval_min = effective_interval_minutes(prov, item.is_hot)
        rs = rs_map.get(pname)
        last_run = rs.last_run_at if rs else None
        next_due = (last_run + timedelta(minutes=interval_min)) if last_run else None
        if last_run and (last_checked_dt is None or last_run > last_checked_dt):
            last_checked_dt = last_run
        if configured:
            if cap.market_type == "secondhand":
                sh_intervals.append(interval_min)
            else:
                rt_intervals.append(interval_min)
        cadence.append(
            {
                "provider": pname,
                "display_name": cap.display_name,
                "market_type": cap.market_type,
                "configured": configured,
                "interval_minutes": interval_min,
                "last_run_at": last_run.isoformat() if last_run else None,
                "next_due_at": next_due.isoformat() if next_due else None,
                "last_status": rs.last_status if rs else "pending",
                "last_error": rs.last_error if rs else "",
                "items_found": rs.items_found if rs else 0,
                "new_alerts": rs.new_alerts if rs else 0,
            }
        )

    new_best_price = best_retail["price_usd"] if best_retail else None
    used_best_price = best_secondhand["landed_cost_usd"] if best_secondhand else None
    hist_best_price = historical_best_retail["price_usd"] if historical_best_retail else None

    new_delta = (
        round(new_best_price - item.new_deal_target_usd, 2)
        if (new_best_price is not None and item.new_deal_target_usd is not None)
        else None
    )
    used_delta = (
        round(used_best_price - item.used_deal_target_usd, 2)
        if (used_best_price is not None and item.used_deal_target_usd is not None)
        else None
    )
    hist_vs_target = (
        round(hist_best_price - item.new_deal_target_usd, 2)
        if (hist_best_price is not None and item.new_deal_target_usd is not None)
        else None
    )
    target_gap = {
        "new_delta_usd": new_delta,
        "new_pct_above_target": (
            round((new_delta / item.new_deal_target_usd) * 100.0, 1)
            if (new_delta is not None and item.new_deal_target_usd and item.new_deal_target_usd > 0)
            else None
        ),
        "new_meets_target": bool(new_delta is not None and new_delta <= 0),
        "used_delta_usd": used_delta,
        "used_pct_above_target": (
            round((used_delta / item.used_deal_target_usd) * 100.0, 1)
            if (used_delta is not None and item.used_deal_target_usd and item.used_deal_target_usd > 0)
            else None
        ),
        "used_meets_target": bool(used_delta is not None and used_delta <= 0),
        "historical_vs_target_usd": hist_vs_target,
    }

    return {
        "id": item.id,
        "name": item.name,
        "raw_request": item.raw_request,
        "category": item.category,
        "search_queries": item.search_queries or [],
        "required_tokens": item.required_tokens or [],
        "negative_keywords": item.negative_keywords or [],
        "bundle_components": item.bundle_components or [],
        "retail_baseline_usd": item.retail_baseline_usd,
        "new_deal_target_usd": item.new_deal_target_usd,
        "used_deal_target_usd": item.used_deal_target_usd,
        "deal_likelihood": item.deal_likelihood,
        "trusted_retailers": item.trusted_retailers or [],
        "active_providers": item.active_providers or [],
        "radius_miles": item.radius_miles,
        "is_hot": item.is_hot,
        "status": item.status,
        "induction_summary": item.induction_summary,
        "image_url": item.image_url,
        "has_image": has_image,
        "image_endpoint": image_endpoint,
        "best_retail": best_retail,
        "historical_best_retail": historical_best_retail,
        "target_gap": target_gap,
        "best_secondhand": best_secondhand,
        "viable_secondhand_deals": viable_secondhand,
        "retailer_matrix": retailer_matrix,
        "cadence": cadence,
        "secondhand_interval_minutes": min(sh_intervals) if sh_intervals else None,
        "retail_interval_minutes": min(rt_intervals) if rt_intervals else None,
        "last_checked_at": last_checked_dt.isoformat() if last_checked_dt else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def build_dashboard_payload(session: Session, settings: Settings) -> Dict[str, Any]:
    """Assemble the complete companion dashboard state in a single query pass."""
    items = session.scalars(
        select(WatchItem)
        .options(
            selectinload(WatchItem.benchmarks),
            selectinload(WatchItem.listings),
            selectinload(WatchItem.run_states),
        )
        .order_by(WatchItem.id.asc())
    ).all()

    gaps = session.scalars(
        select(ProviderGap).order_by(ProviderGap.occurrences.desc(), ProviderGap.domain.asc())
    ).all()
    gap_map = {g.domain.lower().lstrip("www."): g for g in gaps}

    provs = list_providers(settings)
    month = utcnow().strftime("%Y-%m")
    usages = session.scalars(
        select(ApiUsageLog).where(ApiUsageLog.period_month == month)
    ).all()
    usage_map = {u.provider: u for u in usages}
    apify_usage = usage_map.get("facebook_apify")

    enriched_items = [build_item_companion_view(it, gap_map, settings) for it in items]

    all_secondhand_deals: List[Dict[str, Any]] = []
    below_target_count = 0
    best_discount_pct: Optional[float] = None

    for it in enriched_items:
        if it["best_retail"] and it["best_retail"].get("meets_target"):
            below_target_count += 1
        for candidate in (it["best_retail"], it["best_secondhand"]):
            if candidate and candidate.get("pct_vs_baseline") is not None:
                pct = candidate["pct_vs_baseline"]
                if best_discount_pct is None or pct > best_discount_pct:
                    best_discount_pct = pct
        for d in it["viable_secondhand_deals"]:
            all_secondhand_deals.append(
                {
                    **d,
                    "used_deal_target_usd": it["used_deal_target_usd"],
                    "retail_baseline_usd": it["retail_baseline_usd"],
                    "image_endpoint": it["image_endpoint"],
                }
            )

    all_secondhand_deals.sort(
        key=lambda d: (0 if d["meets_target"] else 1, -d["deal_score"], d["landed_cost_usd"])
    )

    provider_rows = []
    for p in provs:
        u = usage_map.get(p["name"])
        provider_rows.append(
            {
                **p,
                "calls_this_month": u.calls_count if u else 0,
                "spent_this_month_usd": u.estimated_cost_usd if u else 0.0,
            }
        )

    gap_rows = [
        {
            "id": g.id,
            "domain": g.domain,
            "retailer_name": g.retailer_name,
            "recommended_by": g.recommended_by,
            "reason": g.reason,
            "sample_item": g.sample_item,
            "status": g.status,
            "occurrences": g.occurrences,
            "last_seen_at": g.last_seen_at.isoformat() if g.last_seen_at else None,
        }
        for g in gaps
    ]

    return {
        "home_postal": settings.home_postal,
        "summary": {
            "watches_total": len(enriched_items),
            "watches_active": sum(1 for i in enriched_items if i["status"] == "active"),
            "watches_hot": sum(1 for i in enriched_items if i["is_hot"] and i["status"] == "active"),
            "viable_secondhand_count": len(all_secondhand_deals),
            "below_target_retail_count": below_target_count,
            "best_discount_pct": best_discount_pct,
            "open_gaps_count": sum(1 for g in gap_rows if g["status"] == "open"),
            "providers_configured": sum(1 for p in provs if p["configured"]),
            "providers_total": len(provs),
            "apify_spent_usd": apify_usage.estimated_cost_usd if apify_usage else 0.0,
            "apify_budget_usd": settings.apify_monthly_budget_usd,
        },
        "items": enriched_items,
        "all_secondhand_deals": all_secondhand_deals,
        "providers": provider_rows,
        "provider_gaps": gap_rows,
        "hermes": build_hermes_status_payload(session),
    }


def _serialize_hermes_activity(row: HermesActivity, now_dt=None) -> Dict[str, Any]:
    ref_now = now_dt or utcnow()
    elapsed = (
        row.duration_seconds
        if row.duration_seconds is not None
        else max(0.0, round((ref_now - row.started_at).total_seconds(), 1))
    )
    return {
        "id": row.id,
        "operation": row.operation,
        "status": row.status,
        "detail": row.detail,
        "target_domain": row.target_domain,
        "watch_item_id": row.watch_item_id,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "duration_seconds": elapsed,
    }


def build_hermes_status_payload(session: Session, limit: int = 25) -> Dict[str, Any]:
    """Return current Hermes operation state and recent task duration log."""
    rows = session.scalars(
        select(HermesActivity)
        .order_by(HermesActivity.started_at.desc(), HermesActivity.id.desc())
        .limit(limit)
    ).all()
    now_dt = utcnow()
    history = [_serialize_hermes_activity(r, now_dt=now_dt) for r in rows]
    running = next((h for h in history if h["status"] == "running"), None)
    current = running or {
        "id": None,
        "operation": "idle",
        "status": "idle",
        "detail": "Idle — awaiting next scheduled check or task",
        "target_domain": "",
        "watch_item_id": None,
        "started_at": history[0]["finished_at"] if history else None,
        "finished_at": None,
        "duration_seconds": 0.0,
    }
    return {"current": current, "history": history}
