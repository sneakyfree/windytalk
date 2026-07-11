# Windy Talk — the self-heal control surface (`control.mcp.v1`)

**Status:** **FROZEN rev.6**, 2026-07-11 — after **five adversarial verify rounds** (a
builder lens + a security/promise lens each round). Findings per round: **16 → 9 → 2 → 1 → 1**,
a clean convergence. Round 5's two reviewers *independently* landed on a single finding (the
tier-2 SIGKILL needed the same pid-identity verify rev.5 gave the takeover kill) and both
recommended freezing rev.6 after that one determinate pin without a sixth round; every other
mechanism survived direct attack in both round-5 reviews. Frozen accordingly. Formalizes the
"knobs" half of ADR-058 **D4** under **D5** tiers. Candidate ADR-059. Frozen ≠ infallible:
additive → v1.1 via PR; breaking → v2 + tell Grant.

Contract: [`contracts/control.mcp.v1.json`](../contracts/control.mcp.v1.json).

> **For the builder (Opus):** the **contract is authoritative** — every pinned number
> (coordinator timings, crash-loop thresholds, heartbeat cadence, rollback criteria, the
> return schemas) lives there, in the governance blocks (`security`, `recovery_coordinator`,
> `crash_loop`, `resurrection`, `self_update`, `safe_mode`, `last_known_good`,
> `diagnostics_privacy`) and each tool's `returns`. This doc is the *rationale and
> architecture*; where prose and contract differ, **the contract wins**. Don't invent
> behavior neither states — flag it for a round-2 pin instead.

---

## The promise

A non-technical user — grandma, a normie — has Windy Talk open. She doesn't know what a
terminal is, or GitHub, or npm, or Anthropic. She thinks AI *is* ChatGPT. The promise:

> Windy Talk is **rock-solid stable** — not a newborn giraffe falling over every 30
> seconds. On the rare occasion something breaks, she just **talks to her agent** and it
> heals — by itself, or because her agent can see every control and fix it. She is never
> stranded.

Stability is the word. This surface makes "it broke" a two-sentence conversation, not a
support ticket.

---

## Architecture in one picture

```
   ┌──────────────────────────────────────────────────────────────┐
   │  OS resurrection service   (launchd / systemd / Task Sched.)  │  ← slice 0
   │  "is Windy Talk alive? (heartbeat file) if not, relaunch"     │     the true floor
   └───────────────┬──────────────────────────────────────────────┘
                   │ relaunches
   ┌───────────────▼──────────────────────────────────────────────┐
   │  SUPERVISOR  (Electron main process — the most stable part)   │
   │  • Layer 1 autonomic recovery + crash-loop detector           │  ← the doctor,
   │  • recovery coordinator (single lock, debounce, rate-limit)   │     not in the
   │  • hosts control.mcp.v1 on :8782  (token+loopback+Origin gate) │     patient
   │  • writes heartbeat file; owns last-known-good config          │
   └───┬───────────────────────────┬──────────────────────────────┘
       │ supervises                │ serves tools to
   ┌───▼─────────┐            ┌─────▼───────────────────────────────┐
   │ voice engine│            │ agents: resident brain · local MCP  │
   │ (:8788)     │            │ client · external via relay (v1.1)  │
   │ renderer    │            └─────────────────────────────────────┘
   │ hands(:8781)│
   └─────────────┘
```

---

## Two layers (the core idea)

Most of the magic is in the layer the user never sees.

### Layer 1 — the autonomic supervisor (invisible; ~95% of the work)

**Not an agent.** A small, dumb, extremely reliable supervisor that owns the engine
connection (and, where the engine is a local child, the engine process). It makes "never
crash-loops" *true*, because the most common catastrophe — brain/engine unreachable — is
handled by code that needs no thinking:

- **Reconnect with exponential backoff + jitter**, with a ceiling (the client already has
  a 25 s liveness watchdog and a 1.5 s reconnect; Layer 1 adds the ceiling and the detector).
- **Crash-loop detector:** more than *N* restarts in *M* seconds → **stop thrashing**,
  drop into **safe mode**, show a calm face + one plain sentence, not a flickering zombie.
- **Last-known-good config**, persisted; any reset lands somewhere that works.
- **Safe mode:** push-to-talk, hands off, default brain, default devices — the reliable floor.

Goal: the user *never notices* the vast majority of failures.

### Layer 2 — the control surface (the agent escape hatch; the other ~5%)

When Layer 1 can't fix it — wrong mic, a mistyped engine URL, an entitlement to switch —
the user talks to her agent, which uses this MCP surface. Every knob is here, tier-gated,
described in **plain English** *because the agent is the translator* between "it won't
answer me" and `get_health` → `reconnect`.

