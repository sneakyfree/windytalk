// Electron main process — the shell hosting the Windy Talk control panel + voice
// client (voice-session.v1), and since control.mcp.v1 slice 0 also the SUPERVISOR:
// the most stable local process, kept alive by the OS resurrection service. It
// owns the single-instance lock, the :8782 control host, and the serving-liveness
// heartbeat ("the doctor is not in the patient" — the patient is the renderer +
// engine; main outlives both).
import { fileURLToPath } from "node:url";
import fs from "node:fs";
import path from "node:path";
import http from "node:http";

import { BrowserWindow, Notification, app, ipcMain } from "electron";

import { controlPaths } from "../dist/electron/control/paths.js";
import { loadOrCreateToken } from "../dist/electron/control/token.js";
import { selfIdentity } from "../dist/electron/control/identity.js";
import { acquireInstanceLock } from "../dist/electron/control/instance.js";
import { ControlServer } from "../dist/electron/control/server.js";
import { makeAttestor } from "../dist/electron/control/attest.js";
import { HeartbeatWriter } from "../dist/electron/control/heartbeat.js";
import { ensureResurrection } from "../dist/electron/resurrection/installer.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEMO = process.env.WINDYTALK_DEMO || "";
const SHOT = process.env.WINDYTALK_SHOT || "";
const HANDS_URL = process.env.WINDYTALK_HANDS_URL || "http://127.0.0.1:8781";
const HANDS_TOKEN = process.env.WINDYTALK_HANDS_TOKEN || "";

let mainWindow = null;

function createWindow() {
  const win = new BrowserWindow({
    width: 360,
    height: 620,
    frame: false,
    transparent: !SHOT, // opaque for screenshots; transparent for the live app
    resizable: true,
    alwaysOnTop: !SHOT,
    show: !SHOT,
    backgroundColor: SHOT ? "#0a0f1a" : "#00000000",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      offscreen: !!SHOT,
    },
  });
  const query = DEMO ? `demo=${encodeURIComponent(DEMO)}` : "";
  win.loadFile(path.join(__dirname, "..", "renderer", "index.html"), { search: query });

  if (SHOT) {
    win.webContents.on("did-finish-load", async () => {
      await new Promise((r) => setTimeout(r, 1200)); // let the canvas animate
      try {
        const img = await win.webContents.capturePage();
        const fsm = await import("node:fs");
        fsm.writeFileSync(SHOT, img.toPNG());
      } catch (e) {
        console.error("capture failed:", e);
      }
      app.quit();
    });
    setTimeout(() => app.quit(), 10000);
  }
  mainWindow = win;
  return win;
}

function focusMainWindow() {
  const win = mainWindow;
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function surface(title, body) {
  console.error(`[windytalk] ${title}: ${body}`);
  try {
    new Notification({ title, body }).show();
  } catch {
    // headless / early boot — the log line stands
  }
}

// Boot the control plane (control.mcp.v1 slice 0): single-instance lock ->
// :8782 wall -> heartbeat -> resurrection self-check/auto-repair. Returns false
// when this launch must exit (a healthy holder was focused, or a squatter case).
async function bootControlPlane() {
  const paths = controlPaths();
  fs.mkdirSync(paths.configDir, { recursive: true, mode: 0o700 });
  fs.mkdirSync(paths.stateDir, { recursive: true, mode: 0o700 });
  const token = loadOrCreateToken(paths.token);
  const identity = await selfIdentity();

  const lock = await acquireInstanceLock({
    socketPath: paths.instanceSocket,
    lockFilePath: paths.instanceLock,
    portFilePath: paths.portFile,
    readToken: () => token,
    identity,
    log: (m) => console.log(`[windytalk] ${m}`),
  });
  if (lock.role === "second-focused") return false; // holder is healthy; we exit
  if (lock.role === "squatter" || lock.role === "error") {
    surface("Windy Talk could not start", lock.detail);
    return false;
  }

  const server = new ControlServer({ token, onFocusRequest: focusMainWindow });
  const bind = await server.bind();
  if (bind.ok) {
    fs.writeFileSync(paths.portFile, String(bind.port) + "\n", { mode: 0o600 });
  } else {
    // $port_note: we HOLD instance.lock but can't bind :8782 — a foreign
    // same-user process is squatting. Surface it; NEVER silently bind another
    // port. The app still runs (voice works); the control surface is down.
    surface(
      "Windy Talk control port blocked",
      `Another program is using port 8782 (${bind.detail}). Self-heal tools are offline until it exits.`,
    );
  }

  const heartbeat = new HeartbeatWriter({
    heartbeatPath: paths.heartbeat,
    identity,
    attest: bind.ok
      ? makeAttestor(server, bind.port, token)
      : async () => false, // no serving path -> no attestation -> honest staleness
    onError: (e) => console.error(`[windytalk] heartbeat write failed: ${String(e)}`),
  });
  heartbeat.start();

  // Resurrection self-check + auto-repair (contract resurrection.self_check).
  // Dev guard: a dev checkout must not register real OS units pointing at the
  // repo unless explicitly asked (WINDYTALK_RESURRECTION=1). Packaged = always.
  if (app.isPackaged || process.env.WINDYTALK_RESURRECTION === "1") {
    const appLaunch = app.isPackaged
      ? { cmd: process.execPath, args: [] }
      : { cmd: process.execPath, args: [path.join(__dirname, "..")], cwd: path.join(__dirname, "..") };
    ensureResurrection({ appLaunch, log: (m) => console.log(`[windytalk] ${m}`) })
      .then((status) => {
        if (!status.armed) surface("Windy Talk protection is off", status.detail);
        else console.log(`[windytalk] resurrection armed: ${status.detail}`);
      })
      .catch((e) => console.error(`[windytalk] resurrection self-check failed: ${String(e)}`));
  } else {
    console.log("[windytalk] resurrection install skipped (dev; set WINDYTALK_RESURRECTION=1 to arm)");
  }
  return true;
}

// Proxy hands tool calls through main (avoids renderer CORS; adds the bearer token).
ipcMain.handle("windytalk:hands", async (_evt, { tool, args }) => {
  return new Promise((resolve) => {
    try {
      const url = new URL("/invoke", HANDS_URL);
      const body = JSON.stringify({ tool, args });
      const req = http.request(
        url,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Content-Length": Buffer.byteLength(body),
            ...(HANDS_TOKEN ? { "X-Windytalk-Token": HANDS_TOKEN } : {}),
          },
          timeout: 30000,
        },
        (res) => {
          let data = "";
          res.on("data", (c) => (data += c));
          res.on("end", () => {
            try {
              resolve(JSON.parse(data));
            } catch {
              resolve({ ok: false, error: "hands: bad response" });
            }
          });
        },
      );
      req.on("timeout", () => {
        req.destroy();
        resolve({ ok: false, error: "timeout" });
      });
      req.on("error", (e) => resolve({ ok: false, error: `hands unreachable: ${e.message}` }));
      req.write(body);
      req.end();
    } catch (e) {
      resolve({ ok: false, error: `hands: ${String(e)}` });
    }
  });
});

ipcMain.on("windytalk:quit", () => app.quit());

app.whenReady().then(async () => {
  // SHOT is an offscreen screenshot utility, not the app: no lock, no :8782, no
  // heartbeat — a capture must never focus-steal or wrestle a live instance.
  if (!SHOT) {
    const proceed = await bootControlPlane().catch((e) => {
      console.error(`[windytalk] control plane failed: ${String(e)}`);
      return true; // the voice app still runs; the doctor being sick isn't fatal
    });
    if (!proceed) {
      app.quit();
      return;
    }
  }
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
