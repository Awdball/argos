"""Human-paced stealth browser provider for Facebook Marketplace."""

import asyncio
import importlib.util
import logging
import os
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

_ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)")
_PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")

_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
"""


def _is_gated_page(url: str, body_text: str) -> bool:
    lower_url = (url or "").lower()
    lower_body = (body_text or "")[:2000].lower()
    if any(tok in lower_url for tok in ("/login", "/checkpoint", "/recover")):
        return True
    return any(
        tok in lower_body
        for tok in (
            "verify you are human",
            "enter the characters",
            "security check",
            "log in to facebook",
            "you must log in to continue",
        )
    )


def parse_facebook_marketplace_cards(
    cards: List[Dict[str, Any]],
    default_city: str = "",
) -> List[RawListing]:
    """Normalize extracted Marketplace anchor cards into RawListing objects."""
    listings: List[RawListing] = []
    seen: set[str] = set()
    for card in cards:
        href = str(card.get("href") or "").strip()
        m_id = _ITEM_ID_RE.search(href)
        if not m_id:
            continue
        ext_id = m_id.group(1)
        if ext_id in seen:
            continue

        text = str(card.get("text") or "").strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        price_usd: Optional[float] = None
        for ln in lines:
            pm = _PRICE_RE.search(ln)
            if pm:
                try:
                    price_usd = float(pm.group(1).replace(",", ""))
                    break
                except ValueError:
                    continue
        if price_usd is None or price_usd <= 0:
            continue

        # Typical FB Marketplace card text lines: [price, (optional strike price), title, location]
        non_price_lines = [ln for ln in lines if not _PRICE_RE.fullmatch(ln)]
        title = non_price_lines[0] if non_price_lines else f"Facebook Marketplace #{ext_id}"
        location_text = non_price_lines[-1] if len(non_price_lines) >= 2 else default_city

        full_url = (
            href
            if href.startswith("http")
            else f"https://www.facebook.com/marketplace/item/{ext_id}/"
        )
        seen.add(ext_id)
        listings.append(
            RawListing(
                provider="facebook_browser",
                external_id=ext_id,
                url=full_url,
                title=title[:250],
                price_usd=price_usd,
                shipping_usd=0.0,
                condition="used",
                description=" | ".join(non_price_lines[:3])[:400],
                location_text=location_text[:120],
                image_url=str(card.get("image_url") or ""),
                market_type="secondhand",
            )
        )
    return listings


@register_provider
class FacebookBrowserProvider(BaseProvider):
    """Human-paced Chromium browser search for Facebook Marketplace (zero API fees)."""

    capabilities = ProviderCapabilities(
        name="facebook_browser",
        display_name="Facebook Marketplace (Human-Paced Browser)",
        market_type="secondhand",
        supports_geo=True,
        supports_shipping=False,
        requires_api_key=False,
        estimated_cost_per_call_usd=0.0,
        normal_interval_minutes=180,
        hot_interval_minutes=60,
    )

    def is_configured(self, settings: Settings) -> bool:
        return bool(
            settings.enable_facebook_browser
            and importlib.util.find_spec("playwright") is not None
        )

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        if not self.is_configured(settings):
            return ProviderResult(listings=[], api_calls=0)

        from playwright.async_api import async_playwright

        queries = (watch_item.search_queries or [watch_item.name])[:2]
        city = (settings.facebook_city_slug or "dallas").strip().strip("/")
        seen: Dict[str, RawListing] = {}
        calls = 0
        gated_error: Optional[str] = None

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
                ctx_kwargs: Dict[str, Any] = {
                    "viewport": {"width": 1440, "height": 900},
                    "timezone_id": "America/Chicago",
                    "locale": "en-US",
                    "user_agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                }
                state_path = (settings.facebook_storage_state_path or "").strip()
                if state_path and os.path.exists(state_path):
                    ctx_kwargs["storage_state"] = state_path

                context = await browser.new_context(**ctx_kwargs)
                await context.add_init_script(_STEALTH_INIT_JS)
                page = await context.new_page()

                for idx, q in enumerate(queries):
                    if idx > 0:
                        await asyncio.sleep(random.uniform(3.0, 6.0))
                    calls += 1
                    search_url = (
                        f"https://www.facebook.com/marketplace/{city}/search/"
                        f"?query={quote_plus(q)}&daysSinceListed=7&exact=false"
                    )
                    try:
                        await page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                        await asyncio.sleep(random.uniform(2.5, 4.5))

                        # Human-like scrolling to trigger lazy-loaded Marketplace cards
                        for _ in range(3):
                            await page.evaluate(
                                f"window.scrollBy(0, {random.randint(450, 850)});"
                            )
                            await asyncio.sleep(random.uniform(1.1, 2.3))

                        cards = await page.evaluate(
                            """() => {
                                const anchors = Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]'));
                                return anchors.slice(0, 30).map(a => {
                                    const img = a.querySelector('img');
                                    return {
                                        href: a.getAttribute('href') || '',
                                        text: (a.innerText || '').trim(),
                                        image_url: img ? (img.getAttribute('src') || '') : ''
                                    };
                                });
                            }"""
                        )
                        parsed = parse_facebook_marketplace_cards(cards, default_city=city.title())
                        if not parsed:
                            body_text = await page.evaluate(
                                "() => document.body ? document.body.innerText : ''"
                            )
                            if _is_gated_page(page.url, body_text):
                                gated_error = (
                                    "Facebook Marketplace gated by login wall or security check; "
                                    "stopping scan cleanly without bypassing."
                                )
                                logger.warning(gated_error)
                                break
                        for item in parsed:
                            seen.setdefault(item.external_id, item)
                    except Exception as exc:
                        logger.warning("Facebook browser query %r failed: %s", q, exc)
            finally:
                await browser.close()

        return ProviderResult(
            listings=list(seen.values()),
            error=gated_error,
            api_calls=calls,
        )
