// Diagnostics scrub helpers (contract diagnostics_privacy) — slice 1 covers the
// fields get_health exposes; slice 2 adds the full positive-allow-list pass +
// the golden negative test across every get_* tool.

/** Short technical strings (last_error, log lines): never a payload. */
export function scrubShortError(raw: string | null): string | null {
  if (raw == null) return null;
  let s = raw;
  // Full home/user paths -> basename (never a username-bearing path).
  s = s.replace(/(?:[A-Za-z]:)?[/\\](?:Users|home)[/\\][^\s'"]+/g, (m) => {
    const base = m.split(/[/\\]/).pop() ?? "<path>";
    return `<path>/${base}`;
  });
  // Secrets/tokens: long hex or base64-ish runs -> ***
  s = s.replace(/\b[0-9a-fA-F]{16,}\b/g, "***");
  s = s.replace(/\b[A-Za-z0-9+/_-]{24,}={0,2}\b/g, "***");
  // URL query strings: strip (a token often rides in ?key=...).
  s = s.replace(/\?[^\s'"]*/g, "");
  // Short and technical, never a payload.
  if (s.length > 160) s = s.slice(0, 157) + "...";
  return s;
}
