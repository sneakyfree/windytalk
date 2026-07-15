"""The vision spine's locator (GAP_CLOSING_PLAN Phase 2 #5).

`screenshot → vision model → target box → coordinate-click` is the universal
fallback that works on every platform and EVERY browser — Chrome/Chromium are
invisible to AT-SPI and can't be woken at runtime, so this is the only way to
click them. The model runs locally (the 5090 engine box per the
no-cloud-cost doctrine), served OpenAI-compatible — the same shape as
brains/mind.py, so llama.cpp / vLLM / the Mind gateway all fit unchanged.

Configuration (all env; unset URL = the vision lane simply doesn't exist and
capabilities stay honest about it):
  WINDYTALK_VISION_URL     — OpenAI-compatible base, e.g. http://10.10.0.6:11434/v1
  WINDYTALK_VISION_KEY     — bearer token if the server wants one
  WINDYTALK_VISION_MODEL   — served model name (default: whatever `default` maps to)
  WINDYTALK_VISION_TIMEOUT — per-request seconds (default 90: cold load + thinking)

The locator NEVER raises into a click path: any transport/parse fault returns
None ("not located"), and the caller reports an honest can't-click. Coordinates
returned are CAPTURE pixels of the given screenshot — exactly the space
mouse_click speaks (hands/coords.py maps them to logical points).
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from pathlib import Path

from .coords import png_size

DEFAULT_MODEL = "default"
DEFAULT_TIMEOUT = 90.0   # cold model load + a thinking pass both fit (live: ~30s)
# Thinking models (live-measured: qwen3-vl:32b on ollama) spend their budget in
# a separate reasoning channel BEFORE emitting content, and max_tokens counts
# THINKING tokens too — 200 returned content='' every time, 2000 still starved
# long deliberations (41s of thought, empty answer). Non-thinkers stop early,
# so generous headroom costs nothing. The SERVER must also allow a context
# window that fits image+prompt+thinking+answer: ollama's 4096 default
# truncated every long locate until the served model carried num_ctx 16384
# (the windy-locator derived model on the 5090).
MAX_TOKENS = 8000
USER_AGENT = "windytalk/1.0"  # never the urllib default (CF WAF 403s Python-urllib/*)

# NORMALIZED 0-1000 bounding box, never absolute pixels. Live-measured
# (qwen3-vl:32b via ollama, 2026-07-12): the serving stack resizes the image
# before the model sees it (~1024x1024, aspect NOT preserved), so pixel answers
# come back in the RESIZED space — a systematic ~0.53x/0.93x error that missed
# 4/5 ground-truth targets. Normalized coordinates are resize-invariant on any
# stack (ollama/vLLM/llama.cpp preprocess differently): same targets, 4/5 hits
# at 2-5px error. A BOX beats a point: asked for a center directly the model
# returned corner-biased points (a live click nearly missed a 420px-wide
# button); the midpoint of its box is dead-center. (Qwen-VL grounding is
# natively normalized boxes anyway.)
_PROMPT = (
    "You are a precise UI element locator. Locate this element in the attached "
    "screenshot: {target}\n"
    "Reply with ONLY a JSON object, no other text: "
    '{{"found": true, "x1": <int>, "y1": <int>, "x2": <int>, "y2": <int>}} — '
    "the element's tight bounding box in NORMALIZED coordinates from 0 to "
    "1000 (x=0 is the left edge, x=1000 the right edge, y=0 the top, y=1000 "
    'the bottom). If the element is not visible, reply {{"found": false}}.'
)


class VisionLocator:
    def __init__(self, base_url: str, api_key: str = "",
                 model: str | None = None, timeout: float | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model or os.environ.get("WINDYTALK_VISION_MODEL", DEFAULT_MODEL)
        if timeout is None:
            try:
                timeout = float(os.environ.get("WINDYTALK_VISION_TIMEOUT", ""))
            except ValueError:
                timeout = DEFAULT_TIMEOUT
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> VisionLocator | None:
        url = os.environ.get("WINDYTALK_VISION_URL", "").strip()
        if not url:
            return None
        return cls(url, api_key=os.environ.get("WINDYTALK_VISION_KEY", ""))

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("WINDYTALK_VISION_URL", "").strip())

    # -- transport (injectable for tests) ---------------------------------------

    def _post(self, body: dict) -> str:
        """POST /chat/completions; return the assistant text content. Raises on
        transport/HTTP/shape errors — locate() owns turning that into None."""
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": USER_AGENT,
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            doc = json.loads(resp.read())
        return doc["choices"][0]["message"]["content"]

    # -- the locate call ---------------------------------------------------------

    def locate(self, image_path: str | Path, target: str) -> tuple[int, int] | None:
        """CENTER of `target` in CAPTURE pixels of `image_path`, or None."""
        size = png_size(image_path)
        if size is None:
            return None
        try:
            b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
            body = {
                "model": self.model,
                "temperature": 0,
                "max_tokens": MAX_TOKENS,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": _PROMPT.format(target=target)},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]}],
            }
            raw = self._post(body)
        except Exception:  # noqa: BLE001 — a dead/misconfigured model = "not located"
            return None
        return _parse_point(raw, size)


def _parse_point(raw: str, size: tuple[int, int]) -> tuple[int, int] | None:
    """Extract the normalized 0-1000 bounding box from model output that may
    wrap the JSON in prose or a markdown fence; return the box CENTER mapped to
    capture pixels of `size`. A bare x/y point (older/other models) is accepted
    as a degenerate box. Out-of-scale or inverted boxes are rejected — a
    hallucinated shape must not become a click."""
    for m in re.finditer(r"\{[^{}]*\}", raw, re.S):
        try:
            doc = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict) or not doc.get("found"):
            continue
        try:
            if "x1" in doc:
                x1, y1 = int(doc["x1"]), int(doc["y1"])
                x2, y2 = int(doc["x2"]), int(doc["y2"])
            else:
                x1 = x2 = int(doc["x"])
                y1 = y2 = int(doc["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= x1 <= x2 <= 1000 and 0 <= y1 <= y2 <= 1000):
            return None  # off-scale/inverted: refuse rather than repair a hallucination
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        return (min(size[0] - 1, round(cx * size[0] / 1000)),
                min(size[1] - 1, round(cy * size[1] / 1000)))
    return None
