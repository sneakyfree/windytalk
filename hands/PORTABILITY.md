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
