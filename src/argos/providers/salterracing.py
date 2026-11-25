"""Salter Racing (salterracing.com) — dedicated stealth retail provider.

Salter Racing is a server-rendered BigCommerce 2.0 (Modez theme) shopfront
protected by Cloudflare.  Every product tile carries structured data
attributes:

* ``data-test-info-type="brandName" | "sku"`` on inner spans
* ``data-product-price-without-tax`` / ``data-product-rrp-price-without-tax``
  on the price spans

So a lean ``BeautifulSoup`` parse (no headless browser) gets us price, brand,
and SKU per tile.

Transport: Cloudflare JA3/TLS-fingerprint filters the ClientHello, so a stock
``httpx``/OpenSSL handshake from the cluster egress is met with a 403
"Just a moment..." challenge (verified 2026-11-24: same IP, 200 from one
Python/TLS build, 403-challenge from the in-cluster one).  All fetches
therefore go through ``browser_utils.fetch_html_curl_cffi`` (curl_cffi
Chrome impersonation), with plain httpx kept only as a best-effort fallback
in environments where curl_cffi is not installed.

Search strategy per watch item:

1. Site search (``/search.php?keywords=...``) for up to two of the item's
   search queries.  Note: Salter's site search matches *loosely* (a query for
   "Holley 750 4150" also returns alternators, starters, etc.), so it is never
   a sole source of truth.
2. For carburetor-related watch items, also crawl the canonical
   ``/carbureutors-and-components/`` catalog (limit=100, up to 3 pages) and
   **union** the tiles — this is the authoritative listing for the Holley
   750/4150 family.

Tiles are pre-filtered by the item's required tokens (AND of OR-groups) and
negative keywords (regex).  WAF/HTTP failures are surfaced as a provider gap
dict so the scheduler can track ``salterracing.com``.
"""

import asyncio
import hashlib
import logging
import random
import re
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from argos.config import Settings
from argos.models import WatchItem
from argos.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    ProviderResult,
    RawListing,
)
from argos.providers.browser_utils import fetch_html_curl_cffi
from argos.providers.registry import register_provider

logger = logging.getLogger(__name__)

SITE = "https://www.salterracing.com"
DOMAIN = "salterracing.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": SITE + "/",
}

WAF_STATUSES = {403, 429, 503}
MAX_SEARCH_QUERIES = 2
MAX_CATEGORY_PAGES = 3
CATEGORY_SLUG = "carbureutors-and-components"


def parse_price(value: Any) -> Optional[float]:
    """Parse attribute prices like ``'1,059.99'`` into a float."""
    if value is None or value is True or value is False:
        return None
    if isinstance(value, (int, float)):
        price = float(value)
    else:
        cleaned = re.sub(r"[^\d.]", "", str(value))
        if not cleaned:
            return None
        try:
            price = float(cleaned)
        except ValueError:
            return None
    if price <= 0:
        return None
    return price


def _tile_attr(parent, *names: str) -> Optional[str]:
    """Read the first child attribute with a matching ``data-test-info-type``."""
    for name in names:
        el = parent.find(attrs={"data-test-info-type": name})
        if el is not None:
            text = el.get_text(" ", strip=True)
            if text:
                return text
    return None


def parse_salter_tiles(html: str) -> List[Dict[str, Any]]:
    """Extract product tiles from a Salter Racing category/search page.

    Pure function (no network) so it is unit-testable offline.  Returns dicts
    with ``url``, ``title``, ``brand``, ``sku``, ``price_usd``, ``rrp_usd``
    (price fields may be None when absent/invalid).
    """
    soup = BeautifulSoup(html, "lxml")
    tiles: List[Dict[str, Any]] = []
    seen_urls = set()
    for h4 in soup.find_all("h4", class_="card-title"):
        a = h4.find("a", href=True)
        if a is None:
            continue
        url = a["href"].split("?", 1)[0]
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Climb to the nearest ancestor tile that exposes SKU/brand data.
        container = h4
        for _ in range(6):
            nxt = container.parent
            if isinstance(nxt, type(soup)) or nxt is None:
                break
            container = nxt
            if container.find(attrs={"data-test-info-type": "sku"}) is not None:
                break

        title = a.get_text(" ", strip=True)
        sku = _tile_attr(container, "sku") or ""
        brand = _tile_attr(container, "brandName") or ""

        price_el = container.find(attrs={"data-product-price-without-tax": True})
        rrp_el = container.find(attrs={"data-product-rrp-price-without-tax": True})
        # Modez theme: the data-product-price-* attribute is a *valueless marker*;
        # the actual price lives in the span's text ("$225.00").  Some catalog
        # pages do put the value in the attribute, so try both — text first.
        price = parse_price(price_el.get_text(strip=True)) if price_el is not None else None
        if price is None and price_el is not None:
            price = parse_price(price_el.get("data-product-price-without-tax"))
        rrp = parse_price(rrp_el.get_text(strip=True)) if rrp_el is not None else None
        if rrp is None and rrp_el is not None:
            rrp = parse_price(rrp_el.get("data-product-rrp-price-without-tax"))

        tiles.append(
            {
                "url": url,
                "title": title,
                "brand": brand,
                "sku": sku,
                "price_usd": price,
                "rrp_usd": rrp,
            }
        )
    return tiles


def matches_watch_item(
    tile: Dict[str, Any],
    required_tokens: Optional[List[str]] = None,
    negative_keywords: Optional[List[str]] = None,
) -> bool:
    """AND-of-OR-groups required tokens + regex negative filter on one tile."""
    text = " ".join(filter(None, [tile.get("title"), tile.get("brand"), tile.get("sku")])).lower()
    for group in required_tokens or []:
        options = [tok.strip() for tok in str(group).split("|") if tok.strip()]
        if options and not any(tok.lower() in text for tok in options):
            return False
    for pattern in negative_keywords or []:
        try:
            if re.search(str(pattern), text, re.IGNORECASE):
                return False
        except re.error:
            if str(pattern).lower() in text:
                return False
    return True


