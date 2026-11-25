"""Tests for intermediary/affiliate link validation and direct retailer URL resolution."""

import httpx
import pytest

from argos.config import Settings
from argos.inductor import _merge_default_retailers
from argos.models import WatchItem
from argos.providers.link_resolver import (
    clean_retailer_url,
    extract_outbound_retailer_candidates,
    is_intermediary_domain,
    resolve_and_validate_url,
)
from argos.providers.slickdeals import SlickdealsProvider
from argos.providers.base import RawListing
from argos.providers.universal_retail import UniversalRetailProvider


def test_is_intermediary_domain_and_clean_retailer_url():
    assert is_intermediary_domain("https://pricehistory.app/p/instant-pot-duo")
    assert is_intermediary_domain("https://lootpost.com/go/12345")
    assert is_intermediary_domain("camelcamelcamel.com")
    assert not is_intermediary_domain("https://www.amazon.com/dp/B07W55DDFB")
    assert not is_intermediary_domain("homedepot.com")

    dirty_amazon = (
        "https://www.amazon.com/Instant-Pot-Plus-60-Programmable/dp/B07W55DDFB/"
        "ref=sr_1_3?tag=pricehistory-20&utm_source=lootpost&qid=12345"
    )
    assert clean_retailer_url(dirty_amazon) == "https://www.amazon.com/dp/B07W55DDFB"

    dirty_hd = "https://www.homedepot.com/p/Instant-Pot/123?utm_medium=aff&irclickid=xyz&store=12"
    assert clean_retailer_url(dirty_hd) == "https://www.homedepot.com/p/Instant-Pot/123?store=12"


def test_merge_default_retailers_filters_intermediaries_and_preserves_walmart():
    merged = _merge_default_retailers(
        [
            {"name": "PriceHistory", "domain": "www.pricehistory.app"},
            {"name": "Walmart", "domain": "www.walmart.com"},
            {"name": "Amazon", "domain": "amazon.com"},
        ],
        category="kitchen",
    )
    domains = [r["domain"] for r in merged]
    assert "pricehistory.app" not in domains
    assert "walmart.com" in domains
    assert "amazon.com" in domains


@pytest.mark.asyncio
async def test_resolve_drops_522_affiliate_when_no_live_retailer():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "lootpost.com":
            return httpx.Response(522, text="Origin connection timed out")
        return httpx.Response(404)

    html = '<a class="buy-btn" href="https://lootpost.com/go/instant-pot">Buy on Amazon</a>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolved = await resolve_and_validate_url(
            "https://pricehistory.app/p/instant-pot-duo",
            html=html,
            client=client,
        )
    assert resolved is None


@pytest.mark.asyncio
async def test_resolve_recovers_direct_amazon_dp_when_lootpost_522s():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "lootpost.com":
            return httpx.Response(522, text="Cloudflare 522")
        if request.url.host == "www.amazon.com" and "/dp/B07W55DDFB" in request.url.path:
            return httpx.Response(200, text="<html>Instant Pot</html>")
        return httpx.Response(404)

    html = """
    <div data-asin="B07W55DDFB">
      <a class="buy-btn" href="https://lootpost.com/go/instant-pot">Buy on Amazon</a>
    </div>
    """
    candidates = extract_outbound_retailer_candidates(
        html, source_url="https://pricehistory.app/p/instant-pot"
    )
    assert "https://lootpost.com/go/instant-pot" in candidates
    assert "https://www.amazon.com/dp/B07W55DDFB" in candidates

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolved = await resolve_and_validate_url(
            "https://pricehistory.app/p/instant-pot",
            html=html,
            client=client,
        )
    assert resolved == "https://www.amazon.com/dp/B07W55DDFB"


@pytest.mark.asyncio
async def test_universal_retail_resolves_pricehistory_to_direct_amazon_or_drops(monkeypatch):
    async def fake_search_web(query, settings, max_results=6):
        return [
            {
                "title": "Instant Pot Duo 6 Qt - PriceHistory.app",
                "url": "https://pricehistory.app/p/instant-pot-duo",
                "snippet": "Instant Pot Duo 6 Qt currently $69.99 on Amazon",
            }
        ]

    monkeypatch.setattr("argos.providers.universal_retail.search_web", fake_search_web)

    # Case 1: pricehistory.app links to lootpost.com (522) and has no working fallback -> dropped
    def dead_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pricehistory.app":
            return httpx.Response(
                200,
                text=(
                    '<html><head><meta property="product:price:amount" content="69.99"></head>'
                    '<body><a href="https://lootpost.com/go/123">Buy Now</a></body></html>'
                ),
            )
        return httpx.Response(522)

    orig_client = httpx.AsyncClient

    def make_dead_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(dead_handler)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr("argos.providers.universal_retail.httpx.AsyncClient", make_dead_client)
    item = WatchItem(name="Instant Pot Duo 6 Qt", search_queries=["Instant Pot Duo 6 Qt"], trusted_retailers=[])
    settings = Settings(database_url="sqlite:///:memory:", enable_telegram=False)

    res_dead = await UniversalRetailProvider().search(item, settings)
    assert res_dead.listings == []

    # Case 2: pricehistory.app links to direct amazon.com/dp/... -> listing URL becomes direct amazon.com/dp/...
    def live_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pricehistory.app":
            return httpx.Response(
                200,
                text=(
                    '<html><head><meta property="product:price:amount" content="69.99"></head>'
                    '<body><a href="https://www.amazon.com/Instant-Pot/dp/B00FLYWNYQ?tag=ph-20">Buy on Amazon</a></body></html>'
                ),
            )
        if request.url.host == "www.amazon.com":
            return httpx.Response(200, text="<html>Instant Pot Duo</html>")
        return httpx.Response(404)

    def make_live_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(live_handler)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr("argos.providers.universal_retail.httpx.AsyncClient", make_live_client)
    res_live = await UniversalRetailProvider().search(item, settings)
    assert len(res_live.listings) == 1
    assert res_live.listings[0].url == "https://www.amazon.com/dp/B00FLYWNYQ"
    assert res_live.listings[0].location_text == "amazon.com"


@pytest.mark.asyncio
async def test_slickdeals_verify_rejects_522_and_resolves_direct_url():
    def dead_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slickdeals.net":
            return httpx.Response(
                200,
                text='<a class="dealDetails__buyButton" href="https://lootpost.com/deal/9">Buy</a>',
            )
        return httpx.Response(522)

    listing = RawListing(
        provider="slickdeals",
        external_id="sd1",
        url="https://slickdeals.net/f/12345-instant-pot",
        title="Instant Pot $69",
        price_usd=69.0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(dead_handler)) as client:
        ok = await SlickdealsProvider()._verify_live_deal(client, listing)
    assert ok is False

    def live_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slickdeals.net":
            return httpx.Response(
                200,
                text='<a class="dealDetails__buyButton" href="https://www.amazon.com/dp/B00FLYWNYQ?tag=sd-20">Buy</a>',
            )
        return httpx.Response(200, text="<html>Instant Pot</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(live_handler)) as client:
        ok2 = await SlickdealsProvider()._verify_live_deal(client, listing)
    assert ok2 is True
    assert listing.url == "https://www.amazon.com/dp/B00FLYWNYQ"
