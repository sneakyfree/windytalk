# Windy Talk — DNA Strand Master Plan

> The genome. The genius is in the plan, not the ribosome. Build exactly what this
> says, one codon at a time, verifying each before the next. If the plan is right,
> the build is mechanical.
>
> **Read before touching code, in this order:** this file → `docs/ADR-058-foundation.md`
> (locked invariants) → `VISION.html` (North Star) → the reference prototype (see §3
> — you PORT proven blocks from it; you never build the pyramid on it) →
> the two memory files. **Read the memories by ABSOLUTE path** — they will NOT
> auto-load unless the session was launched from `/home/grantwhitmer` (Claude Code
> memory is keyed to the launch directory):
> `/home/grantwhitmer/.claude/projects/-home-grantwhitmer/memory/project_windytalk_vision.md`
> `/home/grantwhitmer/.claude/projects/-home-grantwhitmer/memory/project_windy_jarvis_build.md`

---

## 0 · Prime directives (never violated)

1. **The feel is the product.** A voice conversation that lags or can't be interrupted is worthless no matter how many sockets it has. Latency and barge-in quality outrank every feature. The felt-latency budget is §0.1 — numeric, measured, a release gate.
2. **Contracts before code.** Every seam (the voice websocket, the brain call, the hands MCP, the telemetry event) is a *versioned schema written and frozen first*. Implementations conform to the schema; the schema is the DNA. **Frozen ≠ infallible:** additive changes bump the minor (v1 → v1.1) in place via PR; breaking changes require a new v2 file *and telling Grant*. Never silently mutate a frozen contract.
3. **Atomic tasks, verified.** Do one task. Prove its acceptance criteria (a command, a test, an observed behavior). Never proceed on red. Never batch.
4. **Port correct code; rebuild wrong assumptions.** "Solid foundation" means don't inherit wrong assumptions — it does NOT mean rewrite working code. See the ledger (§3).
5. **Build to ADR-058, not to the prototype.** When the prototype and ADR-058 disagree, ADR-058 wins, always.

### 0.1 · The felt-latency budget (frozen — release gates, measured not vibed)

Measured at **p90 over ≥20 real spoken turns** on the reference rig (5090 engine, client over the tunnel), logged by the engine from client-stamped timestamps and carried in telemetry (`latency_ms`). Every phase DoD that says "felt latency ≤ target" means THIS table:

| Metric | Budget |
|---|---|
| End of speech → first reply audio audible | ≤ 1.2 s |
| Barge-in: user starts speaking → TTS fully silent | ≤ 150 ms |
| Wake word → visible listening state | ≤ 300 ms |
| Mic frame sent → engine receipt (transport) | ≤ 60 ms |

Changing a number = a PR editing this table, never a silent reinterpretation.

## 1 · Genome — the invariants (from ADR-058, non-negotiable)

