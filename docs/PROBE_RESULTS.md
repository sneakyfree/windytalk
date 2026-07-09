# Task 0.0 — Dependency Reality-Check: PROBE RESULTS

**Probed live: 2026-07-09 (~04:00 UTC), from Windy 0 (Fedora 44).**
Authed half of the reality-check; unauthenticated probes were done 2026-07-08 and are
already folded into the genome. Every seam below was exercised against production —
no docstrings trusted. **No secrets in this file**; key/token locations noted instead.

## Verdict

| Seam | Status | One-liner |
|---|---|---|
| Windy Mind `/v1/chat` (authed + SSE) | 🟢 | Streams for real — byte-level proof, first byte 0.6 s on fast models |
| `/v1/chat/completions` alias | 🟢 | Live, same response shape |
| `/v1/models` | 🟡 | Live, 15 models — but returns a **bare JSON list**, not OpenAI's `{"data":[...]}` |
| windy-connect pairing | 🟢 (dry-run depth) | Live orchestrator, device code minted; full pairing needs a browser + has one known upstream gap |
| Windy Fly bridge `agent.respond` | 🟢 | Live round-trip against Windy 0's real soul in 6.5 s; confirmed non-streaming |
| Telemetry ingest `admin.windyword.ai` | 🟢 | Event accepted (HTTP 202); required fields discovered and recorded below |

No blockers for Phase 0. Three findings feed directly into contracts and Phase 1 tasks
(marked ⚠ below).

---

## (a) Windy Mind — key mint + `/v1/chat` + SSE

- **Key mint:** `POST https://api.windymind.ai/admin/keys`, `Authorization: Bearer $MIND_ADMIN_TOKEN`
  (token: lockbox → "MIND_ADMIN_TOKEN" entry). Required body fields: `name`, **`subject_email`**
  (`tier`, `note` optional). Omitting `subject_email` → 422.
  Minted dev key **key_id `00df6838`**, subject `windytalk-dev@windyword.ai`, tier
  `credentialed` (5000 req/day), expires 2027-07-09, `issued_by: windy-connect-orchestrator`.
  **Value stored in `windytalk/.env` as `WINDY_MIND_DEV_KEY` (gitignored). Per the genome,
  this key powers Tasks 1.1–1.6 and is deleted from code + disk at Task 1.7.**
- **Non-streaming round-trip:** `POST /v1/chat` with `model:"auto"` → 200, routed to
  `claude-opus-4-7`, exact-echo prompt returned correctly. OpenAI response shape
  (`choices[0].message.content`, `finish_reason`, `usage`).
- **SSE (`stream:true`) — the "does it actually stream" question: YES.**
  Measured by reading the socket 1 byte at a time (no client buffering possible):
  - `llama-3.3-70b-versatile`: first byte **0.60 s**, ~40.7 KB of events flowing
    continuously, done 1.16 s.
  - `gemini-2.5-flash-lite`: first byte **0.77 s**, bytes spread across generation
    (0.77 → 1.71 s) — incremental, not a tail-flush.
  - Chunks are OpenAI-style `chat.completion.chunk` events, `data: [DONE]` terminator.
  - Caveat for latency planning: first-event time is dominated by the **upstream model's
    TTFT**, not Mind. Opus 4.7 ≈ 3 s to first token; `gemini-2.5-flash` (non-lite) hit an
    11 s outlier once. **For the voice path, the brain default must be a fast-TTFT model**
    (llama-3.3 / flash-lite class) or EOS→first-audio ≤ 1.2 s is unreachable regardless
    of engine quality. Model choice is a latency-budget line item, not a quality knob.
- **Alias:** `POST /v1/chat/completions` → 200, identical shape. OpenAI SDKs with
  `base_url=https://api.windymind.ai/v1` work for chat.
- ⚠ **`GET /v1/models` returns a bare list** (`[{"id":...}, ...]`), **not** OpenAI's
  `{"object":"list","data":[...]}` envelope. `client.models.list()` in official SDKs may
  choke. Windy Talk's `brains/mind.py` must parse the bare list; noted as a candidate
  windy-mind fix (not required for the wedge). 15 models live, incl. `claude-opus-4-7`,
  `claude-sonnet-4-6`, `grok-4`, `gpt-5`, `llama-3.3-70b-versatile`, `gemini-2.5-flash`,
  `gemini-2.5-flash-lite`, `gpt-oss-120b`.

## (b) windy-connect — install + pairing dry-run

