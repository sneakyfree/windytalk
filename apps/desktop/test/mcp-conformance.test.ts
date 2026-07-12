// Shared MCP conformance — the CONTROL (TypeScript) driver.
//
// Feeds every case in contracts/mcp-conformance.v1.json to the control surface's
// MCP handler and asserts the result. The behaviors live in that one shared file;
// the hands (Python) surface runs the SAME cases through its own driver
// (tests/test_mcp_conformance.py). Neither rail can drift from the rulebook
// without one of these two suites failing.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { ConfigStore } from "../electron/control/config.js";
import { RecoveryCoordinator } from "../electron/control/coordinator.js";
import { EngineAllowList } from "../electron/control/engine-allow.js";
import { CrashLoopDetector } from "../electron/control/layer1.js";
import { LkgStore } from "../electron/control/lkg.js";
import { LogRing } from "../electron/control/logring.js";
import { ControlMcp } from "../electron/control/mcp.js";
import { ControlTools, OFFLINE_STATUS } from "../electron/control/tools.js";

const SAFE_READ_TOOL = "get_capabilities"; // exempt, auto_allow, side-effect-free

// -- locate the shared rulebook (walk up to the repo root) ----------------------

function findRulebook(): string {
  let dir = path.dirname(fileURLToPath(import.meta.url));
  for (let i = 0; i < 8; i++) {
    const candidate = path.join(dir, "contracts", "mcp-conformance.v1.json");
    if (fs.existsSync(candidate)) return candidate;
    dir = path.dirname(dir);
  }
  throw new Error("could not locate contracts/mcp-conformance.v1.json");
}

interface Case {
  name: string;
  request: unknown;
  expect: { no_response?: boolean; asserts?: unknown[][] };
}

function loadCases(): Case[] {
  const doc = JSON.parse(fs.readFileSync(findRulebook(), "utf8"));
  return doc.cases as Case[];
}

// -- the control MCP handler under test -----------------------------------------

function makeHandler(): (req: unknown) => Promise<Record<string, unknown> | null> {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-conf-"));
  const config = new ConfigStore(dir);
  const tools = new ControlTools({
    coordinator: new RecoveryCoordinator(),
    config,
    allowList: new EngineAllowList(dir),
    detector: new CrashLoopDetector({ tripSafeMode: () => {} }),
    rendererStatus: () => ({ ...OFFLINE_STATUS, connection: "online" }),
    reconnectEngine: async () => true,
    applyActiveConfig: () => {},
    resurrectionArmed: () => true,
    version: "conformance",
    startedAtMs: Date.now(),
    emit: () => {},
    logs: new LogRing(),
    probe: async () => null,
    confirm: async () => "allow",
    lkg: new LkgStore(dir),
    deepReconnectEngine: async () => true,
    clearCaches: async () => {},
    repairResurrection: async () => ({ armed: true, detail: "" }),
    restartApp: () => {},
    resetCrashCounter: () => {},
    entitledBrains: () => [],
  });
  const mcp = new ControlMcp({ tools, version: "conformance" });
  return (req) => mcp.handle(req);
}

// -- the shared evaluator (kept behaviorally identical to the Python driver) -----

const MISSING = Symbol("missing");

function subst(obj: unknown, tool: string): unknown {
  if (Array.isArray(obj)) return obj.map((v) => subst(v, tool));
  if (obj && typeof obj === "object") {
    return Object.fromEntries(Object.entries(obj).map(([k, v]) => [k, subst(v, tool)]));
  }
  return obj === "$SAFE_READ_TOOL" ? tool : obj;
}

function get(obj: unknown, dotted: string): unknown {
  let cur: unknown = obj;
  for (const seg of dotted.split(".")) {
    if (/^-?\d+$/.test(seg)) {
      if (!Array.isArray(cur)) return MISSING;
      const i = Number(seg);
      const idx = i < 0 ? cur.length + i : i;
      if (idx < 0 || idx >= cur.length) return MISSING;
      cur = cur[idx];
    } else if (cur && typeof cur === "object" && seg in (cur as Record<string, unknown>)) {
      cur = (cur as Record<string, unknown>)[seg];
    } else {
      return MISSING;
    }
  }
  return cur;
}

function typeName(v: unknown): string {
  if (v === MISSING) return "missing";
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  const t = typeof v;
  if (t === "object") return "object";
  if (t === "boolean") return "boolean";
  if (t === "number") return "number";
  if (t === "string") return "string";
  return "unknown";
}

function deepEqual(a: unknown, b: unknown): boolean {
  try {
    assert.deepEqual(a, b);
    return true;
  } catch {
    return false;
  }
}

function check(resp: unknown, a: unknown[]): void {
  const op = a[0] as string;
  if (op === "equal") {
    assert.ok(deepEqual(get(resp, a[1] as string), a[2]), `equal ${a[1]}`);
  } else if (op === "type") {
    assert.equal(typeName(get(resp, a[1] as string)), a[2], `type ${a[1]}`);
  } else if (op === "nonempty_array") {
    const v = get(resp, a[1] as string);
    assert.ok(Array.isArray(v) && v.length >= 1, `nonempty_array ${a[1]}`);
  } else if (op === "structured_matches_text") {
    const text = get(resp, "result.content.0.text");
    assert.equal(typeof text, "string", "content text must be a string");
    const parsed = JSON.parse(text as string); // MUST be valid JSON (never str()-rendered)
    assert.ok(deepEqual(parsed, get(resp, "result.structuredContent")), "structuredContent must match text");
  } else {
    throw new Error(`unknown assert op: ${op}`);
  }
}

// -- run every shared case ------------------------------------------------------

for (const c of loadCases()) {
  test(`control MCP conformance: ${c.name}`, async () => {
    const handler = makeHandler();
    const resp = await handler(subst(c.request, SAFE_READ_TOOL));
    if (c.expect.no_response) {
      assert.equal(resp, null, "a notification must produce no response");
      return;
    }
    assert.notEqual(resp, null, "a request must produce a response");
    for (const a of c.expect.asserts ?? []) check(resp, a);
  });
}

test("control MCP conformance: the rulebook is the shared file and non-empty", () => {
  assert.match(findRulebook(), /contracts[/\\]mcp-conformance\.v1\.json$/);
  assert.ok(loadCases().length >= 8);
});
