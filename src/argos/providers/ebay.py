"""Official eBay Browse API provider (free developer tier; supports landed cost)."""

import base64
import logging
import time
from typing import Dict, Optional

import httpx

from argos.config import Settings
from argos.models import WatchItem
from argos.providers.base import BaseProvider, ProviderCapabilities, ProviderResult, RawListing
from argos.providers.registry import register_provider

logger = logging.getLogger(__name__)


@register_provider
class EbayProvider(BaseProvider):
    """Searches eBay Browse API with item price + shipping landed cost."""

    capabilities = ProviderCapabilities(
        name="ebay",
        display_name="eBay Browse API",
        market_type="secondhand",
        supports_geo=False,
        supports_shipping=True,
        requires_api_key=True,
        estimated_cost_per_call_usd=0.0,
        normal_interval_minutes=60,
        hot_interval_minutes=20,
    )

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def is_configured(self, settings: Settings) -> bool:
        return bool(settings.ebay_client_id.strip() and settings.ebay_client_secret.strip())

    async def _get_token(self, client: httpx.AsyncClient, settings: Settings) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        creds = f"{settings.ebay_client_id}:{settings.ebay_client_secret}".encode("utf-8")
        auth = base64.b64encode(creds).decode("ascii")
        resp = await client.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = now + int(payload.get("expires_in", 7200))
        return self._token

    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        if not self.is_configured(settings):
            return ProviderResult(listings=[], api_calls=0)

        queries = (watch_item.search_queries or [watch_item.name])[:3]
        seen: Dict[str, RawListing] = {}
        calls = 0

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                token = await self._get_token(client, settings)
            except Exception as exc:
                return ProviderResult(listings=[], error=f"eBay OAuth failed: {exc}", api_calls=1)

            for q in queries:
                calls += 1
                try:
                    resp = await client.get(
                        "https://api.ebay.com/buy/browse/v1/item_summary/search",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                        },
                        params={
                            "q": q,
                            "limit": 25,
                            "sort": "newlyListed",
                            "filter": "buyingOptions:{FIXED_PRICE|AUCTION}",
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    logger.warning("eBay search %r failed: %s", q, exc)
                    continue

                for item in data.get("itemSummaries", []):
                    ext_id = str(item.get("itemId") or "")
                    if not ext_id or ext_id in seen:
                        continue
                    price_obj = item.get("price") or {}
                    try:
                        price = float(price_obj["value"])
                    except (KeyError, TypeError, ValueError):
                        continue

                    shipping = 0.0
                    ship_opts = item.get("shippingOptions") or []
                    if ship_opts:
                        ship_cost = (ship_opts[0].get("shippingCost") or {}).get("value")
                        try:
                            shipping = float(ship_cost) if ship_cost is not None else 0.0
                        except (TypeError, ValueError):
                            shipping = 0.0

                    loc = item.get("itemLocation") or {}
                    loc_text = ", ".join(
                        p for p in [loc.get("city"), loc.get("stateOrProvince"), loc.get("country")] if p
                    )
                    cond = (item.get("condition") or "used").lower()
                    market_type = "new" if "new" in cond and "open" not in cond else "secondhand"
                    img = (item.get("thumbnailImages") or [{}])[0].get("imageUrl") or (
                        (item.get("image") or {}).get("imageUrl") or ""
                    )

                    seen[ext_id] = RawListing(
                        provider="ebay",
                        external_id=ext_id,
                        url=item.get("itemWebUrl") or "",
                        title=item.get("title") or "",
                        price_usd=price,
                        shipping_usd=shipping,
                        condition=cond,
                        description=item.get("shortDescription") or "",
                        location_text=loc_text,
                        image_url=img,
                        market_type=market_type,
                    )

        return ProviderResult(listings=list(seen.values()), api_calls=calls)
