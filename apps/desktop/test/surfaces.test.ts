// ADR-060 §3.8 discovery registry — register/unregister semantics.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import test from "node:test";

import { registerSurface, unregisterSurface, registryPath } from "../electron/control/surfaces.js";

function tmpHome(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "wt-surfaces-"));
}

const TALK = {
  product: "windytalk",
  version: "0.1.0",
  class: "desktop" as const,
  contract: "control.mcp.v1",
  doctrine: "ADR-060 v1.0",
  http: "http://127.0.0.1:8782",
  mcp: "http://127.0.0.1:8782/mcp",
  token_path: "/home/x/.windytalk/control-token",
  health: "/invoke get_health",
  pid: 4242,
};

function read(home: string) {
  return JSON.parse(fs.readFileSync(registryPath(home), "utf8"));
}

test("register writes a 0600 entry with the ADR-060 §3.8 required fields", () => {
  const home = tmpHome();
  registerSurface(TALK, home);
  const file = registryPath(home);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600, "registry is owner-only");
  const doc = read(home);
  assert.equal(doc.surfaces.length, 1);
  const e = doc.surfaces[0];
  for (const k of ["product", "version", "contract", "http"]) {
    assert.ok(e[k], `required field ${k} present`);
  }
  assert.match(e.http, /^http:\/\/127\.0\.0\.1:[0-9]+/, "http is loopback-only per schema");
  assert.equal(e.pid, 4242);
  fs.rmSync(home, { recursive: true, force: true });
});

test("register is idempotent: re-registering replaces our own entry (no duplicates)", () => {
  const home = tmpHome();
  registerSurface(TALK, home);
  registerSurface({ ...TALK, version: "0.2.0", pid: 9999 }, home);
  const doc = read(home);
  assert.equal(doc.surfaces.length, 1, "one entry per product");
  assert.equal(doc.surfaces[0].version, "0.2.0", "latest wins (stale self-entry overwritten)");
  assert.equal(doc.surfaces[0].pid, 9999);
  fs.rmSync(home, { recursive: true, force: true });
});

test("register PRESERVES other products' entries; unregister removes only ours", () => {
  const home = tmpHome();
  // a sibling product (e.g. Windy Word) already registered
  fs.mkdirSync(path.dirname(registryPath(home)), { recursive: true });
  fs.writeFileSync(
    registryPath(home),
    JSON.stringify({
      surfaces: [
        { product: "windy-word", version: "1.6.2", contract: "control.mcp.v1", http: "http://127.0.0.1:18765" },
      ],
    }),
  );
  registerSurface(TALK, home);
  let products = read(home).surfaces.map((s: { product: string }) => s.product).sort();
  assert.deepEqual(products, ["windy-word", "windytalk"], "both surfaces coexist");

  unregisterSurface("windytalk", home);
  products = read(home).surfaces.map((s: { product: string }) => s.product);
  assert.deepEqual(products, ["windy-word"], "unregister removes only windytalk; the sibling survives");
  fs.rmSync(home, { recursive: true, force: true });
});

test("corrupt or missing registry never throws (best-effort discovery)", () => {
  const home = tmpHome();
  // unregister on a missing file is a silent no-op
  assert.doesNotThrow(() => unregisterSurface("windytalk", home));
  // a corrupt file is treated as fresh, not fatal
  fs.mkdirSync(path.dirname(registryPath(home)), { recursive: true });
  fs.writeFileSync(registryPath(home), "{ this is not json");
  assert.doesNotThrow(() => registerSurface(TALK, home));
  assert.equal(read(home).surfaces.length, 1, "recovered from corrupt into a valid registry");
  fs.rmSync(home, { recursive: true, force: true });
});
