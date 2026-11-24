"""Application settings loaded from environment variables."""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for Argos."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///argos.db"

    telegram_bot_token: str = ""
    telegram_chat_id: Optional[int] = None

    ollama_base_url: str = "http://ollama:11434/v1"
    ollama_model: str = "qwen3.8:27b"
    ollama_timeout_seconds: int = 90

    home_postal: str = "76227"
    home_lat: float = 33.2259
    home_lon: float = -96.9997
    craigslist_site: str = "dallas"

    radius_base_miles: int = 20
    radius_mid_miles: int = 40
    radius_high_miles: int = 60

    price_drop_realert_pct: float = 0.10
    timing_snooze_days: int = 14

    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_sandbox: Optional[bool] = None
    woot_api_key: str = ""
    keepa_api_key: str = ""
    serpapi_key: str = ""

    enable_facebook_browser: bool = True
    enable_bestbuy_browser: bool = True
    facebook_city_slug: str = "dallas"
    facebook_storage_state_path: str = ""

    port: int = 8080
    enable_telegram: bool = True
    enable_scheduler: bool = True
    scheduler_tick_seconds: int = 60
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached runtime settings."""
    return Settings()
