# Windy Talk — Turnover Letter

**From:** Opus 5 (session "Windy Talk 1", 2026-07-25 → 07-30)
**To:** the next instance
**Repo:** `~/windy/windytalk` · `github.com/sneakyfree/windytalk` · default branch **`master`** (not `main`)

Read this whole file before touching anything. It is written to be atomic: every claim
here was measured, and where I was wrong I say so, because you will otherwise repeat it.

---

## 1. THE SINGLE MOST IMPORTANT THING

**Grant's standing instruction, in his words:** *"Don't go by what the code tells you it's
going to do. Go by what the chimpanzee actually sees on the screen."*

He has been burned repeatedly by agents reporting "357 tests green, ready for launch"
while the app white-screens and every fourth link 404s. He is right, and it happened to me
too — twice in this session I reported a finding that was an artifact of my own tooling.

Concretely, this means:
- **Screenshot the real app** at its real window size (360×620). Chrome headless clamps its
  viewport to ~500px on macOS and *crops the PNG* — it will invent clipping bugs that do not
  exist. Use Electron's `capturePage()` instead; it needs no Screen Recording permission.
- **Transcribe the audio back** to confirm what a listener actually hears. Protocol events
  are not proof.
- **Read the hand-test transcript**, not the code, when diagnosing behaviour.
- Before claiming a test proves a fix, **stash the fix and confirm the test fails.** I caught
  three of my own non-discriminating tests this way, and shipped-then-reverted one fix that
  my test "passed" without.

---

## 2. WHO YOU ARE WORKING FOR

Grant Whitmer — solo founder, non-technical by his own description ("I can't write a single
line of Python"). He asked me to act as **CTO**, which he means literally: make the call,
justify it against doctrine, and tell him plainly when he is wrong.

**He responds well to:** hard numbers, admitting error fast and moving on, disagreeing with
him when you have evidence, and refusing to do something unsafe while offering the nearest
safe alternative.

**He does not want:** hedging, fake certainty, or a survey of options instead of a
recommendation.

He hand-tests personally and reports symptoms in plain language. Those reports are gold —
"it only gets one or two words out", "a glass on my desk cut it off", "it feels like a
lobotomized Opus" each led straight to a real root cause.

---

## 3. THE DOCTRINE — READ IT FIRST, IT DECIDES ARGUMENTS

`~/kit-army-config/doctrine/00-GUIDING-PRINCIPLES.md`

**There are EIGHT.** The file was called `00-SEVEN-GUIDING-PRINCIPLES.md` until 2026-07-27
and both names still exist. I spent four days reviewing against seven and missed #8. Read
the file whose header says EIGHT.

1. God's gift to normies & grandmas — terminal-free, honey-badger stability > features
2. Not beholden to any model — portability is the moat (BYOM via Windy Mind)
3. Build for exponentially smarter LLMs — solve the MINIMUM, stay out of the model's way
4. Minimalist — witch-hunt feature bloat (the OpenClaw cautionary tale)
5. Agent-friendly by design — dumb tools, smart model
6. Roll out the red carpet for bots (Eternitas credentials)
7. Context-refresh-proof — she never forgets, resurrects, picks up mid-thought
8. **THE HIERARCHY:** *Stability > the human's control of their own agent > simplicity >
   capability.* Use this when principles collide. Grant: *"A genius that crashes all the
   time... you're just going to want to go back to something slower and more boring that
   never crashes."*

**P3 and P4 are the ones that will block your work.** Grant explicitly predicted this and
was right: my first big fix stacked a 4th tuning layer on a 3-knob subsystem and had to be
rewritten to *delete* more than it added.

Also read `docs/ADR-058-foundation.md` — the locked invariants. Key line: **"Windy owns the
voice and the hands; the agent and the compute are the user's."** Four pluggable sockets:
brain (Mind), agent (Connect), hands, reach.

---

## 4. CURRENT STATE — EXACT

**Branch `fix/audition-before-supersede`, 15 commits ahead of master, NOT MERGED.**
Everything below is on that branch. Master has #71/#74/#76 merged.

```
ac8eafa wire the slowness sense to real turns and the recovery ladder
ffa9593 Layer 1 learns to notice DEGRADATION, not just death
8d36a12 say it out loud when a different brain is answering
1f1ae1d the conversation now survives a restart (§9 resume, P7)
cb4b901 say "still working on that" instead of sitting silent for 90 seconds
debd104 a glass on the desk should not cut her off, and the room should not talk to her
def5994 stop a slow turn from being killed by the follow-up it provokes
c9c2f82 docs(hands): correct the vision-latency diagnosis
3f01265 docs(hands): AX cannot see web pages — the vision spine is the only path
88eb0de give Windy her voice back, and stop the mic reacting to furniture
98180a9 don't hunt for barge-in while she is silent; raise the brain timeout
11f98a3 tune barge-in from a real room — trust the client's AEC, not our guess
e1c0ea3 close the press_keys bypass of the terminal keystroke guard
5b0592c stop chopping questions in half, and say so when speech is lost
833cb75 audition an utterance before letting it kill the reply in flight
```

**Tests:** 349 python + 262 desktop, ruff + tsc clean. CI runs on a **self-hosted Kit 0
runner** (`runs-on: [self-hosted, linux, x64]` — never `ubuntu-latest`, GitHub's hosted
runners are billing-locked account-wide).

**Open issues:** #72 (engine wedges under `WINDYTALK_MIND_STREAM_TOOLS=1`), #73 (latency
gate missed on CPU). #75 closed by #76.
**windy-stt PR #1** open, unreviewed (unpins the STT lane from Veron).

