# Windy Talk — Builder Kickoff Prompt (corrected 2026-07-08)

> Paste everything below the line into a fresh Fable instance to launch the build.
> Launch it from `~/Desktop/Grant's Folder/windytalk` (the memory files are read by
> absolute path, so the launch directory no longer matters for them).

---

You are building Windy Talk — Platform 14 of the Windy ecosystem, the universal
voice layer for AI agents. This is a from-scratch, highest-quality rebuild to a
locked architecture. Quality and correctness outrank speed entirely.

Before writing a single line of code, read these in order (all on this machine):
1. ~/Desktop/Grant's Folder/windytalk/docs/DNA_STRAND_MASTER_PLAN.md   ← your genome; follow it exactly
2. ~/Desktop/Grant's Folder/windytalk/docs/ADR-058-foundation.md        ← the locked invariants
3. ~/Desktop/Grant's Folder/windytalk/VISION.html                       ← the North Star
4. the rest of the ~/Desktop/Grant's Folder/windytalk repo              ← the REFERENCE PROTOTYPE. You PORT proven blocks from it; you NEVER build on it.
5. these two memory files, by ABSOLUTE path (they do NOT auto-load — memory is
   keyed to the launch directory):
   /home/grantwhitmer/.claude/projects/-home-grantwhitmer/memory/project_windytalk_vision.md
   /home/grantwhitmer/.claude/projects/-home-grantwhitmer/memory/project_windy_jarvis_build.md

Facts verified live on 2026-07-08 (already folded into the genome — trust these
over any stale text you encounter elsewhere):
- Windy Mind's chat route is POST api.windymind.ai/v1/chat (OpenAI-compatible,
  SSE via stream:true). The /v1/chat/completions alias is ALSO live (deployed
  2026-07-08), so OpenAI SDKs pointed at base_url .../v1 work unchanged.
  Auth is required (401 bare); mint a dev key via POST /admin/keys (lockbox creds).
- windy-connect is NOT on this machine: `pip install windy-connect` (0.3.1) or
  clone sneakyfree/windy-connect.
- The Windy Fly bridge lives at src/windyfly/bridge/uds_server.py in
  sneakyfree/windy-agent (local clone: ~/Desktop/Grant's Folder/windy-agent).
- The telemetry ingest admin.windyword.ai is up.
- ⚠ GitHub Actions is BILLING-LOCKED account-wide on sneakyfree (since ~Jul 4;
  every workflow refuses to start). Until Grant unlocks it: run lint/typecheck/
  tests LOCALLY as the merge gate (Task 0.6's CI config still gets written —
  it just won't execute yet), and treat "green CI" in the plan as "green local
  run of the same commands."

How you work (from the plan's prime directives):
- Task 0.0 FIRST: finish the authed dependency reality-check and write
  docs/PROBE_RESULTS.md before freezing any contract.
- Contracts before code. Freeze the three seam schemas (voice-session.v1,
  hands.mcp.v1, telemetry.v1) after 0.0 — and run 0.2's two-agent adversarial
  read literally before declaring a contract frozen. Frozen ≠ infallible:
  additive → v1.1 via PR; breaking → v2 + tell Grant.
- One atomic task at a time. Prove its acceptance check. Never proceed on red.
  Never batch. Two standing exceptions the plan grants you: the wake-word
  training pipeline (1.6) runs as a background track from Phase 0, and
  independent VERIFICATION (subagent reviews, probes) may run in parallel —
  implementation stays serial.
- Build to ADR-058, not to the prototype. When they disagree, ADR-058 wins.
- The feel is the product: the §0.1 latency table is a numeric release gate.
  Protect latency and interruption quality above every feature. Barge-in must
  work through speakers (AEC per the contract), not just headphones.
- Feature branches + PRs; self-merge on green (do not stall on review). Emit
  content-free telemetry from the first commit.
- Repo transition is Task 0.1: tag prototype-v0, move the prototype to
  reference/, scaffold the §2 tree at root — same repo, sneakyfree/windytalk.

Confirm you've read and understood the genome: summarize the four sockets and
the port-vs-rebuild line in one paragraph. Then begin Phase 0 at Task 0.0, and
report the PROBE_RESULTS.md findings to Grant before moving to Task 0.2.
Do not skip ahead. /effort max
