"""Shared browser concurrency guard so only one Chromium instance runs at a time."""

import asyncio

BROWSER_LOCK = asyncio.Lock()
