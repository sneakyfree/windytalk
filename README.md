# Windy Talk 🗣️

**The universal voice layer for AI agents — Platform 14 of the Windy ecosystem.**

Download it, point it at any agent and any brain, and talk to it hands-free while it
acts on your computer. Windy owns the voice and the hands; the agent and the compute
are the user's — or Windy's. Switzerland by design.

## Read first (in order)

1. `docs/DNA_STRAND_MASTER_PLAN.md` — the genome; the build follows it exactly
2. `docs/ADR-058-foundation.md` — the locked invariants
3. `docs/VISION.html` — the North Star
4. `docs/PROBE_RESULTS.md` — every external seam, probed live (Task 0.0)
5. `reference/` — the proven prototype (tag `prototype-v0`): port source, never build on it

## Body plan

| Dir | Socket | Language |
|---|---|---|
| `contracts/` | the frozen seams (voice-session, hands MCP, telemetry) | schema docs |
| `engine/` | voice brain-stem: STT/TTS providers, VAD, turn loop, ws server | Python (GPU, server-side) |
| `brains/` | Brain socket → Windy Mind `/v1/chat` | Python |
| `agents/` | Agent socket → windy-connect pairing + Windy Fly bridge | Python |
| `hands/` | Surface socket: HTTP+MCP control surface, §9 trust tiers, OS backends | Python |
| `auth/` | brokered ≤5-min Eternitas tokens | Python |
| `telemetry/` | content-free events → admin.windyword.ai | Python |
| `apps/` | desktop (Electron) / mobile (RN) / cli — the canonical client + face | TypeScript |
| `wakeword/` | "Hey Windy" training pipeline + model | Python + ONNX |
| `server/` | public endpoint, gating, Cloudflare tunnel | Python + config |

Prime directive: **the feel is the product** — the §0.1 latency table in the genome is a
numeric release gate. Contracts before code. One atomic task at a time; never proceed on red.
