# Windy Talk — Gap-Closing Build Plan (locked 2026-07-12)

Every load-bearing assumption below was validated by live testing on real fleet
machines across GNOME-Wayland (Windy 0 5K, OC3 1080p), X11 (OC2/OC4 Ubuntu
24.04), macOS 13 (OC5 Retina 5K), and Windows 11 Pro (GrantW). Evidence lives in
memory `windytalk-live-stress-2026-07-12`. This plan turns that evidence into a
sequenced build that takes every red/yellow to green.

## The validated architecture

- **Universal spine = screenshot → vision model → coordinate-click.** Works on
  every platform and *every browser*. Proven end-to-end on real Chrome (OC2).
  The vision model runs **locally on the 5090** (zero per-use cost).
- **AT-SPI accessibility = the fast lane**, used where it works (native apps,
  Firefox, Electron/Windy Word): faster and more precise than vision, coord-free.
  Proven: clicks + reads + screen coords on GNOME-Wayland.
- **Chrome/Chromium are invisible to AT-SPI** and can't be woken at runtime →
  they *must* use the vision spine. This is why the spine is mandatory, not the
  accessibility path.
- **Per-platform pointer backends** (all proven): xdotool (X11), the
  RemoteDesktop **portal + one-time grant** (GNOME-Wayland — ydotool's pointer
  is ignored by Mutter), cliclick (macOS), SendKeys/.NET (Windows).
- **Keyboard** = ydotool/native everywhere, but with a **focus-target +
  terminal-guard** (the #1 safety fix — mis-focused keystrokes leaked into a
  live terminal during testing; multiple live terminals per box is the norm).
- **Screenshot** needs the display awake; on GNOME use the **portal/flameshot**
  (grim is dead on GNOME), native elsewhere. AT-SPI is the default "sense";
  screenshots are on-demand (they cost ~1.4s vs AT-SPI's ~0.02s).

## Build phases (sequenced to retire the scariest risk first)

### Phase 0 — Safety & honesty foundation (blocks everything)
1. **`type_text` focus-guard + terminal-refuse.** Resolve the focused window via
   AT-SPI, verify it's the intended target, refuse if it's a terminal/unknown.
   Kills the keystroke-leak catastrophe; prerequisite for safely driving apps.
2. **Capabilities report *function*, not binary presence** — a tiny real probe
   at startup (a no-op capture / AT-SPI query), so the app stops over-claiming.

### Phase 1 — The pointer engine (closes the mouse reds/yellows)
3. One `Pointer` interface, four validated backends. Build the
   **coordinate-space mapper** here (AT-SPI points ↔ screenshot px ↔ physical px
   ↔ macOS 2× — a lurking cross-platform bug the moment we click a coordinate).
4. GNOME-Wayland portal backend: create session, `persist_mode=2` so the grant
   is remembered, absolute motion via a linked ScreenCast stream.

### Phase 2 — The vision loop (the browser answer)
5. `screenshot → local 5090 vision model → target box → coordinate-click`, with
   the **AT-SPI fast-lane shortcut**: if the target app is accessible
   (native/Firefox/Electron), find + click via AT-SPI and skip the vision cost.
6. Handle the AT-SPI action-name variance (`click`/`jump`/`activate`) and the
   "found but not actionable" case (get its box → coordinate-click).

### Phase 3 — Screenshot & sensing
7. GNOME: portal-first, flameshot bundled as backup (per the packaging
   doctrine). AT-SPI as the default sense; screenshots on demand only.

### Phase 4 — First-run wizard (the permission reality)
8. Per-OS one-time grants, each with a live self-test proving it worked:
   - **GNOME-Wayland:** the "allow remote control" portal grant (remembered) +
     the uinput/ydotoold keyboard setup.
   - **macOS:** Accessibility + Screen Recording (TCC).
   - **Windows:** nothing beyond install (SendKeys works in an active session).
   - **X11:** works out of the box.

### Phase 5 — The acceptance gauntlet (the definition of green)
9. A repeatable automated test that drives *real* apps and passes only on
   screenshot-verified success: the calculator, a **Chrome button via the vision
   spine**, a Firefox element via AT-SPI, and one real workflow. Run it on X11,
   GNOME-Wayland, macOS, and **Windows (via the VNC channel, screen awake)**.

## Remaining test holes (need conditions, not code)

- **Windows live GUI** (95% of users): VNC channel is live; needs the laptop
  screen awake to run the gauntlet. Highest-priority remaining validation.
- **Real web-app buttons via AT-SPI** (Gmail Send, etc.): needs Phase 0's
  focus-guard first, then probe — but the vision spine already covers these
  regardless, so this only decides how often the fast-lane applies.
- **Fresh-machine permission-denied flows**: every "works" so far was on a
  machine already granted; the wizard's deny→grant path needs a fresh box.

## Why this beats the 10-minute version

Riley Brown's demo ran only the vision spine on one cooperative platform. This
plan keeps that spine as the universal fallback *and* adds the accessibility
fast-lane, per-platform pointer correctness, keyboard safety, and a real
permission wizard — the 30% a demo skips, which is exactly the part that makes
it trustworthy on a stranger's machine.
