"""Offline tests for the Salter Racing (salterracing.com) provider."""

from typing import List, Optional, Tuple

from argos.config import Settings
from argos.models import WatchItem
from argos.providers import salterracing as sr

SAMPLE_PAGE = """
<html><body>
<div class="product-card">
  <h4 class="card-title"><a href="/holley-0-750-4150-78mm-sb-street-hp">Holley 0 750 4150 78mm SB Street HP</a></h4>
  <div class="brand" data-test-info-type="brandName">Holley</div>
  <div class="sku" data-test-info-type="sku">0-17126347B</div>
  <span data-product-price-without-tax="895.99">$895.99</span>
  <span data-product-rrp-price-without-tax="1,059.99">$1,059.99 RRP</span>
</div>
<div class="product-card">
  <h4 class="card-title"><a href="/misc-pump-kit">Holley 80-284 Carb Rebuild Kit</a></h4>
  <div class="brand" data-test-info-type="brandName">Holley</div>
  <div class="sku" data-test-info-type="sku">80-284</div>
  <span data-product-price-without-tax="45.50">$45.50</span>
</div>
<div class="product-card">
  <h4 class="card-title"><a href="/reman-something">No-Price Tile</a></h4>
  <div class="brand" data-test-info-type="brandName">Edelbrock</div>
  <div class="sku" data-test-info-type="sku">1715</div>
</div>
</body></html>
"""


def _install_fake_fetch(monkeypatch, status: int = 200, html: str = SAMPLE_PAGE):
    """Replace the curl_cffi fetch with a recording fake in the provider ns."""

    calls: List[str] = []

    async def fake_fetch(url: str, timeout: float = 25.0) -> Optional[Tuple[int, str]]:
        calls.append(url)
        return (status, html)

    monkeypatch.setattr(sr, "fetch_html_curl_cffi", fake_fetch)
    return calls


def test_fetch_page_prefers_curl_cffi(monkeypatch):
    """Unit test _fetch_page against a faked curl_cffi helper."""
    import asyncio

    async def fake_fetch(url: str, timeout: float = 25.0):
        return (200, "<html>hi</html>")

    monkeypatch.setattr(sr, "fetch_html_curl_cffi", fake_fetch)
    html = asyncio.run(sr._fetch_page("https://www.salterracing.com/x"))
    assert html == "<html>hi</html>"


def test_fetch_page_429_reports_waf(monkeypatch):
    import asyncio

    async def fake_fetch(url: str, timeout: float = 25.0):
        return (429, "")

    monkeypatch.setattr(sr, "fetch_html_curl_cffi", fake_fetch)
    try:
        asyncio.run(sr._fetch_page("https://www.salterracing.com/x"))
        assert False, "expected _SalterFetchError"
    except sr._SalterFetchError as exc:
        assert exc.is_waf
        assert "429" in str(exc)


def test_parse_salter_tiles_extracts_price_brand_sku():
    tiles = sr.parse_salter_tiles(SAMPLE_PAGE)
    assert len(tiles) == 3
    t0 = tiles[0]
    assert t0["title"] == "Holley 0 750 4150 78mm SB Street HP"
    assert t0["brand"] == "Holley"
    assert t0["sku"] == "0-17126347B"
    assert t0["price_usd"] == 895.99
    assert t0["rrp_usd"] == 1059.99
    assert t0["url"] == "/holley-0-750-4150-78mm-sb-street-hp"
    # no-price tile parses with None price (provider drops it later)
    assert tiles[2]["price_usd"] is None


def test_matches_watch_item_tokens_and_negatives():
    tile = {"title": "Holley 0 750 4150 78mm SB Street HP", "brand": "Holley", "sku": "0-17126347B"}
    assert sr.matches_watch_item(tile, required_tokens=["holley", "750", "4150|street hp"])
    assert not sr.matches_watch_item(tile, required_tokens=["holley", "650 cfm"])
    assert not sr.matches_watch_item(tile, negative_keywords=["street hp"])


def test_parse_price_variants():
    assert sr.parse_price("1,059.99") == 1059.99
    assert sr.parse_price("$45.50") == 45.50
    assert sr.parse_price(None) is None
    assert sr.parse_price("0") is None
    assert sr.parse_price("abc") is None


def _make_item(**kw) -> WatchItem:
    base = dict(
        name="Holley 750 CFM 4150 carburetor",
        raw_request="Holley 750",
        search_queries=["holley 750 4150", "holley 0 750 4150"],
        required_tokens=["holley", "750", "4150|street hp"],
        negative_keywords=["650 cfm", "640"],
    )
    base.update(kw)
    return WatchItem(**base)


async def test_search_returns_scoreable_listings(monkeypatch):
    calls = _install_fake_fetch(monkeypatch)
    provider = sr.SalterRacingProvider()
    result = await provider.search(_make_item(), Settings())
    assert result.error is None
    assert result.gaps == []
    assert len(result.listings) == 1  # 750/4150 match kept; rebuild kit lacks "750" token; no-price tile dropped
    top = result.listings[0]
    assert top.provider == "salterracing"
    assert top.price_usd == 895.99
    assert top.condition == "new"
    assert top.market_type == "retail"
    assert top.external_id == "0-17126347B"
    assert "Holley Holley" not in top.title  # no duplicate brand prefix
    # Two search queries + first (and only, <50 tiles) carburetor catalog page
    assert len(calls) == 3
    assert all(u.startswith("https://www.salterracing.com/search.php?keywords=") for u in calls[:2])
    assert calls[2].startswith("https://www.salterracing.com/carburetors-and-components/")


async def test_search_token_filter_drops_mismatches(monkeypatch):
    _install_fake_fetch(monkeypatch)
    provider = sr.SalterRacingProvider()
    item = _make_item(required_tokens=["holley", "20-370"])  # neither tile matches
    result = await provider.search(item, Settings())
    assert result.listings == []


async def test_search_waf_429_reports_gap(monkeypatch):
    _install_fake_fetch(monkeypatch, status=429, html="")
    provider = sr.SalterRacingProvider()
    result = await provider.search(_make_item(), Settings())
    assert result.listings == []
    assert result.error is not None and "429" in result.error
    assert result.gaps[0]["domain"] == "salterracing.com"
    assert result.gaps[0]["retailer_name"] == "Salter Racing"


def test_registry_registers_salterracing():
    from argos.providers.registry import get_provider

    provider = get_provider("salterracing")
    assert provider is not None
    assert provider.capabilities.market_type == "retail"
