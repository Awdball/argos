"""Offline tests for the AutoZone (autozone.com) search-index provider."""

import asyncio

from argos.config import Settings
from argos.models import WatchItem
from argos.providers import autozone as az


def _watch_item() -> WatchItem:
    """Same shape as live Argos item 1 (MSD 8582 Pro-Billet distributor)."""
    return WatchItem(
        id=1,
        name="MSD 8582 Ford 289/302 Billet Distributor | Iron Gear",
        raw_request="MSD 8582 distributor Ford 289 302",
        category="automotive",
        search_queries=["msd 8582 billet distributor ford", "msd 8582 ford 302"],
        required_tokens=["8582"],
        negative_keywords=["msd 8473", "msd 8580", "aluminum gear", "chevy", "351w"],
    )


def test_extract_price_dollar_word_formats():
    # AutoZone Exa capture style: spelled-out current price + "starting at" MSRP.
    text = (
        "The price of this item is: 440 dollars and 09 cents. "
        "Starting at 488 dollars and 99 cents"
    )
    assert az.extract_price(text) == 440.09  # lowest plausible wins
    assert az.extract_price("488 dollars and 99 cents") == 488.99
    assert az.extract_price("In stock. $571.95 MSRP $714.95") == 571.95
    assert az.extract_price("no prices here") is None
    assert az.extract_price("9 dollars and 99 cents") is None  # below PRICE_MIN


def test_token_group_and_negative_filters():
    item = _watch_item()
    assert az.matches_watch_item("msd performance distributor 8582 autozone", item) is True
    assert (
        az.matches_watch_item("msd billet distributor ford 289 302 8582", item) is True
    )
    assert (
        az.matches_watch_item("msd billet distributor 8473 ford 302", item) is False
    )  # negative token
    assert (
        az.matches_watch_item("msd 8582 chevy distributor", item) is False
    )  # negative keyword
    # word-presence fallback keeps multi-word tokens matching split titles
    assert az._token_group_matches("distributor pro 8 10-in-1", "pro 8") is True
    assert az._token_group_matches("msd 8582 iron gear billet", "iron gear") is True


def _az_hit(text_snippet: str) -> dict:
    return {
        "title": "MSD Performance Distributor 8582 | AutoZone",
        "url": "https://www.autozone.com/p/msd-distributor-8582/139655",
        "snippet": text_snippet,
    }


def test_search_emits_listing_for_matching_az_hit(monkeypatch):
    async def fake_exa(query, max_results):
        return [
            _az_hit(
                "Part Number: 8582. The price of this item is: 440 dollars and 09 cents. "
                "Starting at 488 dollars and 99 cents"
            )
        ]

    async def fake_parallel(query, max_results=10):
        return []

    monkeypatch.setattr(az, "_exa_mcp_search", fake_exa)
    monkeypatch.setattr(az, "_parallel_search", fake_parallel)

    result = asyncio.run(az.AutozoneProvider().search(_watch_item(), Settings()))
    assert result.error is None
    assert result.gaps == []
    assert len(result.listings) == 1
    l = result.listings[0]
    assert l.provider == "autozone"
    assert l.price_usd == 440.09
    assert l.market_type == "retail"
    assert l.condition == "new"
    assert l.external_id and len(l.external_id) == 16


def test_search_ignores_non_autozone_hits(monkeypatch):
    async def fake_exa(query, max_results):
        return [
            {
                "title": "MSD 8582 Billet Distributor Ford 289/302",
                "url": "https://www.msdignition.com/products/distributors/parts/8582",
                "snippet": "MSRP $571.95 $456.95 Save $115.00",
            }
        ]

    async def fake_parallel(query, max_results=10):
        return []

    monkeypatch.setattr(az, "_exa_mcp_search", fake_exa)
    monkeypatch.setattr(az, "_parallel_search", fake_parallel)

    result = asyncio.run(az.AutozoneProvider().search(_watch_item(), Settings()))
    assert result.listings == []


def test_search_excludes_wrong_part_even_when_priced(monkeypatch):
    async def fake_exa(query, max_results):
        return [
            {
                "title": "MSD Billet Distributor 8473 | AutoZone",
                "url": "https://www.autozone.com/p/msd-distributor-8473/139654",
                "snippet": "MSD Billet Distributor 8473. The price of this item is: 480 dollars",
            }
        ]

    async def fake_parallel(query, max_results=10):
        return []

    monkeypatch.setattr(az, "_exa_mcp_search", fake_exa)
    monkeypatch.setattr(az, "_parallel_search", fake_parallel)

    result = asyncio.run(az.AutozoneProvider().search(_watch_item(), Settings()))
    assert result.listings == []  # "8473" negative rejects it
    assert result.gaps == []  # avenue works; not an assortment match


def test_parallel_fallback_yields_listing(monkeypatch):
    async def fake_exa(query, max_results):
        return []

    async def fake_parallel(query, max_results=10):
        return [
            {
                "title": "MSD Performance Distributor 8582 | AutoZone",
                "url": "https://www.autozone.com/p/msd-distributor-8582/139655",
                "snippet": "The price of this item is: 497 dollars and 88 cents",
            }
        ]

    monkeypatch.setattr(az, "_exa_mcp_search", fake_exa)
    monkeypatch.setattr(az, "_parallel_search", fake_parallel)

    result = asyncio.run(az.AutozoneProvider().search(_watch_item(), Settings()))
    assert len(result.listings) == 1
    assert result.listings[0].price_usd == 497.88


def test_search_gap_when_both_backends_return_nothing(monkeypatch):
    async def fake_exa(query, max_results):
        return []

    async def fake_parallel(query, max_results=10):
        return []

    monkeypatch.setattr(az, "_exa_mcp_search", fake_exa)
    monkeypatch.setattr(az, "_parallel_search", fake_parallel)

    result = asyncio.run(az.AutozoneProvider().search(_watch_item(), Settings()))
    assert result.listings == []
    assert len(result.gaps) == 1
    assert result.gaps[0]["domain"] == "autozone.com"


def test_provider_registered(monkeypatch):
    from argos.providers import registry

    registry.get_provider("autozone")  # triggers load_all_providers
    assert "autozone" in registry.all_providers()