---

## 5. HOW TO RUN IT (copy-paste)

Env lives in `~/windytalk-cpu/handtest-env.sh`. Three services, in order:

```bash
cd ~/windy/windytalk
set -a; source .env; source ~/windytalk-cpu/handtest-env.sh; set +a
export WINDYTALK_HANDS_AUTOAPPROVE=1        # console prompts break a voice test

# 1. hands (port 8781)
nohup ~/windytalk-cpu/.venv/bin/python -u -m hands >> ~/windytalk-cpu/handtest-hands.log 2>&1 &

# 2. engine (port 8788) — takes ~20s to load whisper+kokoro
nohup ~/windytalk-cpu/.venv/bin/python -u -m engine.server --host 127.0.0.1 --port 8788 \
  >> ~/windytalk-cpu/handtest-engine.log 2>&1 &

# 3. the app
(cd apps/desktop && nohup npx electron . >> ~/windytalk-cpu/handtest-app.log 2>&1 &)
```

**Vision (needed for clicking links) — Veron's ollama over an SSH tunnel:**
```bash
nohup ~/windytalk-cpu/vision-tunnel.sh > ~/windytalk-cpu/vision-tunnel.log 2>&1 &   # self-healing
ssh veron1 'curl -s --max-time 300 http://127.0.0.1:11434/api/generate \
  -d "{\"model\":\"qwen3-vl:32b\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":\"6h\"}"'  # PIN IT
```

