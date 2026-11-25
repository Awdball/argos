"""Validate aggregator/affiliate links and resolve them to direct retailer URLs."""

from __future__ import annotations

import logging
import re
from typing import List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Price trackers, deal/coupon aggregators, and affiliate link-farms that should
# never be surfaced directly as the final deal URL.
PRICE_TRACKER_AND_AGGREGATOR_DOMAINS = frozenset(
    {
        "pricehistory.app",
        "camelcamelcamel.com",
        "keepa.com",
        "honey.com",
        "joinhoney.com",
        "rakuten.com",
        "retailmenot.com",
        "dealnews.com",
        "techbargains.com",
        "bensbargains.com",
        "bradsdeals.com",
        "slickdeals.net",
        "brickseek.com",
        "nowinstock.net",
        "trackalacker.com",
        "hotstock.io",
        "pricegrabber.com",
        "shopzilla.com",
        "nextag.com",
        "capitaloneshopping.com",
        "offers.com",
        "couponfollow.com",
        "dealcatcher.com",
        "hip2save.com",
        "thekrazycouponlady.com",
        "krazycouponlady.com",
    }
)

AFFILIATE_REDIRECT_DOMAINS = frozenset(
    {
        "lootpost.com",
        "amzn.to",
        "bit.ly",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "ow.ly",
        "buff.ly",
        "howl.me",
        "fave.co",
        "rstyle.me",
        "skimresources.com",
        "redirectingat.com",
        "viglink.com",
        "anrdoezrs.net",
        "tkqlhce.com",
        "jdoqocy.com",
        "dpbolvw.net",
        "kqzyfj.com",
        "pjatr.com",
        "pntra.com",
        "avantlink.com",
        "linksynergy.com",
        "shareasale.com",
        "impact.com",
        "ojrq.net",
        "awin1.com",
        "prf.hn",
    }
)

ALL_INTERMEDIARY_DOMAINS = PRICE_TRACKER_AND_AGGREGATOR_DOMAINS | AFFILIATE_REDIRECT_DOMAINS

# HTTP status codes indicating a dead origin, broken proxy, or removed page.
# Note: 403/503 are intentionally excluded for known retail destinations because
# major retailers (Amazon, Home Depot, AutoZone) frequently serve bot challenges
# to headless HTTP clients on live product pages.
DEAD_STATUS_CODES = frozenset({404, 410, 451, 500, 502, 504, 520, 521, 522, 523, 524, 525, 526})

_TRACKING_QUERY_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "tag",
        "linkcode",
        "linkid",
        "ascsubtag",
        "ref",
        "ref_",
        "psc",
        "qid",
        "sr",
        "keywords",
        "dib",
        "dib_tag",
        "pf_rd_p",
        "pf_rd_r",
        "pd_rd_w",
        "pd_rd_r",
        "pd_rd_wg",
        "content-id",
        "fbclid",
        "gclid",
        "msclkid",
        "irclickid",
        "irgwc",
        "awc",
        "cjevent",
        "affid",
        "affiliate_id",
        "subid",
        "subid1",
        "clickid",
        "camp",
        "creative",
        "creativeasin",
    }
)

_REDIRECT_TARGET_PARAMS = ("u2", "u", "url", "dest", "destination", "target", "redirect", "r")

_BUY_BUTTON_KEYWORDS = (
    "buy",
    "amazon",
    "check price",
    "view deal",
    "go to",
    "shop",
    "store",
    "merchant",
    "retailer",
    "deal",
)

_ASIN_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b")
_AMAZON_DP_RE = re.compile(r"amazon\.com/(?:[^\"'\s>]+/)?(?:dp|gp/product)/([A-Z0-9]{10})", re.I)
_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\'][^"\']*url=([^"\'>\s]+)',
    re.I,
)


def domain_of(url: str) -> str:
    """Return lowercase hostname without leading 'www.'."""
    host = urlparse(url or "").netloc.lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def is_intermediary_domain(domain_or_url: str) -> bool:
    """True when host is a price tracker, coupon/deal aggregator, or affiliate redirector."""
    dom = domain_of(domain_or_url) if "://" in domain_or_url else domain_or_url.lower().removeprefix("www.")
    if not dom:
        return False
    return any(dom == d or dom.endswith(f".{d}") for d in ALL_INTERMEDIARY_DOMAINS)


def clean_retailer_url(url: str) -> str:
    """Strip affiliate/tracking parameters and canonicalize Amazon product URLs."""
    parsed = urlparse((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return url

    dom = domain_of(url)
    if dom in ("amazon.com", "smile.amazon.com"):
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", parsed.path, re.I)
        if m:
            return f"https://www.amazon.com/dp/{m.group(1).upper()}"

    if parsed.query:
        qs = parse_qs(parsed.query, keep_blank_values=False)
        kept = [
            (k, v)
            for k, v_list in qs.items()
            if k.lower() not in _TRACKING_QUERY_PARAMS
            for v in v_list
        ]
        new_query = urlencode(kept)
    else:
        new_query = ""

    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ""))


