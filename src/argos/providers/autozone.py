"""AutoZone (autozone.com) provider — search-index price feed (keyless).

Why search-index instead of direct scraping:
    autozone.com sits behind DataDome (captcha-delivery.com): the JS
    challenge page ("Please enable JS and disable any ad blocker",
    ct.captcha-delivery.com challenge cookie ``tqNa7fex...``) is served
    to every request from this cluster's datacenter egress, so plain
    curl is hard 403'd (verified 2026-11-25). Direct scraping and
    Exa web_fetch both land on the challenge page. However the
    *search-engine index* carries AutoZone's own product titles, part
    numbers and current prices (verified 2026-11-25 via Exa MCP:
    "MSD Performance Distributor 8582 | AutoZone ... The price of this
    item is: 440 dollars and 09 cents. Starting at 488 dollars and
    99 cents"). So this provider mines keyless MCP search endpoints —
    Exa (web_search_exa) as primary, Parallel (web_search) as
    fallback — exactly the sanctioned pattern for WAF'd retail domains
    (see the homedepot/salterracing/summitracing precedents).

Behavior:
    - Issues the watch item's own search queries (+ one site-anchored
      query) against both backends.
    - Keeps only candidates whose URL is an autozone.com page and whose
      title+snippet pass the item's required tokens AND negative
      keywords (same AND-of-OR-group matching with a looser
      word-presence fallback).
    - Extracts a price from the candidate snippet. AutoZone's Exa
      captures spell prices out ("440 dollars and 09 cents"), so both
      ``$199.99``-style and dollar/spelled-out formats parse; the
      lowest plausible value wins (current sale price over the
      "starting at" / MSRP figure). Emits one RawListing per unique
      URL, condition "new", market_type "retail".
    - If *neither* backend yields any autozone.com hit for any query
      (backend outage / rate-limit), surfaces a provider gap dict so
      the gap tracker stays honest. AZ hits that fail the item's
      filters are NOT a gap — the avenue works, the product simply
      isn't an assortment match today.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

import httpx

from argos.config import Settings
from argos.models import WatchItem
from argos.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    ProviderResult,
    RawListing,
)
from argos.providers.registry import register_provider
from argos.providers.web_search import _exa_mcp_search, _parse_exa_mcp_text

logger = logging.getLogger(__name__)

DOMAIN = "autozone.com"
SITE = f"https://www.{DOMAIN}"
PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
MAX_QUERIES = 3
MAX_RESULTS_PER_QUERY = 8
PRICE_MIN = 15.0
PRICE_MAX = 3000.0
MCP_TIMEOUT = 20.0
BACKOFF_SECONDS = 1.5

# Classic "$199.99" / HD-style spaced "$ 199 . 99" / plain "$456.95".
PRICE_RE = re.compile(
    r"\$\s?((?:\d{1,3}(?:,\d{3})+|\d{2,5})(?:\s*\.\s*\d{2})?|\d{1,2}\s*\.\s*\d{2})"
)
# AutoZone Exa capture style: "440 dollars and 09 cents" / "488 dollars".
DOLLAR_WORDS_RE = re.compile(
    r"(\d{1,5})\s+dollars(?:\s+and\s+(\d{1,2})\s+cents)?", re.IGNORECASE
)


def extract_price(text: str) -> Optional[float]:
    """Return the lowest plausible USD price in *text*, else None.

    Understands both currency-symbol prices (``$199.99``, ``$ 199 . 99``)
    and AutoZone's spelled-out search-index captures (``440 dollars and
    09 cents``).
    """
    best: Optional[float] = None

    def consider(price: float) -> None:
        nonlocal best
        if PRICE_MIN <= price <= PRICE_MAX:
            best = price if best is None else min(best, price)

    for m in PRICE_RE.finditer(text):
        raw = m.group(1).replace(",", "").replace(" ", "")
        try:
            consider(float(raw))
        except ValueError:
            continue
    for m in DOLLAR_WORDS_RE.finditer(text):
        dollars = int(m.group(1))
        cents = int(m.group(2)) if m.group(2) else 0
        if cents > 99:
            continue
        consider(dollars + cents / 100.0)
    return best


def _token_group_matches(text: str, option: str) -> bool:
    """Match one OR-option of a required-token group against *text*.

    A full substring match always works; otherwise each word of the
    option must be present in *text* (order-insensitive), so ``pro 8``
    matches ``Instant Pot 8 qt ... Duo Pro`` style titles where the
    words appear separately.
    """
    if option in text:
        return True
    words = [w for w in option.split() if w]
    if not words:
        return True
    return all((w in text) for w in words)


def matches_watch_item(text: str, watch_item: WatchItem) -> bool:
    """AND-of-OR-groups required tokens, then negative-regex rejection."""
    tokens = watch_item.required_tokens or []
    for group in tokens:
        opts = group if isinstance(group, list) else str(group).split("|")
        if not any(_token_group_matches(text, o.strip()) for o in opts if o.strip()):
            return False
    for neg in watch_item.negative_keywords or []:
        neg = neg.strip()
        if not neg:
            continue
        try:
            if re.search(neg, text, re.IGNORECASE):
                return False
        except re.error:
            if neg.lower() in text:
                return False
    return True


def _norm_hit(title: str, url: str, snippet: str) -> Dict[str, str]:
    return {
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "snippet": (snippet or "").strip()[:4000],
    }


async def _parallel_search(query: str, max_results: int = 10) -> List[Dict[str, str]]:
    """Keyless Parallel search MCP (fallback backend). Returns [] on any error."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search",
            "arguments": {
                "objective": query,
                "search_queries": [query],
                "session_id": uuid.uuid4().hex,
            },
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "argos-deal-research/0.2",
    }
    try:
        async with httpx.AsyncClient(timeout=MCP_TIMEOUT) as client:
            resp = await client.post(PARALLEL_MCP_URL, json=payload, headers=headers)
            resp.raise_for_status()
        text = _parse_exa_mcp_text(resp.text)
    except Exception as exc:  # noqa: BLE001 - backend outage must not crash scans
        logger.debug("parallel search failed for %r: %s", query, exc)
        return []
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("parallel search returned undecodable text for %r", query)
        return []
    hits: List[Dict[str, str]] = []
    for res in data.get("results") or []:
        if not isinstance(res, dict):
            continue
        url = res.get("url") or ""
        if "autozone" not in url:
            continue
        excerpts = res.get("excerpts") or []
        if isinstance(excerpts, str):
            excerpts = [excerpts]
        snippet = " ".join(" ".join(str(e).split()) for e in excerpts if e)
        hits.append(_norm_hit(res.get("title") or "", url, snippet))
        if len(hits) >= max_results:
            break
    return hits


