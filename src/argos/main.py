"""Application entrypoint for Argos."""

import logging

import uvicorn

from argos.api import create_app
from argos.config import get_settings


def run() -> None:
    """Start the Argos FastAPI + Telegram + Scheduler service."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    app = create_app(settings=settings, enable_background=True)
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    run()