When Windy Talk is itself agent-powered, Layer 2 mostly runs Layer 1's errands for free:
the resident agent notices "no engine audio for 30 s" and calls `reconnect()` first.

---

## §Gap 1 — the supervisor's supervisor (OS-level resurrection) — **slice 0**

Layer 1 survives the *engine* or *brain* dying, but **not the Electron process itself**
dying (hard crash, OOM kill, force-quit, a bad update). When main dies, nothing brings the
app back and grandma stares at a closed window — with no agent to talk to, because the
thing hosting the agent is gone.

The fix is a **tiny OS service whose only job is resurrection** — architecture
**heartbeat-watcher-spawner** on all three OSes (round-1 correction: *not*
child-supervision like systemd `Restart=always`, which only restarts its own child and
misses dock/user launches). The app **touches a heartbeat file that attests the SERVING
path** — bumped only after a renderer↔main round-trip proves it's actually serving, *not* a
free-running timer (a hung renderer with a ticking timer must not read as alive — the
"looks-alive-but-dead" failure round 1 caught). Pinned in the contract `resurrection` block:
touch every 5 s, service checks every 15 s, relaunch ≤ 45 s after `SIGKILL`, via **two
staleness tiers** (round-3 fix): **tier 1** — file absent, or `mtime > 30 s` **and pid
absent** → the process is gone → relaunch. **tier 2** — pid **present** but `mtime > 90 s`
→ possibly *wedged*; an **fs-writability probe** distinguishes disk-full (alive, can't write
— don't `SIGKILL`; surface it via an **OS-level** notification, not the possibly-dead in-app
UI). If the disk is writable, a heartbeat stale past 90 s already *means* serving stopped
(the writer is fate-coupled to the serving loop), so it's a genuine wedge → `SIGKILL` +
relaunch. **A bare TCP/HTTP accept on `:8782` must NOT veto this kill** (round-4 fix): the
reference HTTP listener runs on a *separate thread* that keeps answering even when main's
serving loop is deadlocked, so only a *serving-attesting* round-trip counts as alive. This
closes the wedged-supervisor hole: a deadlocked main with a live pid *is* now recovered.
And the single-instance lock does a **takeover** if the holder doesn't answer (so clicking
the dock icon on a hung app relaunches it, instead of the second instance politely exiting).
Three tiny periodic definitions:

- **macOS:** a `launchd` LaunchAgent that periodically checks the heartbeat.
- **Linux:** a `systemd --user` **timer** (not `Restart=always`) — and the installer must
  `loginctl enable-linger`, or the service dies at logout (the classic gotcha).
- **Windows:** a Scheduled Task (at logon + periodic) or a lightweight service.

Also pinned: a **single-instance lock** (so `restart_app` + the watcher can't double-launch
two `:8782` binds), the service's **own restart ceiling** (so a disk-full app that can't
write its heartbeat doesn't cause infinite resurrection thrash), and an **`resurrection_armed`
self-check** surfaced in `get_health` + a UI warning if the service silently didn't install.

Ship this **first** (slice 0). It is the true floor beneath the floor. **Acceptance:**
`SIGKILL` the app → back within ≤ 45 s, unattended, on all three OSes; and a disk-full app
does not thrash-relaunch.

---

## §Gap 2 — the external-agent path is the emotional core, and it's the hard one

Be honest about what v1 delivers. "She just talks to her agent and it fixes it" in **v1**
means the *resident* Windy agent (down exactly when the brain is down) **plus Layer 1**
(dumb code). The scenario you describe most vividly — *her own* ChatGPT/Claude, an
**external** agent, fixing Windy Talk — is **v1.1**, because it has a sub-problem the rest
of the design doesn't: **a normie cannot register an MCP server or copy a bearer token.**

The external path therefore needs **one-tap onboarding**, and the token is **never shown
as text to copy**. Two acceptable mechanisms (contract `onboarding_note`):
- **(a) One-click connect** — a "Connect my assistant" button that registers the MCP
  server into a supported local client (e.g. Claude Desktop) and injects the per-install
  token for her.
- **(b) Cloud relay** — route through the existing **`windy-fix-me`** relay pattern so she
  pastes nothing and a remote agent reaches the surface through the relay.

**Decision for the doc:** v1 ships self-heal + resident-agent + local-MCP-client, and the
UI/marketing must **not** promise "your ChatGPT fixes it" until (a) or (b) lands. Naming
this now keeps the plan honest and stops us from shipping a promise we can't keep.

