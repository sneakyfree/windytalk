"""Hands tool schemas for the brain (§11.3).

Loads the frozen hands.mcp.v1 contract and converts each tool into the
OpenAI-compatible function format that Mind's /v1/chat expects, so the brain
can actually drive the desktop. The engine only OFFERS the tools; execution
stays in the client (tool_call events → the co-tenant hands surface), so a
client without hands returns errors the brain can speak about honestly.

WINDYTALK_NO_HANDS_TOOLS=1 disables the offer (voice-only deployments).
"""
import json
import os
from pathlib import Path

_CONTRACT = Path(__file__).resolve().parent.parent / "contracts" / "hands.mcp.v1.json"


def load_hands_tools(contract_path: str | os.PathLike | None = None) -> list[dict]:
    """The contract's tools as OpenAI-format function specs (order preserved)."""
    with open(contract_path or _CONTRACT, encoding="utf-8") as f:
        contract = json.load(f)
    return [{"type": "function",
             "function": {"name": t["name"],
                          "description": t["description"],
                          "parameters": t["inputSchema"]}}
            for t in contract["tools"]]


def hands_tools_enabled() -> bool:
    return os.environ.get("WINDYTALK_NO_HANDS_TOOLS") != "1"
