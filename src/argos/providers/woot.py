"""Woot provider (supports Woot Developer API key or keyless public HTML fallback)."""

import hashlib
import logging
import re
from typing import Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from argos.config import Settings
from argos.models import WatchItem
from argos.providers.base import BaseProvider, ProviderCapabilities, ProviderResult, RawListing
from argos.providers.registry import register_provider

logger = logging.getLogger(__name__)

_WOOT_FEEDS = ["https://www.woot.com/category/tools", "https://www.woot.com/alldeals"]


def _extract_price(text: str) -> Optional[float]:
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _matches_item(text: str, watch_item: WatchItem) -> bool:
    hay = text.lower()
    for q in watch_item.search_queries or [watch_item.name]:
        tokens = [t for t in re.split(r"\W+", q.lower()) if len(t) >= 2]
        if tokens and all(t in hay for t in tokens):
            return True
    return False


@register_provider
class WootProvider(BaseProvider):
    """Checks Woot deals via Developer API when keyed, or public deal pages."""

    capabilities = ProviderCapabilities(
        name="woot",
        display_name="Woot Deals & Clearance",
        market_type="retail",
        supports_geo=False,
        supports_shipping=False,
        requires_api_key=False,
        estimated_cost_per_call_usd=0.0,
        normal_interval_minutes=360,
        hot_interval_minutes=120,
    )

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        listings: Dict[str, RawListing] = {}
        calls = 0

        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        ) as client:
            if settings.woot_api_key.strip():
                calls += 1
                try:
                    resp = await client.get(
                        "https://developer.woot.com/feed/All",
                        headers={"x-api-key": settings.woot_api_key.strip()},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    items = data.get("Items", []) if isinstance(data, dict) else data
                    for row in items or []:
                        title = row.get("Title") or ""
                        if not _matches_item(title, watch_item):
                            continue
                        url = row.get("Url") or ""
                        ext_id = str(row.get("OfferId") or hashlib.sha1(url.encode()).hexdigest()[:16])
                        sale_price = row.get("SalePrice")
                        price = float(sale_price.get("Minimum") if isinstance(sale_price, dict) else sale_price or 0)
                        if price <= 0:
                            continue
                        listings[ext_id] = RawListing(
                            provider="woot",
                            external_id=ext_id,
                            url=url,
                            title=title,
                            price_usd=price,
                            condition=(row.get("Condition") or "new").lower(),
                            market_type="retail",
                        )
                    return ProviderResult(listings=list(listings.values()), api_calls=calls)
                except Exception as exc:
                    logger.warning("Woot API call failed, falling back to public HTML: %s", exc)

            for feed_url in _WOOT_FEEDS:
                calls += 1
                try:
                    resp = await client.get(feed_url)
                    if resp.status_code != 200:
                        continue
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for anchor in soup.select("a[href*='/offers/']"):
                        text = anchor.get_text(" ", strip=True)
                        if not text or not _matches_item(text, watch_item):
                            continue
                        href = str(anchor["href"])
                        full_url = href if href.startswith("http") else f"https://www.woot.com{href}"
                        ext_id = hashlib.sha1(full_url.encode("utf-8")).hexdigest()[:16]
                        price = _extract_price(text)
                        if price is None:
                            continue
                        listings[ext_id] = RawListing(
                            provider="woot",
                            external_id=ext_id,
                            url=full_url,
                            title=text[:220],
                            price_usd=price,
                            condition="new",
                            market_type="retail",
                        )
                except Exception as exc:
                    logger.debug("Woot page %s fetch failed: %s", feed_url, exc)

        return ProviderResult(listings=list(listings.values()), api_calls=calls)