@register_provider
class AutozoneProvider(BaseProvider):
    """Keyless search-index price feed for autozone.com (DataDome 403 direct)."""

    capabilities = ProviderCapabilities(
        name="autozone",
        display_name="AutoZone (search-index feed)",
        market_type="retail",
        requires_api_key=False,
        estimated_cost_per_call_usd=0.0,
        normal_interval_minutes=5,
        hot_interval_minutes=1,
    )

    def is_configured(self, settings: Settings) -> bool:
        return True

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        queries = [q for q in (watch_item.search_queries or []) if q][: MAX_QUERIES - 1]
        name_q = watch_item.name.strip()
        if name_q:
            queries.append(f"autozone.com {name_q}")
        if not queries:
            return ProviderResult(error="no search queries for watch item")

        seen_urls: set[str] = set()
        hits: List[Dict[str, str]] = []
        exa_worked = False
        par_worked = False

        for q in queries:
            try:
                exa_hits = await _exa_mcp_search(q, MAX_RESULTS_PER_QUERY)
                exa_worked = exa_worked or bool(exa_hits)
            except Exception as exc:  # noqa: BLE001
                logger.debug("autozone exa search failed for %r: %s", q, exc)
                exa_hits = []
            az_exa = [
                _norm_hit(h.get("title", ""), h.get("url", ""), h.get("snippet", ""))
                for h in exa_hits
                if isinstance(h, dict) and "autozone.com" in h.get("url", "")
            ]
            if not az_exa:
                try:
                    par_hits = await _parallel_search(q, MAX_RESULTS_PER_QUERY)
                    par_worked = par_worked or bool(par_hits)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("autozone parallel search failed for %r: %s", q, exc)
                    par_hits = []
                az_exa = par_hits
            hits.extend(az_exa)
            await self._sleep(BACKOFF_SECONDS)

        gap: Optional[Dict[str, str]] = None
        if not hits and not (exa_worked or par_worked):
            gap = {
                "domain": DOMAIN,
                "retailer_name": "AutoZone",
                "recommended_by": "autozone",
                "reason": "Search-index feeds (Exa + Parallel) returned no autozone.com hits for any query (possible MCP rate-limit/outage).",
                "sample_item": watch_item.name,
            }

        listings: List[RawListing] = []
        for h in hits:
            url = h["url"]
            if url in seen_urls:
                continue
            text = f"{h['title']} {h['snippet']}".lower()
            if not matches_watch_item(text, watch_item):
                continue
            price = extract_price(h["snippet"]) or extract_price(h["title"])
            if price is None:
                continue
            seen_urls.add(url)
            listings.append(
                RawListing(
                    provider=self.capabilities.name,
                    external_id=hashlib.sha1(url.encode()).hexdigest()[:16],
                    url=url,
                    title=h["title"] or url,
                    price_usd=price,
                    condition="new",
                    description=(
                        "AutoZone retail; price sourced from search-index "
                        "capture (direct fetch is DataDome-blocked from this egress)"
                    ),
                    market_type="retail",
                )
            )

        result = ProviderResult(
            listings=listings,
            gaps=[gap] if gap else [],
            error=None,
            api_calls=max(len(queries), 1) * 2,
        )
        logger.info(
            "autozone search item=%s queries=%d az_hits=%d listings=%d gap=%s",
            watch_item.id,
            len(queries),
            len(hits),
            len(listings),
            bool(gap),
        )
        return result

    @staticmethod
    async def _sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)
