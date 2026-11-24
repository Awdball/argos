"""Facebook Marketplace via Apify managed actor (with monthly budget cap)."""

import hashlib
import logging
import re
from typing import Dict, Optional

import httpx

from argos.config import Settings
from argos.models import WatchItem
from argos.providers.base import BaseProvider, ProviderCapabilities, ProviderResult, RawListing
from argos.providers.registry import register_provider

logger = logging.getLogger(__name__)

_APIFY_ACTOR_ENDPOINT = (
    "https://api.apify.com/v2/acts/apify~facebook-marketplace-scraper/run-sync-get-dataset-items"
)


def _parse_price(raw) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = re.search(r"([\d,]+(?:\.\d{1,2})?)", str(raw))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


@register_provider
class FacebookApifyProvider(BaseProvider):
    """Managed Facebook Marketplace provider via Apify (dormant until APIFY_TOKEN is set)."""

    capabilities = ProviderCapabilities(
        name="facebook_apify",
        display_name="Facebook Marketplace (Apify)",
        market_type="secondhand",
        supports_geo=True,
        supports_shipping=False,
        requires_api_key=True,
        estimated_cost_per_call_usd=0.02,
        normal_interval_minutes=360,
        hot_interval_minutes=60,
    )

    def is_configured(self, settings: Settings) -> bool:
        return bool(settings.apify_token.strip())

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        if not self.is_configured(settings):
            return ProviderResult(listings=[], api_calls=0)

        query = (watch_item.search_queries or [watch_item.name])[0]
        radius_km = int(round((watch_item.radius_miles or settings.radius_base_miles) * 1.60934))
        search_url = (
            f"https://www.facebook.com/marketplace/dallas/search/"
            f"?query={httpx.QueryParams({'q': query})['q']}&radius={radius_km}"
        )

        payload = {
            "startUrls": [{"url": search_url}],
            "resultsLimit": 20,
        }
        seen: Dict[str, RawListing] = {}
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    _APIFY_ACTOR_ENDPOINT,
                    params={"token": settings.apify_token.strip()},
                    json=payload,
                )
                resp.raise_for_status()
                rows = resp.json()
        except Exception as exc:
            return ProviderResult(listings=[], error=f"Apify FB error: {exc}", api_calls=1)

        for row in rows or []:
            url = row.get("listingUrl") or row.get("url") or ""
            title = row.get("marketplace_listing_title") or row.get("title") or ""
            if not url or not title:
                continue
            ext_id = str(row.get("id") or hashlib.sha1(url.encode()).hexdigest()[:16])
            price_block = row.get("listing_price") or {}
            price = _parse_price(
                price_block.get("amount") if isinstance(price_block, dict) else row.get("price")
            )
            loc = row.get("location") or {}
            loc_text = loc.get("reverse_geocode", {}).get("city_page", {}).get("display_name", "") if isinstance(loc, dict) else str(loc)
            seen[ext_id] = RawListing(
                provider="facebook_apify",
                external_id=ext_id,
                url=url,
                title=title,
                price_usd=price,
                shipping_usd=0.0,
                condition="used",
                description=row.get("redacted_description", {}).get("text", "") if isinstance(row.get("redacted_description"), dict) else "",
                location_text=loc_text,
                image_url=(row.get("primary_listing_photo") or {}).get("image", {}).get("uri", "") if isinstance(row.get("primary_listing_photo"), dict) else "",
                market_type="secondhand",
            )

        return ProviderResult(listings=list(seen.values()), api_calls=1)
