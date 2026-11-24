"""Reddit classifieds & swap-subreddit search provider (zero-cost JSON feed)."""

import hashlib
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import urlencode

import httpx

from argos.config import Settings
from argos.models import WatchItem
from argos.providers.base import BaseProvider, ProviderCapabilities, ProviderResult, RawListing
from argos.providers.registry import register_provider

logger = logging.getLogger(__name__)

_REDDIT_UA = "linux:argos-deal-monitor:0.2.1 (homelab price watcher)"


def _extract_asking_price(title: str, selftext: str = "") -> Optional[float]:
    for txt in (title, (selftext or "")[:500]):
        m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", txt or "")
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if 5.0 <= val <= 50000.0:
                    return round(val, 2)
            except ValueError:
                continue
    return None


def parse_reddit_search_json(payload: dict) -> List[RawListing]:
    """Parse Reddit /search.json payload into secondhand RawListing objects."""
    children = (payload.get("data") or {}).get("children") or []
    listings: List[RawListing] = []
    seen: set[str] = set()

    for child in children:
        post = child.get("data") if isinstance(child, dict) else None
        if not isinstance(post, dict):
            continue
        title = (post.get("title") or "").strip()
        if not title:
            continue
        lower = title.lower()
        if any(tag in lower for tag in ("[wtb]", "wtb:", "looking for", "[iso]")):
            continue

        selftext = (post.get("selftext") or "").strip()
        price = _extract_asking_price(title, selftext)
        if price is None:
            continue

        permalink = post.get("permalink") or ""
        url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else (post.get("url") or "")
        if not url:
            continue
        ext_id = str(post.get("id") or hashlib.sha1(url.encode("utf-8")).hexdigest()[:16])
        if ext_id in seen:
            continue
        seen.add(ext_id)

        sub = post.get("subreddit_name_prefixed") or "reddit"
        listings.append(
            RawListing(
                provider="reddit_classifieds",
                external_id=ext_id,
                url=url,
                title=title[:250],
                price_usd=price,
                shipping_usd=0.0,
                condition="used",
                description=selftext[:500],
                location_text=sub,
                market_type="secondhand",
            )
        )
    return listings


@register_provider
class RedditClassifiedsProvider(BaseProvider):
    """Monitors Reddit swap/classifieds & enthusiast posts via public JSON search."""

    capabilities = ProviderCapabilities(
        name="reddit_classifieds",
        display_name="Reddit Classifieds & Forums",
        market_type="secondhand",
        supports_geo=False,
        supports_shipping=False,
        requires_api_key=False,
        estimated_cost_per_call_usd=0.0,
        normal_interval_minutes=120,
        hot_interval_minutes=45,
    )

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        queries = (watch_item.search_queries or [watch_item.name])[:2]
        seen: Dict[str, RawListing] = {}
        calls = 0

        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": _REDDIT_UA},
        ) as client:
            for q in queries:
                calls += 1
                params = urlencode(
                    {
                        "q": f"{q} (WTS OR \"for sale\" OR \"[H]\" OR asking)",
                        "sort": "new",
                        "t": "month",
                        "limit": 15,
                    }
                )
                url = f"https://www.reddit.com/search.json?{params}"
                try:
                    resp = await client.get(url)
                    if resp.status_code == 429:
                        return ProviderResult(
                            listings=list(seen.values()),
                            error="Reddit rate limited (429)",
                            api_calls=calls,
                        )
                    resp.raise_for_status()
                    parsed = parse_reddit_search_json(resp.json())
                    for item in parsed:
                        seen.setdefault(item.external_id, item)
                except Exception as exc:
                    logger.debug("Reddit search %r failed: %s", q, exc)

        return ProviderResult(listings=list(seen.values()), api_calls=calls)
