// Resurrection relaunch-cmd pin (control-hardening #7): the relaunch spec must
// never be an arbitrary-command autostart primitive — the cmd must be the app's
// own binary (in production the watcher runs AS it), with an explicit dev/test
// escape.
import assert from "node:assert/strict";
import { test } from "node:test";

import { relaunchCmdAllowed } from "../electron/resurrection/watcher.js";

test("relaunch pin: the app's own binary (process.execPath) is allowed", () => {
  assert.equal(relaunchCmdAllowed(process.execPath, {}), true);
});

test("relaunch pin: an arbitrary command is REFUSED (no autostart injection)", () => {
  assert.equal(relaunchCmdAllowed("/bin/sh", {}), false);
  assert.equal(relaunchCmdAllowed("/usr/bin/python3", {}), false);
  assert.equal(relaunchCmdAllowed("/nonexistent/evil", {}), false);
});

test("relaunch pin: the explicit dev/test escape opts in", () => {
  assert.equal(relaunchCmdAllowed("/bin/sh", { WINDYTALK_ALLOW_FOREIGN_RELAUNCH: "1" }), true);
});

test("relaunch pin: a symlink to the app binary resolves and is allowed", async () => {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-relink-"));
  const link = path.join(dir, "electron");
  fs.symlinkSync(process.execPath, link);
  assert.equal(relaunchCmdAllowed(link, {}), true, "realpath must resolve the symlink to the app binary");
  fs.rmSync(dir, { recursive: true, force: true });
});
