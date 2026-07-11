// The :8782 control host — the FULL security wall (the proven hands/surface.py
// gate, re-expressed in TS per BUILD_NOTES §3) serving:
//   GET  /ping            -> serving-attesting echo (heartbeat + staleness probe)
//   POST /instance/hello  -> single-instance ack: focuses the window, echoes pid
//   GET  /tools           -> the advertised (built) tool list          [slice 1]
//   POST /invoke          -> {tool, args} -> {ok, result, error}       [slice 1]
//   POST /mcp             -> MCP JSON-RPC (initialize lifecycle, tools) [slice 1]
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
  /** Tool dispatch (slice 1+): POST /invoke {tool, args}. */
  dispatch?: (tool: string, args: Record<string, unknown>) => Promise<unknown>;
  /** GET /tools payload (the advertised built tools). */
  toolList?: () => unknown[];
  /** MCP JSON-RPC handler; null result = notification (HTTP 204). */
  mcp?: (req: unknown) => Promise<Record<string, unknown> | null>;
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
    if (req.method === "GET" && path === "/tools" && this.opts.toolList) {
      this.served();
      this.send(res, 200, { tools: this.opts.toolList() });
      return;
    }
    if (req.method === "POST" && path === "/invoke" && this.opts.dispatch) {
      this.readBody(req, res, (body) => {
        const parsed = this.parseJson(body, res);
        if (parsed === undefined) return;
        const tool = String((parsed as { tool?: unknown }).tool ?? "");
        const args = ((parsed as { args?: unknown }).args as Record<string, unknown>) ?? {};
        void this.opts
          .dispatch!(tool, args)
          .then((out) => {
            this.served();
            this.send(res, 200, out);
          })
          .catch((e) => this.send(res, 200, { ok: false, error: `dispatch failed: ${String(e)}` }));
      });
      return;
    }
    if (req.method === "POST" && path === "/mcp" && this.opts.mcp) {
      this.readBody(req, res, (body) => {
        const parsed = this.parseJson(body, res);
        if (parsed === undefined) return;
        void this.opts
          .mcp!(parsed)
          .then((out) => {
            this.served();
            if (out === null) {
              res.writeHead(204).end(); // JSON-RPC notification: no body
            } else {
              this.send(res, 200, out);
            }
          })
          .catch((e) =>
            this.send(res, 200, {
              jsonrpc: "2.0",
              id: null,
              error: { code: -32603, message: `internal: ${String(e)}` },
            }),
          );
      });
      return;
    }
    this.send(res, 404, { error: "not found" });
  }

  /** Parse a JSON body; sends a 400 and returns undefined on bad input. */
  private parseJson(body: string, res: http.ServerResponse): unknown {
    try {
      const parsed: unknown = JSON.parse(body || "{}");
      if (typeof parsed !== "object" || parsed === null) {
        this.send(res, 400, { error: "bad body" });
        return undefined;
      }
      return parsed;
    } catch {
      this.send(res, 400, { error: "bad json" });
      return undefined;
    }
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
