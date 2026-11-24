"""Listing evaluation, bundle matching, deduplication, and re-alert logic."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from argos.config import Settings
from argos.models import Listing, WatchItem, utcnow
from argos.ollama_client import chat_json
from argos.providers.base import RawListing

logger = logging.getLogger(__name__)

_UNIVERSAL_NEGATIVE_TERMS = (
    "wtb",
    "want to buy",
    "wanted:",
    "looking for",
    "iso ",
    "box only",
    "empty box",
    "manual only",
    "for parts only",
    "parts only",
    "core only",
    "broken",
    "non-working",
    "not working",
    "exploded",
)

_PLACEHOLDER_PRICES = {123, 1234, 12345, 9999, 99999}


@dataclass
class EvaluationResult:
    """Outcome of scoring and filtering a candidate RawListing."""

    passed: bool
    landed_cost_usd: float
    deal_score: float
    reason: str
    matched_bundle: bool = False
    matched_bundle_names: List[str] = field(default_factory=list)
    effective_target_usd: Optional[float] = None


def _contains_term(haystack: str, term: str) -> bool:
    t = (term or "").strip().lower()
    if not t:
        return False
    if re.match(r"^[a-z0-9].*[a-z0-9]$", t):
        return bool(re.search(rf"\b{re.escape(t)}\b", haystack))
    return t in haystack


def _matches_required_tokens(haystack: str, required_tokens: list) -> bool:
    if not required_tokens:
        return True
    for entry in required_tokens:
        if isinstance(entry, list):
            alts = [str(a).strip().lower() for a in entry if str(a).strip()]
            if alts and not any(_contains_term(haystack, a) for a in alts):
                return False
        else:
            parts = [p.strip().lower() for p in str(entry).split("|") if p.strip()]
            if parts and not any(_contains_term(haystack, p) for p in parts):
                return False
    return True


def evaluate_listing(
    raw: RawListing,
    item: WatchItem,
    settings: Settings,
) -> EvaluationResult:
    """Deterministically evaluate scam/junk, keyword, geo, bundle, and price criteria."""
    if raw.price_usd is None:
        return EvaluationResult(False, 0.0, 0.0, "missing_price")

    price = float(raw.price_usd)
    shipping = float(raw.shipping_usd or 0.0)
    landed = round(price + shipping, 2)

    if price <= 1.0 or int(round(price)) in _PLACEHOLDER_PRICES:
        return EvaluationResult(False, landed, 0.0, "placeholder_or_scam_price")

    title = (raw.title or "").strip()
    if not title:
        return EvaluationResult(False, landed, 0.0, "missing_title")

    hay = f"{title} {raw.description or ''}".lower()

    for term in _UNIVERSAL_NEGATIVE_TERMS:
        if _contains_term(hay, term):
            return EvaluationResult(False, landed, 0.0, f"universal_negative:{term.strip()}")

    for neg in item.negative_keywords or []:
        if _contains_term(hay, str(neg)):
            return EvaluationResult(False, landed, 0.0, f"negative_keyword:{neg}")

    if not _matches_required_tokens(hay, item.required_tokens or []):
        return EvaluationResult(False, landed, 0.0, "missing_required_token")

    if raw.distance_miles is not None and item.radius_miles:
        if raw.distance_miles > item.radius_miles * 1.15:
            return EvaluationResult(False, landed, 0.0, "outside_radius")

    is_retail = raw.market_type == "retail" or raw.condition == "new"
    base_target = item.new_deal_target_usd if is_retail else item.used_deal_target_usd
    if base_target is None:
        base_target = item.used_deal_target_usd or item.new_deal_target_usd
    if base_target is None and item.retail_baseline_usd:
        base_target = round(item.retail_baseline_usd * (0.85 if is_retail else 0.70), 2)

    bundle_bonus = 0.0
    matched_bundle_names: List[str] = []
    for comp in item.bundle_components or []:
        if not isinstance(comp, dict):
            continue
        comp_tokens = comp.get("tokens") or []
        if comp_tokens and any(_contains_term(hay, str(t)) for t in comp_tokens):
            val = comp.get("new_value_usd") if is_retail else comp.get("used_value_usd")
            if val is None:
                val = comp.get("used_value_usd") or comp.get("new_value_usd") or 0.0
            bundle_bonus += float(val)
            matched_bundle_names.append(str(comp.get("name") or "Bundle item"))

    effective_target = round((base_target or 0.0) + bundle_bonus, 2) if base_target else None
    matched_bundle = bool(matched_bundle_names)

    if effective_target is not None and effective_target > 0:
        if landed > effective_target:
            ratio = (effective_target / landed) * 45.0
            return EvaluationResult(
                False,
                landed,
                round(min(49.0, max(0.0, ratio)), 1),
                f"above_target:${landed:.2f}>${effective_target:.2f}",
                matched_bundle=matched_bundle,
                matched_bundle_names=matched_bundle_names,
                effective_target_usd=effective_target,
            )
        pct_under = (effective_target - landed) / effective_target
        score = round(min(100.0, max(55.0, 55.0 + pct_under * 90.0)), 1)
    else:
        score = 60.0

    reason_parts = []
    if effective_target:
        pct = ((effective_target - landed) / effective_target) * 100.0
        reason_parts.append(f"${landed:.0f} ({pct:.0f}% under ${effective_target:.0f} target)")
    else:
        reason_parts.append(f"${landed:.0f} landed")
    if matched_bundle:
        reason_parts.append(f"includes {' + '.join(matched_bundle_names)}")
    if raw.distance_miles is not None:
        reason_parts.append(f"{raw.distance_miles:.1f} mi away")

    return EvaluationResult(
        True,
        landed,
        score,
        "; ".join(reason_parts),
        matched_bundle=matched_bundle,
        matched_bundle_names=matched_bundle_names,
        effective_target_usd=effective_target,
    )


async def vet_deal_with_ai(
    raw: RawListing,
    item: WatchItem,
    eval_res: EvaluationResult,
    settings: Settings,
) -> EvaluationResult:
    """Optionally sanity-check a candidate deal with Ollama (fast fallback on timeout)."""
    if not eval_res.passed:
        return eval_res
    sys_prompt = (
        "You verify marketplace/retail deal matches. Reply ONLY with JSON: "
        '{"is_match": true|false, "note": "concise 1-sentence reason"}. '
        "Reject accessories-only, manuals, wrong vehicle/model variants, or obvious bait listings."
    )
    user_prompt = (
        f"Wanted Item: {item.name} ({item.raw_request})\n"
        f"Negative keywords: {item.negative_keywords}\n"
        f"Candidate Title: {raw.title}\n"
        f"Source: {raw.provider} | Condition: {raw.condition} | Landed: ${eval_res.landed_cost_usd:.2f}\n"
        f"Description: {(raw.description or '')[:350]}"
    )
    res = await chat_json(sys_prompt, user_prompt, settings, timeout_seconds=15)
    if not res:
        return eval_res
    if res.get("is_match") is False:
        note = str(res.get("note") or "ai_rejected_mismatch")[:200]
        return EvaluationResult(
            False,
            eval_res.landed_cost_usd,
            eval_res.deal_score,
            f"ai_rejected:{note}",
            matched_bundle=eval_res.matched_bundle,
            matched_bundle_names=eval_res.matched_bundle_names,
            effective_target_usd=eval_res.effective_target_usd,
        )
    ai_note = str(res.get("note") or "").strip()
    if ai_note:
        return EvaluationResult(
            True,
            eval_res.landed_cost_usd,
            eval_res.deal_score,
            f"{eval_res.reason} — {ai_note[:160]}",
            matched_bundle=eval_res.matched_bundle,
            matched_bundle_names=eval_res.matched_bundle_names,
            effective_target_usd=eval_res.effective_target_usd,
        )
    return eval_res


def should_notify(
    existing: Optional[Listing],
    new_landed_cost: float,
    eval_res: EvaluationResult,
    settings: Settings,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Enforce zero-duplicate alerting, quiesce rules, and >=10% price drop re-alerts."""
    if not eval_res.passed:
        return False, eval_res.reason

    current_now = now or utcnow()
    if existing is None or existing.state in ("new", "ignored"):
        return True, "new_deal"

    if existing.state in (
        "quiesced_distance",
        "quiesced_wrong_item",
        "quiesced_expired",
        "quiesced_false_positive",
        "bought",
    ):
        return False, f"suppressed_{existing.state}"

    if existing.state == "quiesced_timing":
        if existing.snooze_until and current_now < existing.snooze_until:
            return False, "snoozed"
        return True, "snooze_expired"

    if existing.state in ("notified", "quiesced_price"):
        ref_price = (
            existing.last_notified_price_usd
            if existing.last_notified_price_usd is not None
            else existing.landed_cost_usd
        )
        threshold = ref_price * (1.0 - settings.price_drop_realert_pct)
        if new_landed_cost <= threshold:
            return True, "price_drop"
        return False, "already_notified_no_drop"

    return False, f"unhandled_state:{existing.state}"
