"""Base classes and shared data models for pluggable deal providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional

from argos.config import Settings
from argos.models import WatchItem


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declarative metadata and cadence defaults for a provider plugin."""

    name: str
    display_name: str
    market_type: str  # "secondhand" | "retail" | "aggregator"
    supports_geo: bool = False
    supports_shipping: bool = False
    requires_api_key: bool = False
    estimated_cost_per_call_usd: float = 0.0
    normal_interval_minutes: int = 60
    hot_interval_minutes: int = 20


@dataclass
class RawListing:
    """Normalized candidate listing returned by any provider."""

    provider: str
    external_id: str
    url: str
    title: str
    price_usd: Optional[float]
    shipping_usd: float = 0.0
    condition: str = "unknown"
    description: str = ""
    location_text: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    distance_miles: Optional[float] = None
    image_url: str = ""
    market_type: str = "secondhand"


@dataclass
class ProviderResult:
    """Structured result of running a provider search for a watch item."""

    listings: List[RawListing] = field(default_factory=list)
    gaps: List[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    api_calls: int = 1


class BaseProvider(ABC):
    """Abstract interface that every marketplace or retail provider implements."""

    capabilities: ProviderCapabilities

    def is_configured(self, settings: Settings) -> bool:
        """Return True if this provider has all required credentials to run."""
        return not self.capabilities.requires_api_key

    @abstractmethod
    async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult:
        """Fetch matching candidate listings for the given watch item."""
        raise NotImplementedError
