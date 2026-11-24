"""Tests for Telegram bot retry & error resilience."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError

from argos.config import Settings
from argos.db import get_session, init_db
from argos.models import WatchItem
from argos.telegram_bot import ArgosTelegramBot


@pytest.mark.asyncio
async def test_reply_safe_retries_on_network_error(monkeypatch):
    monkeypatch.setattr("argos.telegram_bot.asyncio.sleep", AsyncMock())
    bot = ArgosTelegramBot(Settings(telegram_bot_token="tok", telegram_chat_id=123))

    msg = SimpleNamespace()
    msg.reply_text = AsyncMock(
        side_effect=[NetworkError("ConnectError"), SimpleNamespace(message_id=42)]
    )

    msg_id = await bot._reply_safe(msg, "hello", attempts=3)
    assert msg_id == 42
    assert msg.reply_text.call_count == 2


@pytest.mark.asyncio
async def test_send_safe_retries_on_network_error(monkeypatch):
    monkeypatch.setattr("argos.telegram_bot.asyncio.sleep", AsyncMock())
    bot = ArgosTelegramBot(Settings(telegram_bot_token="tok", telegram_chat_id=123))
    send_mock = AsyncMock(
        side_effect=[NetworkError("ConnectError"), SimpleNamespace(message_id=99)]
    )
    bot.app = SimpleNamespace(bot=SimpleNamespace(send_message=send_mock))

    msg_id = await bot._send_safe("alert text", attempts=3)
    assert msg_id == 99
    assert send_mock.call_count == 2


@pytest.mark.asyncio
async def test_cmd_add_continues_when_initial_ack_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("argos.telegram_bot.asyncio.sleep", AsyncMock())
    db_path = tmp_path / "tg_add.db"
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        telegram_bot_token="tok",
        telegram_chat_id=123,
    )
    init_db(settings.database_url)

    with get_session() as session:
        item = WatchItem(
            name="Milwaukee M18 String Trimmer",
            raw_request="Milwaukee M18 String Trimmer",
            category="tools",
            status="pending_review",
        )
        session.add(item)
        session.flush()
        item_id = item.id

    induct_mock = AsyncMock(return_value=item_id)
    monkeypatch.setattr("argos.telegram_bot.induct_item", induct_mock)

    bot = ArgosTelegramBot(settings)
    # First 3 reply_text calls (all retries of initial ack) fail with NetworkError;
    # the 4th call (induction card) succeeds.
    reply_mock = AsyncMock(
        side_effect=[
            NetworkError("ConnectError"),
            NetworkError("ConnectError"),
            NetworkError("ConnectError"),
            SimpleNamespace(message_id=777),
        ]
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=123),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    context = SimpleNamespace(args=["Milwaukee", "M18", "String", "Trimmer"])

    await bot.cmd_add(update, context)

    induct_mock.assert_awaited_once()
    assert reply_mock.call_count == 4


def test_deal_keyboard_has_expired_and_false_positive_and_suppresses(tmp_path):
    from argos.models import Listing
    from argos.providers.ebay import EbayProvider
    from argos.providers.facebook_browser import _is_gated_page, parse_facebook_marketplace_cards
    from argos.scheduler import apply_listing_feedback
    from argos.scoring import EvaluationResult, should_notify
    from argos.telegram_bot import deal_quiesce_keyboard

    kb = deal_quiesce_keyboard(101)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "q:expired:101" in callbacks
    assert "q:false_positive:101" in callbacks

    db_path = tmp_path / "quiesce_fp.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    init_db(settings.database_url)

    with get_session() as session:
        item = WatchItem(name="Test Tool", raw_request="Test Tool", category="tools", status="active")
        session.add(item)
        session.flush()
        l1 = Listing(
            watch_item_id=item.id,
            provider="slickdeals",
            external_id="sd-1",
            url="https://slickdeals.net/f/1",
            title="Test Tool",
            price_usd=100.0,
            landed_cost_usd=100.0,
            state="notified",
        )
        l2 = Listing(
            watch_item_id=item.id,
            provider="universal_retail",
            external_id="ur-1",
            url="https://example.com/1",
            title="Test Tool Accessory",
            price_usd=50.0,
            landed_cost_usd=50.0,
            state="notified",
        )
        session.add_all([l1, l2])
        session.flush()

        res_exp = apply_listing_feedback(session, l1.id, "expired", settings)
        res_fp = apply_listing_feedback(session, l2.id, "false_positive", settings)
        assert res_exp["state"] == "quiesced_expired"
        assert res_fp["state"] == "quiesced_false_positive"

        ev = EvaluationResult(passed=True, landed_cost_usd=70.0, deal_score=90.0, reason="ok")
        assert should_notify(l1, 70.0, ev, settings)[0] is False
        assert should_notify(l2, 30.0, ev, settings)[0] is False

    # eBay sandbox auto-detection
    ebay = EbayProvider()
    sbx_settings = Settings(
        ebay_client_id="ReedFieg-argos-SBX-12345",
        ebay_client_secret="SBX-secret",
    )
    assert ebay._is_sandbox(sbx_settings) is True
    assert ebay._base_url(sbx_settings) == "https://api.sandbox.ebay.com"
    prod_settings = Settings(
        ebay_client_id="ReedFieg-argos-PRD-12345",
        ebay_client_secret="PRD-secret",
    )
    assert ebay._is_sandbox(prod_settings) is False
    assert ebay._base_url(prod_settings) == "https://api.ebay.com"

    # Facebook browser card parser & gate check
    cards = [
        {
            "href": "/marketplace/item/123456789012345/?ref=search",
            "text": "$195\nMilwaukee M18 FUEL Hedge Trimmer\nFrisco, TX",
            "image_url": "https://scontent.example.com/1.jpg",
        }
    ]
    parsed = parse_facebook_marketplace_cards(cards, default_city="Dallas")
    assert len(parsed) == 1
    assert parsed[0].external_id == "123456789012345"
    assert parsed[0].price_usd == 195.0
    assert parsed[0].location_text == "Frisco, TX"
    assert _is_gated_page("https://www.facebook.com/login/", "Log in to Facebook") is True
    assert _is_gated_page("https://www.facebook.com/marketplace/dallas/search/", "Milwaukee M18") is False
