"""Item induction, live pricing/retailer research, and provider gap tracking."""

import logging
import re
import statistics
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from argos.config import Settings
from argos.db import get_session
from argos.geo import recommended_radius_miles
from argos.image_fetcher import fetch_product_image
from argos.models import PriceBenchmark, ProviderGap, WatchItem, utcnow
from argos.ollama_client import chat_json
from argos.providers.craigslist import parse_craigslist_html
from argos.providers.link_resolver import is_intermediary_domain
from argos.providers.registry import default_active_providers
from argos.providers.web_search import search_web

logger = logging.getLogger(__name__)

_COVERED_DOMAINS = {
    "craigslist.org",
    "ebay.com",
    "woot.com",
    "slickdeals.net",
    "facebook.com",
}

DEFAULT_RETAILERS_BY_CATEGORY: Dict[str, List[Dict[str, Any]]] = {
    "tools": [
        {
            "name": "Ace Hardware",
            "domain": "acehardware.com",
            "notes": "Hardware, power tools, outdoor equipment, and seasonal deals",
            "uncommon": False,
        }
    ],
    "home": [
        {
            "name": "Ace Hardware",
            "domain": "acehardware.com",
            "notes": "Hardware, home improvement, and seasonal deals",
            "uncommon": False,
        }
    ],
    "outdoor": [
        {
            "name": "Ace Hardware",
            "domain": "acehardware.com",
            "notes": "Lawn, garden, outdoor power equipment, and grills",
            "uncommon": False,
        }
    ],
    "automotive": [
        {
            "name": "Ace Hardware",
            "domain": "acehardware.com",
            "notes": "Automotive tools, fasteners, and shop equipment",
            "uncommon": False,
        }
    ],
    "general": [
        {
            "name": "Ace Hardware",
            "domain": "acehardware.com",
            "notes": "Hardware, tools, home, and outdoor deals",
            "uncommon": False,
        }
    ],
}


def _merge_default_retailers(
    trusted_retailers: List[Any], category: str
) -> List[Dict[str, Any]]:
    """Ensure curated category retailer targets (e.g., acehardware.com) are included."""
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for tr in trusted_retailers:
        if not isinstance(tr, dict):
            continue
        dom = str(tr.get("domain") or "").strip().lower().removeprefix("www.")
        if not dom or dom in seen or is_intermediary_domain(dom):
            continue
        seen.add(dom)
        merged.append({**tr, "domain": dom})

    for default_tr in DEFAULT_RETAILERS_BY_CATEGORY.get(category.lower(), []):
        dom = default_tr["domain"]
        if dom not in seen:
            seen.add(dom)
            merged.append(dict(default_tr))
    return merged


def upsert_provider_gap(
    session: Session,
    domain: str,
    retailer_name: str,
    recommended_by: str,
    reason: str,
    sample_item: str,
) -> ProviderGap:
    """Create or increment a ProviderGap entry without duplicating domains."""
    clean_domain = (domain or "").strip().lower().lstrip("www.")
    if not clean_domain:
        clean_domain = re.sub(r"[^a-z0-9.-]+", "", retailer_name.lower()) or "unknown"
    existing = session.scalar(select(ProviderGap).where(ProviderGap.domain == clean_domain))
    if existing:
        existing.occurrences += 1
        existing.last_seen_at = utcnow()
        if reason and reason not in existing.reason:
            existing.reason = f"{existing.reason}; {reason}"[:500]
        return existing

    gap = ProviderGap(
        domain=clean_domain,
        retailer_name=retailer_name or clean_domain,
        recommended_by=recommended_by or "research",
        reason=reason[:500],
        sample_item=sample_item[:255],
        status="open",
        occurrences=1,
    )
    session.add(gap)
    return gap


def _extract_prices_from_text(text: str) -> List[float]:
    vals: List[float] = []
    for m in re.finditer(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text or ""):
        try:
            v = float(m.group(1).replace(",", ""))
            if 5.0 <= v <= 50000.0:
                vals.append(v)
        except ValueError:
            continue
    return vals


def _clean_short_query(raw_request: str) -> str:
    cleaned = re.sub(r"\([^)]*\)", " ", raw_request)
    words = [w for w in re.split(r"\s+", cleaned.strip()) if w]
    stop = {"for", "with", "that", "includes", "and", "or", "the", "a", "an", "as", "well"}
    core = [w.strip(",.") for w in words if w.lower().strip(",.") not in stop]
    return " ".join(core[:5]) or raw_request[:60]


async def _probe_craigslist_sample(
    query: str, settings: Settings, radius_miles: int = 50
) -> List[dict[str, Any]]:
    url = (
        f"https://{settings.craigslist_site}.craigslist.org/search/sss"
        f"?postal={settings.home_postal}&search_distance={radius_miles}"
        f"&query={httpx.QueryParams({'q': query})['q']}"
    )
    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []
        listings = parse_craigslist_html(resp.text, settings)
        return [
            {
                "title": l.title,
                "price_usd": l.price_usd,
                "url": l.url,
                "location": l.location_text,
                "distance_miles": l.distance_miles,
            }
            for l in listings[:10]
            if l.price_usd and l.price_usd > 5.0
        ]
    except Exception as exc:
        logger.debug("Craigslist induction probe failed: %s", exc)
        return []


