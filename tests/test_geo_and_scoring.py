"""Tests for dynamic geo-radius, landed cost, negative keywords, and bundle scoring."""

from argos.config import Settings
from argos.geo import haversine_miles, recommended_radius_miles
from argos.models import WatchItem
from argos.providers.base import RawListing
from argos.scoring import evaluate_listing


def _settings() -> Settings:
    return Settings(database_url="sqlite:///:memory:", enable_telegram=False)


def test_dynamic_radius_tiers():
    s = _settings()
    assert recommended_radius_miles(None, s) == 20
    assert recommended_radius_miles(125.0, s) == 20
    assert recommended_radius_miles(200.0, s) == 40
    assert recommended_radius_miles(499.99, s) == 40
    assert recommended_radius_miles(500.0, s) == 60
    assert recommended_radius_miles(2100.0, s) == 60


def test_haversine_crossroads_to_little_elm():
    dist = haversine_miles(33.2259, -96.9997, 33.1768, -96.9583)
    assert 3.0 < dist < 6.0


def test_scoring_negative_scam_shipping_and_bundles():
    s = _settings()
    item = WatchItem(
        id=1,
        name="MSD 8582 Pro-Billet Distributor",
        raw_request="MSD 8582 or kit with 6AL and coil",
        search_queries=["msd 8582"],
        required_tokens=["msd", "8582"],
        negative_keywords=["351w", "sbc", "chevy"],
        bundle_components=[
            {"name": "MSD 6AL", "tokens": ["6al", "6425"], "used_value_usd": 140.0, "new_value_usd": 240.0},
            {"name": "Blaster Coil", "tokens": ["blaster", "coil"], "used_value_usd": 35.0, "new_value_usd": 55.0},
        ],
        retail_baseline_usd=460.0,
        new_deal_target_usd=390.0,
        used_deal_target_usd=250.0,
        radius_miles=40,
        status="active",
    )

    # Scam placeholder price rejected
    scam = RawListing("craigslist", "1", "https://ex/1", "MSD 8582 Ford 302", price_usd=1.0)
    assert not evaluate_listing(scam, item, s).passed

    # Negative keyword rejected
    wrong_engine = RawListing("craigslist", "2", "https://ex/2", "MSD 8582 351W distributor", price_usd=180.0)
    res_neg = evaluate_listing(wrong_engine, item, s)
    assert not res_neg.passed
    assert "negative_keyword" in res_neg.reason

    # eBay landed cost (price + shipping) pushes over target
    over_ship = RawListing(
        "ebay", "3", "https://ex/3", "MSD 8582 Ford 302", price_usd=230.0, shipping_usd=35.0, market_type="secondhand"
    )
    res_ship = evaluate_listing(over_ship, item, s)
    assert not res_ship.passed
    assert res_ship.landed_cost_usd == 265.0

    # Bundle kit raises effective target (250 + 140 + 35 = 425) -> $360 passes!
    bundle = RawListing(
        "craigslist",
        "4",
        "https://ex/4",
        "MSD 8582 Ford 302 Distributor with MSD 6AL box and Blaster 2 coil",
        price_usd=360.0,
        distance_miles=18.0,
        market_type="secondhand",
    )
    res_bundle = evaluate_listing(bundle, item, s)
    assert res_bundle.passed
    assert res_bundle.matched_bundle is True
    assert res_bundle.matched_bundle_names == ["MSD 6AL", "Blaster Coil"]
    assert res_bundle.effective_target_usd == 425.0

    # Outside radius (> 40 * 1.15 = 46 mi) rejected
    far = RawListing(
        "craigslist", "5", "https://ex/5", "MSD 8582", price_usd=180.0, distance_miles=58.0, market_type="secondhand"
    )
    assert evaluate_listing(far, item, s).reason == "outside_radius"
