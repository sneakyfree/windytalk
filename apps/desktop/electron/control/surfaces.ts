// The Windy local discovery registry (~/.windy/surfaces.json) — ADR-060 §3.8.
//
// "One read, every knob." Every Class-D Windy product writes its entry here at
// startup and removes it on clean shutdown, so an agent landing on the box can
// enumerate every control surface present in a single read — instead of having
// to know each product's port and token path a priori. This is the piece that
// turns N isolated products into one machine an agent can fully operate.
//
// Schema: windy-contracts/schema/surfaces.v1.schema.json. Readers PROBE BEFORE
// TRUST (a dead port is stale, not gospel — that's why we publish `health` and
// `pid`), so a lingering entry after a crash is harmless.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export interface SurfaceEntry {
  product: string;
  version: string;
  class?: "desktop" | "cloud" | "agent-host";
  contract: string;
  doctrine?: string;
  /** Native transport base — loopback only, `http://127.0.0.1:<port>`. */
  http: string;
  /** How an MCP client attaches (a streamable-http URL, or `stdio:npx …`). */
  mcp?: string;
  /** Where the per-install bearer token lives (0600). */
  token_path?: string;
  /** How a reader probes liveness before trusting this entry. */
  health?: string;
  /** Helps readers reap a stale entry after a crash. */
  pid?: number;
}

interface Registry {
  surfaces: SurfaceEntry[];
}

/** ~/.windy/surfaces.json (the SHARED registry — note ~/.windy, not ~/.windytalk). */
export function registryPath(home: string = os.homedir()): string {
  return path.join(home, ".windy", "surfaces.json");
}

function read(file: string): Registry {
  try {
    const doc = JSON.parse(fs.readFileSync(file, "utf8")) as unknown;
    if (doc && typeof doc === "object" && Array.isArray((doc as Registry).surfaces)) {
      return doc as Registry;
    }
  } catch {
    // missing or corrupt → start fresh; never throw into startup/shutdown.
  }
  return { surfaces: [] };
}

function writeAtomic(file: string, doc: Registry): void {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  // Atomic replace so a concurrent reader never sees a torn file. (A whole-file
  // read-modify-write can still lose a co-registering product's entry under a
  // rare simultaneous write — a per-product ~/.windy/surfaces.d/ split would
  // remove that race; flagged as a doctrine refinement, not needed for v1.)
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(doc, null, 2) + "\n", { mode: 0o600 });
  fs.renameSync(tmp, file);
}

/**
 * Register (or refresh) this product's surface — replaces any prior entry for
 * the same product (so a stale self-entry from a crashed run is overwritten),
 * and PRESERVES every other product's entry. Never throws.
 */
export function registerSurface(entry: SurfaceEntry, home: string = os.homedir()): void {
  try {
    const file = registryPath(home);
    const doc = read(file);
    doc.surfaces = doc.surfaces.filter((s) => s.product !== entry.product);
    doc.surfaces.push(entry);
    writeAtomic(file, doc);
  } catch {
    // discovery is best-effort — a failure here must never break app startup.
  }
}

/** Remove only this product's entry on clean shutdown. Preserves others. Never throws. */
export function unregisterSurface(product: string, home: string = os.homedir()): void {
  try {
    const file = registryPath(home);
    if (!fs.existsSync(file)) return;
    const doc = read(file);
    const next = doc.surfaces.filter((s) => s.product !== product);
    if (next.length !== doc.surfaces.length) writeAtomic(file, { surfaces: next });
  } catch {
    // best-effort — a stale entry is harmless (readers probe before trust).
  }
}
