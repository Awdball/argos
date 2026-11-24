"""Tests for provider registry, deduplication, quiesce transitions, and scheduler cadence."""

from datetime import timedelta

import pytest

from argos.config import Settings
from argos.db import get_session, init_db
from argos.inductor import upsert_provider_gap
from argos.models import Listing, ProviderGap, WatchItem, utcnow
from argos.providers.base import BaseProvider, ProviderCapabilities, ProviderResult, RawListing
from argos.providers.registry import get_provider, list_providers
from argos.scheduler import (
    apply_listing_feedback,
    effective_interval_minutes,
    run_provider_for_item,
)
from argos.scoring import should_notify, EvaluationResult


def test_registry_and_zero_cost_config_status():
    s = Settings(database_url="sqlite:///:memory:", enable_telegram=False)
    provs = {p["name"]: p for p in list_providers(s)}
    assert {"craigslist", "ebay", "woot", "slickdeals", "universal_retail", "facebook_apify"}.issubset(
        provs.keys()
    )
    assert provs["craigslist"]["configured"] is True
    assert provs["universal_retail"]["configured"] is True
    assert provs["ebay"]["configured"] is False
    assert provs["facebook_apify"]["configured"] is False

    cl = get_provider("craigslist")
    ur = get_provider("universal_retail")
    assert cl is not None and ur is not None
    assert effective_interval_minutes(cl, is_hot=False) == 60
    assert effective_interval_minutes(cl, is_hot=True) == 20
    assert effective_interval_minutes(ur, is_hot=False) <= 1440


def test_quiesce_transitions_and_price_drop_realert(tmp_path):
    db_url = f"sqlite:///{tmp_path}/test_dedup.db"
    s = Settings(database_url=db_url, enable_telegram=False, price_drop_realert_pct=0.10)
    init_db(db_url)

    with get_session() as session:
        item = WatchItem(
            name="Holley 750 4150",
            raw_request="Holley 750 4150",
            search_queries=["holley 750 4150"],
            required_tokens=["holley", "750"],
            used_deal_target_usd=350.0,
            radius_miles=40,
            status="active",
        )
        session.add(item)
        session.flush()
        listing = Listing(
            watch_item_id=item.id,
            provider="craigslist",
            external_id="9001",
            url="https://dallas.craigslist.org/9001.html",
            title="Holley 750 4150 Double Pumper",
            price_usd=300.0,
            shipping_usd=0.0,
            landed_cost_usd=300.0,
            state="notified",
            last_notified_price_usd=300.0,
        )
        session.add(listing)
        session.flush()
        lid = listing.id
        iid = item.id

    ev_same = EvaluationResult(True, 300.0, 70.0, "ok")
    with get_session() as session:
        l = session.get(Listing, lid)
        assert should_notify(l, 300.0, ev_same, s)[0] is False

        # Quiesce price -> still silent at $290 (<10% drop), re-alerts at $265 (>=10% drop)
        apply_listing_feedback(session, lid, "price", s)
        assert l.state == "quiesced_price"
        assert should_notify(l, 290.0, EvaluationResult(True, 290.0, 72.0, "ok"), s)[0] is False
        ok_drop, reason = should_notify(l, 265.0, EvaluationResult(True, 265.0, 80.0, "ok"), s)
        assert ok_drop is True and reason == "price_drop"

        # Timing snooze -> silent before expiry, eligible after expiry
        apply_listing_feedback(session, lid, "timing", s)
        assert l.state == "quiesced_timing"
        assert should_notify(l, 265.0, ev_same, s, now=utcnow() + timedelta(days=2))[0] is False
        assert should_notify(l, 265.0, ev_same, s, now=utcnow() + timedelta(days=15))[0] is True

        # Distance / Wrong item stay suppressed even on huge drop
        apply_listing_feedback(session, lid, "distance", s)
        assert should_notify(l, 100.0, EvaluationResult(True, 100.0, 99.0, "ok"), s)[0] is False

        # Bought archives the watch item
        res = apply_listing_feedback(session, lid, "bought", s)
        assert res is not None and res["watch_status"] == "archived"
        assert session.get(WatchItem, iid).status == "archived"


@pytest.mark.asyncio
async def test_scheduler_dedup_across_runs(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/test_sched.db"
    s = Settings(
        database_url=db_url,
        enable_telegram=False,
        ollama_base_url="http://127.0.0.1:1/v1",
        ollama_timeout_seconds=1,
    )
    init_db(db_url)

    class StubProvider(BaseProvider):
        capabilities = ProviderCapabilities(
            name="craigslist",
            display_name="Stub CL",
            market_type="secondhand",
            normal_interval_minutes=60,
            hot_interval_minutes=20,
        )

        async def search(self, watch_item, settings):
            return ProviderResult(
                listings=[
                    RawListing(
                        provider="craigslist",
                        external_id="777",
                        url="https://dallas.craigslist.org/777.html",
                        title="Holley 750 4150 Carb",
                        price_usd=290.0,
                        market_type="secondhand",
                    )
                ],
                gaps=[
                    {
                        "domain": "cjponyparts.com",
                        "retailer_name": "CJ Pony Parts",
                        "reason": "HTTP 403",
                        "sample_item": watch_item.name,
                    }
                ],
            )

    monkeypatch.setattr("argos.scheduler.get_provider", lambda name: StubProvider() if name == "craigslist" else None)

    with get_session() as session:
        item = WatchItem(
            name="Holley 750 4150",
            raw_request="Holley 750 4150",
            search_queries=["holley 750"],
            required_tokens=["holley", "750"],
            used_deal_target_usd=350.0,
            active_providers=["craigslist"],
            radius_miles=40,
            status="active",
        )
        session.add(item)
        session.flush()
        item_id = item.id

    alerts = []

    async def fake_alert(wid, lid, reason):
        alerts.append((wid, lid, reason))
        return 101

    r1 = await run_provider_for_item(item_id, "craigslist", s, alert_cb=fake_alert)
    assert r1["new_alerts"] == 1
    assert len(alerts) == 1

    # Second identical scan must emit 0 alerts (no duplicates).
    r2 = await run_provider_for_item(item_id, "craigslist", s, alert_cb=fake_alert)
    assert r2["new_alerts"] == 0
    assert len(alerts) == 1

    with get_session() as session:
        gap = session.query(ProviderGap).filter_by(domain="cjponyparts.com").one()
        assert gap.occurrences == 2


def test_reddit_classifieds_parser_and_gap_autoresolve(tmp_path):
    from argos.providers.reddit_classifieds import parse_reddit_search_json

    payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "id": "abc1",
                        "title": "[WTS] MSD 8582 Distributor Ford 302 $240 shipped",
                        "selftext": "Barely used.",
                        "permalink": "/r/Mustang/comments/abc1/msd_8582/",
                        "subreddit_name_prefixed": "r/Mustang",
                    }
                },
                {
                    "data": {
                        "id": "abc2",
                        "title": "[WTB] MSD 8582 Distributor $200",
                        "selftext": "",
                        "permalink": "/r/Mustang/comments/abc2/wtb/",
                        "subreddit_name_prefixed": "r/Mustang",
                    }
                },
            ]
        }
    }
    parsed = parse_reddit_search_json(payload)
    assert len(parsed) == 1
    assert parsed[0].external_id == "abc1"
    assert parsed[0].price_usd == 240.0
    assert parsed[0].location_text == "r/Mustang"
