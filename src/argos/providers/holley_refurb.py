"""Holley Factory Refurbished carburetors provider (curl_cffi + httpx)."""

from __future__ import annotations

import logging
import re
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from argos.config import Settings
from argos.models import WatchItem
from argos.providers.base import BaseProvider, ProviderCapabilities, ProviderResult, RawListing
from argos.providers.browser_utils import fetch_html_curl_cffi
from argos.providers.registry import register_provider

logger = logging.getLogger(__name__)

HOLLEY_REFURB_URL = (
    "https://www.holley.com/products/fuel_systems/carburetors/"
    "factory_refurbished_carburetors/?availability=In_Stock"
)
_HOLLEY_BASE = "https://www.holley.com"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _is_holley_carb_watch(item: WatchItem) -> bool:
    """Only query Holley refurbished carburetors for Holley carburetor watches."""
    blob = " ".join(
        [
            item.name or "",
            item.raw_request or "",
            " ".join(item.search_queries or []),
        ]
    ).lower()
    if "holley" not in blob:
        return False
    return any(tok in blob for tok in ("carb", "4150", "4160", "double pumper", "street hp", "brawler", "4779"))


def _parse_price(text: str) -> Optional[float]:
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", text or "")
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
        return val if val > 1.0 else None
    except ValueError:
        return None


def parse_holley_refurb_html(html: str) -> List[RawListing]:
    """Parse in-stock Factory Refurbished carburetor tiles from holley.com."""
    soup = BeautifulSoup(html or "", "html.parser")
    listings: List[RawListing] = []
    seen: set[str] = set()

    for li in soup.select("ul.product-grid > li[partnumber]"):
        part_num = (li.get("partnumber") or "").strip()
        name_el = li.select_one("a.name-link")
        if not name_el:
            continue
        title_txt = name_el.get_text(" ", strip=True)
        href = (name_el.get("href") or "").strip()
        if not title_txt or not href:
            continue

        brand_el = li.select_one(".brand")
        brand = brand_el.get_text(" ", strip=True) if brand_el else ""
        desc_el = li.select_one("span.short-desc")
        short_desc = desc_el.get_text(" ", strip=True) if desc_el else ""

        price_el = li.select_one(".price")
        price_raw = (
            (price_el.get("data-price") or price_el.get_text(" ", strip=True))
            if price_el
            else ""
        )
        price = _parse_price(str(price_raw))
        if price is None:
            continue

        url = href if href.startswith("http") else f"{_HOLLEY_BASE}{href}"
        ext_id = part_num or href
        if ext_id in seen:
            continue
        seen.add(ext_id)

        img_el = li.select_one("img.grid-product-image")
        img_url = (img_el.get("src") or "").strip() if img_el else ""
        if img_url.startswith("//"):
            img_url = f"https:{img_url}"

        ships_free = li.select_one(".ships-free") is not None
        full_title = f"{brand} {title_txt}".strip() if brand and brand.lower() not in title_txt.lower() else title_txt
        desc = f"{short_desc} | Part# {part_num}".strip(" |")

        listings.append(
            RawListing(
                provider="holley_refurb",
                external_id=ext_id,
                url=url,
                title=full_title,
                price_usd=price,
                shipping_usd=0.0 if ships_free else 0.0,
                condition="open_box",
                description=desc[:400],
                location_text="Holley Factory Refurbished",
                image_url=img_url,
                market_type="retail",
            )
        )

    return listings


async def _fetch_holley_refurb_page() -> tuple[Optional[int], str]:
    cffi_res = await fetch_html_curl_cffi(HOLLEY_REFURB_URL, timeout=15.0)
    if cffi_res is not None:
        return cffi_res[0], cffi_res[1]
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    ) as client:
        resp = await client.get(HOLLEY_REFURB_URL)
        return resp.status_code, resp.text


@register_provider
class HolleyRefurbProvider(BaseProvider):
    """Monitors Holley.com in-stock Factory Refurbished carburetors."""

    capabilities = ProviderCapabilities(
        name="holley_refurb",
        display_name="Holley Factory Refurbished",
        market_type="retail",
        supports_geo=False,
        supports_shipping=False,
        requires_api_key=False,
        estimated_cost_per_call_usd=0.0,
        normal_interval_minutes=240,
        hot_interval_minutes=60,
    )

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        if not _is_holley_carb_watch(watch_item):
            return ProviderResult(listings=[], api_calls=0)

        try:
            status, html = await _fetch_holley_refurb_page()
        except Exception as exc:
            logger.debug("HolleyRefurb fetch failed: %s", exc)
            return ProviderResult(
                listings=[],
                gaps=[
                    {
                        "domain": "holley.com",
                        "retailer_name": "Holley Factory Refurbished",
                        "recommended_by": "holley_refurb",
                        "reason": f"Fetch error: {str(exc)[:120]}",
                        "sample_item": watch_item.name,
                    }
                ],
                api_calls=1,
            )

        if status in (403, 429, 503) or status != 200:
            return ProviderResult(
                listings=[],
                gaps=[
                    {
                        "domain": "holley.com",
                        "retailer_name": "Holley Factory Refurbished",
                        "recommended_by": "holley_refurb",
                        "reason": f"HTTP {status} fetching Holley Factory Refurbished catalog",
                        "sample_item": watch_item.name,
                    }
                ],
                api_calls=1,
            )

        listings = parse_holley_refurb_html(html)
        return ProviderResult(listings=listings, api_calls=1)
