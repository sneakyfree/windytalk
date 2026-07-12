# Hands — cross-OS portability matrix

The hands (desktop control) is the only genuinely OS-coupled layer in Windy Talk;
everything else is remote (engine) or cross-OS by construction (Electron client).
Each OS implements the same 12-tool `HandsBackend` ABC. Verified live on the real
fleet 2026-07-10 via `scripts/os_probe.py`.

| | Fedora (Windy 0) | Ubuntu 24 (OC2) | macOS 13 (OC5) | Windows 11 (GrantW) |
|---|---|---|---|---|
| backend | `linux` | `linux` | `macos` | `windows` |
| Node / Python | v22 / 3.14 | v22 / 3.12 | v22 / 3.14 | v24 / 3.14 |
| all 12 tools supported | ✅ | ✅ | ✅ | ✅ |
| `list_apps` (live) | ✅ | ✅ AT-SPI | ✅ System Events | ✅ ran¹ |
| `read_screen` (live) | ✅ | ✅ | ✅ | ✅ ran¹ |
| `screenshot` (live) | ✅ | headless² | ✅ | ✅ saved |

¹ Over SSH the Windows/macOS process runs without an interactive desktop, so
`list_apps`/`read_screen` return empty — the *code path executes cleanly*; on a
logged-in console it sees real windows.
² Ubuntu probe ran over headless SSH (`XDG=tty`); `scrot`/`flameshot` need a
display. `list_apps`/`read_screen` work anyway (AT-SPI uses the session bus).

## What each backend uses

| Tool | Linux | macOS | Windows |
|---|---|---|---|
| open_app | gtk-launch / binary | `open -a` / System Events | `Start-Process` |
| open_url / web_search | xdg-open | `open` | `Start-Process` |
| type_text / press_keys | ydotool (Wayland) / xdotool (X11) | cliclick | SendKeys |
| mouse_click / scroll | ydotool / xdotool | cliclick | user32 mouse_event |
| read_screen / click_element | AT-SPI2 | System Events (AXUIElement) | UIAutomation |
| screenshot | scrot / flameshot | screencapture | .NET CopyFromScreen |
| run_shell | bash | zsh | powershell |

## Setup notes per OS

- **macOS:** `brew install cliclick`; grant the controlling app Accessibility +
  Screen-Recording permission (System Settings → Privacy & Security). The
  read/click tools return a clear "permission not granted" message until then.
- **Windows:** nothing to install — UIAutomation, SendKeys, .NET ship with Windows.
- **Linux:** ydotool needs its uinput socket (`/run/user/<uid>/.ydotool_socket`);
  X11 vs Wayland is auto-detected (`WINDYTALK_INPUT` overrides).

`GET /capabilities` on the hands surface reports the live blade-list for the
current machine, so the agent knows what it can do before it tries.

## type_text focus-guard (GAP_CLOSING_PLAN Phase 0 #1)

Keystroke injection is the one action that lands wherever focus happens to be —
live testing proved a mis-focused `type_text` can submit text into another
running terminal session. So every backend resolves the FOCUSED window before
typing (Linux: AT-SPI active frame; macOS: System Events frontmost process;
Windows: UIAutomation FocusedElement) and **refuses** — typing nothing,
returning `ok:false error:"refused: ..."` — when:

1. focus can't be resolved (never type blind);
2. the focused app is a terminal (matched on app name / AT-SPI focused-element
   role, never the window title; `run_shell` is the sanctioned shell path);
3. the optional `target` arg (app name or title fragment) doesn't match the
   focused window.

Agents should pass `target` whenever the text is meant for a specific window.
On success the result reports where it typed ("Typed 12 characters into
firefox"). `WINDYTALK_TYPE_GUARD=off` disables the guard — dev/chaos only.
`press_keys` is not yet guarded (Phase 0 scope decision — revisit if a live
finding demands it).

## The pointer engine (GAP_CLOSING_PLAN Phase 1)

Pointer mechanisms are session-aware and deliberately separate from the
keyboard chain, because the live matrix split them: Mutter honors ydotool's
virtual KEYBOARD but silently ignores its virtual POINTER on every
GNOME-Wayland box — a phantom prong that reports success while the cursor
never moves, which a fallback chain cannot detect.

| Session | pointer chain (`WINDYTALK_POINTER` overrides) |
|---|---|
| X11 | xdotool → ydotool → portal |
| GNOME-Wayland | **RemoteDesktop portal only** (ydotool = phantom, excluded) |
| other Wayland (wlroots…) | portal → ydotool → xdotool |

The portal path (`hands/backends/portal.py`) holds one remembered
`org.freedesktop.portal.RemoteDesktop` session: a one-time "allow remote
control" grant on first use, persisted via `persist_mode=2` + a single-use
restore token (`~/.windytalk/portal_restore_token`, refreshed every Start),
absolute motion through a linked ScreenCast stream, buttons/axis as evdev
events. The capability probe is a real bus property read that never pops the
dialog.

**Coordinate spaces** (`hands/coords.py`): `mouse_click` coordinates are
pixels of the most recent screenshot, mapped to the pointer's logical space
using the recorded capture geometry (PNG IHDR size vs logical screen size —
portal stream on Wayland, xdotool geometry on X11, Finder desktop bounds on
macOS where Retina captures are 2x points). No screenshot on record → identity
(native screen coordinates). AT-SPI-derived coordinates are already logical
and go through `_click_logical`, bypassing the mapping. Single-monitor
assumption in v1.

## The click ladder + vision spine (GAP_CLOSING_PLAN Phase 2)

`click_element` is a ladder, cheapest-first:

1. **AT-SPI fast lane** — find by label in the active app's tree (budget sized
   for web documents) and run its most click-like named action; action names
   vary by toolkit (`click`/`press`/`jump`/`activate`…), so a recognized name
   is preferred over blind action 0.
2. **Found but not actionable** — click the center of its extents (already
   logical; bypasses capture mapping). Extents that look window-relative
   (GTK4-on-Wayland reports 0,0) are rejected rather than clicked.
3. **Vision spine** — screenshot → local vision model → capture-px point →
   `mouse_click` (which maps px → logical). This is the ONLY rung that works on
   Chrome/Chromium, which are invisible to AT-SPI and can't be woken at
   runtime. All three backends share this rung (`HandsBackend._click_visual`).

The locator (`hands/vision.py`) speaks OpenAI-compatible chat with an image
attachment — point it at the local 5090 model (llama.cpp/vLLM) via
`WINDYTALK_VISION_URL` (+ `_KEY`, `_MODEL`). Unset = the lane doesn't exist and
capabilities say so. The model must answer `{"found":true,"x":..,"y":..}` in
image pixels; off-image answers are rejected, never clamped into a click.

## Functional capability probes (Phase 0 #2)

On Linux, `capabilities()` no longer equates binary presence with function —
grim was present on GNOME yet refused by the compositor, and `gi` can import
while the accessibility bus is dead. AT-SPI (`read_screen`/`list_apps`/
`click_element`/the `type_text` guard) and `screenshot` are decided by one real
probe each (an AT-SPI desktop query; a throwaway capture through the real
chain), cached per backend instance. `type_text` requires working AT-SPI
because the focus-guard fails closed without it. macOS/Windows stay
presence-based deliberately: their checks were fleet-validated accurate, and a
macOS capture probe would cache a false negative whenever the display sleeps.
