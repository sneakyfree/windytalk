"""§9 trust tiers for the hands surface (ADR-010 §9 / ADR-058 D5).

Tiers come from the frozen contract (contracts/hands.mcp.v1.json) — never
hardcoded here — so the gate and the schema can't drift.

  auto_allow      — run immediately.
  ask_first       — confirm per invocation; the user MAY grant a session-scoped
                    "always allow" for that tool.
  always_confirm  — confirm every invocation; no session upgrade possible.

A confirmer is `confirm(tool, args, tier) -> bool`, wired by the surface's owner
to the client (a voice prompt or UI dialog). The default DENIES — a surface with
no confirmer refuses gated actions rather than silently running them. A denial
returns to the agent as ok:false / error:"denied" (never silence).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

AUTO_ALLOW = "auto_allow"
ASK_FIRST = "ask_first"
ALWAYS_CONFIRM = "always_confirm"

Confirmer = Callable[[str, dict, str], bool]

_CONTRACT = Path(__file__).resolve().parent.parent / "contracts" / "hands.mcp.v1.json"


def load_tiers(path: Path | None = None) -> dict[str, str]:
    doc = json.loads((path or _CONTRACT).read_text())
    return {t["name"]: t["tier"] for t in doc["tools"]}


def deny_all(tool: str, args: dict, tier: str) -> bool:
    return False


class TierPolicy:
    """Decides whether a tool invocation may proceed, honoring its tier."""

    def __init__(self, tiers: dict[str, str] | None = None,
                 confirmer: Confirmer | None = None) -> None:
        self.tiers = tiers or load_tiers()
        self.confirmer = confirmer or deny_all
        self._session_allow: set[str] = set()  # tools the user upgraded this session

    def tier_of(self, tool: str) -> str:
        # Unknown tools default to the strictest tier (fail safe).
        return self.tiers.get(tool, ALWAYS_CONFIRM)

    def allowed(self, tool: str, args: dict) -> bool:
        tier = self.tier_of(tool)
        if tier == AUTO_ALLOW:
            return True
        if tier == ASK_FIRST and tool in self._session_allow:
            return True
        ok = bool(self.confirmer(tool, args, tier))
        if ok and tier == ASK_FIRST and _wants_session_allow(args):
            self._session_allow.add(tool)
        return ok


def _wants_session_allow(args: dict) -> bool:
    """The confirmer may signal a session-scoped upgrade via a sentinel arg."""
    return bool(args.get("_always_allow"))
