"""Automatic product image discovery, verification, caching, and category fallback SVG."""

import asyncio
import html
import logging
import re
from typing import Any, List, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_SKIP_URL_TOKENS = (
    ".svg",
    ".gif",
    "logo",
    "favicon",
    "sprite",
    "placeholder",
    "avatar",
    "badge",
    "banner",
)

_ALLOWED_IMAGE_MIMES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/avif",
}

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def is_valid_candidate_url(
    url: str, width: Optional[Any] = None, height: Optional[Any] = None
) -> bool:
    """Filter candidate product image URLs for HTTPS, non-icon paths, and reasonable dimensions."""
    if not url or not isinstance(url, str):
        return False
    u = url.strip()
    if not u.startswith("https://"):
        return False
    lower = u.lower()
    if any(tok in lower for tok in _SKIP_URL_TOKENS):
        return False
    try:
        w = int(width) if width is not None and str(width).isdigit() else None
        h = int(height) if height is not None and str(height).isdigit() else None
    except (TypeError, ValueError):
        w = h = None
    if (w is not None and w < 200) or (h is not None and h < 200):
        return False
    return True


def _ddgs_images_sync(query: str, max_results: int = 6) -> List[str]:
    from ddgs import DDGS

    urls: List[str] = []
    with DDGS() as ddgs:
        for row in ddgs.images(query, max_results=max_results):
            img_url = row.get("image") or ""
            if is_valid_candidate_url(img_url, row.get("width"), row.get("height")):
                urls.append(img_url.strip())
    return urls


async def fetch_product_image(
    item_name: str,
    search_queries: Optional[List[str]] = None,
    retail_urls: Optional[List[str]] = None,
    preferred_url: Optional[str] = None,
) -> dict[str, Any]:
    """Discover, download, and cache a representative product image (<= 1 MB)."""
    candidates: List[str] = []
    seen: set[str] = set()

    def _add(url: Optional[str]) -> None:
        if url and url.startswith("https://") and url not in seen:
            seen.add(url)
            candidates.append(url)

    if preferred_url and is_valid_candidate_url(preferred_url):
        _add(preferred_url.strip())

    # Try clean item name first (stripping parenthetical notes), then primary search query.
    clean_name = re.sub(r"\([^)]*\)", " ", item_name or "").strip()
    clean_name = re.sub(r"\s+", " ", clean_name)
    image_queries: List[str] = []
    if clean_name:
        image_queries.append(clean_name[:80])
    if search_queries:
        first_q = str(search_queries[0]).strip()
        if first_q and first_q.lower() != clean_name.lower():
            image_queries.append(first_q[:80])

    if not preferred_url:
        for q in image_queries[:2]:
            try:
                found = await asyncio.to_thread(_ddgs_images_sync, q, 6)
                for u in found:
                    _add(u)
                if len(candidates) >= 6:
                    break
            except Exception as exc:
                logger.debug("DDGS image search failed for %r: %s", q, exc)

    for u in retail_urls or []:
        if is_valid_candidate_url(u):
            _add(u)

    if not candidates:
        return {"image_url": "", "image_data": None, "image_mime": ""}

    oversized_fallback_url = ""
    async with httpx.AsyncClient(
        timeout=10.0,
        follow_redirects=True,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
        },
    ) as client:
        for url in candidates[:8]:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype not in _ALLOWED_IMAGE_MIMES:
                    # Handle CDNs returning octet-stream for .jpg/.png URLs when path clearly ends in image ext.
                    ext_path = urlparse(url).path.lower()
                    if ctype == "application/octet-stream" and ext_path.endswith(
                        (".jpg", ".jpeg", ".png", ".webp", ".avif")
                    ):
                        ctype = "image/jpeg" if ext_path.endswith((".jpg", ".jpeg")) else f"image/{ext_path.rsplit('.', 1)[-1]}"
                    else:
                        continue
                data = resp.content
                if len(data) < 2048:
                    continue
                if len(data) <= 1_000_000:
                    return {
                        "image_url": url,
                        "image_data": data,
                        "image_mime": "image/jpeg" if ctype == "image/jpg" else ctype,
                    }
                if not oversized_fallback_url:
                    oversized_fallback_url = url
            except Exception as exc:
                logger.debug("Product image download failed for %s: %s", url, exc)
                continue

    if oversized_fallback_url:
        return {"image_url": oversized_fallback_url, "image_data": None, "image_mime": ""}
    return {"image_url": "", "image_data": None, "image_mime": ""}


def fallback_svg_for_category(category: str, name: str) -> bytes:
    """Return a clean inline-safe SVG emblem when no product photo is stored."""
    cat = (category or "general").strip().lower()
    palette = {
        "automotive": ("#0f172a", "#38bdf8", "AUTO"),
        "tools": ("#1c1917", "#f59e0b", "TOOL"),
        "electronics": ("#090d16", "#818cf8", "TECH"),
        "outdoor": ("#052e16", "#34d399", "GEAR"),
        "home": ("#1e1b4b", "#f472b6", "HOME"),
    }
    bg, accent, badge = palette.get(cat, ("#0f172a", "#38bdf8", "ITEM"))
    short_title = html.escape((name or "Argos Watch").strip()[:28])
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="320" viewBox="0 0 480 320">'
        f'<rect width="480" height="320" rx="16" fill="{bg}"/>'
        f'<circle cx="240" cy="132" r="52" fill="none" stroke="{accent}" stroke-width="4" opacity="0.85"/>'
        f'<text x="240" y="140" text-anchor="middle" font-family="system-ui,sans-serif" '
        f'font-size="20" font-weight="700" fill="{accent}">{badge}</text>'
        f'<text x="240" y="232" text-anchor="middle" font-family="system-ui,sans-serif" '
        f'font-size="18" font-weight="600" fill="#e2e8f0">{short_title}</text>'
        f'</svg>'
    )
    return svg.encode("utf-8")
