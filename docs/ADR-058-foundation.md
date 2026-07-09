# ADR-058 — Windy Talk: Platform 14, the universal voice layer

**Status:** ✅ Decided 2026-07-08. Vision blessed by Grant in a multi-session strategy summit; this ADR records the locked foundation so the build begins with the end in mind.
**Amends:** ADR-010 (Vision-Aligned Engineering Invariants) — promotes the platform count from 13 to **14** (§2) and extends the "voice as universal API" invariant (§5) from *Windy-internal* to *agent-agnostic*.
**Sibling:** ADR-044 (Windy Call — voice provider abstraction). Windy Call is telephony voice (Platform 9); Windy Talk is computer/desktop voice. Same VoiceProvider discipline, different transport.
**Owner:** Grant (vision); engineering (execution).

---

## §1 — What Windy Talk is

**Windy Talk is the universal voice layer for AI agents.** You download it, point it at any agent and any brain, and talk to it hands-free while it acts on your computer. Windy owns the voice and the hands; the agent and the compute are the user's — or Windy's.

The idea that makes it work: **decouple the three things normally welded together** — the *voice*, the *agent*, and the *compute*. Because they are separate, Windy Talk is a membrane that snaps onto anything. Four pluggable sockets:

1. **Brain (compute)** — any model, routed through **Windy Mind** (BYOM).
2. **Agent (who you talk to)** — any agent, paired through **Windy Connect**.
3. **Surface (the hands)** — desktop control, app overlays, or **Windy Hand** (cloud browser; the phone's hands).
4. **Reach (bolt-on)** — ships as an MCP server + CLI + REST/webhooks so agents and apps drive every knob.

**The moat is neutrality ("Switzerland").** Every frontier lab will ship a Jarvis inside its own walls. Windy Talk works with all of them, switches between a user's accounts, or runs local — the one thing the labs will not build because lock-in is their point.

**Consumer brand:** Windy Talk. **Repo:** `sneakyfree/windytalk`. **Apex:** windytalk.com. **Default agent for newcomers:** "Windy Buddy." **Wake word (v1, universal):** "Hey Windy."

## §2 — The vision extension (why this amends ADR-010)

ADR-010 §5 made voice the spine, but scoped to *Windy's own agent operating Windy's own platforms*. Windy Talk extends that: **voice for any agent (OpenClaw, Hermes, a raw model), powered by any brain, over any app, on any OS, as a standalone download.** This is a deliberate expansion of the vision, not a violation — and per ADR-010 §15/§16 it required Grant's blessing (given 2026-07-08) and this amendment before building.

Windy Talk is also the *productization of a promise ADR-010 already made*: §3–§4 spec the Windy Word Electron shell to become "the agent's body on the machine — always-on listening, native voice capture, local STT fallback." Windy Talk **is** that body — made agent-agnostic and standalone, and embeddable back into Windy Word as a gated feature.

## §3 — Foundation decisions (locked)

**D1 — Brain routes through Windy Mind.** Per ADR-010 §8, every LLM call goes through Windy Mind's OpenAI-compatible `/v1/chat/completions`. Mind *is* the BYOM/Switzerland layer; Windy Talk never calls a provider directly. The user's "use my own account / local / Chinese model" choice is satisfied by Mind, not by Windy Talk.

**D2 — Agents pair through Windy Connect.** Do not rebuild agent onboarding. Windy Connect (`windy-connect`) already pairs any runtime — OpenClaw, Hermes, Claude Code, generic — with Mail, Chat, Mind, and Eternitas. Windy Talk pairs the agent through Connect, then adds voice + hands on top.

**D3 — The Windy Fly connector is the IPC bridge.** A running Windy Fly agent exposes a JSON-RPC bridge (`windyfly/bridge/uds_server.py`; UDS on Mac/Linux, TCP on Windows). Method `agent.respond {message, session_id} → {response}`. The adapter is ~50 lines. **Gap to close:** the bridge is request/response, not streaming — add an `agent.respond_stream` variant so TTS can speak sentence-by-sentence. Universal fallback for non-Windy agents: OpenAI-compatible chat.

**D4 — The hands are a §6/§7 co-tenant API.** Windy Talk exposes its desktop hands + knobs as a local control surface (HTTP + MCP wrapper), modeled exactly on the proven `windyword.py` capability pattern (the agent reaching into a local app's `127.0.0.1` control surface). Every action has both a human path and an agent-callable path, sharing state.

**D5 — Trust tiers, not a denylist.** Desktop actions obey ADR-010 §9 (auto-allow / ask-first / always-confirm). A voice agent with hands on the machine is exactly where this scaffolding "is the difference between magic and disaster."

**D6 — Brokered short-lived Eternitas tokens.** Per §10, the client never holds a long-lived passport; it fetches ≤5-min scoped tokens per action class from the credential broker.

**D7 — Provider abstraction from day 1 (ADR-044 discipline).** Both the brain (Mind) and the **voice engine** (STT/TTS) are swappable providers. STT: AWS Transcribe Streaming (house standard, ADR-009, <1s first-word) *or* local Whisper (the 5090 path). Never hardcode one vendor.

**D8 — Wake word.** Train a custom on-device "Hey Windy" (openWakeWord); retire the stock "Hey Jarvis." Custom per-agent names ("Hey Matilda") ship later as a **paid** train-your-wake-word service (a Windy Drops offering). Mobile is push-to-talk (OS always-listening limits).

**D9 — Structure & shells.** One socket-aligned monorepo. The **engine stays Python, server-side** (on GPU). The **client is TS/Electron on desktop and native on mobile** (the Python desktop client is a Linux prototype, replaceable). The **protocol (Mind `/v1/chat` + MCP + the voice websocket) is the stable seam**. Honors §3–§4: one canonical UI codebase, three thin shells, **mobile-first**. On mobile the hands are **Windy Hand** (cloud), not local.

**D10 — Admin telemetry from day one.** Per ADR-WA-001, Windy Talk pushes content-free events to the `admin.windyword.ai` ingest (`platform=windy-talk`): session start/end → minutes used, turn counts, model (via Mind), cost (integer microcents), per user/agent id. **Fire-and-forget, inert unless configured, never content** (no transcripts — the ingest rejects content-ish fields). Coarse location (region from IP) is permitted but its granularity is a Grant privacy call (§15); keep it coarse.

## §4 — Ecosystem fit

Windy Talk is the **spine (§5)**, not a spoke — it operates every platform through the paired agent. Reuse, don't rebuild: **Windy Connect** (pairing), **Windy Mind** (brain), **Eternitas** (identity), **Windy Drops** (distribution + voice/capability marketplace). Named synergies to build toward: **Windy Code** (voice-driven coding overlay — the marquee surface), **Windy Clone** (speak in the user's digital-twin voice), **Windy Hand** (cloud/mobile hands), **Windy Call** (telephony sibling, shared VoiceProvider), and **Chat / Mail / Text / Cloud** (actions the agent takes when you talk).

## §5 — Monetization

**Sell software, not compute** (honors Grant's no-cloud-cost-liability principle). Subscription for the Windy Talk connection + gating; compute is the user's (their key via Mind, their local model, a $99 "we set it up" one-time, or Windy metered compute opt-in above cost). Gated on Windy Word / Windy Fly. Windy Talk is the **gateway drug** — the low-friction download that funnels into the whole ecosystem.

## §6 — The wedge (sequence)

1. **Now (5090):** the smoothest way to talk to your Windy agent, hands-free. Gated. Prove people pay to give their agent a voice.
2. Open the brain + agent sockets (Mind routing; pair via Connect).
3. Agent-native MCP + macOS/Windows hands.
4. Mobile + Windy Hand.
5. Windy's own competitive compute.

## §7 — How to apply

- **Building any Windy Talk brain call:** it goes through Windy Mind. If it hits a provider directly, refactor (§8).
- **Adding an agent:** write a Connect adapter, not a bespoke integration. For Windy Fly specifically, use the `agent.respond` bridge.
- **Adding a desktop action:** ship both the human path and the agent-callable API, tier-gated (§7, §9).
- **Touching auth:** brokered short-lived tokens only (§10).
- **Adding a vendor (STT/TTS/model):** it's a provider behind the abstraction, never a hardcode (ADR-044).
- **Shipping any surface:** emit content-free telemetry from the first commit (D10). Missing telemetry is a GAP, like a bug.
- **When this ADR is wrong:** update it before shipping the violating change; tell Grant (ADR-010 §16).

## §8 — Linked
- ADR-010 (invariants — amended here: §2 count, §5 scope)
- ADR-044 (Windy Call VoiceProvider abstraction — sibling discipline)
- ADR-009 (Transcribe streaming — house STT), ADR-022 (Windy Mind), ADR-WA-001 (admin telemetry)
- Product repo: `sneakyfree/windytalk` (+ `VISION.html` North Star)
- Dashboard tile: windy-pro PR #209
