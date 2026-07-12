You are the builder of **control.mcp.v1** — the self-heal control surface for Windy Talk. It
is fully designed and frozen; your job is to build it, slice by slice, in the repo at
`~/Desktop/Grant's Folder/windytalk` (note the space; quote paths).

**The mission, in one sentence:** a non-technical user ("grandma") whose Windy Talk voice app
breaks can have an AI agent (or the app itself) call these local MCP tools to heal it — the app
must be rock-solid stable, never crash-loop, and never leave her stranded.

**READ FIRST, IN FULL, before writing any code:**
1. `docs/CONTROL_BUILD_NOTES.md` — builder orientation: repo state, the reuse map, recommended
   code structure, build/test/lint reality, conventions, landmines, and the slice plan.
2. `contracts/control.mcp.v1.json` — the FROZEN contract. **It is authoritative.** 24 tools +
   governance blocks (security, recovery_coordinator, crash_loop, resurrection, self_update,
   safe_mode, last_known_good, diagnostics_privacy, tiers, response_ordering) with pinned numbers
   and per-tool return schemas.
3. `docs/CONTROL_SURFACE_DESIGN.md` — the architecture, the two-layer model, and the five-round
   adversarial-freeze rationale (why each pin exists).

**Prime directives:**
- The **contract wins** over any prose. Where the design doc and contract differ, follow the
  contract. Every number (coordinator timings, staleness tiers, crash-loop thresholds, rollback
  criteria) and every return schema is pinned there — implement them exactly.
- **Do not invent behavior neither doc states.** If something is genuinely ambiguous, STOP and
  flag it — do not guess. (This contract survived five adversarial rounds specifically so two
  builders produce compatible implementations; a real gap is a finding, not a judgment call.)
- **Forced-honest:** a half-wired capability fails loudly / returns an honest error, never fakes
  success. `apply_update` and `check_for_update` stay INERT until Grant embeds the signing key
  (self_update.source) — build them inert; do not stub a fake success.

**Current state:** the Windy Talk voice wedge (engine, brains, agents, hands, auth, telemetry,
Electron client) is BUILT and green on `master`. The control surface is DESIGN ONLY — no code
exists yet. The two frozen artifacts + BUILD_NOTES are currently untracked; add them in your
first PR. Reuse the *patterns* (not the Python code) of `hands/surface.py` (the token/loopback/
Origin/constant-time wall + MCP transport), `hands/tiers.py` (the tier model — the contract's
`tier_resolution` extends it), `hands/backends/` (OS backend + capabilities), and
`agents/connect.py` (`_require_trusted_url` host-pin). Recommended host = the Electron **main**
process (TS) — see BUILD_NOTES §2/§3 for the full map.

**How to work:**
- Build in slice order (BUILD_NOTES §7). **Slice 0 first** = the OS resurrection service +
  heartbeat (the "true floor"). Then slice 1 = supervisor/Layer-1 + get_health/reconnect/
  enter_safe_mode + the recovery coordinator. One PR per slice.
- Write tests to each slice's **acceptance criteria** as you go; the **chaos/fault-injection
  harness** (slice 6, can start alongside slice 1) is how "steamroller proof" becomes a real,
  passing measurement — not a claim.
- **The merge gate is `scripts/ci.sh` green** (GitHub Actions is billing-locked account-wide).
  Run it from the repo root before every merge. If you add a Python module, add it to that
  script's `ruff check` line. **Branch, never commit to `master`; one PR per slice; self-merge
  after a green gate + diff sanity check.** Content-free `control.action` telemetry from the
  first commit. End commits with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

**Landmines (BUILD_NOTES §6):** Electron-from-sandbox needs `--no-sandbox --disable-gpu` +
`dangerouslyDisableSandbox` (black window otherwise); the app is `"type":"module"` (ESM);
SSH-to-the-5090 drops from a sandbox (verify engine-touching tools with a fake); don't copy
`hands/surface.py`'s two known MCP bugs (no `initialize`, `str()`-rendered results); and the
`set_autonomy` / `set_volume(0)` tier interaction is the recurring trap — make `tier_resolution`
the single source of truth and unit-test the full matrix.

**Your first move:** add the three docs to a branch, read them in full, then **post your slice-0
plan and its acceptance-test list before writing code** — a cheap check that your reading of the
frozen contract matches its intent. Then build.
