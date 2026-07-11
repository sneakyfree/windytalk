// Pinned on-disk locations (contract security.token.storage + resurrection paths).
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { controlPaths } from "../electron/control/paths.js";

test("macOS/Linux: everything under ~/.windytalk (the pinned dir)", () => {
  for (const platform of ["linux", "darwin"] as const) {
    const p = controlPaths(platform, {});
    const base = path.join(os.homedir(), ".windytalk");
    assert.equal(p.token, path.join(base, "control-token"));
    assert.equal(p.portFile, path.join(base, "control-port"));
    assert.equal(p.heartbeat, path.join(base, "heartbeat"));
    assert.equal(p.instanceLock, path.join(base, "instance.lock"));
  }
});

test("Windows: token/port in %APPDATA%, heartbeat in %LOCALAPPDATA% (both pinned)", () => {
  const p = controlPaths("win32", {
    APPDATA: "C:\\Users\\g\\AppData\\Roaming",
    LOCALAPPDATA: "C:\\Users\\g\\AppData\\Local",
    USERNAME: "g",
  });
  assert.equal(p.token, path.join("C:\\Users\\g\\AppData\\Roaming", "WindyTalk", "control-token"));
  assert.equal(p.heartbeat, path.join("C:\\Users\\g\\AppData\\Local", "WindyTalk", "heartbeat"));
  assert.match(p.instanceSocket, /^\\\\\.\\pipe\\windytalk-instance-/);
});

test("WINDYTALK_CONTROL_DIR (tests only) redirects the whole tree", () => {
  const p = controlPaths("linux", { WINDYTALK_CONTROL_DIR: "/tmp/x" });
  assert.equal(p.token, "/tmp/x/control-token");
  assert.equal(p.heartbeat, "/tmp/x/heartbeat");
  assert.equal(p.instanceSocket, "/tmp/x/instance.sock");
});
