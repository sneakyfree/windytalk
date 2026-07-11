# control.mcp.v1 — builder orientation (BUILD NOTES)

**Purpose of this file:** the frozen contract (`contracts/control.mcp.v1.json`) pins *behavior*
and the design doc (`docs/CONTROL_SURFACE_DESIGN.md`) explains *why*. This file is the third
leg: the *codebase orientation* a builder needs but that isn't in either — the reuse map, the
repo's build/test reality, the recommended code structure, the conventions, and the landmines.
Written 2026-07-11 by the designer, before handoff, so the build doesn't depend on
conversation history that no longer exists.

**Read order for the builder:** (1) this file, (2) `contracts/control.mcp.v1.json` IN FULL —
it is authoritative, (3) `docs/CONTROL_SURFACE_DESIGN.md` for architecture + the five-round
freeze rationale. Where prose and contract disagree, **the contract wins**. Don't invent
behavior neither states — if something is genuinely ambiguous, flag it, don't guess.

---

## 1. Current repo state (what exists, what's new)

Repo: `sneakyfree/windytalk`, branch `master` @ `5244e29` (as of handoff). The voice wedge is
BUILT and green:

- `engine/` — voice-session.v1 ws server, VoiceSession turn loop, VAD, segmentation. **Done.**
- `brains/` (Mind), `agents/` (Windy Fly bridge + Connect pairing), `auth/` (Eternitas gate),
  `telemetry/` (content-free emit) — **done.**
- `hands/` — the OTHER MCP surface: desktop control of THIRD-PARTY apps, with the token +
  loopback + Origin wall, tier engine, and linux/macos/windows backends. **Done + hardened.**
- `apps/desktop/` — Electron/TS voice client (`electron/main.js` shell, `src/` protocol +
  renderer, `renderer/` face + panel). **Done.**
- `wakeword/`, `apps/cli` (stub), `apps/mobile` (empty), `server/` (README + empty `__init__`).

**control.mcp.v1 is DESIGN ONLY** — the contract + design doc are frozen; **no code exists
yet**. `contracts/control.mcp.v1.json` and `docs/CONTROL_SURFACE_DESIGN.md` are currently
untracked (add them in your first PR). This is what you are building.

Tests today: ~99 python (`pytest`) + 27 client (`node --test`) green.

---

## 2. Recommended implementation structure (a recommendation — the contract pins behavior, not layout)

The contract says the surface is "hosted by the supervisor (on desktop: the Electron **main**
process)". Concretely, the recommended structure:

- **The supervisor + control surface live in the Electron MAIN process (Node/TS)** — `main` is
  the most stable local process, it already exists (`apps/desktop/electron/main.js`), and it
  owns the window + the engine ws connection. Hosting here satisfies "the doctor is not in the
  patient" (the *patient* is the renderer + the remote/child engine; `main` outlives both).
