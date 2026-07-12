# Windy Talk packaging doctrine — the fat installer

Grant's standing order (2026-07-11): the installer ships **everything** any
variant of the target OS could possibly need, even at the cost of size. The app
must never say "let me go find and download what this machine is missing." This
document is the doctrine; `packaging/manifests/*.json` are the enforceable
ingredient lists; `tests/test_packaging_manifests.py` makes forgetting an
ingredient a red merge gate instead of a support ticket.

## The four rules

1. **Ship the full cocktail, per OS.** Within each OS's installer, include every
   tool for every variant of that OS (Wayland AND X11 AND GNOME AND KDE in one
   Linux package; Intel AND Apple Silicon in one Mac file). The OS split itself
   (a Windows user downloads the Windows installer) is the only "detection"
   involved in what ships.
2. **Bring your own runtime.** The app never uses the machine's Python, Node, or
   any version-dependent runtime — it carries frozen private copies, identical
   on every machine on Earth, forever. This is the rule that makes the
   "this machine's Python is too old" bug class extinct rather than fixed.
   The only borrowed pieces are OS *permanent built-ins* that cannot be absent:
   PowerShell 5.1 + .NET Framework on Windows 10/11; `osascript` /
   `screencapture` / `open` on macOS; `bash` on Linux.
3. **Detection wires and verifies — it never decides what ships.** The first-run
   wizard probes what works, walks the user through the OS permission prompts
   (the one thing no bundle can buy: macOS Accessibility + Screen Recording,
   one Linux sudo for the input rule, one Windows UAC), then proves the install
   with a LIVE self-test — a real keystroke, a real screenshot, green checks on
   the user's actual machine. At runtime, the fallback chains
   (`hands/backends/base.py run_chain`) pick the first working mechanism per
   action, so a machine that changes under us (X11↔Wayland at the login screen)
   keeps working.
4. **Nothing downloads at runtime.** Ever.

## The four artifacts

| Artifact | Floor | Cocktail character |
|---|---|---|
| Windows NSIS installer (x64) | Windows 10 (2015)+ | Leanest: Electron + frozen python + our code; Windows itself provides every external tool (validated on GrantW laptop, zero bugs) |
| macOS Universal2 .dmg | macOS 11 (2020)+ | ONE fat binary covers Intel + Apple Silicon; bundle cliclick + pyobjc-Quartz both-arch; must be signed + notarized |
| Linux AppImage (x86_64) | glibc 2.31+ (~2020) | Fattest: frozen python + all input/screenshot tools **including ydotoold** (Ubuntu apt ships the client with NO daemon — OC3 finding); one file covers every session type |
| Linux .deb | same | convenience twin, same payload |

**The ancient-Windows honesty:** old *hardware* (~2008+) is fully supported via
Windows 10. Dead *OSes* (XP/Vista/7/8) are deliberately excluded — every modern
runtime dropped them, so reaching them means shipping unpatchable security
holes. That is the opposite of rock-solid; one ransomware story would brand the
product forever.

## Update doctrine (v1.x)

- **Notification channel = the app itself.** On launch + daily, fetch a tiny
  **signed** `latest.json` manifest; if newer, show an in-app banner with the
  fix list and a one-click install. (`check_for_update` / `apply_update` in
  control.mcp.v1 are already built; inert until the Ed25519 key is embedded.)
- **Atomic whole-replace, never patch-in-place.** Download the complete new
  version to a staging dir, verify signature + checksum, swap the entire app
  directory in one move. Old code is 100% eradicated — mixed-version "fighting"
  is structurally impossible. One quarantined copy (renamed out of the run
  path, cannot execute) is kept briefly as Last-Known-Good for automatic
  rollback if the new version crash-loops, then purged. Anti-rollback prevents
  an old version ever re-installing over a new one.
- **User data lives OUTSIDE the app directory and survives updates.** Code:
  eradicated. Data: preserved and versioned. If data itself goes bad, that is
  what `reset_to_defaults` (the big QC reset button) is for.
- **Staged rollout.** The manifest can offer a release to N% first; watch
  telemetry for 48h; then open to 100%. A new bug never reaches a million
  people at once.
- **Hosting: Cloudflare R2** (zero egress fees — a million downloads of a
  200MB installer must not create a bandwidth bill).

## Telemetry (content-free, from the first release)

Events carry: app version, OS + version, system locale, anonymous install id,
error **codes** (never content — no words, no audio, ever), crash/recovery
counters, and which fallback mechanism served each action. That is exactly
enough to see "error X spiking among es-MX users on Ubuntu 22.04" and to watch
it fall to zero after the fix ships. Collector: a small Cloudflare Worker
(spec in a later slice).

## Per-OS first-run notes

- **Linux:** `packaging/linux/firstrun-linux.sh` (one sudo) installs the uinput
  udev rule + the `windytalk-ydotoold` system service, verifies the socket.
  System-service (root) ydotoold is deliberate: a lingering user manager keeps
  stale groups, so a user-service hits uinput EPERM until re-login — validated
  on OC3. Flatpak/Snap are rejected: their sandboxes block desktop control.
- **macOS:** wizard walks Accessibility + Screen Recording consents, then
  self-tests. Universal build means zero chip questions.
- **Windows:** nothing to install beyond the app itself; wizard runs the
  self-test directly.
