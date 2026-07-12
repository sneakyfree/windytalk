// P4 drift-catcher: electron-builder's artifactName templates MUST render to
// exactly what ghsource.ts artifactAssetName looks for on the update channel.
// A renamed installer would ship fine and then no install could ever find its
// own update — this locks the two rules together forever.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { artifactAssetName } from "../electron/control/ghsource.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
// compiled test runs from dist/test/, source from test/ — walk up to the app root
function appRoot(): string {
  let d = HERE;
  while (!fs.existsSync(path.join(d, "electron-builder.json"))) {
    const up = path.dirname(d);
    if (up === d) throw new Error("electron-builder.json not found above test dir");
    d = up;
  }
  return d;
}

function render(template: string, version: string, ext: string): string {
  return template.replaceAll("${version}", version).replaceAll("${ext}", ext);
}

test("installer artifact names match the update channel's naming rule", () => {
  const cfg = JSON.parse(
    fs.readFileSync(path.join(appRoot(), "electron-builder.json"), "utf8"),
  );
  const v = "9.9.9";
  assert.equal(render(cfg.linux.artifactName, v, "AppImage"), artifactAssetName(v, "linux", "x64"));
  assert.equal(render(cfg.win.artifactName, v, "exe"), artifactAssetName(v, "win32", "x64"));
  assert.equal(render(cfg.mac.artifactName, v, "dmg"), artifactAssetName(v, "darwin", "arm64"));
  assert.equal(render(cfg.mac.artifactName, v, "dmg"), artifactAssetName(v, "darwin", "x64"));
  // and the mac target really is the ONE universal fat binary
  assert.deepEqual(cfg.mac.target, [{ target: "dmg", arch: ["universal"] }]);
});
