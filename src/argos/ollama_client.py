"""Lightweight async Ollama OpenAI-compatible client with thinking-tag stripping."""

import json
import logging
import re
from typing import Any, Optional

import httpx

from argos.config import Settings

logger = logging.getLogger(__name__)


def strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks emitted by reasoning models like qwen3.8."""
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def extract_json_object(raw: str) -> Optional[dict[str, Any]]:
    """Parse a JSON object from model output, tolerating thinking blocks and code fences."""
    cleaned = strip_thinking(raw)
    if not cleaned:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S).strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(cleaned[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


async def chat_json(
    system_prompt: str,
    user_prompt: str,
    settings: Settings,
    timeout_seconds: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Send a JSON-oriented chat completion request to Ollama and return parsed dict."""
    url = f"{settings.ollama_base_url.rstrip('/')}/chat/completions"
    timeout = float(timeout_seconds or settings.ollama_timeout_seconds)
    payload = {
        "model": settings.ollama_model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return extract_json_object(content)
    except Exception as exc:
        logger.warning("Ollama chat_json failed (%s): %s", settings.ollama_model, exc)
        return None
