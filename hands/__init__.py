"""The Surface socket (ADR-058 D4/D5). Local desktop hands, tier-gated, co-tenant."""
from __future__ import annotations

from .surface import HandsSurface
from .tiers import ALWAYS_CONFIRM, ASK_FIRST, AUTO_ALLOW, TierPolicy

__all__ = ["HandsSurface", "TierPolicy", "AUTO_ALLOW", "ASK_FIRST", "ALWAYS_CONFIRM"]