**Test venv** (the repo's own venv has no pytest):
`/private/tmp/.../scratchpad/wtvenv/bin/python` — recreate with
`python3 -m venv wtvenv && wtvenv/bin/pip install ruff pytest pytest-asyncio jsonschema numpy websockets`

**Logs to read after every hand test:**
- `~/windytalk-cpu/handtest-2026-07-26.jsonl` — **the important one.** Every turn: heard,
  say_start/end/cancel, barge_in, tool_call, errors. Join say_start↔say_end on `say_id`,
  NOT `turn_id` (say_end carries no turn_id — this confounded my first analysis).
- `handtest-engine.log` — per-turn EOS→first-audio latency
- `handtest-hands.log`, `handtest-app.log`

---

## 6. WHAT WAS BROKEN AND WHY (the catalogue)

Grant's two original symptoms, both now fixed, both with non-obvious causes:

**"It only gets one or two words out."** Not truncation — **self-barge**. `webrtcvad` calls
her own speaker echo "voiced", so the engine thought he was interrupting. Loopback proved
even **5% bleed cut every reply at exactly 0.82s** (600ms grace + 240ms confirm). Fixed
across four commits, ending at: the autonomous detector only runs *while audio is actually
streaming* (11 of 18 cuts fired while she was silent during tool rounds), plus a
self-calibrating echo floor.

**"It answers a question from three questions ago."** Not stale answers — **dropped turns**.
`_start_turn` cancelled the in-flight reply *before* transcribing the new audio, so a cough
killed the answer and then evaporated. 29 of 53 turns produced nothing. Fixed by
auditioning (transcribe first, supersede only if real).

Others found and fixed:
- Client sent `platform:"unknown"` → brain used **ctrl on a Mac** → every "open this website"
  failed. #64 shipped the engine half and left the client half unwired.
- Endpointing at 700ms **chopped his sentences mid-spell** ("wagutank.com, w-h-"). Now 1200ms.
- `press_keys` bypassed the terminal keystroke guard — the model found the workaround
  *unprompted* within seconds of being refused.
- Brain/Hands lamps were the engine websocket under two other labels.
- The system prompt literally said **"Answer in ONE short sentence"** — that was the
  "lobotomized Opus". Written when llama was the brain and every reply was being cut off.
- 48 "didn't catch that" per session from room noise (VAD aggressiveness 2 → 3).

---

## 7. HARD-WON FACTS (each of these cost hours)

- **macOS Chrome exposes NO page content to the accessibility tree**, and there is no switch.
  `AXManualAccessibility` returns `-25205 unsupported`. The **vision spine is the only way to
  click a web link.** It was built and wired all along — it just had no model configured.
- **Vision latency is model *loading*, not inference.** Veron's 32GB can't hold qwen3-vl:32b
  (24GB) and qwen3-coder:30b (21GB) together, so they evict each other: 90s when cold, **11-14s
  when pinned**. `OLLAMA_NUM_PARALLEL=6` inflates residency ~3× (a 6GB model occupied 18.7GB).
  Lowering it would likely end the thrash — **Veron is shared production; that's Grant's call.**
- **`keep_alive` is silently ignored** by ollama's OpenAI-compatible endpoint.
- **Don't use the fast 7B vision model.** It returns *absolute pixels* where the prompt demands
  0-1000 normalized. It fails by clicking the wrong place.
- **Mind silently substitutes models.** On 07-30 every provider errored and it served
  `qwen2.5:7b-instruct` while reporting `claude-opus-4-8`. Grant tested a whole session on the
  wrong brain. The response carries the truth in `model` + `_provider`. Now announced.
- **The engine degrades ~3× over ~13 hours** (13.2s vs 4.5s per turn). **STILL UNEXPLAINED.**
  Restart clears it. Not a simple connection leak (6 turns leak 1 CLOSE_WAIT). Leading
  hypothesis: executor threads held by long brain calls (`DEFAULT_TIMEOUT` is 90s). Untested.
- **The SSH tunnel to Veron dies** and takes `click_element` down *silently* — a dead endpoint
  reads as "element not found". Hence the supervised `vision-tunnel.sh`.

---

## 8. THE CREDENTIAL QUESTION — READ BEFORE TOUCHING IT

Grant asked me to power the brain with his **Anthropic OAuth token** (`sk-ant-oat01-…`, his
personal $200/mo Max subscription). I declined for the *product* case and he pushed back
hard and fairly — he's in a sandbox with one user.

**The resolution:** Mind's Anthropic lane **already uses that token**, so his own testing was
always on his subscription; nothing needed changing. I verified this *after* lecturing him,
which was sloppy — check first.

The line that still holds, and it's **his own doctrine**, not mine
(`~/kit-army-config/shared/battle-scars/anthropic-oauth-direct-api.md:92`):

> *"Shipping to other users via spoofed Claude-Code identification at scale: clean ToS
> violation. Anthropic could revoke. Use per-user `sk-ant-api03-…` keys for any multi-tenant
> product."*

Revocation would also take down OC1, Herm 0, Herm OC1 and Herm OC2 — they share it. **Launch
checklist item, not a today problem.** Do not re-litigate it with him; he has heard it.

Current brain: `claude-opus-4-8` via a **credentialed** `wm_` Mind key at
`~/windytalk-cpu/.mind-opus-key` (the old dev key was free-tier and silently fell back).

---

## 9. TUNABLES NOW IN FORCE — AND WHOSE EARS SET THEM

| knob | value | who decides |
|---|---|---|
| `WINDYTALK_ECHO_MARGIN` | 3.5 | **Grant's ears.** Hard to interrupt → lower. Cuts herself off → raise. |
| `WINDYTALK_VAD_AGGRESSIVENESS` | 3 | **Grant's ears.** Room talks to her → raise. Quiet questions missed → lower. |
| `silence_ms` (client hello) | 1200 | feels sluggish to *start* → lower |
| `_BARGE_GRACE_MS` | 300 | echo-floor calibration window |
| `_BARGE_AUTONOMOUS_CONFIRM_MS` | 200 | rule 5 only; client-signalled stays at the contract's 60 |
| `WINDYTALK_SUPERSEDE_STUCK_MS` | 25000 | must stay **above** a realistic turn |
| `_WORKING_AFTER_S` | 18.0 | when she volunteers "still working on that" |
| `DEFAULT_TIMEOUT` (mind.py) | 90.0 | brain call ceiling |

**`WINDYTALK_MIND_STREAM_TOOLS` must stay UNSET.** It wedges the engine every ~7 turns
(windy-mind#75 not shipped). Proven: 23/26 turns with it, 12/12 without.

---

## 10. WHAT I'D DO NEXT

1. **Let it run overnight and see the slowness detector trip.** It's wired but has never fired
   on a real degradation, because I keep restarting the engine before he tests. That's the run
   worth having.
2. **The visible restart button (#5 on our plan).** The ladder exists (reconnect →
   deep-reconnect → safe mode → factory reset); rungs 1-3 should be silent, only rung 4 asks.
   P8 says grandma should always have this escape hatch.
3. **Root-cause the 13-hour degradation.** Instrument executor thread count and brain-call
   duration over a long run.
4. **Merge `fix/audition-before-supersede`** once he's happy — 15 commits is a lot to sit
   unmerged.
5. **#73 latency** is the real shippability question: p90 2.7-4.3s CPU vs a 1200ms gate.
   Decide honestly — lease the GPU lane, or re-cut §0.1 for a CPU tier.

**Not started:** the wake word. `wakeword/train.py` **does not train anything** — it prints a
recipe and returns 0. Both the ecosystem audit and I called it "written, just needs GPU time".
It isn't written. openWakeWord 0.4.0's pip package has no trainer, and `loadWakeDetector()` is
unimplemented, so a trained ONNX wouldn't wake anything yet. Veron's env IS now ready (torch
`2.11.0+cu128` — the old `cu124` didn't support the 5090's sm_120) and 200 verified "Hey Windy"
positives exist at `/tmp/hey_windy_pos` on Veron.

---

## 11. MY OWN FAILURE MODES — WATCH FOR THESE IN YOURSELF

I got things wrong in a consistent, predictable pattern. Assume you will too.

- **Blind string-replace edits fail silently.** One of mine matched nothing and reported
  success; I caught it only by counting call sites (2 when it should have been 3). Another
  inserted a statement between an `if` body and its `else`. **Verify structural edits by
  reading them back.**
- **Tests that don't discriminate.** I wrote a fix + test, then found the test passed on
  *unfixed* code (CPython refcounting made my `closing()` fix a no-op). I reverted it rather
  than ship an unproven change. **Always stash the fix and watch the test fail.**
- **Thresholds chosen by taste, not measurement.** I set the slowness trip at 3× when the real
  event was **2.93×** — my detector would have missed the exact bug it was written for.
- **Tooling artifacts read as bugs.** The Chrome-headless viewport clamp. Also: I once reported
  the socket row as clipped, then measured and it fit.
- **Tests polluting the real machine.** Conversation persistence made every test write
  `t.json` into Grant's actual home dir and start resuming each other's conversations.
- **I killed the engine mid-hand-test twice** with a hung restart command. Check whether he's
  testing before you restart anything.

---

## 12. THINGS THAT ARE GOOD — DON'T "FIX" THEM

- The repo is **genuinely well-engineered**: contract-first with frozen seams, 349 tests that
  drive a real state machine, an honest culture (`auth/eternitas.py` refuses to fake an
  entitlement; the server docstring admitted resume was unimplemented).
- The **autonomic supervisor already exists** — Layer 1 crash detector, resurrection
  installer/watcher, LKG, coordinator, safe mode. It's disabled in dev **on purpose**
  (arming it registers a real launchd unit pointing at the repo). Packaged builds arm it
  automatically. I recommended flipping that and was wrong; the guard is correct.
- **Opus fixed the honesty problem completely.** Real quotes from the logs:
  *"So honestly, you can see it better than I can right now. What are you looking at?"*
  *"I don't know that there's a 'kit army config' repo… that's your description, not
  something I've verified."*
  llama confidently invented answers; Opus refuses.

---

*Windy Talk is close. The two bugs that ruined his hand tests are fixed and proven under the
conditions that reproduce them. What's left is latency, the restart button, and the wake word
— and the honest gap between here and shippable is smaller than it looks.*
