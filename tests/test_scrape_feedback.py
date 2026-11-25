"""Tests for user-reported scrape_issue feedback opening/reopening ProviderGap."""

from fastapi.testclient import TestClient

from argos.api import create_app
from argos.config import Settings
from argos.db import get_session, init_db
from argos.models import Listing, ProviderGap, WatchItem
from argos.scheduler import apply_listing_feedback
from argos.telegram_bot import deal_quiesce_keyboard


def test_scrape_issue_opens_and_reopens_provider_gap_and_keyboard(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'scrape_feedback.db'}"
    settings = Settings(database_url=db_url, enable_telegram=False)
    init_db(db_url)

    # Verify Telegram inline keyboard exposes the Scrape Issue action
    kb = deal_quiesce_keyboard(55)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "q:scrape_issue:55" in callbacks

    with get_session() as session:
        # Pre-seed a previously resolved gap for homedepot.com
        existing_gap = ProviderGap(
            domain="homedepot.com",
            retailer_name="Home Depot",
            recommended_by="hermes",
            reason="Initial scraper built",
            sample_item="Old item",
            status="resolved",
            occurrences=2,
        )
        session.add(existing_gap)

        item = WatchItem(
            name="CyberPower 1500VA UPS",
            raw_request="CyberPower 1500VA UPS",
            new_deal_target_usd=180.0,
        )
        session.add(item)
        session.flush()

        listing = Listing(
            watch_item_id=item.id,
            provider="homedepot",
            external_id="hd-cyber-1",
            url="https://www.homedepot.com/p/CyberPower-1500VA/312345678",
            title="CyberPower 1500VA Battery Backup",
            price_usd=25.0,
            shipping_usd=0.0,
            landed_cost_usd=25.0,
            condition="new",
            location_text="Home Depot",
            state="notified",
        )
        session.add(listing)
        session.flush()
        listing_id = listing.id

        res = apply_listing_feedback(
            session,
            listing_id,
            "scrape_issue",
            settings,
            notes="Scraped $25, actual price on Home Depot is $274",
        )
        assert res is not None
        assert res["state"] == "quiesced_false_positive"
        assert res["provider_gap_domain"] == "homedepot.com"
        assert res["provider_gap_id"] == existing_gap.id

        session.refresh(existing_gap)
        assert existing_gap.status == "open"
        assert existing_gap.occurrences == 3
        assert "Scraped $25, actual price on Home Depot is $274" in existing_gap.reason
        assert existing_gap.sample_item == "CyberPower 1500VA UPS"

    # Also verify via FastAPI POST /api/listings/{id}/feedback
    app = create_app(settings)
    client = TestClient(app)
    with get_session() as session:
        item2 = WatchItem(
            name="Test Item 2",
            raw_request="Test Item 2",
            new_deal_target_usd=100.0,
        )
        session.add(item2)
        session.flush()
        listing2 = Listing(
            watch_item_id=item2.id,
            provider="autozone",
            external_id="az-1",
            url="https://www.autozone.com/p/test/123",
            title="Test Part",
            price_usd=10.0,
            shipping_usd=0.0,
            landed_cost_usd=10.0,
            condition="new",
            location_text="AutoZone",
            state="notified",
        )
        session.add(listing2)
        session.flush()
        l2_id = listing2.id

    resp = client.post(
        f"/api/listings/{l2_id}/feedback",
        json={"action": "scrape_issue", "notes": "Wrong price extracted"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "quiesced_false_positive"
    assert body["provider_gap_domain"] == "autozone.com"
    assert body["provider_gap_id"] is not None
