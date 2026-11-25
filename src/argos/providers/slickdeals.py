"""Slickdeals RSS deal aggregator provider with expiry, freshness, and merchant-link verification."""

import email.utils
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from argos.config import Settings
from argos.models import WatchItem
from argos.providers.base import BaseProvider, ProviderCapabilities, ProviderResult, RawListing
from argos.providers.link_resolver import (
    DEAD_STATUS_CODES,
    clean_retailer_url,
    is_intermediary_domain,
)
from argos.providers.registry import register_provider

logger = logging.getLogger(__name__)

_DEFAULT_MAX_AGE_DAYS = 14
_EXPIRED_THREAD_PATTERNS = (
    r"this deal has expired",
    r"deal has expired",
    r"deal is expired",
    r"expired\s+deal",
    r'"isExpired"\s*:\s*true',
    r'class="[^"]*\bexpired\b[^"]*"',
)
_OOS_MERCHANT_PATTERNS = (
    r"\bout of stock\b",
    r"\bsold out\b",
    r"\bno longer available\b",
    r"\bcurrently unavailable\b",
    r"\bpage not found\b",
)


def _extract_price(title: str, desc: str = "") -> Optional[float]:
    for text in (title, desc):
        m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text or "")
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _is_expired_url(url: str) -> bool:
    path = urlparse(url or "").path.lower()
    return "/e/" in path


def _is_fresh_pubdate(pub_date: str, max_age_days: int = _DEFAULT_MAX_AGE_DAYS) -> bool:
    if not pub_date:
        return True
    try:
        dt = email.utils.parsedate_to_datetime(pub_date.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return age <= timedelta(days=max_age_days)
    except Exception:
        return True


def is_slickdeals_thread_expired(html: str) -> bool:
    """Detect expired deal banners or badges inside a Slickdeals thread page."""
    lower = (html or "").lower()
    return any(re.search(pat, lower) for pat in _EXPIRED_THREAD_PATTERNS)


def extract_merchant_url_from_thread(html: str) -> Optional[str]:
    """Find the primary outbound merchant or Slickdeals click-redirect link in a thread."""
    soup = BeautifulSoup(html or "", "html.parser")
    for sel in ("a.dealDetails__buyButton", "a[data-click-location='buyNow']", "a[href*='slickdeals.net/click']", "a[href*='u2=']"):
        el = soup.select_one(sel)
        if el and el.get("href"):
            href = str(el["href"]).strip()
            if href.startswith("/"):
                return f"https://slickdeals.net{href}"
            if href.startswith("http"):
                return href
    return None


def parse_slickdeals_rss(xml_text: str, max_age_days: int = _DEFAULT_MAX_AGE_DAYS) -> List[RawListing]:
    """Parse Slickdeals RSS XML, dropping /e/ expired URLs and stale threads."""
    root = ET.fromstring(xml_text)
    results: List[RawListing] = []
    seen: set[str] = set()
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        desc = (item.findtext("description") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        if _is_expired_url(link):
            continue
        if not _is_fresh_pubdate(pub_date, max_age_days=max_age_days):
            continue
        ext_id = hashlib.sha1(guid.encode("utf-8")).hexdigest()[:20]
        if ext_id in seen:
            continue
        price = _extract_price(title, desc)
        if price is None:
            continue
        seen.add(ext_id)
        results.append(
            RawListing(
                provider="slickdeals",
                external_id=ext_id,
                url=link,
                title=title,
                price_usd=price,
                condition="new",
                description=re.sub(r"<[^>]+>", " ", desc)[:400],
                market_type="retail",
            )
        )
    return results


@register_provider
class SlickdealsProvider(BaseProvider):
    """Monitors Slickdeals search RSS feeds and verifies threads are not expired/404."""

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

    async def _verify_live_deal(self, client: httpx.AsyncClient, listing: RawListing) -> bool:
        """Fetch thread page and optional merchant link to reject expired or 404 deals."""
        try:
            resp = await client.get(listing.url)
            if resp.status_code in (404, 410):
                return False
            if resp.status_code != 200:
                return True
            if is_slickdeals_thread_expired(resp.text):
                return False

            merchant_url = extract_merchant_url_from_thread(resp.text)
            if merchant_url:
                try:
                    m_resp = await client.get(merchant_url)
                    if m_resp.status_code in DEAD_STATUS_CODES:
                        return False
                    if m_resp.status_code == 200:
                        m_head = m_resp.text[:8000].lower()
                        if any(re.search(p, m_head) for p in _OOS_MERCHANT_PATTERNS):
                            return False
                    final_url = str(m_resp.url)
                    if final_url and not is_intermediary_domain(final_url):
                        listing.url = clean_retailer_url(final_url)
                except httpx.HTTPError:
                    return False
        except Exception as exc:
            logger.debug("Slickdeals thread verify failed for %s: %s", listing.url, exc)
        return True

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
                    candidates = parse_slickdeals_rss(resp.text)
                except Exception as exc:
                    logger.debug("Slickdeals RSS query %r failed: %s", q, exc)
                    continue

                for cand in candidates:
                    if cand.external_id in seen:
                        continue
                    if await self._verify_live_deal(client, cand):
                        seen[cand.external_id] = cand

        return ProviderResult(listings=list(seen.values()), api_calls=calls)
