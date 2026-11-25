"""Offline tests for the Salter Racing (salterracing.com) provider."""

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


class _FakeResponse:
    def __init__(self, html: str, status_code: int = 200):
        self.text = html
        self.status_code = status_code


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str):
        self.calls.append(url)
        return _FakeResponse(SAMPLE_PAGE)


class _FakeWafClient(_FakeClient):
    async def get(self, url: str):
        self.calls.append(url)
        return _FakeResponse("", status_code=429)


async def test_search_returns_scoreable_listings(monkeypatch):
    class Client(_FakeClient):
        instances = []

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            Client.instances.append(self)

    monkeypatch.setattr(sr.httpx, "AsyncClient", Client)
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
    urls = [c for c in Client.instances[0].calls]
    # Two search queries + first (and only, <50 tiles) /carburetors/ catalog page
    assert len(urls) == 3
    assert all(u.startswith("https://www.salterracing.com/search.php?keywords=") for u in urls[:2])
    assert urls[2].startswith("https://www.salterracing.com/carburetors/")


async def test_search_token_filter_drops_mismatches(monkeypatch):
    monkeypatch.setattr(sr.httpx, "AsyncClient", _FakeClient)
    provider = sr.SalterRacingProvider()
    item = _make_item(required_tokens=["holley", "20-370"])  # neither tile matches
    result = await provider.search(item, Settings())
    assert result.listings == []


async def test_search_waf_429_reports_gap(monkeypatch):
    monkeypatch.setattr(sr.httpx, "AsyncClient", _FakeWafClient)
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
