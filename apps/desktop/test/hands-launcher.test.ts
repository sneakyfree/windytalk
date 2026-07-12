// P4 tests: the bundled-hands launcher (BYO-runtime at runtime). Fixture
// payload dirs + an injected spawn prove interpreter selection per OS/arch and
// the env contract (shared token, PYTHONPATH, bundled tools ahead of PATH)
// without spawning anything real.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { launchBundledHands, payloadPython } from "../electron/control/hands-launcher.js";

function fixture(pythonRel: string | null): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "wt-launcher-"));
  if (pythonRel) {
    const p = path.join(root, "payload", pythonRel);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, "");
  }
  return root;
}

test("payloadPython picks the right frozen interpreter per OS/arch", () => {
  const lin = fixture("python/bin/python3");
  assert.equal(payloadPython(lin, "linux", "x64"), path.join(lin, "payload", "python", "bin", "python3"));

  const win = fixture("python/python.exe");
  assert.equal(payloadPython(win, "win32", "x64"), path.join(win, "payload", "python", "python.exe"));

  // macOS dual-runtime payload: arch picks the folder
  const macX = fixture("python-x64/bin/python3");
  assert.equal(payloadPython(macX, "darwin", "x64"), path.join(macX, "payload", "python-x64", "bin", "python3"));
  const macA = fixture("python-arm64/bin/python3");
  assert.equal(payloadPython(macA, "darwin", "arm64"), path.join(macA, "payload", "python-arm64", "bin", "python3"));
  // a future universal python/ wins over per-arch dirs
  const macU = fixture("python/bin/python3");
  assert.equal(payloadPython(macU, "darwin", "arm64"), path.join(macU, "payload", "python", "bin", "python3"));

  assert.equal(payloadPython(fixture(null)), null); // dev checkout: no payload
});

test("launchBundledHands: env contract (token, PYTHONPATH, tools on PATH)", () => {
  const root = fixture(
    process.platform === "win32" ? "python/python.exe" : "python/bin/python3",
  );
  const calls: Array<{ cmd: string; args: string[]; env: NodeJS.ProcessEnv }> = [];
  const fakeSpawn = ((cmd: string, args: string[], opts: { env: NodeJS.ProcessEnv }) => {
    calls.push({ cmd, args, env: opts.env });
    return { on() {}, kill() {} };
  }) as never;

  const launch = launchBundledHands(root, { env: { PATH: "/usr/bin" }, spawnImpl: fakeSpawn });
  assert.ok(launch, "payload present -> must launch");
  assert.ok(launch.token.length >= 32, "a real generated token");
  assert.equal(calls.length, 1, "spawn was called once");
  const g = calls[0];
  assert.deepEqual(g.args, ["-m", "hands"]);
  assert.equal(g.env.WINDYTALK_HANDS_TOKEN, launch.token); // ONE shared secret
  assert.equal(g.env.PYTHONPATH, path.join(root, "payload", "app-py"));
  assert.ok(
    g.env.PATH!.startsWith(path.join(root, "payload", "tools") + path.delimiter),
    "bundled tools ride ahead of the system PATH",
  );
  // caller-provided token is reused, not replaced
  const relaunch = launchBundledHands(root, {
    env: { PATH: "/usr/bin", WINDYTALK_HANDS_TOKEN: "grant-set-this" },
    spawnImpl: fakeSpawn,
  });
  assert.equal(relaunch!.token, "grant-set-this");

  assert.equal(launchBundledHands(fixture(null), { spawnImpl: fakeSpawn }), null);
});
