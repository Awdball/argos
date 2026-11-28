"""Tests for Holley Factory Refurbished carburetor provider."""

import pytest

from argos.config import Settings
from argos.models import WatchItem
from argos.providers.holley_refurb import HolleyRefurbProvider, parse_holley_refurb_html
from argos.providers.registry import get_provider


SAMPLE_HOLLEY_REFURB_HTML = """
<html><body>
<ul class="product-grid">
  <li partnumber="FR-4779S">
    <a class="image" href="/products/fuel_systems/carburetors/factory_refurbished_carburetors/parts/FR-4779S">
      <img class="grid-product-image" src="//images.holley.com/x220/fr4779s.jpg" />
    </a>
    <div class="product-info">
      <div class="name">
        <div class="brand">Holley</div>
        <a class="name-link" href="/products/fuel_systems/carburetors/factory_refurbished_carburetors/parts/FR-4779S">Holley Double Pumper Carburetor - Factory Refurbished</a>
        <span class="short-desc">750 CFM - 4150 - Mechanical Secondaries - Shiny - Gasoline</span>
      </div>
      <div class="part-number">
        <div class="ships-free"><span>Free Shipping</span></div>
      </div>
    </div>
    <div class="price-cta">
      <div class="price" data-price="399.95">$399.95</div>
    </div>
  </li>
  <li partnumber="FR-80670">
    <div class="product-info">
      <div class="name">
        <div class="brand">Holley</div>
        <a class="name-link" href="/products/fuel_systems/carburetors/factory_refurbished_carburetors/parts/FR-80670">Holley Street Avenger Carburetor - Factory Refurbished</a>
        <span class="short-desc">670 CFM - 4150 - Vacuum Secondaries</span>
      </div>
    </div>
    <div class="price-cta">
      <div class="price" data-price="359.95">$359.95</div>
    </div>
  </li>
</ul>
</body></html>
"""


def test_parse_holley_refurb_html_extracts_tiles():
    listings = parse_holley_refurb_html(SAMPLE_HOLLEY_REFURB_HTML)
    assert len(listings) == 2
    first = listings[0]
    assert first.provider == "holley_refurb"
    assert first.external_id == "FR-4779S"
    assert first.price_usd == 399.95
    assert first.condition == "open_box"
    assert first.market_type == "retail"
    assert first.url == "https://www.holley.com/products/fuel_systems/carburetors/factory_refurbished_carburetors/parts/FR-4779S"
    assert first.image_url == "https://images.holley.com/x220/fr4779s.jpg"
    assert "750 CFM - 4150" in first.description


def test_holley_refurb_registered():
    prov = get_provider("holley_refurb")
    assert prov is not None
    assert prov.capabilities.name == "holley_refurb"


@pytest.mark.asyncio
async def test_holley_refurb_skips_non_carb_and_parses_carb(monkeypatch):
    async def fake_fetch():
        return 200, SAMPLE_HOLLEY_REFURB_HTML

    monkeypatch.setattr("argos.providers.holley_refurb._fetch_holley_refurb_page", fake_fetch)
    settings = Settings(database_url="sqlite:///:memory:", enable_telegram=False)

    non_carb = WatchItem(name="Instant Pot Duo 6 Qt", raw_request="Instant Pot Duo")
    res_skip = await HolleyRefurbProvider().search(non_carb, settings)
    assert res_skip.listings == []
    assert res_skip.api_calls == 0

    carb = WatchItem(
        name="Holley 750 CFM 4150 4-Barrel Carburetor",
        raw_request="Holley 750 CFM 4150 carburetor",
    )
    res_hit = await HolleyRefurbProvider().search(carb, settings)
    assert res_hit.api_calls == 1
    assert len(res_hit.listings) == 2
