"""Craigslist all-category local search provider with JSON-LD coordinate parsing."""

import asyncio
import hashlib
import json
import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from argos.config import Settings
from argos.geo import haversine_miles
from argos.models import WatchItem
from argos.providers.base import BaseProvider, ProviderCapabilities, ProviderResult, RawListing
from argos.providers.registry import register_provider

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = text.replace(",", "")
    m = re.search(r"(\d+(?:\.\d{1,2})?)", cleaned)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _extract_post_id(url: str) -> str:
    m = re.search(r"/(\d+)\.html", url)
    if m:
        return m.group(1)
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _extract_jsonld_geo(soup: BeautifulSoup) -> Dict[Tuple[str, str], dict]:
    """Index ItemList JSON-LD geo and images by (normalized title, rounded price)."""
    geo_map: Dict[Tuple[str, str], dict] = {}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data.get("itemListElement") if isinstance(data, dict) else None
        if not isinstance(items, list):
            continue
        for entry in items:
            item = entry.get("item") if isinstance(entry, dict) else None
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip().lower()
            offers = item.get("offers") or {}
            price_val = offers.get("price")
            try:
                price_key = f"{float(price_val):.2f}" if price_val is not None else ""
            except (TypeError, ValueError):
                price_key = ""
            avail = offers.get("availableAtOrFrom") or {}
            geo = avail.get("geo") or {}
            addr = avail.get("address") or {}
            imgs = item.get("image") or []
            img_url = imgs[0] if isinstance(imgs, list) and imgs else (imgs if isinstance(imgs, str) else "")
            meta = {
                "lat": _safe_float(geo.get("latitude")),
                "lon": _safe_float(geo.get("longitude")),
                "locality": (addr.get("addressLocality") or "").strip(),
                "region": (addr.get("addressRegion") or "").strip(),
                "image_url": img_url,
                "description": (item.get("description") or "").strip(),
            }
            if name:
                geo_map[(name, price_key)] = meta
                geo_map.setdefault((name, ""), meta)
    return geo_map


def _safe_float(val) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_craigslist_html(html: str, settings: Settings) -> List[RawListing]:
    """Parse Craigslist static search HTML + JSON-LD into RawListing objects."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    geo_map = _extract_jsonld_geo(soup)
    listings: List[RawListing] = []
    seen_ids: set[str] = set()

    nodes = soup.select("li.cl-static-search-result")
    if not nodes:
        nodes = soup.select("li.cl-search-result")

    for li in nodes:
        link_el = li.find("a", href=True)
        if not link_el:
            continue
        url = str(link_el["href"]).strip()
        if not url.startswith("http"):
            continue
        ext_id = _extract_post_id(url)
        if ext_id in seen_ids:
            continue

        title_attr = li.get("title")
        title_el = li.select_one(".title")
        title = (title_el.get_text(" ", strip=True) if title_el else "") or str(title_attr or "").strip()
        if not title:
            continue

        price_el = li.select_one(".price, .priceinfo")
        price = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

        loc_el = li.select_one(".location, .supertitle")
        loc_text = loc_el.get_text(" ", strip=True) if loc_el else ""

        price_key = f"{price:.2f}" if price is not None else ""
        meta = geo_map.get((title.lower(), price_key)) or geo_map.get((title.lower(), "")) or {}

        lat = meta.get("lat")
        lon = meta.get("lon")
        dist = None
        if lat is not None and lon is not None:
            dist = haversine_miles(settings.home_lat, settings.home_lon, lat, lon)

        if not loc_text and meta.get("locality"):
            loc_text = f"{meta['locality']}, {meta.get('region', '')}".strip(", ")

        seen_ids.add(ext_id)
        listings.append(
            RawListing(
                provider="craigslist",
                external_id=ext_id,
                url=url,
                title=title,
                price_usd=price,
                shipping_usd=0.0,
                condition="used",
                description=meta.get("description", ""),
                location_text=loc_text,
                lat=lat,
                lon=lon,
                distance_miles=dist,
                image_url=meta.get("image_url", ""),
                market_type="secondhand",
            )
        )
    return listings


@register_provider
class CraigslistProvider(BaseProvider):
    """All-category Craigslist searcher for Cross Roads, TX (76227)."""

    capabilities = ProviderCapabilities(
        name="craigslist",
        display_name="Craigslist (All Categories)",
        market_type="secondhand",
        supports_geo=True,
        supports_shipping=False,
        requires_api_key=False,
        estimated_cost_per_call_usd=0.0,
        normal_interval_minutes=60,
        hot_interval_minutes=20,
    )

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        queries = (watch_item.search_queries or [watch_item.name])[:4]
        radius = max(10, int(watch_item.radius_miles or settings.radius_base_miles))
        all_listings: Dict[str, RawListing] = {}
        calls = 0

        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            for idx, q in enumerate(queries):
                if idx > 0:
                    await asyncio.sleep(0.6)
                params = urlencode(
                    {
                        "postal": settings.home_postal,
                        "search_distance": radius,
                        "query": q,
                        "sort": "date",
                    }
                )
                url = f"https://{settings.craigslist_site}.craigslist.org/search/sss?{params}"
                calls += 1
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    parsed = parse_craigslist_html(resp.text, settings)
                    for item in parsed:
                        all_listings.setdefault(item.external_id, item)
                except Exception as exc:
                    logger.warning("Craigslist query %r failed: %s", q, exc)
                    if not all_listings and idx == len(queries) - 1:
                        return ProviderResult(
                            listings=[], error=str(exc), api_calls=calls
                        )

        return ProviderResult(listings=list(all_listings.values()), api_calls=calls)
