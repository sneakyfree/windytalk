# The acceptance gauntlet (GAP_CLOSING_PLAN Phase 5)

A repeatable test that drives **real apps** through the hands backend and passes
only on **screenshot-verified** success — after each scenario a local vision
model answers a strict yes/no about what is visibly true. Nothing passes on a
tool's return string alone; the pixels are the verdict. The same 5090 model that
powers the click spine is the judge, so one live lane proves both directions.

## Run it

On the target box (repo or payload root importable), pointed at the local model:

```bash
WINDYTALK_VISION_URL=http://10.10.0.6:11434/v1 \
WINDYTALK_VISION_MODEL=windy-locator \
python3 -m gauntlet.runner [--only calculator,workflow] \
                           [--known-red chrome-vision] [--json out.json]
```

`scripts/run_gauntlet.sh`-style wrappers should harvest the live desktop session
env from the compositor process — an SSH shell does **not** inherit `DISPLAY` /
`WAYLAND_DISPLAY` / `DBUS_SESSION_BUS_ADDRESS`, and the gauntlet drives the real
screen.

## Scenarios

| id | what it proves |
|----|----------------|
| `calculator`    | `click_element`'s AT-SPI fast lane: `7 × 3 =` → the display shows **21** |
| `chrome-vision` | the **vision spine** — a real Chrome button (invisible to AT-SPI on Linux, so the click *must* ride screenshot→model→coordinate) turns the page green with `CLICKED OK` |
| `firefox-fast`  | the AT-SPI fast lane on a real browser (Linux only) |
| `workflow`      | a real editor: type a sentence with the focus-guard on → the words are on screen |

Each result is `pass` / `fail` / `skip` (app absent) / `known-red` (a documented
platform finding, e.g. GNOME 46 portal `devices=0` — reported loudly, doesn't
fail the run). Exit 0 = nothing failed.

## The model

Served by ollama on the 5090 (Veron) as `windy-locator` — qwen3-vl:32b with
`num_ctx 16384` (the 4096 default truncated long locates). Two hard-won
requirements live in `hands/vision.py`: **normalized 0–1000 bounding boxes** (not
absolute pixels — the serving stack resizes the image, so pixel answers land in
the wrong space) and a generous **`max_tokens`** (thinking models spend it on a
hidden reasoning channel before answering).

## Platform reality (see docs/GAP_CLOSING_PLAN.md Phase 5 for the full matrix)

- **X11:** the clean full-green target (native tools + portal + vision all work).
- **GNOME-Wayland:** capabilities proven; a focus thief can steal the active app
  mid-scenario (no external window-activate on Wayland), and GNOME 46 refuses the
  portal pointer (`devices=0`) so vision-spine clicks there are `known-red`.
- **macOS:** needs the Screen Recording TCC grant — without it `screencapture`
  redacts app windows, so verification can't see anything (the wizard says so).
- **Windows:** must run **inside the user's interactive session** (Session-0
  isolation makes the desktop invisible to sshd) with the panel awake.
