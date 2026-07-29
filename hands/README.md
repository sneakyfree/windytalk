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

**Cost, measured on this setup — this is the real tradeoff:**

| model | latency | result |
|---|---|---|
| `qwen3-vl:32b` cold | 77 s | correct |
| `qwen3-vl:32b` warm | 42 s | correct |
| `qwen2.5vl:7b` | 21 s | **missed the link** |

42 s to click one link is not conversational. The 7B is fast and wrong, which is worse
than slow and right. Before reaching for a different model, note `MAX_TOKENS = 8000` in
`vision.py`: qwen3-vl is a thinking model and is being given room to reason at length
for what is ultimately a coordinate — that is the first thing to tune.

**Screen Recording permission is required** for any of this: without it `screencapture`
silently writes a wallpaper-only image and the vision spine is blind while still
reporting success. `backends/macos.py::_screen_recording_ok()` checks it; the
screenshot tool says so in its result rather than lying.
