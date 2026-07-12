"""The vision spine's locator (GAP_CLOSING_PLAN Phase 2 #5).

`screenshot → vision model → target box → coordinate-click` is the universal
fallback that works on every platform and EVERY browser — Chrome/Chromium are
invisible to AT-SPI and can't be woken at runtime, so this is the only way to
click them. The model runs locally (the 5090 engine box per the
no-cloud-cost doctrine), served OpenAI-compatible — the same shape as
brains/mind.py, so llama.cpp / vLLM / the Mind gateway all fit unchanged.

Configuration (all env; unset URL = the vision lane simply doesn't exist and
capabilities stay honest about it):
  WINDYTALK_VISION_URL    — OpenAI-compatible base, e.g. http://veron:8000/v1
  WINDYTALK_VISION_KEY    — bearer token if the server wants one
  WINDYTALK_VISION_MODEL  — served model name (default: whatever `default` maps to)

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
DEFAULT_TIMEOUT = 30.0

_PROMPT = (
    "You are a precise UI element locator. The attached screenshot is {w}x{h} "
    "pixels. Locate this element: {target}\n"
    'Reply with ONLY a JSON object, no other text: {{"found": true, "x": <int>, '
    '"y": <int>}} where x,y is the CENTER of the element in image pixels '
    '(0,0 = top-left). If the element is not visible, reply {{"found": false}}.'
)


class VisionLocator:
    def __init__(self, base_url: str, api_key: str = "",
                 model: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model or os.environ.get("WINDYTALK_VISION_MODEL", DEFAULT_MODEL)
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
                "max_tokens": 200,
                "messages": [{"role": "user", "content": [
                    {"type": "text",
                     "text": _PROMPT.format(w=size[0], h=size[1], target=target)},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]}],
            }
            raw = self._post(body)
        except Exception:  # noqa: BLE001 — a dead/misconfigured model = "not located"
            return None
        return _parse_point(raw, size)


def _parse_point(raw: str, size: tuple[int, int]) -> tuple[int, int] | None:
    """Extract {"found":true,"x":..,"y":..} from model output that may wrap the
    JSON in prose or a markdown fence. Off-image coordinates are rejected — a
    hallucinated point must not become a click."""
    for m in re.finditer(r"\{[^{}]*\}", raw, re.S):
        try:
            doc = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict) or not doc.get("found"):
            continue
        try:
            x, y = int(doc["x"]), int(doc["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= x < size[0] and 0 <= y < size[1]:
            return x, y
        return None  # found-but-off-image: refuse rather than clamp a hallucination
    return None
