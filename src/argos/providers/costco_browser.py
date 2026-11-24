"""Human-paced stealth browser provider for Costco."""

import asyncio
import hashlib
import importlib.util
import logging
import random
import re
import shutil
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

from argos.config import Settings
from argos.models import WatchItem
from argos.providers.base import BaseProvider, ProviderCapabilities, ProviderResult, RawListing
from argos.providers.browser_utils import (
    BROWSER_LOCK,
    get_async_playwright,
    is_browser_available,
    launch_stealth_chromium,
)
from argos.providers.registry import register_provider

logger = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_ITEM_URL_RE = re.compile(r"\.product\.(\d+)\.html")

_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
"""


def parse_costco_cards(cards: List[Dict[str, Any]]) -> List[RawListing]:
    """Normalize extracted Costco search result cards into RawListing objects."""
    listings: List[RawListing] = []
    seen: set[str] = set()
    for card in cards:
        href = str(card.get("href") or "").strip()
        if not href:
            continue
        full_url = href if href.startswith("http") else f"https://www.costco.com{href}"
        m = _ITEM_URL_RE.search(full_url)
        ext_id = m.group(1) if m else hashlib.sha1(full_url.encode("utf-8")).hexdigest()[:16]
        if ext_id in seen:
            continue

        text = str(card.get("text") or "").strip()
        lower_text = text.lower()
        if any(tok in lower_text for tok in ("out of stock", "sold out")):
            continue

        lines = [
            ln.strip()
            for ln in text.splitlines()
            if ln.strip() and ln.strip().lower() not in ("compare", "member only", "online-only")
        ]
        if not lines:
            continue

        title = lines[0]
        price_usd: Optional[float] = None
        for ln in lines:
            ll = ln.lower()
            if ll.startswith(("(", "after $", "save $")):
                continue
            pm = _PRICE_RE.search(ln)
            if pm:
                try:
                    val = float(pm.group(1).replace(",", ""))
                    if val > 1.0:
                        price_usd = val
                        break
                except ValueError:
                    continue
        if price_usd is None:
            continue

        seen.add(ext_id)
        listings.append(
            RawListing(
                provider="costco_browser",
                external_id=ext_id,
                url=full_url,
                title=title[:250],
                price_usd=price_usd,
                shipping_usd=0.0,
                condition="new",
                description=" | ".join(lines[1:4])[:400],
                location_text="Costco",
                image_url=str(card.get("image_url") or ""),
                market_type="retail",
            )
        )
    return listings


@register_provider
class CostcoBrowserProvider(BaseProvider):
    """Human-paced Chromium browser search for Costco retail deals."""

    capabilities = ProviderCapabilities(
        name="costco_browser",
        display_name="Costco (Human-Paced Browser)",
        market_type="retail",
        supports_geo=False,
        supports_shipping=True,
        requires_api_key=False,
        estimated_cost_per_call_usd=0.0,
        normal_interval_minutes=360,
        hot_interval_minutes=90,
    )

    def is_configured(self, settings: Settings) -> bool:
        return bool(settings.enable_costco_browser and is_browser_available())

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        if not self.is_configured(settings):
            return ProviderResult(listings=[], api_calls=0)

        async_pw, using_patchright = get_async_playwright()

        queries = (watch_item.search_queries or [watch_item.name])[:2]
        seen: Dict[str, RawListing] = {}
        calls = 0

        async with BROWSER_LOCK, async_pw() as p:
            browser = await launch_stealth_chromium(p)
            try:
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    timezone_id="America/Chicago",
                    locale="en-US",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                if not using_patchright:
                    await context.add_init_script(_STEALTH_INIT_JS)
                page = await context.new_page()

                for idx, q in enumerate(queries):
                    if idx > 0:
                        await asyncio.sleep(random.uniform(2.5, 4.5))
                    calls += 1
                    search_url = f"https://www.costco.com/s?keyword={quote_plus(q)}"
                    try:
                        await page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                        await asyncio.sleep(random.uniform(4.5, 6.0))

                        cards = await page.evaluate(
                            """() => {
                                const links = Array.from(document.querySelectorAll('a[href*=".product."]'));
                                const seenHref = new Set();
                                const out = [];
                                for (const a of links) {
                                    const href = a.getAttribute('href') || '';
                                    if (!href || seenHref.has(href)) continue;
                                    const card = a.closest('[data-testid*="ProductTile"], .product-tile-set, .product, div') || a;
                                    const text = (card.innerText || a.innerText || '').trim();
                                    if (!text.includes('$')) continue;
                                    seenHref.add(href);
                                    const img = card.querySelector('img');
                                    out.push({
                                        href,
                                        text,
                                        image_url: img ? (img.getAttribute('src') || '') : ''
                                    });
                                    if (out.length >= 20) break;
                                }
                                return out;
                            }"""
                        )
                        for item in parse_costco_cards(cards):
                            seen.setdefault(item.external_id, item)
                    except Exception as exc:
                        logger.warning("Costco browser query %r failed: %s", q, exc)
            finally:
                await browser.close()

        return ProviderResult(listings=list(seen.values()), api_calls=calls)