def _fallback_research_plan(
    raw_request: str,
    retail_hits: List[dict],
    used_hits: List[dict],
    cl_samples: List[dict],
) -> dict[str, Any]:
    short_q = _clean_short_query(raw_request)
    retail_prices: List[float] = []
    for h in retail_hits:
        retail_prices.extend(_extract_prices_from_text(f"{h.get('title', '')} {h.get('snippet', '')}"))
    used_prices: List[float] = []
    for h in used_hits:
        used_prices.extend(_extract_prices_from_text(f"{h.get('title', '')} {h.get('snippet', '')}"))
    for c in cl_samples:
        if c.get("price_usd"):
            used_prices.append(float(c["price_usd"]))

    retail_baseline = round(statistics.median(retail_prices), 2) if retail_prices else 300.0
    new_target = round(retail_baseline * 0.85, 2)
    used_target = (
        round(min(retail_baseline * 0.65, statistics.median(used_prices) * 0.9), 2)
        if used_prices
        else round(retail_baseline * 0.60, 2)
    )

    tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9]{2,}", short_q)[:3]]
    trusted = []
    seen_dom: set[str] = set()
    for h in retail_hits:
        dom = urlparse(h.get("url", "")).netloc.lower().lstrip("www.")
        if dom and dom not in seen_dom and not any(dom.endswith(c) for c in _COVERED_DOMAINS):
            seen_dom.add(dom)
            trusted.append({"name": dom, "domain": dom, "notes": "Found via retail search", "uncommon": False})
        if len(trusted) >= 5:
            break

    return {
        "name": raw_request.strip()[:120],
        "category": "general",
        "search_queries": [short_q],
        "required_tokens": tokens,
        "negative_keywords": ["broken", "parts only", "box only"],
        "bundle_components": [],
        "retail_baseline_usd": retail_baseline,
        "new_deal_target_usd": new_target,
        "used_deal_target_usd": used_target,
        "deal_likelihood": (
            f"Estimated from {len(retail_prices)} retail and {len(used_prices)} secondary price points "
            f"({len(cl_samples)} active local Craigslist hits within 50 mi)."
        ),
        "trusted_retailers": trusted,
        "induction_summary": (
            f"Baseline ~${retail_baseline:.0f}; targeting <=${new_target:.0f} new / <=${used_target:.0f} used."
        ),
    }


