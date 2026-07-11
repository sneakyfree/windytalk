// MCP JSON-RPC for the control surface. Compliance is NORMATIVE and goes beyond
// the hands reference ($mcp_protocol_note): the server MUST answer `initialize`
// (echoing protocolVersion 2025-06-18) and `notifications/initialized`; results
// are canonical JSON in the text content AND `structuredContent`. The two known
// bugs in hands/surface.py.handle_mcp (no initialize lifecycle; str()-rendered
// results, which is not valid JSON) are exactly what this module must not copy.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import type { ControlTools, Envelope } from "./tools.js";

export const MCP_PROTOCOL = "2025-06-18";

interface ToolMeta {
  name: string;
  description: string;
  tier: string;
  inputSchema: Record<string, unknown>;
}

const EMPTY_SCHEMA = { type: "object", properties: {}, additionalProperties: false };

/** Load tool metadata from the frozen contract (authoritative descriptions). */
export function loadContractTools(explicitPath?: string): Map<string, ToolMeta> {
  const candidates = [
    explicitPath,
    process.env.WINDYTALK_CONTROL_CONTRACT,
    // dev checkout: <repo>/apps/desktop/dist/electron/control/mcp.js -> <repo>/contracts
    path.join(path.dirname(fileURLToPath(import.meta.url)), "../../../../..", "contracts", "control.mcp.v1.json"),
    // packaged: contracts shipped beside the app resources (future packaging)
    path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "contracts", "control.mcp.v1.json"),
  ].filter((p): p is string => !!p);
  const out = new Map<string, ToolMeta>();
  for (const candidate of candidates) {
    try {
      const doc = JSON.parse(fs.readFileSync(candidate, "utf8"));
      for (const t of doc.tools ?? []) {
        out.set(t.name, {
          name: t.name,
          description: t.description ?? "",
          tier: t.tier ?? "ask_first",
          inputSchema: t.inputSchema ?? EMPTY_SCHEMA,
        });
      }
      if (out.size > 0) return out;
    } catch {
      // try the next candidate
    }
  }
  return out; // empty: callers fall back to minimal metadata
}

export interface McpServerOpts {
  tools: ControlTools;
  version: string;
  contractTools?: Map<string, ToolMeta>;
}

export class ControlMcp {
  private readonly opts: McpServerOpts;
  private readonly meta: Map<string, ToolMeta>;

  constructor(opts: McpServerOpts) {
    this.opts = opts;
    this.meta = opts.contractTools ?? loadContractTools();
  }

  /** Advertised tools: the BUILT set, with contract metadata where available. */
  toolList(): ToolMeta[] {
    return this.opts.tools.builtTools().map(
      (name) =>
        this.meta.get(name) ?? {
          name,
          description: `${name} (see contracts/control.mcp.v1.json)`,
          tier: "auto_allow",
          inputSchema: EMPTY_SCHEMA,
        },
    );
  }

  /**
   * Handle one JSON-RPC message. Returns null for notifications (no response
   * body — the HTTP layer answers 204).
   */
  async handle(req: unknown): Promise<Record<string, unknown> | null> {
    // A batch (array) or non-object is Invalid Request. MCP 2025-06-18 removed
    // batching; answer -32600 rather than silently 204 it (an array would
    // otherwise destructure to id/method=undefined and look like a notification).
    if (typeof req !== "object" || req === null || Array.isArray(req)) {
      return { jsonrpc: "2.0", id: null, error: { code: -32600, message: "Invalid Request" } };
    }
    const { id, method, params } = req as { id?: unknown; method?: string; params?: Record<string, unknown> };
    const isNotification = id === undefined;

    // A message with no id is a NOTIFICATION: the server must send no response.
    // Only genuine notification methods are honored; a request method
    // (initialize/tools/call/…) sent without an id is malformed — do NOT
    // execute it and reply with a dropped id; ignore it per JSON-RPC.
    if (isNotification) {
      if (method === "notifications/initialized") return null;
      return null; // ignore any other id-less message; never execute+reply
    }

    if (method === "initialize") {
      return {
        jsonrpc: "2.0",
        id,
        result: {
          protocolVersion: MCP_PROTOCOL,
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: "windytalk-control", version: this.opts.version },
        },
      };
    }
    if (method === "ping") return { jsonrpc: "2.0", id, result: {} };
    if (method === "tools/list") {
      const tools = this.toolList().map((t) => ({
        name: t.name,
        description: t.description,
        inputSchema: t.inputSchema,
      }));
      return { jsonrpc: "2.0", id, result: { tools } };
    }
    if (method === "tools/call") {
      const name = String(params?.name ?? "");
      const args = (params?.arguments as Record<string, unknown>) ?? {};
      const res: Envelope = await this.opts.tools.dispatch(name, args);
      // Canonical JSON of the full envelope in BOTH representations — valid
      // JSON always (never a str()-rendered object), lossless for agents.
      const canonical = JSON.stringify(res);
      return {
        jsonrpc: "2.0",
        id,
        result: {
          content: [{ type: "text", text: canonical }],
          structuredContent: res as unknown as Record<string, unknown>,
          isError: !res.ok,
        },
      };
    }
    return { jsonrpc: "2.0", id, error: { code: -32601, message: `Method not found: ${method}` } };
  }
}