- `pip install windy-connect` → **0.3.1** installs clean (Python 3.14 venv, Fedora 44).
- ⚠ **The console script is `windy`, not `windy-connect`.** Subcommands: `connect`,
  `status`, `disconnect`, `doctor`, `refresh`, `version`. `connect` supports `--dry-run`,
  `--mock`, `--no-eternitas` / `--with-eternitas` (Tier 1 vs Tier 2), `--force`.
- **Dry-run result:** agent detection works (found OpenClaw, Claude Code, and a generic
  `~/.windy/bundle.json` on this box; correctly reported Hermes/Himalaya absent).
  Selecting `generic` reached the **live orchestrator `https://api.windyconnect.com`**
  and minted a real device-pairing code (`open /pair in a browser, enter code`).
  Stopped there — completing pairing requires a human browser step.
- ⚠ **Known upstream gap for Task 1.3** (from the lockbox, not re-probed): the
  `windy-connect-orchestrator` Worker does **not** have `MIND_ADMIN_TOKEN` set, so
  hatch-time Mind-key provisioning through Connect may fail even after a successful
  browser pairing ("Connect prod is stale/sandbox" note, 2026-07-02). Re-verify at 1.3;
  budget a day for orchestrator fixes if pairing yields a bundle without a working
  Mind key.

## (c) Windy Fly bridge — `agent.respond` live round-trip

- **The bridge is a separate process**, not part of the channel runtimes:
  `python -m windyfly.bridge.uds_server`. On Windy 0, `windy-0@telegram` +
  `windy-0@matrix` were running but **no bridge socket existed** until I started one.
  Boot requirements (from `_serve_forever`): cwd containing `windyfly.toml`
  (production agent dir: `~/.local/share/windyfly/agent/`), env loaded
  (`~/.windy/windy-0.env`), then it claims a runtime slot via Mind (ADR-051 A.5).
- **Socket:** default `<tempdir>/windyfly.sock`; `WINDYFLY_IPC_PATH` env overrides
  (used a scratch path for the probe). UDS on Linux/mac, TCP on Windows.
  Protocol: newline-delimited JSON, `{"method":"agent.respond","params":{"message":...,
  "session_id":...}}` → `{"result":{"response":"..."}}`.
- **Live result:** probe message → Windy 0's actual soul → `PROBE OK` in **6.5 s**;
  no runtime-claim conflict with the two channel runtimes; clean shutdown released the
  claim (`POST /v1/runtime/release` 200).
- ⚠ **Two adapter-facing facts for Task 1.2:**
  1. **Non-streaming confirmed** — one blob after full generation. 6.5 s to first
    audio-able text is unusable for voice; `agent.respond_stream` (the planned PR to
    `sneakyfree/windy-agent`) is genuinely load-bearing, not nice-to-have.
  2. **Replies carry a status-banner prefix** (`[🪰 Windy Fly · Jul 09, 12:00 AM · 🟢 99%]`)
    — the windyfly adapter must strip it (or the bridge grows a `plain:true` param)
    before TTS, or the agent will read its own dashboard aloud every turn.
- Also relevant to 1.2/1.5: Windy Talk must **own bridge lifecycle** (spawn it against
  the user's agent dir if no socket exists) — we cannot assume a running bridge.

## (d) Telemetry ingest — event landed

- `POST https://admin.windyword.ai/v1/events`, per-emitter bearer token
  (probe used the `verify-oc5` token; values live in kit-army-config
  `secrets/windy-admin/ingest-tokens.env` on OC5 — **Windy Talk needs its own emitter
  token minted before Task 0.4's live test**; rotation runbook in that dir's README).
- **Envelope discovered by 422-probing:** body is `{"events":[...]}` (batch array, even
  for one event). Required per event: **`service`, `event_type`, `actor_type`**
  (`platform`, `session_id`, `ts`, `dur_ms` accepted alongside).
- **Result:** `{"accepted":1}`, HTTP **202**. These field names are inputs to
  `contracts/telemetry.v1.json` (Task 0.3) — the contract must match the live ingest,
  and the ingest's content-key rejection (422 on `transcript`/`message`) still needs a
  live negative test at 0.4.

---

## Standing environment facts (verified this session)

- **GitHub Actions billing-locked** account-wide → merge gate = green local run of the
  CI commands (genome Task 0.6 note stands).
- Windy 0's agent runs as templated user units `windy-0@{telegram,matrix}.service`
  (old `windy-0.service` is masked); `EnvironmentFile=~/.windy/windy-0.env`,
  `WorkingDirectory=~/.local/share/windyfly/agent`.

**Next per the genome: report these findings to Grant, then Task 0.1 (repo transition)
and Task 0.2 (freeze voice-session.v1).**