- **Brain → Windy Mind.** Every LLM turn goes through **`POST api.windymind.ai/v1/chat`** (OpenAI-compatible request/response *shape*; SSE streaming via `stream:true`). Never a provider SDK directly. Mind *is* the BYOM/Switzerland layer. ✅ **Verified live 2026-07-08:** the canonical route is `/v1/chat`; the alias `/v1/chat/completions` is **deployed** (windy-mind PR #56, live at commit 62404de) so off-the-shelf OpenAI SDKs point `base_url` at `api.windymind.ai/v1` and work unchanged. Either path is fine; treat `/v1/chat` as canonical in Windy Talk's own code.
- **Agents → Windy Connect.** Pair any runtime via `windy-connect`; don't rebuild onboarding. It is **not on this machine** — get it via `pip install windy-connect` (PyPI, 0.3.1 as of 2026-07-08) or clone `sneakyfree/windy-connect`. Windy Fly's own connector is its JSON-RPC bridge (`src/windyfly/bridge/uds_server.py` in `sneakyfree/windy-agent`, method `agent.respond`).
- **Hands = §6/§7 co-tenant API.** Local control surface (HTTP + MCP), modeled on `windyword.py`. Every action has a human path AND an agent-callable path sharing state, gated by **§9 trust tiers** (auto-allow / ask-first / always-confirm).
- **Auth = §10 brokered tokens.** ≤5-min scoped Eternitas tokens per action class. Never a long-lived passport in the client.
- **Provider abstraction from day 1 (ADR-044).** Both the brain (Mind) and the voice engine (STT + TTS) are swappable providers behind an ABC. STT: AWS Transcribe Streaming (house std) *or* local faster-whisper. TTS: Kokoro *or* cloud.
- **Telemetry from the first commit (ADR-WA-001).** Content-free events to `admin.windyword.ai`, `platform=windy-talk`. Fire-and-forget, inert-unless-configured, NEVER content. Missing telemetry is a bug.
- **One canonical UI codebase, three thin shells (§3/§4), mobile-first.** Engine is **Python, server-side**. Client is **TypeScript** (Electron desktop, RN/Capacitor mobile, browser SPA) — never Python. On mobile the hands are **Windy Hand** (cloud), not local.

## 2 · Body plan — the target monorepo

```
windytalk/
  contracts/                 # THE SEAMS — versioned schemas, frozen first (§Phase 0)
    voice-session.v1.md      # client ⇄ engine websocket protocol
    hands.mcp.v1.json        # the hands MCP tool schema
    telemetry.v1.json        # the content-free event schema
  engine/                    # Python, server-side (GPU). The voice brain-stem.
    providers/
      stt/{base,whisper,transcribe}.py     # VoiceProvider(STT) — ADR-044
      tts/{base,kokoro,cloud}.py           # VoiceProvider(TTS)
    vad.py  session.py  server.py          # webrtcvad, the turn loop, the ws server
  brains/                    # the Brain socket
    mind.py                  # → api.windymind.ai/v1/chat (the ONE real path)
    openai_compat.py         # fallback for non-Mind endpoints (non-Windy agents)
  agents/                    # the Agent socket
    connect.py               # pair via windy-connect
    windyfly.py              # the agent.respond(+_stream) bridge adapter
  hands/                     # the Surface socket (control surface + backends)
    surface.py               # local HTTP + MCP control surface (windyword.py pattern)
    tiers.py                 # §9 trust tiers
    backends/{linux,macos,windows,windyhand}.py
  auth/eternitas.py          # §10 brokered short-lived tokens
  telemetry/emit.py          # content-free, fire-and-forget (ADR-WA-001)
  apps/
    desktop/                 # Electron (TS) — the canonical client + face; the "agent's body"
    mobile/                  # RN/Capacitor (later)
    cli/                     # headless (TS)
  wakeword/                  # "Hey Windy" training pipeline + model
  server/                    # relay/host: endpoint, gating, Cloudflare tunnel
  docs/                      # this file, ADR-058, VISION.html
```

**The three seams, stated once (freeze these in Phase 0):**
- **voice-session.v1** — client streams pcm16 mic frames (**20 ms frames @ 16 kHz in; engine audio out pcm16 @ 24 kHz**) + control (mic on/off, barge-in); engine streams audio out + events (`heard`, `say`, `state`, `tool_call`, `level`). Reconnect + session_id. **Client capture requirements live IN the contract:** echo cancellation ON (Chromium/WebRTC AEC via `getUserMedia` constraints — the mic hears the speakers; without AEC the engine barges in on its own TTS, and 1.5's "barge-in works" passes in headphones then fails in the real world), AudioWorklet-grade capture (never MediaRecorder chunking — it alone can eat 100–200 ms of the §0.1 budget), and **client-stamped timestamps** on frames/events so the latency budget is measured, not vibed. Sentence-chunking policy for streaming TTS (who buffers tokens → sentences: the engine) is specified here too.
- **hands.mcp.v1** — the tool list the agent sees: `open_app`, `type_text`, `press_keys`, `click_element`, `read_screen`, `web_search`, `run_shell`, … each with a §9 tier tag.
- **telemetry.v1** — `{platform:"windy-talk", event, session_id, user_id, agent_id, ts, dur_ms?, model?, cost_microcents?, latency_ms?, region?}` — ids/counts/costs/latencies only (all numeric — content-free), schema rejects content.

## 3 · Port vs Rebuild ledger (the prototype → the real build)

| Prototype asset | Verdict | Why / how |
|---|---|---|
| Engine loop (faster-whisper + Kokoro + VAD + ws) `server/veron_server.py` | **PORT, refactor** | The right shape. Split into `engine/` with the STT/TTS provider ABCs; route brain to Mind. ~80% reused. |
| Hands logic (AT-SPI / ydotool / xdotool, X11+Wayland) `hands.py` | **PORT verbatim, re-expose** | Correct, hard-won. Move logic under `hands/backends/linux.py`; expose via the control surface, not direct calls. |
| Face design (canvas states, lip-sync) `desktop/index.html` | **PORT design, rewire** | Visual design stays; becomes the real client's face wired to voice-session.v1, not a Python subprocess. |
| Conversation flow (barge-in, wake gating, tool-call-back) | **PORT as behavior spec** | The proven flow is the acceptance spec for the new client/engine. |
| Provider abstraction `providers/*` | **PORT the pattern** | Right idea; re-home to `brains/` + `engine/providers/`; concretes change (Mind, Transcribe). |
| Python desktop client `jarvis.py`, `audio.py`, `ui_bridge.py`, `wake.py` | **REBUILD in TS** | §D9: client is TypeScript. Logic transfers; language doesn't. |
| License / kill-switch `licenses.json`, `admin.py`, online.json | **DISCARD → Eternitas + Windy Word** | Stopgap. Replaced by §10 tokens + Windy Word accounts + real telemetry. |
| Direct-Ollama brain | **DISCARD → Windy Mind** | §D1. Never a provider directly. |
| Denylist safety | **DISCARD → §9 trust tiers** | Replaced by the tiered model. |
| "Hey Jarvis" stock model | **DISCARD → train "Hey Windy"** | §D8. |
| Cloudflare tunnel + installer + OC3 learnings | **PORT** | The endpoint + packaging + cross-distro findings still apply. |

## 4 · Phase 0 — Foundation (ATOMIC — do these first, in order)

Each task: **do → verify (the stated check) → commit → next.** Feature branches + PR (ecosystem branching policy). **Merge policy:** self-merge when CI is green and the task's verify passed — do not stall waiting for human review; Grant reads PRs after the fact. "Never proceed on red" refers to checks, not to waiting on a reviewer.

- **0.0** **Dependency reality-check** — verify every external seam LIVE before freezing anything against it. Already verified 2026-07-08 (unauthenticated probes): Mind is up, chat route is `POST /v1/chat` (`/v1/chat/completions` 404s), `/v1/models` 200 and lists providers; telemetry ingest `admin.windyword.ai` up; `windy-connect` exists (PyPI 0.3.1 + `sneakyfree/windy-connect`) but is NOT installed on this machine; Windy Fly bridge is at `src/windyfly/bridge/uds_server.py`. **You complete the authed half:** (a) mint a dev Mind key (`POST /admin/keys`; admin creds in the lockbox) and run one `/v1/chat` round-trip **including SSE** (`stream:true`) — confirm streaming actually streams, don't trust the docstring; (b) `pip install windy-connect` and do a pairing dry-run; (c) ping the windyfly bridge (`agent.respond`) against a running agent; (d) land one test event on the telemetry ingest. *Verify:* `docs/PROBE_RESULTS.md` recording each seam's observed behavior (routes, auth mode, streaming yes/no, versions); report it to Grant before Task 0.2.
- **0.1** Repo transition + scaffold. First: tag current HEAD `prototype-v0`, `git mv` the entire prototype into `reference/` (read-only from then on — the ledger's port source), then scaffold the §2 monorepo tree at root (empty modules, README per dir stating its socket + language). *Verify:* `reference/` runs untouched; tree matches §2; `tsc --noEmit` and `python -m compileall engine` both clean on empty stubs.
- **0.2** Write & freeze `contracts/voice-session.v1.md`. *Verify — run it literally:* spawn two independent subagents, one writing a client pseudo-implementation and one an engine pseudo-implementation **from the doc alone**; diff their interpretations. Every divergence is an ambiguity in the contract — fix and re-run until the interpretations agree. Then freeze.
- **0.3** Write & freeze `contracts/hands.mcp.v1.json` (tool schema + §9 tier per tool) and `contracts/telemetry.v1.json`. *Verify:* schemas validate; telemetry schema rejects any content-ish key (test with `transcript`, `message` → 422).
- **0.4** `telemetry/emit.py` — content-free, async, ≤200ms timeout, swallow all errors, no-op if unconfigured. *Verify:* unit test proves it never raises + never sends content; live test shows an event at `admin.windyword.ai` tail (token `verify-oc5` in lockbox).
- **0.5** Port the engine core into `engine/` with the STT/TTS provider ABCs; implement `whisper` + `kokoro` concretes; `transcribe` + `cloud` as `NotImplementedError` stubs (ADR-044 forced-honest). *Verify:* fed a wav file, engine transcribes → (echo brain) → speaks; per-stage latency logged against the §0.1 budget from this first run onward.
- **0.6** CI: lint + typecheck + the unit tests above on every PR. *Verify:* green pipeline. ⚠ GitHub Actions is billing-locked account-wide (since ~2026-07-04); write the workflow now, but until the lock clears the merge gate is a green LOCAL run of the same commands.

**Phase 0 DoD:** every external seam probed live and recorded (0.0); the prototype archived under `reference/`; the three seams are frozen docs that survived the two-agent adversarial read; the engine runs headless on the 5090 through the provider ABCs with §0.1 latency logging; telemetry emits; CI green. No client, no agent yet — just a correct spine and correct contracts.

## 5 · Phase 1 — The wedge (ATOMIC): talk to your Windy agent, hands-free, gated

- **1.1** `brains/mind.py` — call `POST api.windymind.ai/v1/chat` (SSE streaming; the `/v1/chat/completions` alias is live too, so an OpenAI SDK with `base_url=.../v1` also works). **Auth sequencing:** use the dev Mind key minted in Task 0.0 for 1.1–1.6; Task 1.7 swaps it for brokered Eternitas tokens, and *removing the dev key from code and disk is part of 1.7's verify* — no hardcoded key survives. *Verify:* a text turn streams tokens back; falls back cleanly if Mind unreachable.
- **1.2** `agents/windyfly.py` — connect to the Windy Fly bridge; `agent.respond`. Then **add `agent.respond_stream` to the windy-agent bridge** (`src/windyfly/bridge/uds_server.py`) via a PR to `sneakyfree/windy-agent`, so replies stream. *Verify:* spoken text → agent → streamed reply, sentence-by-sentence TTS.
- **1.3** `agents/connect.py` — pair the local agent via `windy-connect` (`pip install windy-connect`). *Verify:* `windy-connect` pairing yields a usable agent handle Windy Talk can address.
- **1.4** `hands/surface.py` + `hands/tiers.py` + `hands/backends/linux.py` — port `hands.py` logic; expose as a local HTTP + MCP surface; tag each action §9. *Verify:* the agent, via its capability/MCP, drives `open_app`/`type_text`/`click_element` on the desktop; an `always-confirm` action prompts first.
- **1.5** `apps/desktop/` (Electron/TS) — the canonical client: mic capture (AEC on, AudioWorklet, per the contract) → voice-session.v1 → engine; play reply; render the ported face + states; dispatch `tool_call` to the hands surface. *Verify:* end-to-end voice loop with the face animating; §0.1 budget met at p90 over ≥20 turns; barge-in works **through speakers, not just headphones**.
- **1.6** `wakeword/` — train "Hey Windy" (openWakeWord custom pipeline: synth voices + negatives → train → validate ≥0.95 TP / near-0 FP). Wire into the client. **Explicit exception to strict serialism:** this is the one ML mini-project in the strand, with an unpredictable iteration count — kick the training pipeline off as a *background track* as early as Phase 0 (it's fully independent and compute-bound on the 5090), and keep stock `hey_jarvis` as a dev-only fallback so Task 1.5 never blocks on it. The ≥0.95 TP gate holds for **release**, not for progress. *Verify:* "Hey Windy" wakes; other speech doesn't; `hey_jarvis` fallback removed at release.
- **1.7** `auth/eternitas.py` + gating — brokered ≤5-min tokens; gate the feature behind Windy Word/Fly entitlement. *Verify:* an unentitled user is refused; an entitled user connects; no long-lived token on disk.
- **1.8** Telemetry live: session start/end (minutes), turns, model, cost. *Verify:* usage for a test user appears on the admin panel.

**Phase 1 DoD (the wedge is real):** on the 5090, a gated, entitled user says "Hey Windy," talks to their hatched Windy Fly agent, it acts on their Linux desktop with trust-tier safety, replies stream in voice, and every session reports content-free usage to the admin panel. **This is the thing people pay for. Ship it, prove it, then open the sockets.**

## 6 · Phases 2–5 — milestones (re-atomize each at its start; do not fake-atomize the future)

- **Phase 2 — Open the sockets.** BYO-account through Mind (user picks the model); agent adapters beyond Windy Fly (OpenClaw, Hermes, generic via Connect + OpenAI-compat); ship Windy Talk as an **MCP server** so any agent self-configures it (the "agent finishes the setup" principle). *Gate:* a non-Windy agent + a non-default model both work with zero core changes.
- **Phase 3 — Agent-native + cross-platform hands.** `hands/backends/macos.py` (Accessibility API) and `windows.py` (UIAutomation). One canonical UI codebase, Electron shell per §4. *Gate:* the same client + face runs on macOS and Windows with working hands.
- **Phase 4 — Mobile + Windy Hand.** RN/Capacitor client (conversation core); hands = **Windy Hand** cloud browser; push-to-talk. *Gate:* talk to your agent on a phone; it acts in the cloud.
- **Phase 5 — Own compute.** Windy compute packages (Unmute/Pipecat-class or successors on Windy GPUs) as a first-class brain/voice provider behind the same ABCs. *Gate:* a user runs entirely on Windy compute at a competitive price.

**Re-atomization rule:** when a phase begins, write its own atomic task list (like §4/§5) as a fresh planning pass. The genome expresses genes when the organism needs them.

## 7 · Definition of done for the whole strand

Windy Talk is a downloadable app on Mac/Windows/Linux/mobile that lets anyone talk hands-free to any agent (default: Windy Buddy; premium: a hatched Windy agent) powered by any brain (their account via Mind, local, or Windy compute), acting on their computer or the cloud, gated as paid software, reporting content-free usage to the admin panel — and it *feels magical*. When every phase gate is green, that is done.

---

*Authored 2026-07-08 as the durable distillation of the Windy Talk strategy summit, so a fresh builder can execute with the full genome and none of the summit's context. Conforms to ADR-058; supersede via ADR + this file, never silently.*

*Amended 2026-07-08 (Fable pre-launch verification pass): corrected the live Mind route (`/v1/chat`, not `/v1/chat/completions`) and windyfly bridge path; added Task 0.0 dependency reality-check; froze the §0.1 latency budget; specified AEC/frame/timestamp requirements in voice-session.v1; repo transition in 0.1; two-agent adversarial contract verify in 0.2; dev-key auth sequencing (1.1→1.7); wake-word background track; contract change-control and merge policy.*
