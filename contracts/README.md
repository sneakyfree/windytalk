# contracts/ — THE SEAMS
Versioned schemas, frozen first (Phase 0). `voice-session.v1.md` (client ⇄ engine websocket),
`hands.mcp.v1.json` (hands MCP tool schema), `telemetry.v1.json` (content-free event schema).
Frozen ≠ infallible: additive → v1.1 via PR; breaking → new v2 file + tell Grant. Never mutate silently.
