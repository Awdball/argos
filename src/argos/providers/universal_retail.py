"""Universal retail search + JSON-LD/OpenGraph + Exa/snippet price extraction."""

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
from argos.providers.web_search import (
    fetch_urls_exa,
    search_google_shopping_serpapi,
    search_web,
)

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


def extract_price_from_text(
    text: str, baseline_usd: Optional[float] = None
) -> Optional[float]:
    """Extract current retail price from indexed snippet/Exa text, ignoring savings/shipping."""
    if not text:
        return None
    # Focus on hero section at top of page before related-parts carousels.
    head = text[:1000]
    # Remove savings & shipping-threshold phrases so they aren't parsed as item price.
    cleaned = re.sub(
        r"(?:you\s+)?save(?:\s+up\s+to)?\s*\$?\s*[\d,]+(?:\.\d{1,2})?",
        " ",
        head,
        flags=re.I,
    )
    cleaned = re.sub(
        r"(?:free\s+shipping\s+over|orders\s+over|shipping\s+over)\s*\$\s*[\d,]+(?:\.\d{1,2})?",
        " ",
        cleaned,
        flags=re.I,
    )

    # Match both standard ($2,338.95) and split-cent ($ 456 95) price tokens.
    pattern = re.compile(
        r"\$\s*([\d,]+(?:\.\d{1,2}|\s+\d{2}(?=\s|$|\$)))"
    )
    candidates: List[float] = []
    for m in pattern.finditer(cleaned):
        raw_num = re.sub(r"\s+", ".", m.group(1).strip().replace(",", ""))
        try:
            val = float(raw_num)
        except ValueError:
            continue
        if val <= 5.0 or val > 50000.0:
            continue
        if baseline_usd and baseline_usd >= 50.0 and val < baseline_usd * 0.30:
            continue
        candidates.append(round(val, 2))
        if len(candidates) >= 2:
            break

    if not candidates:
        return None
    # When a page shows crossed-out MSRP followed by sale price, pick the lower valid price.
    return min(candidates)


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
            offer_list = (
                offers
                if isinstance(offers, list)
                else ([offers] if isinstance(offers, dict) else [])
            )
            for off in offer_list:
                if not isinstance(off, dict):
                    continue
                price = _safe_float(off.get("price") or off.get("lowPrice"))
                if price and price > 1.0:
                    cond_raw = str(off.get("itemCondition") or "").lower()
                    cond = (
                        "used"
                        if "used" in cond_raw
                        else (
                            "open_box"
                            if "refurb" in cond_raw or "open" in cond_raw
                            else "new"
                        )
                    )
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
    """Searches trusted retailers via direct JSON-LD, indexed highlights, and batch Exa fetch."""

    capabilities = ProviderCapabilities(
        name="universal_retail",
        display_name="Universal Retail (Search + JSON-LD + Exa)",
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
        seen_domains: set[str] = set()
        calls = 0

        def _collect_hits(hits: List[dict[str, str]]) -> None:
            for h in hits:
                url = h["url"]
                if url.lower().endswith(".pdf"):
                    continue
                dom = _domain_of(url)
                if not dom or any(dom.endswith(s) for s in _SKIP_DOMAINS):
                    continue
                if url not in seen_urls:
                    seen_urls.add(url)
                    seen_domains.add(dom)
                    candidate_urls.append({**h, "domain": dom})

        for q in queries:
            calls += 1
            if trusted_map:
                domains_slice = list(trusted_map.keys())[:4]
                site_filter = " OR ".join(f"site:{d}" for d in domains_slice)
                search_q = f"{q} ({site_filter})"
            else:
                search_q = f"{q} buy price"
            _collect_hits(await search_web(search_q, settings, max_results=6))

        # Per-domain targeted fallback for trusted retailers crowded out by OR queries.
        if trusted_map and queries:
            missing_domains = [d for d in trusted_map if d not in seen_domains]
            for dom in missing_domains[:5]:
                calls += 1
                _collect_hits(
                    await search_web(f"site:{dom} {queries[0]}", settings, max_results=2)
                )

        listings_by_url: Dict[str, RawListing] = {}
        needs_exa_fetch: List[dict] = []
        blocked_reasons: Dict[str, str] = {}

        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        ) as client:
            for idx, cand in enumerate(candidate_urls[:12]):
                if idx > 0:
                    await asyncio.sleep(0.25)
                url = cand["url"]
                dom = cand["domain"]
                retailer_name = (trusted_map.get(dom) or {}).get("name") or dom
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        meta = extract_product_metadata(resp.text)
                        if meta:
                            ext_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
                            listings_by_url[url] = RawListing(
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
                            continue
                    elif resp.status_code in (403, 429, 503):
                        blocked_reasons[url] = f"HTTP {resp.status_code} anti-bot / access blocked"
                except Exception as exc:
                    blocked_reasons[url] = f"Fetch error: {str(exc)[:100]}"

                # Step 2: Check if indexed search snippet already contains a valid price.
                snip_price = extract_price_from_text(
                    cand.get("snippet", ""), baseline_usd=watch_item.retail_baseline_usd
                )
                if snip_price is not None:
                    ext_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
                    listings_by_url[url] = RawListing(
                        provider="universal_retail",
                        external_id=ext_id,
                        url=url,
                        title=cand.get("title") or watch_item.name,
                        price_usd=snip_price,
                        shipping_usd=0.0,
                        condition="new",
                        description=cand.get("snippet", "")[:400],
                        location_text=retailer_name,
                        market_type="retail",
                    )
                else:
                    needs_exa_fetch.append(cand)

        # Step 3: Batch-fetch remaining 403-blocked or JS-rendered pages via Exa MCP.
        if needs_exa_fetch:
            calls += 1
            exa_pages = await fetch_urls_exa([c["url"] for c in needs_exa_fetch])
            for cand in needs_exa_fetch:
                url = cand["url"]
                dom = cand["domain"]
                retailer_name = (trusted_map.get(dom) or {}).get("name") or dom
                page = exa_pages.get(url)
                if page:
                    exa_price = extract_price_from_text(
                        page.get("text", ""), baseline_usd=watch_item.retail_baseline_usd
                    )
                    if exa_price is not None:
                        ext_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
                        listings_by_url[url] = RawListing(
                            provider="universal_retail",
                            external_id=ext_id,
                            url=url,
                            title=page.get("title") or cand.get("title") or watch_item.name,
                            price_usd=exa_price,
                            shipping_usd=0.0,
                            condition="new",
                            description=page.get("text", "")[:400],
                            location_text=retailer_name,
                            market_type="retail",
                        )

        # Step 4: Optional SerpApi Google Shopping results when SERPAPI_KEY is configured.
        if settings.serpapi_key and queries:
            calls += 1
            shop_rows = await search_google_shopping_serpapi(
                queries[0], settings.serpapi_key, max_results=6
            )
            for row in shop_rows:
                url = row.get("link") or row.get("product_link") or ""
                price = _safe_float(row.get("extracted_price") or row.get("price"))
                if not url or not price:
                    continue
                ext_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
                listings_by_url.setdefault(
                    url,
                    RawListing(
                        provider="universal_retail",
                        external_id=ext_id,
                        url=url,
                        title=row.get("title") or watch_item.name,
                        price_usd=price,
                        shipping_usd=0.0,
                        condition="new",
                        description=row.get("snippet") or "",
                        location_text=row.get("source") or _domain_of(url),
                        image_url=row.get("thumbnail") or "",
                        market_type="retail",
                    ),
                )

        # Step 5: Only record gaps for trusted retailers where ALL resolution methods failed.
        gaps: Dict[str, dict] = {}
        for cand in candidate_urls:
            url = cand["url"]
            dom = cand["domain"]
            if url in listings_by_url or dom not in trusted_map:
                continue
            retailer_name = (trusted_map.get(dom) or {}).get("name") or dom
            gaps[dom] = {
                "domain": dom,
                "retailer_name": retailer_name,
                "recommended_by": "universal_retail_crawl",
                "reason": blocked_reasons.get(url)
                or "No Product JSON-LD or structured price tags found",
                "sample_item": watch_item.name,
            }

        return ProviderResult(
            listings=list(listings_by_url.values()),
            gaps=list(gaps.values()),
            api_calls=calls,
        )
