"""Human-paced stealth browser provider for Best Buy."""

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
from argos.providers.registry import register_provider

logger = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_SKU_URL_RE = re.compile(r"(?:skuId=|/sku/(\d+)|/([A-Z0-9]{7,12})(?:\?|$))")

_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
"""


def _is_blocked_page(url: str, title: str, body_text: str) -> bool:
    lower_title = (title or "").lower()
    lower_body = (body_text or "")[:1500].lower()
    if "access denied" in lower_title or "access denied" in lower_body:
        return True
    return any(
        tok in lower_body
        for tok in ("verify you are human", "robot or human", "unusual traffic")
    )


def parse_bestbuy_cards(cards: List[Dict[str, Any]]) -> List[RawListing]:
    """Normalize extracted Best Buy product cards into RawListing objects."""
    listings: List[RawListing] = []
    seen: set[str] = set()
    for card in cards:
        href = str(card.get("href") or "").strip()
        if not href:
            continue
        full_url = href if href.startswith("http") else f"https://www.bestbuy.com{href}"
        sku = str(card.get("sku") or "").strip()
        if not sku:
            m = _SKU_URL_RE.search(full_url)
            sku = (m.group(1) or m.group(2)) if m else hashlib.sha1(full_url.encode("utf-8")).hexdigest()[:16]
        if sku in seen:
            continue

        text = str(card.get("text") or "").strip()
        lower_text = text.lower()
        if any(tok in lower_text for tok in ("sold out", "out of stock", "coming soon")):
            continue

        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and ln.strip().lower() != "sponsored"]
        if not lines:
            continue
        title = lines[0]

        price_usd: Optional[float] = None
        for ln in lines[1:]:
            ll = ln.lower()
            if ll.startswith(("save ", "was ", "reg", "or $", "from $", "your price")):
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

        seen.add(sku)
        listings.append(
            RawListing(
                provider="bestbuy_browser",
                external_id=sku,
                url=full_url,
                title=title[:250],
                price_usd=price_usd,
                shipping_usd=0.0,
                condition="new",
                description=" | ".join(lines[1:5])[:400],
                location_text="Best Buy",
                image_url=str(card.get("image_url") or ""),
                market_type="retail",
            )
        )
    return listings


@register_provider
class BestBuyBrowserProvider(BaseProvider):
    """Human-paced Chromium browser search for Best Buy retail deals."""

    capabilities = ProviderCapabilities(
        name="bestbuy_browser",
        display_name="Best Buy (Human-Paced Browser)",
        market_type="retail",
        supports_geo=False,
        supports_shipping=True,
        requires_api_key=False,
        estimated_cost_per_call_usd=0.0,
        normal_interval_minutes=360,
        hot_interval_minutes=90,
    )

    def is_configured(self, settings: Settings) -> bool:
        return bool(
            settings.enable_bestbuy_browser
            and importlib.util.find_spec("playwright") is not None
        )

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        if not self.is_configured(settings):
            return ProviderResult(listings=[], api_calls=0)

        from playwright.async_api import async_playwright

        queries = (watch_item.search_queries or [watch_item.name])[:2]
        seen: Dict[str, RawListing] = {}
        calls = 0
        blocked_err: Optional[str] = None

        async with async_playwright() as p:
            exe_path = shutil.which("chromium") or shutil.which("chromium-browser") or None
            browser = await p.chromium.launch(
                executable_path=exe_path,
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
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
                await context.add_init_script(_STEALTH_INIT_JS)
                page = await context.new_page()

                for idx, q in enumerate(queries):
                    if idx > 0:
                        await asyncio.sleep(random.uniform(2.5, 5.0))
                    calls += 1
                    search_url = f"https://www.bestbuy.com/site/searchpage.jsp?st={quote_plus(q)}"
                    try:
                        await page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                        await asyncio.sleep(random.uniform(2.5, 4.0))

                        for _ in range(2):
                            await page.evaluate(
                                f"window.scrollBy(0, {random.randint(400, 750)});"
                            )
                            await asyncio.sleep(random.uniform(1.0, 2.0))

                        cards = await page.evaluate(
                            """() => {
                                const lis = Array.from(document.querySelectorAll('li.sku-item, [data-testid="sku-item"], .product-list-item'));
                                return lis.slice(0, 20).map(li => {
                                    const link = li.querySelector('a[href*="/product/"], a[href*="/site/"], a[href]');
                                    const img = li.querySelector('img');
                                    return {
                                        sku: li.getAttribute('data-sku-id') || '',
                                        href: link ? (link.getAttribute('href') || '') : '',
                                        text: (li.innerText || '').trim(),
                                        image_url: img ? (img.getAttribute('src') || '') : ''
                                    };
                                });
                            }"""
                        )
                        parsed = parse_bestbuy_cards(cards)
                        if not parsed:
                            body_text = await page.evaluate(
                                "() => document.body ? document.body.innerText : ''"
                            )
                            if _is_blocked_page(page.url, await page.title(), body_text):
                                blocked_err = "Best Buy blocked or gated by anti-bot check; stopping cleanly."
                                logger.warning(blocked_err)
                                break
                        for item in parsed:
                            seen.setdefault(item.external_id, item)
                    except Exception as exc:
                        logger.warning("Best Buy browser query %r failed: %s", q, exc)
            finally:
                await browser.close()

        return ProviderResult(
            listings=list(seen.values()),
            error=blocked_err,
            api_calls=calls,
        )
