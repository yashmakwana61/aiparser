"""LLM-assisted matching (tiebreaker/advisor, never first line).

Runs only on ambiguous/unresolved lines after all deterministic levels
fail, batched to a single gateway call per order. The model SELECTS from
bounded candidate sets — it can never invent products or customers, and any
id outside the offered sets is ignored. Transport failures degrade to the
existing button flow; nothing here raises into order processing.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from order_parser.ai.puter import puter_chat
from order_parser.ai.text_parser import TextParser
from order_parser.config import get_settings
from order_parser.core import metrics

logger = structlog.get_logger(__name__)

# Test seam: monkeypatched to avoid real sleeping in unit tests.
_sleep = time.sleep

MATCH_PROMPT = """You match order lines to an ERP catalog. Pick the best candidate or null.

Rules:
- Match ONLY by product identity (what the item IS), never by price, quantity or pack size alone.
- Different pack sizes of the same product family are different products: prefer the closest pack when stated.
- If no candidate is clearly the same product, use null. Never guess.
- "confidence" is 0-100: 95+ only when the match is essentially certain.

Return ONLY valid JSON, exactly:
{"picks": [{"index": 0, "product_id": 123, "confidence": 96, "reason": "..."}]}

Items:
{ITEMS}
"""


class AIMatcher:
    """Batched LLM product matching + customer re-ranking via the AI gateway."""

    def __init__(self, client: Any | None = None, settings=None):
        self._settings = settings or get_settings()
        self._client = client
        self.model = getattr(self._settings, "ai_match_model", "gpt-4.1-mini")
        self.max_attempts = max(1, int(getattr(self._settings, "ai_match_attempts", 2) or 0))
        self.max_candidates = max(1, int(getattr(self._settings, "ai_match_max_candidates", 8) or 0))
        self.max_items = 10

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "ai_match_enabled", False))

    def _transport(self, args: dict[str, Any]) -> str:
        if self._client is not None:
            return self._client(args)
        return puter_chat(args)

    def _call(self, prompt: str, max_tokens: int = 800) -> dict[str, Any]:
        args = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        last_exc: ValueError | None = None
        for attempt in range(1, self.max_attempts + 1):
            raw = self._transport(args)
            try:
                return TextParser._clean_json(raw)
            except ValueError as exc:
                last_exc = exc
                metrics.incr("ai_api_retries_total", outcome="invalid_json")
                logger.warning("ai_match.invalid_json_retry", model=self.model, attempt=attempt)
                if attempt < self.max_attempts:
                    _sleep(1.0 * attempt)
        raise ValueError(f"AI match response was not valid JSON after {self.max_attempts} attempts.") from last_exc

    def match_products(self, items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """Match ambiguous order lines. Returns {index: pick} for valid picks only.

        ``items``: [{"index", "raw_name", "quantity", "uom", "price",
        "candidates": [{"id", "name", "price", "uom"}]}]. Picks whose id was
        not offered are dropped (hallucination guard).
        """
        jobs = []
        offered: dict[int, set[int]] = {}
        for entry in items[: self.max_items]:
            try:
                index = int(entry.get("index"))
            except (TypeError, ValueError):
                continue
            candidates = [c for c in (entry.get("candidates") or []) if isinstance(c, dict)]
            candidates = candidates[: self.max_candidates]
            if not candidates:
                continue
            offered[index] = {int(c["id"]) for c in candidates
                              if c.get("id") is not None}
            jobs.append({
                "index": index,
                "raw_name": str(entry.get("raw_name") or ""),
                "quantity": entry.get("quantity"),
                "uom": entry.get("uom"),
                "price": entry.get("price"),
                "candidates": [
                    {"id": c.get("id"), "name": str(c.get("name") or ""),
                     "price": c.get("price"), "uom": c.get("uom")}
                    for c in candidates
                ],
            })
        if not jobs:
            return {}
        import json as _json

        prompt = MATCH_PROMPT.replace("{ITEMS}", _json.dumps(jobs, ensure_ascii=False)[:12000])
        try:
            response = self._call(prompt)
        except Exception:
            logger.exception("ai_match.products_failed", items=len(jobs))
            return {}
        picks: dict[int, dict[str, Any]] = {}
        for pick in response.get("picks") or []:
            if not isinstance(pick, dict):
                continue
            try:
                index = int(pick.get("index"))
                product_id = int(pick.get("product_id"))
            except (TypeError, ValueError):
                continue
            if index not in offered or product_id not in offered[index]:
                logger.warning("ai_match.unknown_pick_ignored", index=index,
                               product_id=product_id)
                continue
            try:
                confidence = float(pick.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            picks[index] = {"product_id": product_id,
                            "confidence": max(0.0, min(100.0, confidence)),
                            "reason": str(pick.get("reason") or "")[:200]}
        metrics.incr("ai_match_products_total", outcome="ok")
        logger.info("ai_match.products_completed", model=self.model,
                    items=len(jobs), picks=len(picks))
        return picks

    def rank_customers(self, detected_name: str,
                       candidates: list[dict[str, Any]]) -> list[int]:
        """Order candidate partner ids best-first. Empty on any failure."""
        pool = [c for c in candidates if isinstance(c, dict)][: self.max_candidates]
        if len(pool) < 2:
            return [int(c.get("partner_id")) for c in pool if c.get("partner_id") is not None]
        import json as _json

        prompt = (
            "Rank these ERP customers best-first for the buyer name "
            f"{detected_name!r}. Return ONLY valid JSON, exactly:\n"
            '{"ranking": [{"partner_id": 1, "confidence": 90, "reason": "..."}]}\n'
            "Candidates:\n"
            + _json.dumps(
                [{"partner_id": c.get("partner_id"), "name": str(c.get("name") or "")}
                 for c in pool], ensure_ascii=False)[:6000]
        )
        try:
            response = self._call(prompt, max_tokens=500)
        except Exception:
            logger.exception("ai_match.customers_failed")
            return []
        ordered: list[int] = []
        for entry in response.get("ranking") or []:
            if not isinstance(entry, dict):
                continue
            try:
                partner_id = int(entry.get("partner_id"))
            except (TypeError, ValueError):
                continue
            if partner_id not in {int(c.get("partner_id")) for c in pool
                                 if c.get("partner_id") is not None}:
                continue
            if partner_id not in ordered:
                ordered.append(partner_id)
        return ordered
