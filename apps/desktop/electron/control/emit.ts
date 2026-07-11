// Content-free telemetry emitter — the TS port of telemetry/emit.py for the
// control surface (ADR-WA-001 / contracts/telemetry.v1.json, additive v1.1
// control.action). Fire-and-forget: never throws, never blocks past 200 ms, a
// silent no-op unless configured. Content-free is STRUCTURAL: only allow-listed
// keys survive, so a caller passing transcript="..." leaks nothing by accident.
import http from "node:http";
import https from "node:https";

const DEFAULT_URL = "https://admin.windyword.ai/v1/events";
const TIMEOUT_MS = 200;

const ALLOWED_FIELDS = new Set([
  "event_type", "actor_type", "actor_id", "session_id", "user_id", "agent_id",
  "ts", "dur_ms", "turns", "model", "cost_microcents", "latency_ms", "tool",
  "tier_outcome", "error_code", "region", "metadata",
  // additive v1.1 (control.action):
  "ok", "mode",
]);
const ALLOWED_METADATA = new Set([
  "app_version", "install_id", "os", "device", "region", "arch", "locale",
]);

export type Emitter = (fields: Record<string, unknown>) => void;

export function makeEmitter(env: NodeJS.ProcessEnv = process.env): Emitter {
  const token = (env.WINDYTALK_TELEMETRY_TOKEN ?? "").trim();
  const url = env.WINDYTALK_TELEMETRY_URL || DEFAULT_URL;
  if (!token) return () => {}; // inert unless configured
  return (fields) => {
    try {
      const event: Record<string, unknown> = { service: "windytalk", platform: "windy-talk" };
      for (const [k, v] of Object.entries(fields)) {
        if (!ALLOWED_FIELDS.has(k) || v == null) continue;
        if (k === "metadata" && typeof v === "object") {
          event[k] = Object.fromEntries(
            Object.entries(v as Record<string, unknown>).filter(([mk]) => ALLOWED_METADATA.has(mk)),
          );
        } else {
          event[k] = v;
        }
      }
      event.actor_type ??= "system";
      event.session_id ??= "none";
      event.ts ??= new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
      post(url, token, JSON.stringify({ events: [event] }));
    } catch {
      // fire-and-forget: telemetry may never hurt the caller
    }
  };
}

/**
 * control.action (contract `telemetry`): emitted for every MUTATING invocation
 * that PASSES the gate and EXECUTES — gate rejections never emit, so the
 * self-heal-rate denominator is "actions taken". Content-free: no arg values.
 */
export function emitControlAction(
  emit: Emitter,
  fields: { tool: string; ok: boolean; error?: string; mode: string },
): void {
  emit({
    event_type: "control.action",
    tool: fields.tool,
    ok: fields.ok,
    error_code: fields.error,
    mode: fields.mode,
  });
}

function post(url: string, token: string, body: string): void {
  try {
    const parsed = new URL(url);
    const mod = parsed.protocol === "http:" ? http : https;
    const req = mod.request(
      parsed,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
        timeout: TIMEOUT_MS,
      },
      (res) => res.resume(),
    );
    req.on("timeout", () => req.destroy());
    req.on("error", () => {});
    req.write(body);
    req.end();
  } catch {
    // swallow — ≤200 ms then give up silently, per the genome
  }
}