---

## §Gap 3 — one healing coordinator (no fix-storms)

Three healers can fire at once — Layer 1, the resident agent, a (future) external agent —
and uncoordinated they *cause* the thrash we're preventing. So there is **one recovery
coordinator** (contract `recovery_coordinator`, now with pinned numbers so its acceptance
test is writable):

- **Single recovery lock** held by the 8 lock-holding recovery tools (enumerated in the
  contract; `repair_resurrection` is a recovery-class tool but deliberately *not* a holder).
  `enter_safe_mode` may **preempt** it (the escape hatch must never be blocked by a stuck
  reconnect). Config `set_*` tools don't hold the lock but return `already_recovering` if
  it's held. Lock auto-releases on completion or a **30 s ceiling** (no deadlock).
- **Debounce + rate-limit:** 5 s min between same-key calls (key = tool **name + args**, so
  "try the other mic" isn't debounced); ceiling **5 executed calls / tool / 300 s**; over
  it → `rate_limited`. Rejected calls don't count toward the ceiling.
- **Layer 1 is exempt and unbounded:** its own slow-retry reconnect goes through the lock
  but is *not* charged the rate limit and **never permanently gives up** — so a long real
  outage can't leave `rate_limited` as the permanent answer (round-1 fix).
- **Read tools exempt by name** (not "get_*", which round 1 noted wrongly excludes
  `list_audio_devices`/`check_for_update`); `run_selftest` is lock-exempt but rate-limited.

**Acceptance:** fire `reconnect` 50× in a tight loop → **≤ 5 execute; every other call
returns `rate_limited` *or* `already_recovering`** (a call landing inside `reconnect`'s
≤10 s lock window returns the latter — round-5 correction, so the assertion doesn't demand
`rate_limited` specifically); a `restart_engine` during an in-flight recovery returns
`already_recovering`; `enter_safe_mode` during a stuck reconnect still runs (preempts).

---

## §Gap 4 — diagnostics must not leak off-machine

The moment grandma's **external** ChatGPT reads `get_logs` / `get_health` / `get_config`,
**that data leaves her machine to a cloud we don't control.** D10's content-free rule was
about *our* ingest; it says nothing about a third-party brain. So every `get_*` tool obeys
the **diagnostics scrub rule** (contract `diagnostics_privacy`), *regardless of
destination*:

- **Never** any conversation transcript or user text.
- **Never** a secret / token / passport / api-key — redact to `***`.
- **Minimize** file paths (basename or a role like `<config-dir>`, never a full home path
  with the username), usernames, IP/MAC/SSID, and precise geolocation.
- `last_error` is a short technical string, never a payload.
- **When in doubt, omit.** A scrubbed diagnostic that leaks nothing beats a rich one that
  leaks a path.

**Acceptance:** a golden test feeds crafted internal state (a home path, a fake token, a
transcript-looking string) through every `get_*` tool and asserts none of it appears in
the output — the same shape as the telemetry content-free negative tests already in
`tests/test_contracts.py`.

---

## §Gap 5 — prove "steamroller proof" with a chaos harness

"Never crash-loops" must be a **measurement**, not a hope — this ecosystem's ethos is
"measured, not vibed," and it's full of stress harnesses for exactly this. Build a
**chaos / fault-injection harness** that deliberately, unattended:

- kills the engine mid-turn, kills the renderer, `SIGKILL`s the whole app (feeds slice 0),
- **hangs the renderer, and separately deadlocks the MAIN process** (pid alive, heartbeat
  stopped — the wedged-supervisor case round 3 caught; tier-2 must relaunch within budget),
  **including the case where main's serving loop is wedged but the separate `:8782` listener
  thread still answers** (round-4 case — a bare port-accept must NOT veto the kill), and
  asserts **a wedged pid holding `instance.lock` still relaunches** on a dock-icon click,
- **a foreign process squats `:8782`** (the legit app, holding `instance.lock`, must surface
  it rather than assume-live),
- **the crashed app's pid is recycled onto an innocent process** (identity-verify must treat
  the mismatched pid as *absent* → relaunch the app, and must **NOT** SIGKILL the innocent),
