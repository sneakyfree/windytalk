# hands/ — the Surface socket · **Python**
`surface.py` = local HTTP + MCP control surface (windyword.py co-tenant pattern, ADR-058 D4).
`tiers.py` = §9 trust tiers (auto-allow / ask-first / always-confirm). `backends/` = linux (ported
from reference/hands.py), macos, windows, windyhand. Every action: human path AND agent path, shared state.

## Clicking things inside a web page (measured 2026-07-28, macOS + Chrome)

**The accessibility tree cannot see web content, and there is no switch to make it.**
`read_screen` on a Chrome window returns the browser chrome only — Back, Forward, the
address bar, extensions — and nothing from the page. Not the headings, not the links.

The old workaround (`AXManualAccessibility`, which used to force Chromium to build its
AX tree) is gone: setting it returns `-25205 kAXErrorAttributeUnsupported` on current
Chrome, both through System Events (`-10006`) and through the raw AX API. Don't spend
time on it again.

So **the vision spine in `vision.py` is not a fallback for web pages — it is the only
path.** `click_element` already routes to it when AX comes up empty
(`backends/macos.py`, "AX couldn't see it (Chrome, canvas UI) → the vision spine").
That code was written and wired long before this was measured; what it lacked was a
model to ask. Configure it:

    WINDYTALK_VISION_URL=http://127.0.0.1:11434/v1   # OpenAI-compatible
    WINDYTALK_VISION_MODEL=qwen3-vl:32b
    WINDYTALK_VISION_TIMEOUT=120

Veron's ollama serves this; 10.10.0.6 is not directly reachable from the Mac mini, so
tunnel it: `ssh -N -L 11434:127.0.0.1:11434 veron1` (veron1 hops via Kit 0).

**Verified end to end:** with vision configured, `click_element{"label":"Learn more"}`
on example.com located the link visually and clicked it, and the browser navigated to
`iana.org/help/example-domains`. Without it, the same call returns "Couldn't find a
clickable element".

**Cost — and the real cause, which is NOT the prompt.**

First measurement said 42s warm / 77s cold, and blamed `MAX_TOKENS = 8000` letting a
thinking model ramble. That was wrong: the model emits ~216-356 completion tokens.
Instrumenting the call directly showed where the time actually goes.

| condition | latency | result |
|---|---|---|
| `qwen3-vl:32b`, model NOT resident | 77-90 s | correct |
| `qwen3-vl:32b`, model pinned in VRAM | **11-14 s** | correct, 3/3 |
| `qwen2.5vl:7b` | 6-8 s | **unreliable — see below** |

**Nearly all of the 90 s was ollama loading the model**, not inference or prompt
processing. Veron's GPU is 32 GB; `qwen3-vl:32b` occupies ~24 GB resident and
`qwen3-coder:30b` ~21 GB, so the two cannot co-reside and evict each other on every
switch. Whichever model the *other* consumer wants, the next vision call pays a full
21 GB reload.

Two things worth knowing before tuning this:

- **`keep_alive` cannot be set from the OpenAI-compatible endpoint.** Ollama accepts
  the field in `/v1/chat/completions` without error and ignores it — the expiry moved
  90 s, not the 2 h requested. It has to come from `OLLAMA_KEEP_ALIVE` on the server
  (already `30m` on Veron) or a native `/api/generate` preload.
- **`OLLAMA_NUM_PARALLEL=6` inflates residency roughly 3x.** `qwen2.5vl:7b` is a ~6 GB
  model and was measured occupying **18.7 GB** resident, because ollama sizes KV cache
  for six concurrent slots. For a single-user vision workload this is most of why
  nothing co-resides. Lowering it would likely let a vision model and a coder model
  share the card — but Veron is a shared production box (the windy-stt lane lives
  there too), so that is Grant's call, not a change to make quietly.

**Don't reach for the 7B to go faster.** It is quicker but does not follow the output
contract: on a full-desktop screenshot it returned `x1: 1167` — absolute pixels, when
the prompt asks for 0-1000 normalized — and emitted malformed JSON with stray bare
numbers. On a cropped image it normalized correctly. It is a formatting problem, not
only a seeing problem, and silently produces coordinates that land in the wrong place.

**Screen Recording permission is required** for any of this: without it `screencapture`
silently writes a wallpaper-only image and the vision spine is blind while still
reporting success. `backends/macos.py::_screen_recording_ok()` checks it; the
screenshot tool says so in its result rather than lying.
