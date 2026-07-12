"""GNOME-Wayland raw-coordinate pointer via org.freedesktop.portal.RemoteDesktop
(GAP_CLOSING_PLAN Phase 1 #4).

Mutter ignores ydotool's virtual POINTER entirely (proven on 5K and 1080p
GNOME-Wayland alike — the clicks return success while the cursor never moves).
The portal is the sanctioned API the compositor honors: one interactive "allow
remote control" grant on first use, remembered across sessions via
persist_mode=2 + a restore token, then absolute motion + buttons + axis flow
with no further UI.

Protocol notes (validated live on Windy 0, 2026-07-12):
  - Every setup call is a Request/Response dance: the options' handle_token
    MUST match the Request object path you subscribed to BEFORE the call.
  - Absolute motion needs a ScreenCast stream LINKED to the RemoteDesktop
    session (SelectSources on the same session); NotifyPointer* are plain
    method calls, not requests.
  - Start pops the grant dialog the first run; with a saved restore token it
    completes silently. Tokens are SINGLE-USE: every Start hands back a fresh
    one, which we persist immediately.

Coordinates given to click()/move() are the portal stream's LOGICAL space —
the caller (LinuxBackend) owns capture-px → logical mapping via hands.coords.
gi/GLib imports are lazy so this module loads on any OS.
"""
from __future__ import annotations

import os
from pathlib import Path

# evdev button codes (input-event-codes.h) — what NotifyPointerButton speaks.
BTN_CODES = {"left": 0x110, "right": 0x111, "middle": 0x112}

_DEVICE_POINTER = 2       # SelectDevices bitmask: 1=keyboard, 2=pointer, 4=touch
_PERSIST_UNTIL_REVOKED = 2
_SOURCE_MONITOR = 1
_AXIS_VERTICAL = 0

_PORTAL_BUS = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"


def _token_file() -> Path:
    return Path(os.environ.get("WINDYTALK_PORTAL_TOKEN_FILE")
                or Path.home() / ".windytalk" / "portal_restore_token")


def _start_timeout() -> float:
    # First-run Start blocks on the human clicking the grant dialog; later runs
    # (restore token) return immediately. Bounded so a headless/unattended box
    # fails the mechanism (and the chain / capability report stays honest)
    # instead of wedging the surface thread.
    try:
        return float(os.environ.get("WINDYTALK_PORTAL_TIMEOUT", "25"))
    except ValueError:
        return 25.0


class PortalError(RuntimeError):
    """Setup/notify failure — the mechanism chain treats it as a dead prong."""