async def induct_item(
    raw_request: str,
    settings: Settings,
    is_hot: bool = False,
    auto_activate: bool = False,
    override_payload: Optional[Dict[str, Any]] = None,
    fetch_image: bool = True,
) -> int:
    """Research an item outside any DB transaction, then persist and return its ID."""
    short_q = _clean_short_query(raw_request)
    retail_hits: List[dict] = []
    used_hits: List[dict] = []
    forum_hits: List[dict] = []
    cl_samples: List[dict] = []

    if override_payload is None:
        retail_hits = await search_web(f"{short_q} price buy online", settings, max_results=6)
        used_hits = await search_web(f"{short_q} used sold price ebay", settings, max_results=6)
        forum_hits = await search_web(
            f"{short_q} best place to buy reddit OR forum", settings, max_results=5
        )
        cl_samples = await _probe_craigslist_sample(short_q, settings, radius_miles=50)

        sys_prompt = (
            "You are Argos, an expert universal deal-research analyst (automotive, tools, electronics, "
            "outdoor gear, home goods). Given live web and local Craigslist search evidence, return ONLY a "
            "valid JSON object with these exact keys:\n"
            '{\n'
            '  "name": "concise clean item name",\n'
            '  "category": "short domain tag e.g. automotive|tools|electronics|outdoor|home|general",\n'
            '  "search_queries": ["2 to 4 concise marketplace search strings"],\n'
            '  "required_tokens": ["1 to 2 minimal lowercase tokens/synonyms like \'302|sbf\' that MUST appear; avoid overly strict multi-word chains"],\n'
            '  "negative_keywords": ["words that identify wrong fitment/knockoffs/junk e.g. 351w, sbc, chevy, 4160"],\n'
            '  "bundle_components": [{"name": "...", "tokens": ["..."], "used_value_usd": 100, "new_value_usd": 180}],\n'
            '  "retail_baseline_usd": 450.0,\n'
            '  "new_deal_target_usd": 380.0,\n'
            '  "used_deal_target_usd": 250.0,\n'
            '  "deal_likelihood": "1-2 sentences on supply & odds of hitting target new vs secondary",\n'
            '  "trusted_retailers": [{"name": "Summit Racing", "domain": "summitracing.com", "notes": "...", "uncommon": false}],\n'
            '  "induction_summary": "concise 2-sentence buyer brief"\n'
            '}\n'
            "Mark uncommon=true for niche/forum-recommended retailers (not big-box/Amazon/eBay). "
            "Include acehardware.com (Ace Hardware) as a valid retailer target for tools, hardware, outdoor, home, or automotive items. "
            "Populate bundle_components ONLY if the user mentioned optional kit/bundle add-ons."
        )
        user_prompt = (
            f"User Request: {raw_request}\n\n"
            f"Retail Search Snippets:\n{retail_hits[:5]}\n\n"
            f"Used/Sold Search Snippets:\n{used_hits[:5]}\n\n"
            f"Forum/Reddit Retailer Recommendations:\n{forum_hits[:5]}\n\n"
            f"Local Craigslist Sample (50 mi around {settings.home_postal}):\n{cl_samples[:6]}"
        )
        plan = await chat_json(sys_prompt, user_prompt, settings)
        if not plan:
            plan = _fallback_research_plan(raw_request, retail_hits, used_hits, cl_samples)
    else:
        plan = dict(override_payload)

    retail_baseline = _to_float(plan.get("retail_baseline_usd"))
    new_target = _to_float(plan.get("new_deal_target_usd"))
    used_target = _to_float(plan.get("used_deal_target_usd"))
    radius = int(plan.get("radius_miles") or recommended_radius_miles(retail_baseline, settings))

    category = str(plan.get("category") or "general")[:100]
    trusted_retailers = plan.get("trusted_retailers") or []
    if not isinstance(trusted_retailers, list):
        trusted_retailers = []
    trusted_retailers = _merge_default_retailers(trusted_retailers, category)

    item_name = str(plan.get("name") or raw_request)[:255]
    item_queries = [str(q) for q in (plan.get("search_queries") or [short_q])][:5]
    image_meta: Dict[str, Any] = {"image_url": "", "image_data": None, "image_mime": ""}
    if fetch_image:
        image_meta = await fetch_product_image(
            item_name=item_name,
            search_queries=item_queries,
            preferred_url=plan.get("image_url"),
        )

    with get_session() as session:
        item = WatchItem(
            name=item_name,
            raw_request=raw_request,
            category=category,
            search_queries=item_queries,
            required_tokens=plan.get("required_tokens") or [],
            negative_keywords=[str(k) for k in (plan.get("negative_keywords") or [])],
            bundle_components=plan.get("bundle_components") or [],
            retail_baseline_usd=retail_baseline,
            new_deal_target_usd=new_target,
            used_deal_target_usd=used_target,
            deal_likelihood=str(plan.get("deal_likelihood") or ""),
            trusted_retailers=trusted_retailers,
            active_providers=plan.get("active_providers") or default_active_providers(),
            radius_miles=radius,
            is_hot=is_hot,
            status="active" if auto_activate else "pending_approval",
            induction_summary=str(plan.get("induction_summary") or ""),
            image_url=str(image_meta.get("image_url") or ""),
            image_data=image_meta.get("image_data"),
            image_mime=str(image_meta.get("image_mime") or ""),
        )
        session.add(item)
        session.flush()

        # Store observed benchmarks from research + local Craigslist probe.
        for hit in retail_hits[:5]:
            for p in _extract_prices_from_text(
                f"{hit.get('title', '')} {hit.get('snippet', '')}"
            )[:1]:
                session.add(
                    PriceBenchmark(
                        watch_item_id=item.id,
                        source=urlparse(hit.get("url", "")).netloc or "web",
                        market_type="new",
                        price_usd=p,
                        url=hit.get("url", ""),
                        title=hit.get("title", "")[:250],
                    )
                )
        for cl in cl_samples[:5]:
            if cl.get("price_usd"):
                session.add(
                    PriceBenchmark(
                        watch_item_id=item.id,
                        source="craigslist",
                        market_type="used",
                        price_usd=float(cl["price_usd"]),
                        url=cl.get("url", ""),
                        title=str(cl.get("title", ""))[:250],
                    )
                )

        # Record any uncommon forum/Reddit-recommended retailers without dedicated scrapers.
        for tr in trusted_retailers:
            if not isinstance(tr, dict):
                continue
            dom = str(tr.get("domain") or "").lower().lstrip("www.")
            if tr.get("uncommon") and dom and not any(dom.endswith(c) for c in _COVERED_DOMAINS):
                upsert_provider_gap(
                    session=session,
                    domain=dom,
                    retailer_name=str(tr.get("name") or dom),
                    recommended_by="induction_forum_research",
                    reason=str(tr.get("notes") or "Uncommon niche retailer without dedicated plugin"),
                    sample_item=item.name,
                )

        return item.id


def _to_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return round(float(val), 2)
    except (TypeError, ValueError):
        return None
