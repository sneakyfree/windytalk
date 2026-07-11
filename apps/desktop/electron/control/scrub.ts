// Diagnostics scrub (contract diagnostics_privacy) — outputs of every read tool
// may be read by an EXTERNAL third-party brain and LEAVE the machine, so scrub
// REGARDLESS of destination. Implemented as helpers each tool's POSITIVE
// allow-list of typed fields runs through (never a blocklist over free-form
// state). Three deliberate exceptions ride verbatim: an allow-listed engine
// URL's scheme+host+port (engine-allow.ts), scrubbed device names (below), and
// the brain model id.

/**
 * Short technical strings (last_error, log lines): never a payload. Default-
 * safe over-scrub is deliberate ("when in doubt, omit"):
 *  - a home/Users path cuts the string THERE (paths may contain spaces, and
 *    what follows a path in an error is usually payload);
 *  - speech-verb markers (said/heard/asked...) cut the string there — the way
 *    transcript text enters an error/log line is via the speech pipeline's
 *    "<who> said: ..." shapes.
 */
export function scrubShortError(raw: string | null): string | null {
  if (raw == null) return null;
  let s = raw;
  // Cut at the first home/user path — never a username-bearing path, and
  // nothing after it either (a space-bearing path can't be safely bounded).
  const pathIdx = s.search(/(?:[A-Za-z]:)?[/\\](?:Users|home)[/\\]/);
  if (pathIdx >= 0) s = s.slice(0, pathIdx) + "<path>";
  // Cut at transcript markers (conversation text must never ride along).
  const saidIdx = s.search(/\b(?:said|says|heard|asked|replied|told me|user text)\b/i);
  if (saidIdx >= 0) s = s.slice(0, saidIdx) + "[scrubbed]";
  // Secrets/tokens: long hex or base64-ish runs -> ***
  s = s.replace(/\b[0-9a-fA-F]{16,}\b/g, "***");
  s = s.replace(/\b[A-Za-z0-9+/_-]{24,}={0,2}\b/g, "***");
  // URL query strings: strip (a token often rides in ?key=...).
  s = s.replace(/\?[^\s'"]*/g, "");
  // Short and technical, never a payload.
  if (s.length > 160) s = s.slice(0, 157) + "...";
  return s;
}

// -- device-name scrub (diagnostics_privacy.deliberate_exceptions.audio_device_names)
//
// The id (not the name) is what set_audio_input consumes; the name is only a
// human hint. Goal (best-effort but DEFAULT-SAFE): no first/last name reaches a
// third-party cloud brain. Accept some over-scrub to make the guarantee real.

/** Words that are confidently device vocabulary, never personal names. */
const DEVICE_WORDS = new Set([
  "airpods", "airpod", "pro", "max", "mini", "buds", "earbuds", "headset",
  "headphones", "speaker", "speakers", "soundbar", "microphone", "mic",
  "webcam", "camera", "usb", "bluetooth", "wireless", "audio", "sound",
  "stereo", "mono", "hdmi", "displayport", "internal", "external", "built-in",
  "builtin", "default", "digital", "analog", "analogue", "output", "input",
  "line", "jack", "port", "device", "array", "dock", "hub", "monitor", "tv",
  "iphone", "ipad", "macbook", "imac", "galaxy", "pixel", "surface", "echo",
  "home", "studio", "gaming", "wired", "left", "right", "rear", "front",
  "virtual", "loopback", "controller", "interface", "codec", "chipset", "hd",
  "uhd", "duet", "solo", "quad", "one", "two", "go", "air", "flex", "beam",
  // major audio brands (public product vocabulary, not personal data)
  "sony", "bose", "jbl", "sennheiser", "logitech", "jabra", "shure", "blue",
  "yeti", "snowball", "rode", "audio-technica", "beats", "anker", "soundcore",
  "corsair", "razer", "hyperx", "steelseries", "plantronics", "poly", "apple",
  "samsung", "google", "amazon", "sonos", "realtek", "intel", "nvidia", "amd",
  "cirrus", "conexant", "focusrite", "scarlett", "behringer", "elgato", "wave",
]);

/**
 * Scrub one device label. Strategy, per the pinned rule:
 *  1. strip leading English genitive ("Grant's AirPods" -> "AirPods");
 *  2. strip trailing localized possessives ("iPhone de Marie" -> "iPhone");
 *  3. strip hostname-derived prefixes ("GRANT-PC Bluetooth" -> "Bluetooth");
 *  4. if ANY remaining token could be a personal name (not recognized device
 *     vocabulary), DEFAULT to the device TYPE + id — raw label stays local-only.
 */
export function scrubDeviceName(rawName: string, id: string, kind: "input" | "output"): string {
  let name = rawName.trim();
  // 1. leading English genitive: "Grant's X", "Grants' X"
  name = name.replace(/^[\p{L}\p{N}._-]+['’]s?\s+/u, "");
  // 2. trailing localized possessive: "<device> de|von|di|van|af|av <Name>"
  name = name.replace(/\s+(?:de|von|di|van|af|av|do|da|của|של)\s+\S+\s*$/iu, "");
  // 3. hostname-derived prefix: an ALL-CAPS/hyphenated machine token first
  name = name.replace(/^[A-Z0-9][A-Z0-9-]*(?:-PC|-MAC|-LAPTOP|-DESKTOP)?\s+(?=\S)/, (m) =>
    /^[A-Z0-9-]+$/.test(m.trim()) && m.trim().includes("-") ? "" : m,
  );
  name = name.trim();
  if (!name) return fallbackDeviceLabel(id, kind);

  // 4. default-safe: every token must be confident device vocabulary.
  const tokens = name.split(/[\s()\[\],/+]+/).filter(Boolean);
  const confident = tokens.every((t) => {
    const w = t.toLowerCase().replace(/[^a-z0-9-]/g, "");
    if (!w) return true;
    if (DEVICE_WORDS.has(w)) return true;
    if (/\d/.test(w)) return true; // model codes carry digits (wh-1000xm5, cs8409); names don't
    return false;
  });
  return confident ? name : fallbackDeviceLabel(id, kind);
}

function fallbackDeviceLabel(id: string, kind: "input" | "output"): string {
  const type = kind === "input" ? "Microphone" : "Speaker";
  // A short stable id suffix keeps two scrubbed devices distinguishable.
  const suffix = id.replace(/[^a-zA-Z0-9]/g, "").slice(-6) || "0";
  return `${type} (${suffix})`;
}
