"""Shared browser concurrency guard, Patchright stealth launcher, and curl_cffi helpers."""

import asyncio
import importlib.util
import shutil
from typing import Any, Optional

BROWSER_LOCK = asyncio.Lock()


def is_browser_available() -> bool:
    """Return True if patchright or playwright is installed."""
    return (
        importlib.util.find_spec("patchright") is not None
        or importlib.util.find_spec("playwright") is not None
    )


def get_async_playwright() -> tuple[Any, bool]:
    """Return (async_playwright, using_patchright), preferring Patchright."""
    if importlib.util.find_spec("patchright") is not None:
        from patchright.async_api import async_playwright

        return async_playwright, True
    from playwright.async_api import async_playwright

    return async_playwright, False


async def launch_stealth_chromium(p: Any) -> Any:
    """Launch headless Chromium using the system Debian binary if present."""
    exe_path = shutil.which("chromium") or shutil.which("chromium-browser") or None
    return await p.chromium.launch(
        executable_path=exe_path,
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )


async def fetch_html_curl_cffi(url: str, timeout: float = 12.0) -> Optional[tuple[int, str]]:
    """Fetch a URL using Chrome TLS/HTTP2 impersonation via curl_cffi if available."""
    if importlib.util.find_spec("curl_cffi") is None:
        return None
    from curl_cffi.requests import AsyncSession

    async with AsyncSession(impersonate="chrome", timeout=timeout) as session:
        resp = await session.get(url, allow_redirects=True)
        return resp.status_code, resp.text
