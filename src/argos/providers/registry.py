"""Provider plugin registry."""

from typing import Dict, List, Optional, Type

from argos.config import Settings
from argos.providers.base import BaseProvider

_REGISTRY: Dict[str, BaseProvider] = {}
_LOADED = False


def register_provider(cls: Type[BaseProvider]) -> Type[BaseProvider]:
    """Class decorator registering a provider instance by capabilities.name."""
    instance = cls()
    _REGISTRY[instance.capabilities.name] = instance
    return cls


def load_all_providers() -> None:
    """Import all built-in provider modules once so decorators execute."""
    global _LOADED
    if _LOADED:
        return
    from argos.providers import (  # noqa: F401
        craigslist,
        ebay,
        facebook_apify,
        facebook_browser,
        reddit_classifieds,
        slickdeals,
        universal_retail,
        woot,
    )

    _LOADED = True


def get_provider(name: str) -> Optional[BaseProvider]:
    """Return a registered provider by slug name."""
    load_all_providers()
    return _REGISTRY.get(name)


def all_providers() -> Dict[str, BaseProvider]:
    """Return all registered provider instances."""
    load_all_providers()
    return dict(_REGISTRY)


def default_active_providers() -> List[str]:
    """Return all registered provider names for new watch items."""
    load_all_providers()
    return list(_REGISTRY.keys())


def list_providers(settings: Settings) -> List[dict]:
    """Return serializable metadata and configuration status for all providers."""
    load_all_providers()
    result = []
    for name, prov in _REGISTRY.items():
        cap = prov.capabilities
        result.append(
            {
                "name": name,
                "display_name": cap.display_name,
                "market_type": cap.market_type,
                "configured": prov.is_configured(settings),
                "supports_geo": cap.supports_geo,
                "supports_shipping": cap.supports_shipping,
                "requires_api_key": cap.requires_api_key,
                "estimated_cost_per_call_usd": cap.estimated_cost_per_call_usd,
                "normal_interval_minutes": cap.normal_interval_minutes,
                "hot_interval_minutes": cap.hot_interval_minutes,
            }
        )
    return result