class PortalPointer:
    """One remembered RemoteDesktop session; move/click/scroll against it."""

    def __init__(self) -> None:
        self._bus = None
        self._session: str | None = None     # session object path
        self._stream: int | None = None      # ScreenCast node id (absolute motion target)
        self._stream_size: tuple[int, int] | None = None
        self._counter = 0

    # ---- availability (NO dialog, no session): a real property read -----------

    @staticmethod
    def available() -> bool:
        """Is a RemoteDesktop portal with pointer support on the session bus?
        A functional probe (real D-Bus property read), but deliberately NOT a
        session Start — probing must never pop the grant dialog."""
        try:
            from gi.repository import Gio, GLib
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = bus.call_sync(
                _PORTAL_BUS, _PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", ("org.freedesktop.portal.RemoteDesktop",
                                      "AvailableDeviceTypes")),
                GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 3000, None)
            (types,) = reply.unpack()
            return bool(int(types) & _DEVICE_POINTER)
        except Exception:  # noqa: BLE001 — no portal / no bus / no gi ⇒ not available
            return False

    # ---- the Request/Response dance (seam for tests) ---------------------------

    def _get_bus(self):
        if self._bus is None:
            from gi.repository import Gio
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._bus

    def _request(self, iface: str, method: str, prefix: tuple, signature: str,
                 options: dict, timeout: float = 8.0) -> dict:
        """Call a portal request method and wait for its Response signal.
        `options` values are (variant_type, value) pairs; returns the response
        vardict unpacked to plain Python. Raises PortalError on any non-0 code
        (1 = user cancelled, 2 = other) or timeout."""
        from gi.repository import Gio, GLib
        bus = self._get_bus()
        self._counter += 1
        token = f"windytalk_{os.getpid()}_{self._counter}"
        sender = (bus.get_unique_name() or ":0.0")[1:].replace(".", "_")
        req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

        result: dict = {}
        loop = GLib.MainLoop()

        def on_response(_c, _s, _p, _i, _m, params):
            code, vardict = params.unpack()
            result["code"], result["res"] = code, vardict
            loop.quit()

        sub = bus.signal_subscribe(_PORTAL_BUS, "org.freedesktop.portal.Request",
                                   "Response", req_path, None,
                                   Gio.DBusSignalFlags.NONE, on_response)
        try:
            opts = {k: GLib.Variant(t, v) for k, (t, v) in options.items()}
            opts["handle_token"] = GLib.Variant("s", token)
            reply = bus.call_sync(
                _PORTAL_BUS, _PORTAL_PATH, f"org.freedesktop.portal.{iface}", method,
                GLib.Variant(signature, (*prefix, opts)),
                GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 5000, None)
            (actual_path,) = reply.unpack()
            if actual_path != req_path:
                # pre-0.9 portals ignore handle_token; re-subscribe to the real
                # path (documented race accepted — the fleet runs modern portals).
                bus.signal_unsubscribe(sub)
                sub = bus.signal_subscribe(_PORTAL_BUS, "org.freedesktop.portal.Request",
                                           "Response", actual_path, None,
                                           Gio.DBusSignalFlags.NONE, on_response)
            timer = GLib.timeout_add(int(timeout * 1000),
                                     lambda: (result.setdefault("code", -1), loop.quit()) and False)
            loop.run()
            if "res" in result:  # response won the race; the one-shot timer is still pending
                GLib.source_remove(timer)
        finally:
            bus.signal_unsubscribe(sub)
        if result.get("code") != 0:
            raise PortalError(f"{iface}.{method} response code {result.get('code')}"
                              + (" (timeout)" if result.get("code") == -1 else ""))
        return result.get("res") or {}

    def _notify(self, method: str, signature: str, args: tuple) -> None:
        """Plain (non-request) RemoteDesktop call — the NotifyPointer* family."""
        from gi.repository import Gio, GLib
        self._get_bus().call_sync(
            _PORTAL_BUS, _PORTAL_PATH, "org.freedesktop.portal.RemoteDesktop", method,
            GLib.Variant(signature, args), None, Gio.DBusCallFlags.NONE, 5000, None)

    # ---- session lifecycle ------------------------------------------------------

    def ensure_session(self) -> None:
        """Create-or-reuse the remembered remote-desktop session."""
        if self._session is not None and self._stream is not None:
            return
        res = self._request("RemoteDesktop", "CreateSession", (), "(a{sv})", {
            "session_handle_token": ("s", f"windytalk_s{os.getpid()}"),
        })
        session = res.get("session_handle")
        if not session:
            raise PortalError("CreateSession returned no session_handle")
        select_opts: dict = {"types": ("u", _DEVICE_POINTER),
                             "persist_mode": ("u", _PERSIST_UNTIL_REVOKED)}
        saved = self._load_token()
        if saved:
            select_opts["restore_token"] = ("s", saved)
        self._request("RemoteDesktop", "SelectDevices", (session,), "(oa{sv})", select_opts)
        # Link a ScreenCast monitor stream to the SAME session — absolute pointer
        # motion is addressed to a stream node, not to "the screen".
        self._request("ScreenCast", "SelectSources", (session,), "(oa{sv})", {
            "types": ("u", _SOURCE_MONITOR), "multiple": ("b", False),
        })
        res = self._request("RemoteDesktop", "Start", (session, ""), "(osa{sv})", {},
                            timeout=_start_timeout())
        # devices is a bitmask of what was ACTUALLY granted. Some compositors
        # (observed live: GNOME 46 / gnome-remote-desktop 46.3 on Ubuntu 24.04)
        # grant the ScreenCast stream while returning devices=0 — every later
        # NotifyPointer then fails with a confusing per-call "not allowed".
        # Detect it here and fail the mechanism crisply and immediately, so the
        # chain reports honest unsupported instead of a phantom-success session.
        granted = int(res.get("devices") or 0)
        if not granted & _DEVICE_POINTER:
            raise PortalError(
                "the RemoteDesktop portal granted no pointer device (devices=0) — "
                "this compositor/gnome-remote-desktop version refuses portal pointer "
                "input (seen on GNOME 46 / g-r-d 46.3)")
        streams = res.get("streams") or []
        if not streams:
            raise PortalError("Start granted no ScreenCast stream (absolute motion needs one)")
        node, props = streams[0]
        self._session, self._stream = session, int(node)
        size = props.get("size")
        self._stream_size = (int(size[0]), int(size[1])) if size else None
        token = res.get("restore_token")
        if token:
            self._save_token(str(token))  # single-use: every Start mints a new one

    def _load_token(self) -> str | None:
        try:
            return _token_file().read_text().strip() or None
        except OSError:
            return None

    def _save_token(self, token: str) -> None:
        try:
            f = _token_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(token)
            f.chmod(0o600)  # the token re-grants desktop control — owner-only
        except OSError:
            pass  # worst case: the grant dialog shows again next session

    def _reset(self) -> None:
        self._session = self._stream = self._stream_size = None

    def _with_session(self, fn) -> None:
        """Run fn against a live session; one transparent re-establish if the
        compositor closed ours (logout, revoked grant) since the last call."""
        self.ensure_session()
        try:
            fn()
        except PortalError:
            raise
        except Exception:  # noqa: BLE001 — dead session: rebuild once, then let it raise
            self._reset()
            self.ensure_session()
            fn()

    # ---- pointer actions (logical / stream coordinates) -------------------------

    @property
    def stream_size(self) -> tuple[int, int] | None:
        """Logical size of the granted monitor stream — the pointer coordinate
        space, and the authoritative 'logical screen size' for capture mapping."""
        return self._stream_size

    def move(self, x: int, y: int) -> None:
        self._with_session(lambda: self._notify(
            "NotifyPointerMotionAbsolute", "(oa{sv}udd)",
            (self._session, {}, self._stream, float(x), float(y))))

    def click(self, x: int | None, y: int | None, button: str = "left") -> None:
        code = BTN_CODES.get(button, BTN_CODES["left"])

        def do() -> None:
            if x is not None and y is not None:
                self._notify("NotifyPointerMotionAbsolute", "(oa{sv}udd)",
                             (self._session, {}, self._stream, float(x), float(y)))
            self._notify("NotifyPointerButton", "(oa{sv}iu)", (self._session, {}, code, 1))
            self._notify("NotifyPointerButton", "(oa{sv}iu)", (self._session, {}, code, 0))
        self._with_session(do)

    def scroll(self, amount: int) -> None:
        # hands scroll(): positive = up. Portal axis-discrete: positive steps =
        # down/right (libinput convention) — hence the sign flip.
        self._with_session(lambda: self._notify(
            "NotifyPointerAxisDiscrete", "(oa{sv}ui)",
            (self._session, {}, _AXIS_VERTICAL, -int(amount))))
