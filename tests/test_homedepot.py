"""Offline tests for the Home Depot (homedepot.com) search-index provider."""

import asyncio

from argos.config import Settings
from argos.models import WatchItem
from argos.providers import homedepot as hd


def _watch_item() -> WatchItem:
    """Same shape as live Argos item 6 (Instant Pot Pro 8-Quart 10-in-1)."""
    return WatchItem(
        id=6,
        name="Instant Pot Pro 8-Quart 10-in-1",
        raw_request="Instant Pot Pro 8 quart",
        category="kitchen",
        search_queries=[
            "Instant Pot Pro 8 quart pressure cooker",
            "Instant Pot Pro 8qt 10-in-1 multi cooker",
        ],
        required_tokens=["instant pot", "pro 8"],
        negative_keywords=["duo", "6 qt", "mini", "ultra", "smart", "lite"],
    )


def test_extract_price_formats():
    assert hd.extract_price("Model# 113-0044-01 $ 199 . 99") == 199.99
    assert hd.extract_price("Price $149") == 149.0
    assert hd.extract_price("$1,059.99 each") == 1059.99
    assert hd.extract_price("no prices here") is None
    assert hd.extract_price("$2.99") is None  # below PRICE_MIN


def test_token_group_word_presence_fallback():
    text = "instant pot 8 qt matte black duo pro electric pressure cooker"
    # "pro 8" is not contiguous, but words "pro" and "8" are both present.
    assert hd._token_group_matches(text, "pro 8") is True
    assert hd._token_group_matches(text, "instant pot") is True
    assert hd._token_group_matches(text, "air fryer") is False


def test_matches_watch_item_duo_negative_excludes_hd_duo_line():
    item = _watch_item()
    text = "instant pot 8 qt matte black duo pro model# 113-0044-01"
    assert hd.matches_watch_item(text, item) is False  # "duo" negative

    text = "instant pot pro 8-quart 10-in-1 multi cooker"
    assert hd.matches_watch_item(text, item) is True


def _item6_text_ok() -> str:
    return "instant pot pro 8-quart 10-in-1 multi cooker"


def test_search_emits_listing_for_matching_hd_hit(monkeypatch):
    async def fake_exa(query, max_results):
        if "homedepot" in query or "pro 8" in query.lower():
            return [
                {
                    "title": "Instant Pot Pro 8-Quart 10-in-1 Multi Cooker",
                    "url": "https://www.homedepot.com/p/Instant-Pot-Pro-8-Quart-10-in-1-Multi-Cooker/331234567",
                    "snippet": "New. In stock online. $ 199 . 99 Buy online, pick up in a store",
                }
            ]
        return []

    async def fake_parallel(query, max_results=10):
        return []

    monkeypatch.setattr(hd, "_exa_mcp_search", fake_exa)
    monkeypatch.setattr(hd, "_parallel_search", fake_parallel)

    result = asyncio.run(hd.HomedepotProvider().search(_watch_item(), Settings()))
    assert result.error is None
    assert result.gaps == []
    assert len(result.listings) == 1
    l = result.listings[0]
    assert l.provider == "homedepot"
    assert l.price_usd == 199.99
    assert l.market_type == "retail"
    assert l.condition == "new"
    assert l.external_id and len(l.external_id) == 16


def test_search_excludes_duo_line_even_when_priced(monkeypatch):
    async def fake_exa(query, max_results):
        return [
            {
                "title": "Instant Pot 8 qt. Matte Black Duo Pro Electric Pressure Cooker",
                "url": "https://www.homedepot.com/p/Instant-Pot-8-Quart-Duo-Pro/318242621",
                "snippet": "Model# 113-0044-01 $ 199 . 99",
            }
        ]

    async def fake_parallel(query, max_results=10):
        return []

    monkeypatch.setattr(hd, "_exa_mcp_search", fake_exa)
    monkeypatch.setattr(hd, "_parallel_search", fake_parallel)

    result = asyncio.run(hd.HomedepotProvider().search(_watch_item(), Settings()))
    assert result.listings == []  # "duo" negative must reject
    assert result.gaps == []  # avenue works; not an assortment match


def test_search_gap_when_both_backends_return_nothing(monkeypatch):
    async def fake_exa(query, max_results):
        return []

    async def fake_parallel(query, max_results=10):
        return []

    monkeypatch.setattr(hd, "_exa_mcp_search", fake_exa)
    monkeypatch.setattr(hd, "_parallel_search", fake_parallel)

    result = asyncio.run(hd.HomedepotProvider().search(_watch_item(), Settings()))
    assert result.listings == []
    assert len(result.gaps) == 1
    assert result.gaps[0]["domain"] == "homedepot.com"
