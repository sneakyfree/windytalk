#!/usr/bin/env bash
# Windy Talk Linux first-run wiring — the wizard's ONE sudo step.
#
# Installs exactly two system pieces and verifies both:
#   1. /etc/udev/rules.d/99-windytalk-uinput.rules  (uinput group access)
#   2. windytalk-ydotoold.service                    (bundled uinput daemon;
#      socket at /run/windytalk/.ydotool_socket, owned by the desktop user)
#
# Idempotent: safe to re-run any number of times. Fully reversible:
#   sudo ./firstrun-linux.sh --uninstall
#
# Usage:
#   sudo ./firstrun-linux.sh --user <desktop-user> [--ydotoold-bin <path>]
#   sudo ./firstrun-linux.sh --uninstall
#
# The app (and any test) must set YDOTOOL_SOCKET=/run/windytalk/.ydotool_socket
# — the hands backend and the ydotool client both honor it. The system path is
# used (not /run/user/<uid>) so the daemon works from boot, before any login.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RULE_SRC="$HERE/99-windytalk-uinput.rules"
RULE_DST="/etc/udev/rules.d/99-windytalk-uinput.rules"
UNIT_SRC="$HERE/windytalk-ydotoold.service.in"
UNIT_DST="/etc/systemd/system/windytalk-ydotoold.service"
BIN_DST="/usr/local/bin/windytalk-ydotoold"
SOCKET="/run/windytalk/.ydotool_socket"

die() { echo "FIRSTRUN FAIL: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "must run as root (the wizard's one sudo prompt)"

TARGET_USER="" ; YDOTOOLD_BIN="" ; UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --user) TARGET_USER="$2"; shift 2 ;;
    --ydotoold-bin) YDOTOOLD_BIN="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) die "unknown arg: $1" ;;
  esac
done

if [ "$UNINSTALL" -eq 1 ]; then
  systemctl disable --now windytalk-ydotoold.service 2>/dev/null || true
  rm -f "$UNIT_DST" "$BIN_DST" "$RULE_DST"
  systemctl daemon-reload
  udevadm control --reload-rules 2>/dev/null || true
  echo "FIRSTRUN: uninstalled (udev rule, service, daemon binary removed)"
  exit 0
fi

TARGET_USER="${TARGET_USER:-${SUDO_USER:-}}"
[ -n "$TARGET_USER" ] || die "--user <desktop-user> required (or run via sudo)"
OWN_UID="$(id -u "$TARGET_USER")" || die "no such user: $TARGET_USER"
OWN_GID="$(id -g "$TARGET_USER")"

# --- 1. daemon binary: bundled path preferred, source build as fallback ------
if [ -z "$YDOTOOLD_BIN" ]; then
  if [ -x "$HERE/out/ydotoold" ]; then
    YDOTOOLD_BIN="$HERE/out/ydotoold"        # produced by build-ydotoold.sh
  else
    echo "no bundled ydotoold supplied — building from source (fallback path)"
    "$HERE/build-ydotoold.sh" "$HERE/out"
    YDOTOOLD_BIN="$HERE/out/ydotoold"
  fi
fi
[ -x "$YDOTOOLD_BIN" ] || die "ydotoold binary not executable: $YDOTOOLD_BIN"
install -m 0755 "$YDOTOOLD_BIN" "$BIN_DST"

# --- 2. uinput udev rule ------------------------------------------------------
install -m 0644 "$RULE_SRC" "$RULE_DST"
modprobe uinput 2>/dev/null || true
udevadm control --reload-rules
udevadm trigger --name-match=uinput 2>/dev/null || true
[ -e /dev/uinput ] || die "/dev/uinput missing after modprobe+udev reload"

# --- 3. system service ---------------------------------------------------------
sed -e "s|@YDOTOOLD_BIN@|$BIN_DST|" \
    -e "s|@OWN_UID@|$OWN_UID|" \
    -e "s|@OWN_GID@|$OWN_GID|" \
    "$UNIT_SRC" > "$UNIT_DST"
systemctl daemon-reload
systemctl enable --now windytalk-ydotoold.service
systemctl restart windytalk-ydotoold.service   # idempotent re-runs pick up changes

# --- 4. verify: the daemon actually came up and the socket is usable ----------
for _ in $(seq 1 25); do [ -S "$SOCKET" ] && break; sleep 0.2; done
[ -S "$SOCKET" ] || { systemctl status windytalk-ydotoold.service --no-pager || true; \
                      die "socket never appeared at $SOCKET"; }
SOCK_OWNER="$(stat -c %u "$SOCKET")"
[ "$SOCK_OWNER" = "$OWN_UID" ] || die "socket owned by uid $SOCK_OWNER, expected $OWN_UID"

echo "FIRSTRUN OK: udev rule installed, windytalk-ydotoold running,"
echo "socket $SOCKET owned by $TARGET_USER (uid $OWN_UID, mode 0600)."
echo "App env must set: YDOTOOL_SOCKET=$SOCKET"