def _extract_embedded_target(url: str) -> Optional[str]:
    """Extract direct destination URL embedded inside a redirector's query string."""
    parsed = urlparse(url or "")
    if not parsed.query:
        return None
    qs = parse_qs(parsed.query)
    for key in _REDIRECT_TARGET_PARAMS:
        vals = qs.get(key) or qs.get(key.upper())
        if vals:
            cand = vals[0].strip()
            if cand.startswith(("http://", "https://")):
                return cand
    return None


def extract_outbound_retailer_candidates(html: str, source_url: str = "") -> List[str]:
    """Extract prioritized outbound retailer/affiliate URLs from an aggregator page."""
    if not html:
        return []

    source_dom = domain_of(source_url)
    prioritized: List[str] = []
    secondary: List[str] = []
    seen: set[str] = set()

    def _add(target_list: List[str], candidate: str) -> None:
        c = (candidate or "").strip()
        if not c.startswith(("http://", "https://")):
            return
        c_dom = domain_of(c)
        if not c_dom or c_dom == source_dom:
            return
        if c not in seen:
            seen.add(c)
            target_list.append(c)

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if not href.startswith(("http://", "https://")):
            continue
        embedded = _extract_embedded_target(href)
        if embedded and not is_intermediary_domain(embedded):
            _add(prioritized, embedded)

        text_blob = " ".join(
            [
                a.get_text(" ", strip=True) or "",
                " ".join(a.get("class") or []) if isinstance(a.get("class"), list) else str(a.get("class") or ""),
                str(a.get("id") or ""),
                str(a.get("title") or ""),
                str(a.get("aria-label") or ""),
            ]
        ).lower()
        href_dom = domain_of(href)
        looks_like_buy = any(kw in text_blob for kw in _BUY_BUTTON_KEYWORDS)
        if looks_like_buy or (not is_intermediary_domain(href_dom) and ("/dp/" in href or "/p/" in href or "/ip/" in href)):
            _add(prioritized, href)
        else:
            _add(secondary, href)

    # Direct Amazon product links or ASINs referenced in markup/scripts.
    for m in _AMAZON_DP_RE.finditer(html):
        _add(prioritized, f"https://www.amazon.com/dp/{m.group(1).upper()}")

    # If the page references Amazon/ASINs (e.g., data-asin="B0..."), add canonical dp URL as fallback.
    if "amazon" in html.lower() or "asin" in html.lower() or "amazon" in (source_url or "").lower():
        for m in _ASIN_RE.finditer(html):
            _add(secondary, f"https://www.amazon.com/dp/{m.group(1).upper()}")

    return prioritized + secondary


async def _follow_and_validate_candidate(
    candidate_url: str,
    client: httpx.AsyncClient,
) -> Optional[str]:
    """Follow redirects on a candidate URL and return a clean direct retailer URL if live."""
    embedded = _extract_embedded_target(candidate_url)
    target = embedded or candidate_url

    try:
        resp = await client.get(target, follow_redirects=True)
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("Link validation failed for %s: %s", target, exc)
        return None

    if resp.status_code in DEAD_STATUS_CODES:
        logger.debug("Link validation rejected HTTP %s for %s", resp.status_code, target)
        return None

    final_url = str(resp.url)
    # Check for meta-refresh redirect if still on an intermediary domain.
    if is_intermediary_domain(final_url) and resp.status_code == 200:
        m = _META_REFRESH_RE.search(resp.text[:4000])
        if m:
            refresh_url = m.group(1).strip()
            if refresh_url.startswith(("http://", "https://")) and not is_intermediary_domain(refresh_url):
                return await _follow_and_validate_candidate(refresh_url, client)
        return None

    if is_intermediary_domain(final_url):
        return None

    return clean_retailer_url(final_url)


async def resolve_and_validate_url(
    url: str,
    html: str = "",
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    """Resolve price-tracker/affiliate URLs to a live, direct retailer URL.

    Returns a cleaned direct retailer URL, or None if the link is on an
    intermediary domain and cannot be resolved to a live retailer destination.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None

    if not is_intermediary_domain(url):
        return clean_retailer_url(url)

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    try:
        # 1. If the URL itself embeds a target or is a pure redirector, follow it directly.
        embedded = _extract_embedded_target(url)
        if embedded:
            resolved = await _follow_and_validate_candidate(embedded, client)
            if resolved:
                return resolved

        dom = domain_of(url)
        if any(dom == d or dom.endswith(f".{d}") for d in AFFILIATE_REDIRECT_DOMAINS):
            return await _follow_and_validate_candidate(url, client)

        # 2. Price-tracker / aggregator page: fetch HTML if needed and inspect outbound links.
        page_html = html
        if not page_html:
            try:
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code in DEAD_STATUS_CODES:
                    return None
                final_url = str(resp.url)
                if not is_intermediary_domain(final_url):
                    return clean_retailer_url(final_url)
                if resp.status_code == 200:
                    page_html = resp.text
            except httpx.HTTPError as exc:
                logger.debug("Failed to fetch aggregator page %s: %s", url, exc)
                return None

        candidates = extract_outbound_retailer_candidates(page_html, source_url=url)
        for cand in candidates[:6]:
            resolved = await _follow_and_validate_candidate(cand, client)
            if resolved:
                return resolved

        return None
    finally:
        if owns_client:
            await client.aclose()
