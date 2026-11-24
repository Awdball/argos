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