- **Port the proven PATTERNS from Python to TS — do not import the Python.** "Reuse
  hands/surface.py" in the contract means reuse its *design*, in `main`:
  - the MCP + local-HTTP transport, the `{ok,result,error}` envelope, and the
    **token + loopback-only + reject-any-Origin + constant-time-compare** wall
    (`hands/surface.py` is the reference implementation — re-express it in TS in `main`);
  - the three-tier model + the `tier_resolution` algorithm (`hands/tiers.py` is the reference;
    the contract's `tiers.tier_resolution` EXTENDS it — implement the 4-step algorithm exactly);
  - the OS-backend + `capabilities()` tri-state pattern (`hands/backends/` is the reference for
    per-OS mechanism behind one vocabulary).
- **The 24 tool handlers are TS in `main`.** Most reach things `main` already has (the engine
  ws client, config store, `navigator`/OS audio enumeration, child-process control). A handler
  MAY shell out to a small Python/OS helper where that's simplest (e.g. an OS-service install).
- **The OS resurrection service (slice 0)** = three tiny native service definitions (a `launchd`
  LaunchAgent plist, a `systemd --user` timer unit, a Windows Scheduled Task) that periodically
  run a small watcher, plus the heartbeat writer + self-check/auto-repair in `main`. This is the
  one unavoidably-OS-specific piece; keep it tiny and boring.
- **New TS lives under `apps/desktop/`** (e.g. `apps/desktop/electron/control/` for the surface +
  supervisor, `apps/desktop/electron/resurrection/` for the service definitions + installer).
  Keep it out of `src/` (which is the renderer-side protocol client).
- **If you add any Python** (e.g. a shared helper), you MUST add its module to the `ruff check`
  line in `scripts/ci.sh` (it lists modules by name — see §4) or the gate won't lint it.

You are free to choose a different structure if you find a cleaner one — the contract only
constrains observable behavior. But this layout reuses the most and fights the codebase least.

---

## 3. Reuse map (exact files → what to take)

| File | Reuse for | Note |
|---|---|---|
| `hands/surface.py` | the MCP+HTTP transport + `{ok,result,error}` envelope + the **token/loopback/Origin/constant-time wall** | reference pattern; re-express in TS in `main`. NOTE the contract flags two bugs to NOT copy: its `handle_mcp` has no `initialize` lifecycle and renders results via `str()` (invalid JSON) — the control surface must do real MCP + canonical JSON + `structuredContent`. |
| `hands/tiers.py` | the three-tier model + confirmer protocol | the contract's `tier_resolution` is an EXTENSION (value-conditional `set_volume`/`set_autonomy`, the `always_confirm_floor`, autonomy bands) — implement the 4-step algorithm in `tiers.tier_resolution` exactly. |
| `hands/backends/` (`base.py` + linux/macos/windows) | the OS-backend ABC + `capabilities()` tri-state | model for capability-gated tools (`restart_engine`, `clear_cache`, `restart_app`, `repair_resurrection`). |
| `agents/connect.py` (`_require_trusted_url`) | the host-pin discipline for `set_engine_url` | uses `urlparse().hostname` + leading-dot suffix match — the contract's `security.engine_allow_list.host_match` requires exactly this (don't reintroduce substring matching). |
| `telemetry/emit.py` + `contracts/telemetry.v1.json` | the content-free `control.action` events | fire-and-forget, inert unless configured, never content. Emit on EXECUTED mutating calls only (contract `telemetry`). |
| `tests/test_contracts.py` | the content-free-scrub NEGATIVE test pattern | model for the `diagnostics_privacy` golden test (feed crafted PII/paths/tokens through every `get_*`, assert none leaks). |
| `apps/desktop/electron/main.js` | the current supervisor host + the IPC-to-hands proxy pattern | the control surface is the mirror of the existing `windytalk:hands` IPC proxy — same token-in-main, no-CORS approach. |
| `apps/desktop/src/renderer.ts` | the 25 s liveness watchdog + reconnect | already exists; feed it into Layer-1 autonomic recovery. |

---

## 4. Build / test / lint reality

**GitHub Actions is billing-locked account-wide (since ~2026-07-04).** A green run of
`scripts/ci.sh` is THE merge gate — it runs the same commands as the (dormant) workflow. Run it
from the repo root before every merge. It does:

```
ruff check engine brains agents hands auth telemetry wakeword tests   # add new py modules here
python3 -m pytest tests/ -q
npx -p typescript tsc --noEmit -p apps/desktop     # and apps/cli
( cd apps/desktop && npm test )                    # tsc build + node --test dist/test/*.test.js
```

- **New Python module → add it to the `ruff check` line in `scripts/ci.sh`** (it lists modules
  by name; an unlisted module is silently un-linted, which reads as passing).
- Client (TS) tests use Node's built-in runner (`node --test`) against compiled `dist/` — mirror
  the existing `apps/desktop/test/*.test.ts` style (see `wake.test.ts` for the injected-clock
  pattern, ideal for the coordinator/staleness state machines).
- pytest uses lazy CUDA imports — no GPU needed for unit tests.
- Ruff config is in `pyproject.toml` (E/F/W/I/UP/B; `reference/` excluded).

**Write tests to the contract's acceptance criteria** (§7) as you go — especially the chaos
harness (see §7 slice 6): "steamroller proof" is only real as a passing fault-injection suite.

---

## 5. Conventions (from the ecosystem; non-negotiable)

- **Branch, never commit to `master`.** One PR per slice; merge after `scripts/ci.sh` is green +
  self-review. (Standing self-merge authority applies to `sneakyfree/windy-*` repos after a
  green gate + diff sanity check.)
- **Content-free telemetry from the first commit** (ADR-WA-001 / D10): the surface emits
  `control.action` to `admin.windyword.ai` (platform `windy-talk`). Fire-and-forget, inert
  unless configured, NEVER content (counts/ids/durations/models only; the ingest 422s content-ish
  keys — fix the event, never loosen the guard).
- **Commit trailer:** end commit messages with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
  End PR bodies with the Claude Code generated-with line.
- **Forced-honest:** a half-wired capability must fail loudly / return an honest error, never
  fake success (the contract already encodes this for `apply_update`, `check_for_update`, the
  resurrection self-check — keep the discipline everywhere).

---

## 6. Landmines (things that have bitten before)

- **Launching Electron from a Claude Code sandbox = black window / hang.** Fix:
  `TMPDIR=/tmp setsid nohup ./node_modules/.bin/electron . --no-sandbox --disable-gpu` plus the
  Bash `dangerouslyDisableSandbox` flag; for offscreen screenshots set `WINDYTALK_SHOT`. A CJS
  helper script fails under the app's `"type":"module"` — use `.cjs` or ESM. (You'll want this to
  drive the chaos harness / visual checks.)