- **safety-inverse assertions** (so a trigger-happy build can't pass): a second launch against
  a **healthy** holder must *focus + exit, holder uninterrupted* (never killed); and after
  `reset_to_defaults`, later Layer-1 recovery lands on **factory**, never the pre-reset
  customization (asserts `reset_invalidates_lkg`),
- corrupts / truncates the config file **and every last-known-good generation** (must fall
  back to baked-in factory defaults),
- forces a crash loop (make launch fail ≥3× in 120 s → must land in safe mode, not thrash),
- pulls the network / makes the brain endpoint 500 or hang,
- yanks and swaps the selected audio device,
- **fills the disk** (heartbeat can't write — must not thrash-relaunch),
- **disables/unarms the resurrection service** (must self-detect + **auto-repair / re-arm**;
  warn only if privilege genuinely blocks it),
- **deletes the token file** (must re-onboard, not silently orphan),
- **applies an unsigned, a signed-but-broken, and a signed-but-OLDER update** (refuse /
  roll back to last-known-good binary / refuse the downgrade),
- **`restart_app` racing the resurrection service** (single-instance lock must prevent a
  double `:8782` bind),
- **kills the renderer, then triggers a gated recovery** (confirmer-down → the supervisor
  draws the native dialog, or the tool fails closed — never a silent hang),

…and **asserts recovery to a working state within a target time**, on each OS. This is how
"steamroller proof" becomes a number you defend, and how every future change stays proven.
It doubles as the permanent regression guard for Layer 1. **Acceptance:** the harness runs
in CI (or `scripts/chaos.sh` locally while Actions is billing-locked) and is green;
each fault class has an asserted recovery-time budget.

---

## §Gap 6 — safe self-update (bugs shouldn't be permanent on her machine)

If a bad build ships or a state arises that `reset_to_defaults` can't fix, a normie has no
path forward — she can't update. So the surface has `check_for_update` (read-only) and
`apply_update` (`always_confirm`). Round 1 flagged that the first cut was RCE with a soft
guard; the contract `self_update` block now makes these **normative**:

- **Signature-verified before staging** against a public key pinned in the shipped app.
  `run_selftest` is **not** an integrity gate (it checks reachability, which a malicious
  build passes) — unsigned/untrusted ⇒ refuse.
- **Out-of-process rollback:** the rollback trigger lives in the OS resurrection service /
  a separate watchdog, so a hostile new build **cannot suppress its own rollback** (round-1
  fix — the previous design's rollback lived inside the build it was rolling back).
- **Rollback criteria = launch + heartbeat + bind + engine-reachability within 60 s.**
  Audio-device stages are reported but are **not** rollback triggers (a healthy update must
  not roll back forever just because grandma's headset was unplugged).
- **Disk precheck fails closed** (`insufficient disk`) rather than half-staging.

This keeps a bug from living forever on a normie's install **and** makes "an update *owned*
grandma's machine" impossible, not just "bricked". **Acceptance:** an unsigned update is
refused; a signed-but-broken update ends up rolled back to the last-known-good binary,
working, unattended, driven by the *external* watcher.

---

## The OS question, answered

**One vocabulary. Per-OS implementations underneath. Capability negotiation to stay
honest.** The agent sees one uniform tool list; the words mean the same thing on Apple
Silicon, Mac Intel, Windows, and every Linux, and it **never needs to know the OS.** Only a
handful of tools are capability-gated because their *mechanism* is OS/install-specific, and
`get_capabilities` reports what this box supports (the exact pattern the hands layer uses):

| Tool | Why capability-gated |
|---|---|
| `restart_engine` | Restarts a process only where the engine is a **local child**; on the remote 5090 engine it degrades to a deep reconnect. |
| `clear_cache` | Cache paths differ per OS (uniform tool, different plumbing). |
| `restart_app` | Desktop relaunch; different on mobile. |
| *(the OS resurrection service)* | Not a tool — three tiny OS-specific service definitions (§Gap 1). |

Everything else is identical everywhere.

---

## Security — and "without giving away the farm"

A tool like `set_brain` or `reset_to_defaults` exposes a **capability, not a line of
source.** Grandma's agent turns knobs without ever seeing the code. But it is only safe
**behind the same wall we already built and proved for the hands surface**, and it is
non-negotiable here:

- **Per-install bearer token**, constant-time compare.
- **Loopback-only bind.**
- **Reject any request carrying an `Origin` header** (blocks a webpage from driving it).
- **No CORS.**

An ungated localhost port that can reset and reconfigure the app is an RCE hole — the exact
reason Windy Word ships its `:18765` control port disabled. We reuse the closed version of
that wall from day one, plus `set_engine_url` is **host-pinned** to an allow-list (same
discipline as the brain-URL pin in `agents/connect.py`) so an agent can't point the voice
stream at an attacker's server.

**Trust tiers** keep blast radius sane (D5): read-only diagnostics and safe-direction
recovery (`reconnect`, `enter_safe_mode`) are `auto_allow`; config changes are `ask_first`
(a voice prompt or a tap — *never* a terminal); the two dangerous tools
(`reset_to_defaults`, `apply_update`) are `always_confirm` and can't be auto-upgraded.
**Safe-by-default:** anything moving toward a more reliable state is free; anything that
loses customization or loosens safety asks first. `set_autonomy` is direction-aware —
*lowering* trust is free, *raising* it needs an explicit yes.

---

## The tool list at a glance (24 knobs)

**Diagnostics — the agent's eyes (all `auto_allow`, read-only, scrubbed per §Gap 4; each
has a pinned `returns` schema in the contract):** `get_health` (the linchpin — plain-English
`summary` + least-destructive `suggested_fix`), `get_status`, `get_config`, `get_logs`,
`list_audio_devices`, `run_selftest`, `get_capabilities`, `check_for_update`.

**Recovery — the "off and on again" ladder (governed by the coordinator, §Gap 3):**
`reconnect` *(auto)* → `enter_safe_mode` *(auto, may preempt)* → `exit_safe_mode` *(floor —
always confirms)* → `repair_resurrection` *(ask — re-arms the floor when it's missing, added
in round 2)* → `restart_engine` *(ask; "degraded" on a remote engine)* → `clear_cache`
*(ask; excludes on-device models)* → `restart_app` *(ask)* → `reset_to_defaults`
*(always-confirm — the big red button; FACTORY defaults, not last-known-good)* →
`apply_update` *(always-confirm — the one RCE-by-design tool, §Gap 6)*.

**Configuration — the dials:**
`set_audio_input` / `set_audio_output` *(ask)*, `set_volume` *(auto above 0; confirm to
mute)*, `set_engine_url` *(ask, host-pinned, non-upgradeable)*, `set_brain` *(ask,
non-upgradeable, offline-cache validated)*, `set_wake_mode` *(ask)*, `set_autonomy`
*(direction-aware via `tier_override`, non-upgradeable)*.

The linchpin is **`get_health`**: a structured snapshot *plus* a `summary` the agent reads
aloud and a `suggested_fix` (always the least-destructive plausible one). Read the summary,
call the suggested fix, confirm with `get_status`. That three-step loop is the whole
grandma experience.

---

## Walkthroughs (what "she just talks to it" looks like)

**"I asked you a question and you didn't answer."** Agent → `get_health` →
`{healthy:false, engine:{connected:false}, summary:"The connection to your assistant
dropped a minute ago — reconnecting usually fixes it.", suggested_fix:"reconnect"}` →
`reconnect` → `get_status` → `idle`. *(Usually Layer 1 already did this first.)*

**"It can't hear me."** Agent → `list_audio_devices` → headset present but built-in mic
selected → `set_audio_input(headset)` *(one tap)* → `run_selftest` → mic stage passes.

**"It's gone crazy / keeps flickering."** Layer 1's crash-loop detector already forced
**safe mode** (no wobble). Agent → `get_health` → `{mode:"safe", crash_loop:true}` →
offers `reset_to_defaults`; on her *(always-confirm)* yes, clean restart on **factory
defaults** (the button restores factory, not last-known-good — LKG is Layer 1's, not the
button's).

**"A fix is available."** Agent → `check_for_update` → newer known-good build → explains →
`apply_update` *(always-confirm)* → staged install, selftest passes; if it had failed, the
supervisor silently rolled back and she stays on the working build.

**Nobody's agent is reachable, or the app is closed.** The **OS resurrection service**
relaunches a crashed app; the physical **Reset** button calls `reset_to_defaults` with no
agent, no terminal, no GitHub. The floor beneath the floor beneath the floor.

---

## Also: grandma-readable status without an agent

The face already carries state; add **one plain sentence in the UI** ("I lost the
connection — reconnecting…") so she isn't dependent on an agent even to *understand* the
problem. And publish the control server itself as **`windytalk-mcp` on npm** (mirroring
`windy-word-mcp`, 115 tools) so *any* agent stack can `npx windytalk-mcp` and get the tools
— the concrete, correct realization of the "register a bunch of npm commands" instinct.

---

## What's reused vs. new

**Reused (already built + hardened this month):** the MCP transport + `{ok,result,error}`
envelope (hands surface), the three-tier model (`hands/tiers.py`), the OS-backend +
`capabilities()` pattern (`hands/backends/`), the security wall (token + loopback +
Origin-reject + constant-time compare), the host-pin discipline (`agents/connect.py`), the
content-free-scrub test pattern (`tests/test_contracts.py`), and the client's 25 s liveness
watchdog + reconnect (feeds Layer 1). The `windy-fix-me` relay exists for the v1.1 external
path.

**New:** the **OS resurrection service** (§Gap 1, three definitions + heartbeat), the
**supervisor / Layer 1** (crash-loop detector, backoff ceiling, safe mode, last-known-good),
the **recovery coordinator** (§Gap 3), the **control surface host** on `:8782`, the
**24 tool handlers**, the **structured `get_health`**, the **diagnostics scrub** (§Gap 4),
the **chaos harness** (§Gap 5), and **safe self-update** (§Gap 6).

---

## Build order (each a shippable slice with acceptance criteria)

0. **OS resurrection service + heartbeat.** Three tiny service definitions; app touches the
   heartbeat. *Accept:* `SIGKILL` the app → back within the interval, all three OSes.
1. **Supervisor / Layer 1 + `get_health` + `reconnect` + `enter_safe_mode` + the recovery
   coordinator.** The invisible stability win *and* the two safest fixes, governed so they
   can't thrash. Read-only + safe-direction only. *Accept:* crash-loop → safe mode not
   zombie loop; `reconnect` ×50 → mostly `rate_limited`.
2. **Rest of diagnostics** (`get_status/config/logs`, `list_audio_devices`, `run_selftest`,
   `get_capabilities`, `check_for_update`) **+ the scrub rule + its golden test.**
3. **The recovery ladder + `reset_to_defaults` + the physical Reset button.**
4. **The config dials** (`set_*`), each behind ask-first; `set_engine_url` host-pinned.
5. **Safe self-update** (`apply_update` + A/B rollback).
6. **The chaos harness** (can start alongside slice 1; must be green before calling it
   "steamroller proof").
7. **v1.1:** external-agent onboarding (§Gap 2) via one-click connect or the `windy-fix-me`
   relay; `windytalk-mcp` npm publish; per-argument tiers.

Ship slice 0 then slice 1 first: the biggest grandma win is the failure she *never sees*.

---

## §Decisions (all RESOLVED 2026-07-11 — Grant confirmed the leans; build these)

1. **Control port:** **separate process/port `:8782`** (the doctor must outlive the patient).
2. **`reset_to_defaults` scope:** **settings-only** (history preserved); a history wipe is its
   own explicit tool.
3. **Telemetry on control actions:** **emit content-free `control.action` events (D10)** — the
   self-heal-rate metric.
4. **Autonomy ceiling for fresh normie installs:** **cap autonomy low (3) until she opts up.**
5. **Self-update channel + signing (RESOLVED):** channel = **GitHub Releases** (channel-head =
   newest non-prerelease Release); signing = a **self-generated keypair whose private half
   Grant holds**, public half embedded in the app for verify-before-stage. Tools stay
   forced-honest and inert until the public key is embedded (Grant's real-world action before
   slice 5: generate keypair, embed public key, wire signing into release publishing). See
   contract `self_update.source`.
6. **Confirmer when the renderer is down (RESOLVED):** the **supervisor draws a minimal native
   OS dialog** (chosen over plain fail-closed, to keep the most recovery reachable in deep
   failure); fail-closed only when even a native dialog can't render, with the physical Reset
   button as the final agent-free path. See contract `security.confirmer_fallback`.

---

## Freeze status — FROZEN rev.6 after five rounds

**Round 5 (2026-07-11, confirming pass on rev.5):** two fresh reviewers — both NOT-YET, both
with **the exact same single major**, independently: rev.5 added executable-path + start_time
identity verification before the *takeover's* SIGKILL, but the OS service's **tier-2 kill**
(the path round 4 made unconditional) got no such verify — so on a hard crash where the OS
recycles the pid onto an innocent process, tier 2 could SIGKILL the wrong process. → rev.6
makes "pid present/absent" **identity-aware everywhere** (a mismatched recycled pid is treated
as absent → relaunch, never killed; `{pid, started_at, exe}` is the identity for *both* kill
paths). Plus small pins: the single-instance verify baseline + mismatch branch, update-staging
process ownership, `repair_resurrection` capability value, reset clearing the safe-mode flag,
the `reconnect`-50× acceptance wording, and three chaos safety-inverse assertions (healthy
holder never killed, pid-recycle victim survives, reset → factory).

Both reviewers explicitly attacked and **confirmed everything else in rev.5 held** — the
serving-attesting `:8782` fix is genuinely closed (no busy-app or spoofing regression), the
takeover can't be abused beyond conceded local-same-user trust, `reset_invalidates_lkg` can't
strand, and the tier matrix has no regression — and both recommended freezing rev.6 after the
one determinate pin without a sixth full round. **Frozen accordingly.** The convergence
(16 → 9 → 2 → 1 → 1) earns the freeze by the same standard `voice-session.v1` and
`hands.mcp.v1` were held to.

---

## Freeze status — earlier rounds

**Round 4 (2026-07-11, confirming pass on rev.4):** two fresh reviewers — again NOT-YET, but
down to **one substantive finding**, and they **converged on it independently** with the same
root cause: the tier-2 `:8782` liveness ping. Because the reference HTTP listener
(`hands/surface.py`) runs on a *separate daemon thread*, that port keeps answering even when
main's serving loop is deadlocked — so "port answered → wait" would never relaunch a genuinely
wedged supervisor, silently re-opening round 3's hole. Both reviewers traced it to the
separate-thread reference. → rev.5 pins the `:8782` probe as **serving-attesting** (a
renderer↔main round-trip, not a bare accept), and makes writable-disk + stale-heartbeat a
genuine wedge that's killed regardless of a bare port answer. Everything else round 4 raised
was a precision pin on the new machinery: the single-instance ack (3 s + retry, pid verified
by exe-path/start_time before kill), `service_backoff` numbers (3/300 s → 1/5 min),
`heartbeat_content` (`{pid, started_at}`; process-name scanning forbidden), the stale
`exit_safe_mode` description, `repair_resurrection`'s lock treatment, `reset` invalidating
LKG, the device-scrub default-to-type+id for marker-less names, and the `:8782`-squat
reconciliation. Reviewer B explicitly re-attacked and **confirmed the `set_autonomy` fix,
instance-takeover safety, safe-mode-keeps-engine-url, and the error/result split all hold**.

**Why one more:** rev.5 refined the staleness/takeover machinery yet again, and the discipline
that's paid off every round is "confirm the new fix didn't introduce a new divergence." The
trend (16 → 9 → 2 → 1) says round 5 should be the clean one — at which point the freeze is
earned by the same standard as `voice-session.v1`.

---

## Freeze status — earlier rounds

**Round 3 (2026-07-11, confirming pass on rev.3):** two fresh reviewers — **both NOT-YET,
one major each**, and they again converged (the `set_autonomy` collision was flagged by both).
Reviewer B also attacked all eight round-2 closure claims and confirmed seven genuinely held.
The two majors, both pinned into rev.4:

- **`set_autonomy` floor/override collision** — I'd put `set_autonomy` in the
  `always_confirm_floor.always` list unconditionally, but its `tier_override` says *lowering*
  is `auto_allow`; since the floor overrides the override, lowering wrongly became
  always-confirm (dead-coding the safe-direction branch). → moved to `conditional` ("when
  raising"). Same defect *class* as round 2's mute-guard — a recurring subtle spot.
- **Wedged-supervisor unrecoverable** (slice 0) — rev.3's staleness rule (`pid absent`) never
  relaunched a *deadlocked-but-live* main; the block even contradicted itself ("goes stale
  anyway" was false). The naive fix (`mtime>30s → kill`) would kill-loop a disk-full-but-alive
  app and `SIGKILL` a mid-confirm dialog. → **two-tier staleness** (tier 2: pid-present +
  stalled → fs-writability probe for disk-full, `:8782` ping for wedge, then kill+relaunch) +
  **instance-lock takeover** (a wedged holder no longer traps the dock-icon relaunch).

Plus five surgical pins: Layer-1 exemption now covers the crash-loop `enter_safe_mode` trip
(so it can't be `rate_limited` mid-thrash); coordinator checks run *before* the confirmer;
`error` is a bare code with the reason in `result`; device-name scrub extended to trailing/
localized possessives; `safe_mode` exit reworded (drop overlay, keep safe-mode saves) and
`engine_url` exempted from the factory overlay (so safe mode isn't voiceless on a LAN engine).

**Why a round 4:** rev.4 added genuinely new machinery (staleness tiers, takeover), and every
round so far proved that a fix pass can introduce a fresh divergence (round 2 found round 1's;
round 3 found round 2's/rev.3's). One more confirming pass on rev.4 is the honest bar to
freeze. The trend (16 → 9 → 2) suggests it should be near-clean.

---

## Freeze status — round 1 & 2 history

**Round 2 (2026-07-11):** two fresh reviewers against rev.2 — both returned **NOT-YET**, and
independently converged on the same core contradiction (strong signal). **9 blocker/major
items**, all pinned into rev.3:

- **Two safety guarantees the round-1 fixes silently voided at autonomy ≥7** — the mute-guard
  (`set_volume(0)`) and `exit_safe_mode`'s confirm both dissolved because they weren't in the
  no-auto-grant set. → replaced `non_session_upgradeable` with an **`always_confirm_floor`**
  (tools/conditions that *always* confirm, never dissolved by session grant or autonomy) +
  a single stated **`tier_resolution`** algorithm so the four gating mechanisms can't collide.
- **`apply_update` downgrade attack** — signature proves authenticity, not freshness. →
  **anti-rollback** (reject version ≤ current, channel-head only) + the rollback watchdog is
  now **immutable across `apply_update`** (a hostile build can't ship a neutered watchdog).
- **The resurrection floor was observed-but-not-repaired** (a single point of failure). →
  launch-time **auto-repair** + a new agent-callable **`repair_resurrection`** tool (#24).
- **Two broken return schemas** (`run_selftest`'s unresolvable `$ref`, `get_config`'s
  prose-only `$fields`) → real inline schema + a document-root `$defs/config`.
- **`safe_mode` vs `last_known_good` disagreed** on what safe mode lands on → pinned: safe
  mode = **factory** defaults; LKG is Layer-1 auto-recovery only.
- Plus: `exit_safe_mode` coordinator treatment, `apply_update` lock window vs the 30 s
  ceiling, device-name scrub, URL-parse discipline, reserved-error split, telemetry
  denominator, and the doc/contract count + factory-vs-LKG walkthrough syncs.

**Round 1 (2026-07-11):** two reviewers found 16 blocker-class divergences (below), all
pinned into rev.2.

---

### Round 1 detail

**Round 1 (2026-07-11):** two independent adversarial reviewers read the draft cold — one
in the **builder lens** (where would two implementers diverge?), one in the **security +
does-it-deliver-the-promise lens**. They converged independently on the same worst items
(strong signal). **16 blocker-class findings**, all now pinned into `control.mcp.v1.json`
rev.2. The load-bearing fixes:

1. **Direction-aware tiers were unenforceable** in the reused tier engine (`ask_first` tools
   are session-upgradeable). → `security.non_session_upgradeable` set + `set_autonomy`
   `tier_override`, resolved by the surface, not `TierPolicy` as-is.
2. **Heartbeat detected a timer, not serving** → serving-liveness heartbeat + single-instance
   lock + service backoff + `resurrection_armed` self-check + heartbeat-watcher architecture.
3. **`apply_update` was RCE with a soft guard** → signature-verify-before-stage +
   out-of-process rollback + reachability-only rollback criteria + disk precheck.
4. **`set_engine_url` allow-list was unspecified** (the https brain-pin can't cover the LAN
   engine) → concrete `security.engine_allow_list`, immutable via MCP, `wss` for non-loopback.
5. **The coordinator had no numbers** → pinned lock set, preempt rule, 30 s ceiling, 5 s
   debounce, 5/300 s rate limit, Layer-1 exemption, named exempt read set.
6. **Safe mode was a one-way door** → `exit_safe_mode` (tool #23) + overlay-not-config-write
   semantics + persistence.
7. **`reset_to_defaults` fused factory ≠ last-known-good** → factory (immutable baked-in) for
   the button; LKG (atomic, checksummed, N generations, factory fallback) for auto-recovery.
8. **Six of eight read tools had no return shape** → real `returns` schemas on all of them;
   closed enums; `get_logs` returns an object (the envelope carries no bare arrays).
9. Token lifecycle (per-install, persisted, distinct from hands, survives reset), MCP
   `initialize` compliance, scrub-as-allow-list with three documented exceptions, restart
   response-ordering, `control.action` telemetry, `set_volume` mute-guard, `clear_cache`
   model-exclusion, `set_brain` offline-cache validation — all pinned.

**Freeze decision:** round 2's 9 items were all determinate surgical pins (broken schemas,
a version-comparison rule, a floor membership, an added tool) — not open judgment calls — so
rev.3 is **frozen per Grant's fix-and-freeze directive**. A confirming **round-3** pass
(the belt-and-suspenders that `voice-session.v1` got) remains available and is cheap
insurance before Opus builds slice 0; it is recommended but not required. If round 3 is run
and finds nothing, this is unchanged; if it finds something, it becomes a v1.1 additive pin.

**The two product calls left for Grant are now RESOLVED (2026-07-11):** self-update = GitHub
Releases + a Grant-held self-generated signing key (inert until the public key is embedded);
confirmer-when-renderer-down = supervisor-drawn native OS dialog (fail-closed only when even
that can't render). Both are baked into the contract (`self_update.source`,
`security.confirmer_fallback`) and §Decisions above — nothing is left open.
