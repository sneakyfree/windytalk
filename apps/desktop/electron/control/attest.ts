// Serving attestation for the heartbeat (resurrection.heartbeat.liveness_semantics
// + staleness_tiers.exemptions). An attestation is a PROOF the serving loop just
// served, never a free-running signal:
//   1. a guarded :8782 request served within the last interval, or
//   2. a fresh self round-trip through our own control server (the contract's
//      "third attestation source" — covers the renderer-down native-confirm case).
// A deadlocked main can produce neither (its event loop can't run this code, and
// its server can't complete a round-trip), so the heartbeat goes stale and the
// watcher's tier-2 kill fires — which is the design working.
import { HEARTBEAT_INTERVAL_MS } from "./constants.js";
import type { Attestor } from "./heartbeat.js";
import { ControlServer, pingControlPort } from "./server.js";

export function makeAttestor(
  server: ControlServer,
  port: number,
  token: string,
  opts: { now?: () => number; ping?: typeof pingControlPort } = {},
): Attestor {
  const now = opts.now ?? Date.now;
  const ping = opts.ping ?? pingControlPort;
  return async () => {
    const servedAgo = now() - server.lastServedAt;
    if (server.lastServedAt > 0 && servedAgo <= HEARTBEAT_INTERVAL_MS) return true;
    // A real loopback HTTP exchange with ourselves; 2 s cap so a sick server
    // can't stall the writer past its own cadence.
    const res = await ping(port, token, 2_000);
    return res.ok && res.pid === process.pid;
  };
}