- **The app's `package.json` is `"type":"module"`** — `main.js`/new main code is ESM (`import`).
- **SSH-to-Veron (the 5090) long-lived exec drops (exit 255) from a CC sandbox** — live engine
  runs are Grant's; verify engine-touching tools with a local fake or a short-lived call.
- **`hands/surface.py`'s MCP handler is a known-imperfect starting point** (no `initialize`,
  `str()`-rendered results) — the contract requires you to do better; don't copy those two bugs.
- **`set_autonomy` and `set_volume(0)` are the recurring trap** (three of five freeze rounds hit
  the tier interaction) — implement `tier_resolution` as the single source of truth and unit-test
  the full matrix (raise/lower/equal, mute/unmute, at autonomy 2/5/8).

---

## 7. Build slices (consolidated from the design doc — build in order, one PR each)

Each slice ships behind `scripts/ci.sh` green with tests to its acceptance criteria.

0. **OS resurrection service + heartbeat** (the true floor — build FIRST). Three tiny service
   definitions + heartbeat writer (`{pid, started_at, exe}`, JSON, bumped by the serving loop) +
   install/self-check/auto-repair. *Accept:* `SIGKILL` the app → back ≤45 s unattended on all
   three OSes; a disk-full app does not thrash-relaunch; a wedged (deadlocked-but-live) main is
   killed+relaunched via tier-2 (serving-attesting `:8782` probe, not a bare accept); a recycled
   pid is treated as absent (identity verify), innocent process NOT killed; dock-click on a
   wedged holder relaunches (instance-lock takeover).
1. **Supervisor / Layer 1 + `get_health` + `reconnect` + `enter_safe_mode` + the recovery
   coordinator.** Read-only + safe-direction only; the invisible stability win. *Accept:*
   crash-loop (≥3 restarts / 120 s) → safe mode, not a zombie loop; `reconnect` ×50 → ≤5 execute,
   rest `rate_limited`/`already_recovering`; `enter_safe_mode` preempts a stuck lock.
2. **Rest of diagnostics** (`get_status/config/logs`, `list_audio_devices`, `run_selftest`,
   `get_capabilities`, `check_for_update`) **+ the diagnostics-scrub rule + its golden test.**
3. **Recovery ladder** (`exit_safe_mode`, `repair_resurrection`, `restart_engine`, `clear_cache`,
   `restart_app`) **+ `reset_to_defaults` + the physical Reset button.**
4. **Config dials** (`set_*`), each ask-first; `set_engine_url` host-pinned; the `tier_resolution`
   matrix fully unit-tested (the trap in §6).
5. **Safe self-update** (`apply_update` + signed channel-head + A/B + out-of-process rollback).
   Channel + key are decided (§8) but INERT until Grant embeds the public key — build it inert.
6. **The chaos / fault-injection harness** (can start alongside slice 1; must be green before
   claiming "steamroller proof"). Cases the doc §Gap 5 enumerates: engine/renderer/app kill,
   main-hang with the `:8782` thread still answering, config + all-LKG corruption, crash loop,
   network/brain 500/hang, audio-device swap, disk-full, resurrection-unarmed, token-loss,
   unsigned/broken/older update, `:8782`-squat, pid-recycle-victim, and the SAFETY-INVERSE
   assertions (a healthy holder is NEVER killed; reset → factory, never pre-reset customization).
7. **v1.1 (later):** external-agent onboarding (one-tap connect / `windy-fix-me` relay);
   `windytalk-mcp` npm publish; per-argument tiers.

---

## 8. Resolved decisions (baked into the contract — no product calls left open)

- **Self-update:** channel = GitHub Releases (channel-head = newest non-prerelease Release);
  signing = a self-generated keypair whose PRIVATE half Grant holds, public half embedded in the
  app. Build the tools INERT until the public key is embedded (Grant's real-world action before
  slice 5). Contract: `self_update.source`.
- **Confirmer when the renderer is down:** the supervisor draws a minimal NATIVE OS dialog;
  fail-closed only when even that can't render; physical Reset button is the final agent-free
  path. Contract: `security.confirmer_fallback`.
- Also decided (built as default): separate control port `:8782`; `reset_to_defaults` is
  settings-only; `control.action` telemetry on; fresh-install autonomy cap = 3.

---

## 9. First move for the builder

Add the two frozen artifacts + this file to a branch, read all three, then **post your slice-0
plan (the resurrection service + heartbeat, with the two-tier staleness + identity-verify +
takeover) and your acceptance-test list before writing code** — a cheap check that your reading
of the frozen contract matches its intent. Then build slice 0 → green → PR → slice 1.
