"""Zero-cost web search & Exa batch page-fetch helpers with SerpApi Shopping support."""

import asyncio
import json
import logging
import re
from typing import Dict, List, Optional

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


def _parse_exa_mcp_text(raw_body: str) -> str:
    data_obj = None
    for line in raw_body.splitlines():
        if line.startswith("data: "):
            try:
                data_obj = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
    if data_obj is None:
        try:
            data_obj = json.loads(raw_body)
        except json.JSONDecodeError:
            return ""
    content_blocks = data_obj.get("result", {}).get("content", [])
    return "\n".join(
        blk.get("text", "") for blk in content_blocks if isinstance(blk, dict)
    )


async def _exa_mcp_call(tool_name: str, arguments: dict, timeout: float = 20.0) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "argos-deal-research/0.2",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(EXA_MCP_URL, json=payload, headers=headers)
        resp.raise_for_status()
        return _parse_exa_mcp_text(resp.text)


async def _exa_mcp_search(query: str, max_results: int) -> List[dict[str, str]]:
    combined = await _exa_mcp_call(
        "web_search_exa", {"query": query, "numResults": max_results}, timeout=18.0
    )
    if not combined:
        return []

    hits: List[dict[str, str]] = []
    blocks = re.split(r"\n(?=Title:\s)", combined)
    for blk in blocks:
        title_m = re.search(r"^Title:\s*(.+)$", blk, re.M)
        url_m = re.search(r"^URL:\s*(\S+)$", blk, re.M)
        text_m = re.search(r"^(?:Highlights|Text):\s*(.+)$", blk, re.M | re.S)
        if url_m:
            hits.append(
                {
                    "title": title_m.group(1).strip() if title_m else "",
                    "url": url_m.group(1).strip(),
                    "snippet": (text_m.group(1).strip()[:1600] if text_m else blk[:1200]),
                }
            )
    return hits[:max_results]


async def fetch_urls_exa(urls: List[str]) -> Dict[str, dict[str, str]]:
    """Batch-fetch indexed page contents via Exa MCP web_fetch_exa (zero-cost)."""
    clean_urls = [u for u in urls if u and u.startswith("http")][:12]
    if not clean_urls:
        return {}
    try:
        combined = await _exa_mcp_call("web_fetch_exa", {"urls": clean_urls}, timeout=25.0)
    except Exception as exc:
        logger.debug("Exa web_fetch_exa failed: %s", exc)
        return {}
    if not combined:
        return {}

    pages: Dict[str, dict[str, str]] = {}
    # Split multi-URL response on "# <Title>\nURL: ..." or "URL: ..." boundaries.
    chunks = re.split(r"\n(?=# [^\n]+\nURL:\s|\bURL:\s)", combined)
    for chunk in chunks:
        url_m = re.search(r"^URL:\s*(\S+)$", chunk, re.M)
        if not url_m:
            continue
        url = url_m.group(1).strip()
        title_m = re.search(r"^#\s+(.+)$", chunk, re.M)
        body = chunk[url_m.end() :].strip()
        pages[url] = {
            "title": title_m.group(1).strip() if title_m else "",
            "text": body[:2500],
        }
    return pages


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


async def search_google_shopping_serpapi(
    query: str, api_key: str, max_results: int = 10
) -> List[dict]:
    """Query SerpApi Google Shopping when SERPAPI_KEY is configured."""
    if not api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine": "google_shopping",
                    "q": query,
                    "api_key": api_key,
                    "num": max_results,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return data.get("shopping_results", [])[:max_results]
    except Exception as exc:
        logger.warning("SerpApi Google Shopping failed for %r: %s", query, exc)
        return []


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
