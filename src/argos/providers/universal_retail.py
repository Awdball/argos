"""Universal zero-cost retailer search + JSON-LD/OpenGraph price extractor."""

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from argos.config import Settings
from argos.models import WatchItem
from argos.providers.base import BaseProvider, ProviderCapabilities, ProviderResult, RawListing
from argos.providers.registry import register_provider
from argos.providers.web_search import search_web

logger = logging.getLogger(__name__)

_SKIP_DOMAINS = {
    "craigslist.org",
    "ebay.com",
    "facebook.com",
    "reddit.com",
    "youtube.com",
    "pinterest.com",
    "tiktok.com",
    "instagram.com",
}

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _safe_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = re.sub(r"[^\d.]", "", str(val))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _walk_jsonld_products(obj: Any) -> List[dict]:
    found: List[dict] = []
    if isinstance(obj, list):
        for elem in obj:
            found.extend(_walk_jsonld_products(elem))
    elif isinstance(obj, dict):
        t = obj.get("@type")
        types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
        if any("Product" in str(x) for x in types):
            found.append(obj)
        if "@graph" in obj:
            found.extend(_walk_jsonld_products(obj["@graph"]))
    return found


def extract_product_metadata(html: str) -> Optional[dict]:
    """Extract price, condition, and title from Product JSON-LD or meta tags."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for prod in _walk_jsonld_products(parsed):
            offers = prod.get("offers")
            offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
            for off in offer_list:
                if not isinstance(off, dict):
                    continue
                price = _safe_float(off.get("price") or off.get("lowPrice"))
                if price and price > 1.0:
                    cond_raw = str(off.get("itemCondition") or "").lower()
                    cond = "used" if "used" in cond_raw else ("open_box" if "refurb" in cond_raw or "open" in cond_raw else "new")
                    img = prod.get("image")
                    if isinstance(img, list):
                        img = img[0] if img else ""
                    elif isinstance(img, dict):
                        img = img.get("url", "")
                    return {
                        "price": price,
                        "title": (prod.get("name") or "").strip(),
                        "condition": cond,
                        "image_url": str(img or ""),
                    }

    # Meta tag fallback (OpenGraph / schema.org microdata).
    for attr_name, attr_val in [
        ("property", "product:price:amount"),
        ("property", "og:price:amount"),
        ("itemprop", "price"),
    ]:
        tag = soup.find("meta", attrs={attr_name: attr_val})
        if tag and tag.get("content"):
            price = _safe_float(tag["content"])
            if price and price > 1.0:
                title_tag = soup.find("meta", attrs={"property": "og:title"}) or soup.find("title")
                title = (
                    title_tag.get("content")
                    if title_tag and title_tag.has_attr("content")
                    else (title_tag.get_text(" ", strip=True) if title_tag else "")
                )
                return {
                    "price": price,
                    "title": (title or "").strip(),
                    "condition": "new",
                    "image_url": "",
                }
    return None


@register_provider
class UniversalRetailProvider(BaseProvider):
    """Searches trusted/discovered retail domains via web search + JSON-LD extraction."""

    capabilities = ProviderCapabilities(
        name="universal_retail",
        display_name="Universal Retail (Search + JSON-LD)",
        market_type="retail",
        supports_geo=False,
        supports_shipping=False,
        requires_api_key=False,
        estimated_cost_per_call_usd=0.0,
        normal_interval_minutes=720,
        hot_interval_minutes=240,
    )

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        queries = (watch_item.search_queries or [watch_item.name])[:2]
        trusted = watch_item.trusted_retailers or []
        trusted_map: Dict[str, dict] = {}
        for tr in trusted:
            dom = (tr.get("domain") or "").lower().lstrip("www.")
            if dom:
                trusted_map[dom] = tr

        candidate_urls: List[dict] = []
        seen_urls: set[str] = set()
        calls = 0

        for q in queries:
            calls += 1
            if trusted_map:
                domains_slice = list(trusted_map.keys())[:4]
                site_filter = " OR ".join(f"site:{d}" for d in domains_slice)
                search_q = f"{q} ({site_filter})"
            else:
                search_q = f"{q} buy price"
            hits = await search_web(search_q, settings, max_results=6)
            for h in hits:
                url = h["url"]
                dom = _domain_of(url)
                if not dom or any(dom.endswith(s) for s in _SKIP_DOMAINS):
                    continue
                if url not in seen_urls:
                    seen_urls.add(url)
                    candidate_urls.append({**h, "domain": dom})

        listings: List[RawListing] = []
        gaps: Dict[str, dict] = {}

        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        ) as client:
            for idx, cand in enumerate(candidate_urls[:8]):
                if idx > 0:
                    await asyncio.sleep(0.3)
                url = cand["url"]
                dom = cand["domain"]
                retailer_name = (trusted_map.get(dom) or {}).get("name") or dom
                try:
                    resp = await client.get(url)
                    if resp.status_code in (403, 429, 503):
                        gaps[dom] = {
                            "domain": dom,
                            "retailer_name": retailer_name,
                            "recommended_by": "universal_retail_crawl",
                            "reason": f"HTTP {resp.status_code} anti-bot / access blocked",
                            "sample_item": watch_item.name,
                        }
                        continue
                    if resp.status_code >= 400:
                        continue
                    meta = extract_product_metadata(resp.text)
                    if not meta:
                        # Only log as a gap if it was a specifically recommended retailer.
                        if dom in trusted_map:
                            gaps[dom] = {
                                "domain": dom,
                                "retailer_name": retailer_name,
                                "recommended_by": "universal_retail_crawl",
                                "reason": "No Product JSON-LD or structured price tags found",
                                "sample_item": watch_item.name,
                            }
                        continue
                    ext_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
                    listings.append(
                        RawListing(
                            provider="universal_retail",
                            external_id=ext_id,
                            url=url,
                            title=meta.get("title") or cand.get("title") or watch_item.name,
                            price_usd=meta["price"],
                            shipping_usd=0.0,
                            condition=meta.get("condition", "new"),
                            description=cand.get("snippet", "")[:400],
                            location_text=retailer_name,
                            image_url=meta.get("image_url", ""),
                            market_type="retail",
                        )
                    )
                except Exception as exc:
                    logger.debug("Retail fetch error for %s: %s", url, exc)
                    if dom in trusted_map:
                        gaps[dom] = {
                            "domain": dom,
                            "retailer_name": retailer_name,
                            "recommended_by": "universal_retail_crawl",
                            "reason": f"Fetch error: {str(exc)[:120]}",
                            "sample_item": watch_item.name,
                        }

        return ProviderResult(
            listings=listings,
            gaps=list(gaps.values()),
            api_calls=calls,
        )
