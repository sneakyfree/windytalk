// The engine-URL allow-list (contract security.engine_allow_list) + the
// diagnostics scrub for engine URLs (diagnostics_privacy.deliberate_exceptions).
//
// Host matching is the EXACT discipline of agents/connect.py._require_trusted_url:
// match on the PARSED hostname (never a substring of the raw URL — 'evil.com/
// windymind.ai' and 'user@evil' must not pass), and suffix-match cloud hosts
// with a LEADING DOT. The allow-list is NOT mutable via MCP; paired hosts are
// added only by the in-UI pairing flow, persisted beside the config.
import fs from "node:fs";
import path from "node:path";

const LOOPBACK = new Set(["127.0.0.1", "::1", "localhost"]);
/** Cloud engine domains — leading dot = any subdomain, wss:// required. */
const CLOUD_SUFFIXES = [".thewindstorm.uk"];

export type EngineUrlVerdict =
  | { allowed: true }
  | { allowed: false; host: string; reason: string };

export class EngineAllowList {
  private readonly pairedPath: string;

  constructor(configDir: string) {
    this.pairedPath = path.join(configDir, "paired-engines.json");
  }

  /** Hosts recorded during an explicit in-UI engine-pairing flow. */
  pairedHosts(): string[] {
    try {
      const parsed = JSON.parse(fs.readFileSync(this.pairedPath, "utf8"));
      return Array.isArray(parsed) ? parsed.filter((h) => typeof h === "string") : [];
    } catch {
      return [];
    }
  }

  /** UI pairing flow ONLY — no MCP tool may reach this (immutable_via_mcp). */
  recordPairedHost(host: string): void {
    const hosts = new Set(this.pairedHosts());
    hosts.add(host.toLowerCase());
    fs.mkdirSync(path.dirname(this.pairedPath), { recursive: true, mode: 0o700 });
    fs.writeFileSync(this.pairedPath, JSON.stringify([...hosts]), { mode: 0o600 });
  }

  check(url: string): EngineUrlVerdict {
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      return { allowed: false, host: url, reason: "unparseable url" };
    }
    const host = (parsed.hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
    const scheme = parsed.protocol.replace(/:$/, "");
    if (!host) return { allowed: false, host: url, reason: "no host" };

    if (LOOPBACK.has(host)) {
      // Loopback: ws:// permitted (wss not required on-box).
      return scheme === "ws" || scheme === "wss"
        ? { allowed: true }
        : { allowed: false, host, reason: `scheme ${scheme} is not a websocket` };
    }
    const cloud = CLOUD_SUFFIXES.some((suf) => host.endsWith(suf));
    const paired = this.pairedHosts().includes(host);
    if (!cloud && !paired) return { allowed: false, host, reason: "not on the allow-list" };
    // Non-loopback REQUIRES wss (both cloud and paired).
    if (scheme !== "wss") return { allowed: false, host, reason: "wss:// required off-box" };
    return { allowed: true };
  }

  /**
   * diagnostics_privacy: scheme + host + port verbatim IF allow-listed, else
   * '<untrusted-host>'. Never the path, never a query string.
   */
  scrubForDiagnostics(url: string): string {
    const verdict = this.check(url);
    if (!verdict.allowed) return "<untrusted-host>";
    const parsed = new URL(url);
    const port = parsed.port ? `:${parsed.port}` : "";
    return `${parsed.protocol}//${parsed.hostname}${port}`;
  }
}
