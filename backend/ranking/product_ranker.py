"""
ProductRanker
─────────────
Ranks a list of product dicts by conversion potential.

Scoring factors (higher = better):
  1. order_frequency   — how often product appears in completed/paid orders (0–5 pts)
  2. in_stock          — penalise out-of-stock products (-4 pts)
  3. has_price         — products with known price score higher (1 pt)
  4. stats_converted   — explicit conversion count from extra_metadata (0–3 pts)
  5. small_random_noise — breaks ties for variety (0–0.1 pts)

Falls back gracefully: if no order history exists, ranking is stock > price > title.
Input product dicts must have at minimum: 'id', 'title'.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.ranking")


def rank_products(
    products: List[Dict[str, Any]],
    db,
    tenant_id: int,
) -> List[Dict[str, Any]]:
    """
    Return products sorted by estimated conversion potential (descending).
    Never modifies the original list.
    """
    if not products:
        return products

    # ── Build order frequency map ──────────────────────────────────────────────
    order_freq: Dict[str, int] = {}
    try:
        # Import here to avoid circular imports at module load
        import os, sys
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
        from database.models import Order

        recent_orders = (
            db.query(Order)
            .filter(
                Order.tenant_id == tenant_id,
                Order.status.in_(["completed", "paid", "delivered", "confirmed"]),
            )
            .order_by(Order.id.desc())
            .limit(500)
            .all()
        )
        for order in recent_orders:
            for item in (order.line_items or []):
                pid = str(item.get("product_id") or item.get("id") or "")
                if pid:
                    order_freq[pid] = order_freq.get(pid, 0) + 1
    except Exception as exc:
        logger.warning(f"[Ranker] Could not build order frequency map: {exc}")

    # ── Score each product ─────────────────────────────────────────────────────
    def score(p: Dict[str, Any]) -> float:
        pid = str(p.get("id", ""))
        s = 0.0

        # Order frequency (0–5 pts)
        freq = order_freq.get(pid, 0)
        s += min(freq * 0.5, 5.0)

        # Stock (–4 pts if out of stock)
        if not p.get("in_stock", True):
            s -= 4.0

        # Has a known price (1 pt)
        if p.get("price"):
            s += 1.0

        # Explicit conversion stats from extra_metadata
        meta = p.get("extra_metadata") or {}
        if isinstance(meta, dict):
            conv = float(meta.get("stats_converted", 0) or 0)
            s += min(conv, 3.0)

        # Small noise for tie-breaking (0–0.1 pts)
        s += random.uniform(0, 0.1)

        return s

    return sorted(products, key=score, reverse=True)
