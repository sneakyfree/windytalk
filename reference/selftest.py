"""
Hands self-test — verify the Linux desktop-control layer with NO API key and NO
microphone. Exercises the same agent.call_tool() path the voice model uses.

  python3 selftest.py            # safe: launch + read + semantic-click + screenshot
  JARVIS_SELFTEST_TYPING=1 python3 selftest.py   # ALSO injects keystrokes (see warning)

Tip: run detached from your terminal so app-focus changes can't disturb this shell:
  PYTHONPATH=. setsid --wait python3 selftest.py < /dev/null
"""
import os
import time

import agent
import hands


def check(name, args=None):
    args = args or {}
    print(f"\n• {name}({args})")
    out = agent.call_tool(name, args)
    print("  →", out.replace("\n", "\n    "))
    return out


def main():
    print("=" * 60, "\nWINDY JARVIS — hands self-test\n" + "=" * 60)

    check("list_apps")
    check("screenshot", {"path": "/tmp/jarvis_selftest.png"})

    # Semantic control demo: open the calculator and compute 7 + 5 = 12 purely by
    # clicking UI elements through the accessibility tree (no coordinates, no keys).
    check("open_app", {"name": "calculator"})
    time.sleep(3)
    print("\n  active window:", hands.read_screen().splitlines()[0])
    for label in ["7", "+", "5", "="]:
        check("click_element", {"label": label})
        time.sleep(0.4)
    time.sleep(0.5)
    result = [l for l in hands.read_screen().splitlines() if l.startswith("[label]")][:4]
    print("\n  calculator now reads:", result, "  (expect 7+5 … 12)")

    if os.environ.get("JARVIS_SELFTEST_TYPING") == "1":
        # WARNING: keystrokes go to whatever window has focus. If you run this from a
        # terminal that keeps focus, the text lands in your shell. The editor normally
        # grabs focus on open, so this is usually fine — but review before enabling.
        print("\n--- typing test (keystroke injection) ---")
        check("open_app", {"name": "text editor"})
        time.sleep(3)
        check("type_text", {"text": "Windy Jarvis hands are working."})
        check("press_keys", {"combo": "Return"})
        check("type_text", {"text": "Second line typed by the voice tools."})
        time.sleep(0.6)
        print("\n  editor now reads:")
        for l in hands.read_screen().splitlines():
            if "Windy Jarvis" in l or "Second line" in l:
                print("   ", l)

    print("\nDone. Review the output above and /tmp/jarvis_selftest.png.")


if __name__ == "__main__":
    main()
