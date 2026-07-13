"""The acceptance gauntlet (GAP_CLOSING_PLAN Phase 5 #9).

A repeatable automated test that drives REAL apps through the hands backend
and passes only on SCREENSHOT-VERIFIED success: after every scenario the
runner captures the screen and a vision model answers a strict yes/no question
about what is visibly true. No scenario passes on a tool's return message
alone — the pixels are the verdict. (The same local 5090 model that powers the
click spine is the judge; one lane, live-proven both directions.)

Scenarios (per-OS table, plan item #9):
  calculator   — 7 x 3 = via click_element's fast lane -> display shows 21
  chrome-vision— a REAL Chrome button, which AT-SPI cannot see on Linux, so
                 the click MUST ride the vision spine; the button flips the
                 page green with the text CLICKED OK (hermetic local file)
  firefox-fast — the same button in Firefox via the accessibility fast lane
  workflow     — a real editor: type a sentence with the focus-guard on ->
                 the words are on screen

Usage on the target box (repo or payload root on sys.path):
    WINDYTALK_VISION_URL=http://10.10.0.6:11434/v1 \
    WINDYTALK_VISION_MODEL=qwen3-vl:32b \
    python3 -m gauntlet.runner [--only calculator,workflow] [--json out.json]

Every result is one of: pass / fail / skip (app not present) / known-red (a
documented platform finding, e.g. GNOME 46 portal pointer devices=0 — reported
loudly, doesn't fail the run). Exit 0 = no scenario failed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from hands.backends import get_backend
from hands.vision import MAX_TOKENS, VisionLocator

BUTTON_HTML = """<!doctype html><html><body id="b" style="background:#fff">
<h1 style="font-size:40px">Windy Talk gauntlet page</h1>
<button style="font-size:60px;padding:40px;margin-top:60px"
 onclick="document.getElementById('b').style.background='#00c853';
          this.outerHTML='<h1 style=&quot;font-size:80px&quot;>CLICKED OK</h1>'">
