"""Zero-cost web search helper using ddgs with Exa public MCP and SerpApi fallbacks."""

import asyncio
import json
import logging
import re
from typing import List

import httpx

from argos.config import Settings

logger = logging.getLogger(__name__)

EXA_MCP_URL = "https://mcp.exa.ai/mcp"


def _ddgs_sync(query: str, max_results: int) -> List[dict[str, str]]:
    from ddgs import DDGS

    hits: List[dict[str, str]] = []
    with DDGS() as ddgs:
        for row in ddgs.text(query, max_results=max_results):
            url = row.get("href") or row.get("url") or ""
            title = row.get("title") or ""
            snippet = row.get("body") or row.get("snippet") or ""
            if url:
                hits.append({"url": url, "title": title, "snippet": snippet})
    return hits


async def _exa_mcp_search(query: str, max_results: int) -> List[dict[str, str]]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search_exa",
            "arguments": {"query": query, "numResults": max_results},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "argos-deal-research/0.1",
    }
    async with httpx.AsyncClient(timeout=18.0) as client:
        resp = await client.post(EXA_MCP_URL, json=payload, headers=headers)
        resp.raise_for_status()
        text = resp.text

    # Parse SSE or plain JSON payload returned by Exa MCP.
    data_obj = None
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                data_obj = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
    if data_obj is None:
        try:
            data_obj = json.loads(text)
        except json.JSONDecodeError:
            return []

    content_blocks = data_obj.get("result", {}).get("content", [])
    combined = "\n".join(
        blk.get("text", "") for blk in content_blocks if isinstance(blk, dict)
    )
    if not combined:
        return []

    # Exa MCP returns structured text blocks with Title/URL/Text sections.
    hits: List[dict[str, str]] = []
    blocks = re.split(r"\n(?=Title:\s)", combined)
    for blk in blocks:
        title_m = re.search(r"^Title:\s*(.+)$", blk, re.M)
        url_m = re.search(r"^URL:\s*(\S+)$", blk, re.M)
        text_m = re.search(r"^Text:\s*(.+)$", blk, re.M | re.S)
        if url_m:
            hits.append(
                {
                    "title": title_m.group(1).strip() if title_m else "",
                    "url": url_m.group(1).strip(),
                    "snippet": (text_m.group(1).strip()[:400] if text_m else blk[:300]),
                }
            )
    return hits[:max_results]


async def _serpapi_search(query: str, max_results: int, api_key: str) -> List[dict[str, str]]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://serpapi.com/search.json",
            params={"q": query, "api_key": api_key, "num": max_results},
        )
        resp.raise_for_status()
        data = resp.json()
    hits: List[dict[str, str]] = []
    for item in data.get("organic_results", [])[:max_results]:
        if item.get("link"):
            hits.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                }
            )
    return hits


async def search_web(
    query: str, settings: Settings, max_results: int = 8
) -> List[dict[str, str]]:
    """Run web search preferring SerpApi (if keyed), else Exa MCP / ddgs."""
    if settings.serpapi_key:
        try:
            hits = await _serpapi_search(query, max_results, settings.serpapi_key)
            if hits:
                return hits
        except Exception as exc:
            logger.warning("SerpApi search failed for %r: %s", query, exc)

    try:
        hits = await _exa_mcp_search(query, max_results)
        if hits:
            return hits
    except Exception as exc:
        logger.debug("Exa MCP search failed for %r: %s", query, exc)

    try:
        return await asyncio.to_thread(_ddgs_sync, query, max_results)
    except Exception as exc:
        logger.warning("DDGS search failed for %r: %s", query, exc)
        return []