class _SalterFetchError(Exception):
    """Transport-level or WAF-level failure while fetching Salter Racing."""

    def __init__(self, message: str, is_waf: bool = False):
        super().__init__(message)
        self.is_waf = is_waf


async def _fetch_page(url: str) -> str:
    """Fetch one page; raise _SalterFetchError on network or WAF failures.

    Prefers curl_cffi Chrome TLS impersonation (resistant to Cloudflare's
    JA3/ClientHello fingerprinting); falls back to stock httpx only in
    environments where the curl_cffi package is absent.
    """
    result = await fetch_html_curl_cffi(url, timeout=25.0)
    if result is not None:
        status_code, html = result
        if status_code in WAF_STATUSES:
            raise _SalterFetchError(f"HTTP {status_code} from {url}", is_waf=True)
        if status_code != 200:
            raise _SalterFetchError(f"HTTP {status_code} from {url}")
        return html
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise _SalterFetchError(f"network error fetching {url}: {exc}") from exc
    if resp.status_code in WAF_STATUSES:
        raise _SalterFetchError(f"HTTP {resp.status_code} from {url}", is_waf=True)
    if resp.status_code != 200:
        raise _SalterFetchError(f"HTTP {resp.status_code} from {url}")
    return resp.text


@register_provider
class SalterRacingProvider(BaseProvider):
    """Stealth BigCommerce catalog provider for salterracing.com."""

    capabilities = ProviderCapabilities(
        name="salterracing",
        display_name="Salter Racing",
        market_type="retail",
        supports_geo=False,
        supports_shipping=False,
        requires_api_key=False,
        estimated_cost_per_call_usd=0.0,
        normal_interval_minutes=300,
        hot_interval_minutes=60,
    )

    def is_configured(self, settings: Settings) -> bool:
        return True

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:  # noqa: C901
        name = (watch_item.name or "").lower()
        queries = [q.strip() for q in (watch_item.search_queries or []) if q and q.strip()][:MAX_SEARCH_QUERIES]
        if not queries:
            queries = [watch_item.name or "performance parts"]

        pages_html: List[str] = []
        error: Optional[str] = None
        gap: Optional[Dict[str, str]] = None

        for query in queries:
            url = f"{SITE}/search.php?keywords={urllib.parse.quote_plus(query)}"
            try:
                pages_html.append(await _fetch_page(url))
            except _SalterFetchError as exc:
                if exc.is_waf:
                    gap = {
                        "domain": DOMAIN,
                        "retailer_name": "Salter Racing",
                        "recommended_by": "salterracing",
                        "reason": str(exc),
                        "sample_item": (watch_item.name or "")[:255],
                    }
                error = str(exc)
                break
            await asyncio.sleep(random.uniform(1.0, 2.0))

        candidates = [t for t in (parse_salter_tiles(h) for h in pages_html) for t in t]

        # Salter's /search.php matches loosely, so for carburetor watches
        # we *always* union in the canonical /carbureutors-and-components/
        # catalog — it is the authoritative source for the Holley 750 / 4150
        # family and often lists variants the loose search misses.
        carb_related = bool(
            re.search(r"carburetor|\bbbl\b|4150|750", name, re.IGNORECASE)
            or any(re.search(r"carb|4150", q, re.IGNORECASE) for q in queries)
        )
        if carb_related:
            for page in range(1, MAX_CATEGORY_PAGES + 1):
                url = f"{SITE}/{CATEGORY_SLUG}/?limit=100&page={page}"
                try:
                    html = await _fetch_page(url)
                except _SalterFetchError as exc:
                    if exc.is_waf and gap is None:
                        gap = {
                            "domain": DOMAIN,
                            "retailer_name": "Salter Racing",
                            "recommended_by": "salterracing",
                            "reason": str(exc),
                            "sample_item": (watch_item.name or "")[:255],
                        }
                    if not error:
                        error = str(exc)
                    break
                pages_html.append(html)
                tiles = parse_salter_tiles(html)
                candidates.extend(tiles)
                if len(tiles) < 50:
                    break  # last page (or empty)
                await asyncio.sleep(random.uniform(1.0, 2.0))

        seen_urls = set()
        listings: List[RawListing] = []
        for tile in candidates:
            price = tile.get("price_usd")
            if not price:
                continue
            if not matches_watch_item(
                tile,
                watch_item.required_tokens,
                watch_item.negative_keywords,
            ):
                continue
            url = tile["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            external_id = tile.get("sku") or hashlib.sha1(url.encode()).hexdigest()[:16]
            title = tile["title"]
            if tile.get("brand") and tile["brand"].lower() not in title.lower():
                title = f"{tile['brand']} {title}"
            listings.append(
                RawListing(
                    provider=self.capabilities.name,
                    external_id=external_id,
                    url=url,
                    title=title,
                    price_usd=price,
                    condition="new",
                    description=f"Salter Racing retail; SKU {tile.get('sku') or 'n/a'}",
                    market_type="retail",
                )
            )

        result = ProviderResult(
            listings=listings,
            gaps=[gap] if gap else [],
            error=error,
            api_calls=max(len(pages_html), 1) if pages_html else 1,
        )
        logger.info(
            "salterracing search item=%s candidates=%d listings=%d pages=%d error=%s",
            watch_item.id,
            len(candidates),
            len(listings),
            len(pages_html),
            error,
        )
        return result
