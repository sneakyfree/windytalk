#!/usr/bin/env python3
"""
Windy Jarvis license admin — run on the Veron box to see who's using it and to
lock / unlock / expire copies in real time. Edits licenses.json next to the server;
the running server picks up changes within a few seconds (no restart).

  python3 admin.py list                       # licenses + who's online right now
  python3 admin.py new "Bill"                 # mint a new key for Bill
  python3 admin.py lock  WINDY-XXXXXX  "Text Grant 'I'm in' to unlock."
  python3 admin.py unlock WINDY-XXXXXX
  python3 admin.py expire WINDY-XXXXXX 2026-08-01
  python3 admin.py rm WINDY-XXXXXX
"""
import json
import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIC = os.path.join(HERE, "licenses.json")
ONLINE = os.path.join(HERE, "online.json")


def load():
    try:
        return json.load(open(LIC))
    except Exception:
        return {}


def save(d):
    json.dump(d, open(LIC, "w"), indent=2)


def _blank(name):
    return {"name": name, "status": "active", "expires": None,
            "gate_message": "", "gate_url": ""}


def cmd_list():
    lic = load()
    print(f"=== LICENSES ({len(lic)}) ===")
    for k, v in lic.items():
        gate = f"  🔒 {v['gate_message']}" if v.get("gate_message") else ""
        print(f"  {k:16s} {v.get('name','?'):16s} {v.get('status','active'):7s} "
              f"exp={v.get('expires') or '—'}{gate}")
    print("=== ONLINE NOW ===")
    try:
        online = json.load(open(ONLINE))
    except Exception:
        online = []
    if not online:
        print("  (nobody connected)")
    for s in online:
        print(f"  ● {s.get('name','?'):16s} key={s.get('license') or '—':16s} "
              f"ip={s.get('ip','?'):15s} since {s.get('since','?')}  [{s.get('status')}]")


def cmd_new(name):
    key = "WINDY-" + secrets.token_hex(3).upper()
    d = load(); d[key] = _blank(name); save(d)
    print(f"created {key} for {name}")


def cmd_add(key, name, expires=None):
    d = load(); d[key] = _blank(name); d[key]["expires"] = expires; save(d)
    print(f"added {key}")


def cmd_lock(key, *msg):
    d = load()
    if key not in d:
        return print("no such key")
    d[key]["status"] = "locked"; d[key]["gate_message"] = " ".join(msg); save(d)
    print(f"LOCKED {key}: {' '.join(msg) or '(no message)'}")


def cmd_unlock(key):
    d = load()
    if key not in d:
        return print("no such key")
    d[key]["status"] = "active"; d[key]["gate_message"] = ""; save(d)
    print(f"unlocked {key}")


def cmd_expire(key, date):
    d = load()
    if key not in d:
        return print("no such key")
    d[key]["expires"] = date; save(d)
    print(f"{key} expires {date}")


def cmd_rm(key):
    d = load(); d.pop(key, None); save(d)
    print(f"removed {key}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__); sys.exit(0)
    fn = {"list": cmd_list, "new": cmd_new, "add": cmd_add, "lock": cmd_lock,
          "unlock": cmd_unlock, "expire": cmd_expire, "rm": cmd_rm}.get(a[0])
    if not fn:
        print("unknown command:", a[0]); print(__doc__); sys.exit(1)
    fn(*a[1:])
