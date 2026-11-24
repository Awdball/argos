"""Slickdeals RSS deal aggregator provider."""

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from typing import Dict, Optional
from urllib.parse import urlencode

import httpx

from argos.config import Settings
from argos.models import WatchItem
from argos.providers.base import BaseProvider, ProviderCapabilities, ProviderResult, RawListing
from argos.providers.registry import register_provider

logger = logging.getLogger(__name__)


def _extract_price(title: str, desc: str = "") -> Optional[float]:
    for text in (title, desc):
        m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text or "")
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


@register_provider
class SlickdealsProvider(BaseProvider):
    """Monitors Slickdeals search RSS feeds at zero cost."""

    capabilities = ProviderCapabilities(
        name="slickdeals",
        display_name="Slickdeals RSS",
        market_type="aggregator",
        supports_geo=False,
        supports_shipping=False,
        requires_api_key=False,
        estimated_cost_per_call_usd=0.0,
        normal_interval_minutes=180,
        hot_interval_minutes=60,
    )

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        queries = (watch_item.search_queries or [watch_item.name])[:3]
        seen: Dict[str, RawListing] = {}
        calls = 0

        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        ) as client:
            for q in queries:
                calls += 1
                params = urlencode(
                    {
                        "mode": "frontpage",
                        "searcharea": "deals",
                        "searchin": "first",
                        "rss": "1",
                        "q": q,
                    }
                )
                url = f"https://slickdeals.net/newsearch.php?{params}"
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    root = ET.fromstring(resp.text)
                except Exception as exc:
                    logger.debug("Slickdeals RSS query %r failed: %s", q, exc)
                    continue

                for item in root.findall(".//item"):
                    title = (item.findtext("title") or "").strip()
                    link = (item.findtext("link") or "").strip()
                    guid = (item.findtext("guid") or link).strip()
                    desc = (item.findtext("description") or "").strip()
                    if not title or not link:
                        continue
                    ext_id = hashlib.sha1(guid.encode("utf-8")).hexdigest()[:20]
                    if ext_id in seen:
                        continue
                    price = _extract_price(title, desc)
                    if price is None:
                        continue
                    seen[ext_id] = RawListing(
                        provider="slickdeals",
                        external_id=ext_id,
                        url=link,
                        title=title,
                        price_usd=price,
                        condition="new",
                        description=re.sub(r"<[^>]+>", " ", desc)[:400],
                        market_type="retail",
                    )

        return ProviderResult(listings=list(seen.values()), api_calls=calls)