Press Me</button></body></html>"""

_JUDGE_PROMPT = (
    "Look carefully at the attached screenshot and answer this question about "
    "what is VISIBLY true in it: {question}\n"
    'Reply with ONLY a JSON object, no other text: {{"answer": true}} or '
    '{{"answer": false}}.'
)


class VlmJudge:
    """Screenshot-verified means a model LOOKED. Strict yes/no over the same
    OpenAI-compatible lane the locator uses; unanswerable/garbage = False
    (a scenario never passes on a judge fault)."""

    def __init__(self) -> None:
        loc = VisionLocator.from_env()
        if loc is None:
            raise SystemExit("gauntlet needs WINDYTALK_VISION_URL (the judge "
                             "IS the screenshot verification)")
        self._loc = loc

    def sees(self, image_path: str, question: str) -> bool:
        import base64
        try:
            b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
            raw = self._loc._post({
                "model": self._loc.model, "temperature": 0,
                "max_tokens": MAX_TOKENS,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": _JUDGE_PROMPT.format(question=question)},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]}]})
        except Exception as e:  # noqa: BLE001 — judge fault = not verified
            print(f"    judge fault: {e}", flush=True)
            return False
        return _parse_answer(raw)


def _parse_answer(raw: str) -> bool:
    import re
    for m in re.finditer(r"\{[^{}]*\}", raw):
        try:
            doc = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict) and "answer" in doc:
            return doc["answer"] is True
    return False


@dataclass
class Result:
    name: str
    status: str          # pass | fail | skip | known-red
    detail: str
    elapsed_s: float
    screenshot: str = ""
    steps: list[str] = field(default_factory=list)


class Gauntlet:
    def __init__(self, shots_dir: str | None = None) -> None:
        self.b = get_backend()
        self.judge = VlmJudge()
        self.shots = Path(shots_dir or Path.home() / ".windytalk" / "gauntlet")
        self.shots.mkdir(parents=True, exist_ok=True)
        self.os = ("linux" if sys.platform.startswith("linux")
                   else "macos" if sys.platform == "darwin" else "windows")

    # ---- plumbing ------------------------------------------------------------

    def _shot(self, name: str) -> str:
        msg = self.b.screenshot(f"gauntlet_{name}.png")
        return msg.rsplit("Saved screenshot to ", 1)[-1].strip()

    def _verify(self, res: Result, question: str) -> bool:
        path = self._shot(res.name)
        res.screenshot = path
        ok = self.judge.sees(path, question)
        res.steps.append(f"judge[{question!r}] -> {ok}")
        return ok

    def _launch(self, app: str, wait: float = 5.0) -> tuple[bool, str]:
        try:
            out = self.b.open_app(app)
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        time.sleep(wait)
        return True, out

    def _close(self, *procs: str) -> None:
        if self.os == "windows":
            for p in procs:
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"Stop-Process -Name '{p}' -Force -EA SilentlyContinue"],
                               capture_output=True, timeout=20)
        else:
            for p in procs:
                subprocess.run(["pkill", "-f", p], capture_output=True, timeout=10)
        time.sleep(1.0)

    def _button_page(self) -> str:
        f = Path(self.shots) / "gauntlet_button.html"
        f.write_text(BUTTON_HTML)
        return f.as_uri()

    def _activate(self, title_regex: str) -> None:
        """Best-effort focus of the app under test — live gauntlet runs lost
        focus to a Software Updater popup mid-scenario (click_element honestly
        walks the ACTIVE app, so a focus thief fails the click). X11 only;
        elsewhere the freshest launch already owns focus."""
        if self.os == "linux" and shutil.which("xdotool") \
                and (os.environ.get("XDG_SESSION_TYPE") or "").lower() != "wayland":
            subprocess.run(["xdotool", "search", "--name", title_regex,
                            "windowactivate"], capture_output=True, timeout=10)
            time.sleep(0.8)

    # ---- scenarios -----------------------------------------------------------

    def calculator(self, res: Result) -> None:
        app, procname = {
            "linux": ("gnome-calculator", "gnome-calculator"),
            "macos": ("Calculator", "Calculator"),
            "windows": ("calc", "CalculatorApp"),
        }[self.os]
        if self.os == "linux" and not shutil.which(app):
            res.status, res.detail = "skip", f"{app} not installed"
            return
        try:
            ok, msg = self._launch(app)
            if not ok:
                res.status, res.detail = "skip", f"couldn't launch {app}: {msg}"
                return
            for label in ("7", "×" if self.os == "linux" else "multiply by",
                          "3", "="):
                out = self.b.click_element(label)
                res.steps.append(f"click {label!r}: {out}")
                if "Clicked" not in out:
                    res.status, res.detail = "fail", f"couldn't click {label!r}: {out}"
                    return
                time.sleep(0.6)
            time.sleep(1.0)
            if self._verify(res, "Does the calculator on screen show the "
                                 "result 21 in its display?"):
                res.status, res.detail = "pass", "7x3=21 screenshot-verified"
            else:
                res.status, res.detail = "fail", "judge did not see 21 on screen"
        finally:
            self._close(procname)

    def chrome_vision(self, res: Result) -> None:
        """The plan's core requirement: a REAL Chrome button via the vision
        spine (Chrome is invisible to AT-SPI on Linux and can't be woken)."""
        chrome = {"linux": ("google-chrome", "chrome"),
                  "macos": ("Google Chrome", "Google Chrome"),
                  "windows": ("chrome", "chrome")}[self.os]
        app, procname = chrome
        if self.os == "linux" and not shutil.which(app):
            res.status, res.detail = "skip", "google-chrome not installed"
            return
        url = self._button_page()
        try:
            if self.os == "linux":
                subprocess.Popen([app, "--new-window", url],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif self.os == "macos":
                subprocess.run(["open", "-a", app, url], capture_output=True, timeout=20)
            else:
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"Start-Process chrome '{url}'"],
                               capture_output=True, timeout=30)
            time.sleep(9.0)  # a cold Chrome + page paint; the click must land on a drawn button
            self._activate("btn.html|gauntlet")
            out = self.b.click_element("the big Press Me button")
            res.steps.append(f"click: {out}")
            if "Clicked" not in out:
                res.status, res.detail = "fail", out
                return
            if self.os == "linux" and "located visually" not in out:
                # On Linux the fast lane CANNOT see Chrome — if it claims a
                # click anyway something is lying; the spine must have run.
                res.status = "fail"
                res.detail = f"expected the vision spine on Linux Chrome, got: {out}"
                return
            time.sleep(2.5)  # let the onclick repaint before the judge looks
            if self._verify(res, "Is there a page with a bright green "
                                 "background showing the text CLICKED OK?"):
                res.status, res.detail = "pass", f"real Chrome button clicked ({out})"
            else:
                res.status, res.detail = "fail", "page did not turn green/CLICKED OK"
        finally:
            self._close(procname)

    def firefox_fast(self, res: Result) -> None:
        if self.os != "linux":
            res.status, res.detail = "skip", "fast-lane browser scenario is Linux/AT-SPI"
            return
        if not shutil.which("firefox"):
            res.status, res.detail = "skip", "firefox not installed"
            return
        url = self._button_page()
        try:
            # Firefox enables its AT-SPI tree lazily and only when it detects an
            # assistive client — GNOME_ACCESSIBILITY=1 makes it expose the DOM
            # from launch (without it the fast lane sees an empty document).
            env = {**os.environ, "GNOME_ACCESSIBILITY": "1"}
            subprocess.Popen(["firefox", "--new-window", url], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(11.0)  # Firefox cold start + a11y tree build is slow
            self._activate("Mozilla Firefox")
            out = self.b.click_element("Press Me")
            res.steps.append(f"click: {out}")
            if "Clicked" not in out:
                res.status, res.detail = "fail", out
                return
            time.sleep(1.5)
            if self._verify(res, "Is there a page with a bright green "
                                 "background showing the text CLICKED OK?"):
                res.status, res.detail = "pass", f"Firefox button via fast lane ({out})"
            else:
                res.status, res.detail = "fail", "page did not turn green/CLICKED OK"
        finally:
            self._close("firefox")

    def workflow(self, res: Result) -> None:
        """A real user workflow: open the OS text editor, type a sentence
        (focus-guard live), read it back off the pixels."""
        editor, procname = {
            "linux": ("gnome-text-editor", "gnome-text-editor"),
            "macos": ("TextEdit", "TextEdit"),
            "windows": ("notepad", "notepad"),
        }[self.os]
        if self.os == "linux" and not shutil.which(editor):
            editor, procname = "gedit", "gedit"
            if not shutil.which(editor):
                res.status, res.detail = "skip", "no gnome-text-editor/gedit"
                return
        sentence = "the windstorm rides again"
        try:
            ok, msg = self._launch(editor)
            if not ok:
                res.status, res.detail = "skip", f"couldn't launch {editor}: {msg}"
                return
            out = self.b.type_text(sentence)
            res.steps.append(f"type: {out}")
            time.sleep(1.0)
            if self._verify(res, f"Is a text editor window open showing the "
                                 f"typed words '{sentence}'?"):
                res.status, res.detail = "pass", "typed sentence screenshot-verified"
            else:
                res.status, res.detail = "fail", "judge did not see the sentence"
        finally:
            self._close(procname)

    # ---- the run ---------------------------------------------------------------

    SCENARIOS = ("calculator", "chrome-vision", "firefox-fast", "workflow")

    def run(self, only: list[str] | None = None,
            known_red: list[str] | None = None) -> list[Result]:
        results: list[Result] = []
        for name in self.SCENARIOS:
            if only and name not in only:
                continue
            res = Result(name=name, status="fail", detail="", elapsed_s=0.0)
            t0 = time.time()
            try:
                getattr(self, name.replace("-", "_"))(res)
            except Exception as e:  # noqa: BLE001 — a crashed scenario is a failure
                res.status, res.detail = "fail", f"scenario crashed: {e}"
            res.elapsed_s = round(time.time() - t0, 1)
            if res.status == "fail" and known_red and name in known_red:
                res.status = "known-red"
            results.append(res)
            print(f"[{res.status.upper():9}] {name} ({res.elapsed_s}s) {res.detail}",
                  flush=True)
        return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma-separated scenario names")
    ap.add_argument("--known-red", default="",
                    help="scenarios whose failure is a documented platform finding")
    ap.add_argument("--json", default="", help="write results to this path")
    args = ap.parse_args(argv)
    g = Gauntlet()
    results = g.run(only=[s for s in args.only.split(",") if s] or None,
                    known_red=[s for s in args.known_red.split(",") if s])
    doc = {"os": g.os, "results": [r.__dict__ for r in results]}
    if args.json:
        Path(args.json).write_text(json.dumps(doc, indent=1))
    failed = [r for r in results if r.status == "fail"]
    print(json.dumps({r.name: r.status for r in results}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
