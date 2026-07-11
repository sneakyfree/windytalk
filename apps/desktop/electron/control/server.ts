// The :8782 control host — slice 0 ships the FULL security wall (the proven
// hands/surface.py gate, re-expressed in TS per BUILD_NOTES §3) plus the two
// endpoints slice 0 needs:
//   GET  /ping            -> serving-attesting echo (heartbeat + staleness probe)
//   POST /instance/hello  -> single-instance ack: focuses the window, echoes pid
// Slice 1 grows /tools, /invoke and real MCP on this same wall.
//
// Serving attestation: unlike the Python reference (whose listener lives on a
// SEPARATE daemon thread — the round-4 trap), Node handlers run ON the main
// event loop, so a completed response here IS a serving-path round-trip. The
// kill decision still never consults a bare TCP accept (see watcher.ts).
import http from "node:http";

import { CONTROL_PORT, MAX_BODY_BYTES, TOKEN_HEADER } from "./constants.js";
import { tokenEquals } from "./token.js";

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

export interface ControlServerOpts {
  token: string;
  port?: number;
  /** Called on a valid /instance/hello ack (a second launch wants focus). */
  onFocusRequest?: () => void;
  /** Slice 1 dispatch hook; slice 0 has no tools yet. */
  now?: () => number;
}

export type BindResult =
  | { ok: true; port: number }
  | { ok: false; reason: "squatter" | "error"; detail: string };

export class ControlServer {
  private server: http.Server | null = null;
  private lastServedAtMs = 0;
  readonly opts: ControlServerOpts;

  constructor(opts: ControlServerOpts) {
    this.opts = opts;
  }

  /** ms timestamp of the last successfully SERVED (guard-passing) round-trip. */
  get lastServedAt(): number {
    return this.lastServedAtMs;
  }

  /**
   * Bind loopback:8782. Per the contract $port_note we NEVER silently bind a
   * different port: EADDRINUSE while THIS process holds instance.lock means a
   * foreign same-user process is squatting — surfaced to the caller.
   */
  bind(): Promise<BindResult> {
    const port = this.opts.port ?? CONTROL_PORT;
    return new Promise((resolve) => {
      const server = http.createServer((req, res) => this.handle(req, res));
      server.on("error", (err: NodeJS.ErrnoException) => {
        this.server = null;
        if (err.code === "EADDRINUSE") {
          resolve({ ok: false, reason: "squatter", detail: `port ${port} is taken by a foreign process` });
        } else {
          resolve({ ok: false, reason: "error", detail: String(err) });
        }
      });
      server.listen(port, "127.0.0.1", () => {
        this.server = server;
        const addr = server.address();
        resolve({ ok: true, port: typeof addr === "object" && addr ? addr.port : port });
      });
    });
  }

  close(): void {
    this.server?.close();
    this.server = null;
  }

  private send(res: http.ServerResponse, code: number, payload: unknown): void {
    const body = JSON.stringify(payload);
    // deliberately NO Access-Control-Allow-Origin: no site may read us.
    res.writeHead(code, {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(body),
    });
    res.end(body);
  }

  /** The wall: any Origin -> reject; loopback Host only; constant-time token. */
  private guard(req: http.IncomingMessage, res: http.ServerResponse): boolean {
    if (req.headers.origin) {
      this.send(res, 403, { ok: false, error: "forbidden" });
      return false;
    }
    const hostHdr = (req.headers.host ?? "").replace(/:\d+$/, "").replace(/^\[|\]$/g, "");
    if (hostHdr && !LOOPBACK_HOSTS.has(hostHdr)) {
      this.send(res, 403, { ok: false, error: "forbidden host" });
      return false;
    }
    const presented = String(req.headers[TOKEN_HEADER] ?? "");
    if (!tokenEquals(presented, this.opts.token)) {
      this.send(res, 401, { ok: false, error: "unauthorized" });
      return false;
    }
    return true;
  }

  private handle(req: http.IncomingMessage, res: http.ServerResponse): void {
    if (req.method === "OPTIONS") {
      // No CORS preflight is ever honored — a browser cannot use this API.
      this.send(res, 405, { error: "method not allowed" });
      return;
    }
    if (!this.guard(req, res)) return;
    const path = (req.url ?? "/").split("?")[0].replace(/\/+$/, "") || "/";

    if (req.method === "GET" && path === "/ping") {
      this.served();
      this.send(res, 200, { ok: true, result: { pong: true, pid: process.pid } });
      return;
    }
    if (req.method === "POST" && path === "/instance/hello") {
      this.readBody(req, res, () => {
        this.opts.onFocusRequest?.();
        this.served();
        this.send(res, 200, { ok: true, result: { pid: process.pid } });
      });
      return;
    }
    this.send(res, 404, { error: "not found" });
  }

  private served(): void {
    this.lastServedAtMs = (this.opts.now ?? Date.now)();
  }

  private readBody(
    req: http.IncomingMessage,
    res: http.ServerResponse,
    done: (body: string) => void,
  ): void {
    let size = 0;
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > MAX_BODY_BYTES) {
        this.send(res, 413, { ok: false, error: "too large" });
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => {
      if (size <= MAX_BODY_BYTES) done(Buffer.concat(chunks).toString("utf8"));
    });
  }
}

/**
 * One serving-attesting round-trip against a control port: a full guarded HTTP
 * exchange, never a bare TCP accept. Used by the second instance's ack and by
 * the supervisor's own heartbeat attestation (the "third attestation source").
 */
export function pingControlPort(
  port: number,
  token: string,
  timeoutMs: number,
): Promise<{ ok: boolean; pid: number | null }> {
  return new Promise((resolve) => {
    const req = http.request(
      { host: "127.0.0.1", port, path: "/ping", method: "GET", headers: { [TOKEN_HEADER]: token }, timeout: timeoutMs },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            const parsed = JSON.parse(data);
            resolve({ ok: parsed?.ok === true, pid: parsed?.result?.pid ?? null });
          } catch {
            resolve({ ok: false, pid: null });
          }
        });
      },
    );
    req.on("timeout", () => {
      req.destroy();
      resolve({ ok: false, pid: null });
    });
    req.on("error", () => resolve({ ok: false, pid: null }));
    req.end();
  });
}

/** The /instance/hello ack round-trip (asks the holder to focus itself). */
export function helloControlPort(
  port: number,
  token: string,
  timeoutMs: number,
): Promise<{ ok: boolean; pid: number | null }> {
  return new Promise((resolve) => {
    const body = JSON.stringify({ pid: process.pid });
    const req = http.request(
      {
        host: "127.0.0.1",
        port,
        path: "/instance/hello",
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
          [TOKEN_HEADER]: token,
        },
        timeout: timeoutMs,
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            const parsed = JSON.parse(data);
            resolve({ ok: parsed?.ok === true, pid: parsed?.result?.pid ?? null });
          } catch {
            resolve({ ok: false, pid: null });
          }
        });
      },
    );
    req.on("timeout", () => {
      req.destroy();
      resolve({ ok: false, pid: null });
    });
    req.on("error", () => resolve({ ok: false, pid: null }));
    req.write(body);
    req.end();
  });
}
