# hands/ — the Surface socket · **Python**
`surface.py` = local HTTP + MCP control surface (windyword.py co-tenant pattern, ADR-058 D4).
`tiers.py` = §9 trust tiers (auto-allow / ask-first / always-confirm). `backends/` = linux (ported
from reference/hands.py), macos, windows, windyhand. Every action: human path AND agent path, shared state.
