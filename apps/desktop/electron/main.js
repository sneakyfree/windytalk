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

import { BrowserWindow, Notification, app, dialog, ipcMain, session } from "electron";

import { controlPaths } from "../dist/electron/control/paths.js";
import { loadOrCreateToken } from "../dist/electron/control/token.js";
import { selfIdentity } from "../dist/electron/control/identity.js";
import { acquireInstanceLock } from "../dist/electron/control/instance.js";
import { ControlServer } from "../dist/electron/control/server.js";
import { makeAttestor } from "../dist/electron/control/attest.js";
import { HeartbeatWriter } from "../dist/electron/control/heartbeat.js";
import { ensureResurrection, checkArmed } from "../dist/electron/resurrection/installer.js";
import { ConfigStore } from "../dist/electron/control/config.js";
import { EngineAllowList } from "../dist/electron/control/engine-allow.js";
import { RecoveryCoordinator } from "../dist/electron/control/coordinator.js";
import { CrashLoopDetector } from "../dist/electron/control/layer1.js";
import { Supervisor } from "../dist/electron/control/supervisor.js";
import { ControlTools } from "../dist/electron/control/tools.js";
import { ControlMcp } from "../dist/electron/control/mcp.js";
import { makeEmitter } from "../dist/electron/control/emit.js";
import { LogRing } from "../dist/electron/control/logring.js";
import { LkgStore } from "../dist/electron/control/lkg.js";
import { removeHeartbeat } from "../dist/electron/control/heartbeat.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEMO = process.env.WINDYTALK_DEMO || "";
const SHOT = process.env.WINDYTALK_SHOT || "";
const HANDS_URL = process.env.WINDYTALK_HANDS_URL || "http://127.0.0.1:8781";
const HANDS_TOKEN = process.env.WINDYTALK_HANDS_TOKEN || "";

let mainWindow = null;
let configStore = null; // the hands proxy gates on safe mode (hands off as a unit)

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

  const sup = global.__windytalkSupervisor;
  if (sup) {
    win.webContents.on("unresponsive", () =>
      sup.supervisor.onRendererGone("unresponsive", () => win.webContents.forcefullyCrashRenderer()),
    );
    win.webContents.on("render-process-gone", (_e, details) => {
      if (details.reason !== "clean-exit") {
        sup.supervisor.onRendererGone(details.reason, () => win.webContents.reload());
      }
    });
  }

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

// Boot the control plane (control.mcp.v1): single-instance lock -> supervisor
// (Layer 1 + coordinator + tools) -> :8782 wall -> heartbeat -> resurrection
// self-check/auto-repair. Returns false when this launch must exit (a healthy
// holder was focused, or a squatter case).
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

  // -- supervisor graph (Layer 1 + coordinator + the tool surface) ------------
  const logs = new LogRing();
  const slog = (m, level = "info") => {
    console.log(`[windytalk] ${m}`);
    logs.append(level, m);
  };
  const lkg = new LkgStore(paths.configDir);
  configStore = new ConfigStore(paths.configDir, { fallback: () => lkg.loadBest() });
  if (configStore.loadedFrom !== "config") {
    slog(`config recovered from ${configStore.loadedFrom} (config.json was corrupt/absent)`, "warn");
  }
  const allowList = new EngineAllowList(paths.configDir);
  const coordinator = new RecoveryCoordinator();
  let tools = null; // created below; the detector's trip closes over it
  const detector = new CrashLoopDetector({
    tripSafeMode: (reason) => {
      slog(`layer1 safe-mode trip: ${reason}`, "warn");
      supervisor.notice("Something kept crashing, so I switched to safe mode to stay stable.");
      if (tools) void tools.layer1TripSafeMode();
    },
    log: (m) => slog(m),
  });
  const supervisor = new Supervisor({
    detector,
    sendCommand: (cmd) => {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("windytalk:cmd", cmd);
    },
    log: (m) => slog(m, "warn"),
  });
  let resurrectionArmed = false;
  tools = new ControlTools({
    coordinator,
    config: configStore,
    allowList,
    detector,
    rendererStatus: () => supervisor.rendererStatus(),
    reconnectEngine: (t) => supervisor.reconnectEngine(t),
    applyActiveConfig: () => supervisor.applyActiveConfig(configStore.getActive()),
    resurrectionArmed: () => resurrectionArmed,
    version: app.getVersion(),
    startedAtMs: Date.now(),
    emit: makeEmitter(),
    logs,
    probe: (kind, timeoutMs) => supervisor.probe(kind, timeoutMs),
    confirm: (req) => nativeConfirm(req),
    lkg,
    deepReconnectEngine: (t) => supervisor.deepReconnectEngine(t),
    clearCaches: async () => {
      // Transient caches only: HTTP cache + shader/code caches. Settings,
      // history, and on-device MODELS are untouched (contract clear_cache).
      await session.defaultSession.clearCache();
      await session.defaultSession.clearCodeCaches({ urls: [] }).catch(() => {});
      slog("caches cleared");
    },
    repairResurrection: async () => {
      const appLaunch = app.isPackaged
        ? { cmd: process.execPath, args: [] }
        : { cmd: process.execPath, args: [path.join(__dirname, "..")], cwd: path.join(__dirname, "..") };
      const status = await ensureResurrection({ appLaunch, log: (m) => slog(m) });
      resurrectionArmed = status.armed;
      return status;
    },
    restartApp: () => {
      // The single resurrection path: remove the heartbeat (tier1-absent ->
      // immediate relaunch), exit with the distinguished code.
      slog("restart_app: exiting via the resurrection path", "warn");
      removeHeartbeat(paths.heartbeat);
      app.exit(77);
    },
    resetCrashCounter: (why) => detector.resetCounter(why),
    notify: (text) => supervisor.notice(text),
    entitledBrains: () => {
      // The last-synced entitlement cache (offline-valid). Written by the
      // Mind session layer when entitlements sync; absent = only 'default'.
      try {
        const list = JSON.parse(fs.readFileSync(path.join(paths.configDir, "entitled-brains.json"), "utf8"));
        return Array.isArray(list) ? list.filter((b) => typeof b === "string") : [];
      } catch {
        return [];
      }
    },
    // Self-update (contract self_update). INERT until Grant embeds the signing
    // key: updateConfigured() is false, so updateSource returns null and
    // apply_update/check_for_update stay forced-honest. When keyed, a GitHub-
    // Releases source is constructed here. Built inert, never faked.
    updateSource: () => null,
    freeBytes: () => {
      try {
        const st = fs.statfsSync(paths.stateDir);
        return st.bavail * st.bsize;
      } catch {
        return Number.MAX_SAFE_INTEGER;
      }
    },
    stageUpdate: async (_artifact) => {
      // IMMUTABILITY (self_update.out_of_process_rollback): apply_update A/B-
      // swaps ONLY the app binary — it MUST NOT touch the resurrection service,
      // the watchdog, or their configs. Staging is a no-op while INERT; the
      // real A/B copy + pointer flip lands with the keyed source. It runs OFF
      // the serving loop (a worker) so the heartbeat keeps bumping during
      // mode='updating'.
      throw new Error("staging unavailable: no update source configured");
    },
    engineIsLocal: () => {
      try {
        const host = new URL(configStore.getActive().engine_url).hostname;
        return ["127.0.0.1", "localhost", "::1"].includes(host);
      } catch {
        return false;
      }
    },
  });
  const mcp = new ControlMcp({ tools, version: app.getVersion() });

  let lastConn = null;
  ipcMain.on("windytalk:status", (_evt, status) => {
    if (status && status.connection !== lastConn) {
      slog(`engine connection: ${lastConn ?? "boot"} -> ${status.connection}`,
        status.connection === "online" ? "info" : "warn");
      lastConn = status.connection;
    }
    supervisor.onRendererStatus(status);
  });
  ipcMain.on("windytalk:probe", (_evt, { reqId, result }) => supervisor.onProbeResult(reqId, result));
  // The physical Reset button: in-process, no HTTP, no bearer token; its
  // in-app confirm dialog IS the always_confirm (contract consumers.the_reset_button).
  ipcMain.handle("windytalk:reset", () => tools.dispatch("reset_to_defaults", {}, { preconfirmed: true }));

  // The confirmer (security.confirmer_fallback): a native OS dialog drawn by
  // the SUPERVISOR — reachable even when the renderer is down. Fails CLOSED
  // ('unavailable') only if even the native dialog cannot render.
  async function nativeConfirm({ tool, message, allowSessionGrant }) {
    try {
      const buttons = allowSessionGrant
        ? ["Allow", "Always (this session)", "Don't allow"]
        : ["Allow", "Don't allow"];
      const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined;
      const cancelIndex = buttons.length - 1;
      const opts = {
        type: "question",
        title: "Windy Talk",
        message: "Your assistant wants to make a change",
        detail: message,
        buttons,
        // Default to "Don't allow": a stray Enter / focus-steal / auto-select
        // must NOT authorize a change (defense in depth for the floor tools).
        defaultId: cancelIndex,
        cancelId: cancelIndex,
        noLink: true,
      };
      const t0 = Date.now();
      const shown = win ? dialog.showMessageBox(win, opts) : dialog.showMessageBox(opts);
      // An unattended dialog must not hang the caller forever ('a denial
      // returns denied — never silence'): 60 s with no answer = deny.
      const timed = await Promise.race([
        shown,
        new Promise((r) => setTimeout(() => r(null), 60_000)),
      ]);
      if (timed === null) {
        slog(`confirm for ${tool} unanswered for 60 s — denied`, "warn");
        return "deny";
      }
      // FAIL CLOSED on a non-interactive dialog. On a headless/broken display
      // Electron's showMessageBox returns the DEFAULT button (index 0) without
      // a human ever seeing it — indistinguishable from a real "Allow" by value
      // alone. No human reacts to a fresh modal in under ~400 ms, so a sub-400ms
      // return means the dialog could not genuinely engage a person: deny (a
      // floor tool must NEVER auto-allow because the OS couldn't render).
      const elapsed = Date.now() - t0;
      if (elapsed < 400) {
        slog(`confirm for ${tool} returned in ${elapsed}ms (no real interaction) — failing closed`, "warn");
        return "unavailable";
      }
      const { response } = timed;
      if (response === 0) return "allow";
      if (allowSessionGrant && response === 1) return "allow_session";
      return "deny";
    } catch (e) {
      slog(`confirmer could not render (${String(e)}) — failing closed`, "error");
      return "unavailable";
    }
  }
  global.__windytalkSupervisor = { supervisor, tools, configStore, detector };

  const server = new ControlServer({
    token,
    onFocusRequest: focusMainWindow,
    dispatch: (tool, args) => tools.dispatch(tool, args),
    toolList: () => mcp.toolList(),
    mcp: (req) => mcp.handle(req),
  });
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
        resurrectionArmed = status.armed;
        if (!status.armed) surface("Windy Talk protection is off", status.detail);
        else console.log(`[windytalk] resurrection armed: ${status.detail}`);
      })
      .catch((e) => console.error(`[windytalk] resurrection self-check failed: ${String(e)}`));
  } else {
    console.log("[windytalk] resurrection install skipped (dev; set WINDYTALK_RESURRECTION=1 to arm)");
    checkArmed({ appLaunch: { cmd: process.execPath, args: [] } })
      .then((st) => { resurrectionArmed = st.armed; })
      .catch(() => {});
  }

  // LKG snapshots: after 60 s of continuous online (and not in safe mode —
  // a safe-mode session proves the OVERLAY works, not the saved config), the
  // saved config is a WORKING customization. LkgStore.write dedupes content.
  let onlineSince = null;
  setInterval(() => {
    const online = supervisor.rendererStatus().connection === "online";
    if (!online) {
      onlineSince = null;
      return;
    }
    if (onlineSince === null) onlineSince = Date.now();
    if (Date.now() - onlineSince >= 60_000 && !configStore.inSafeMode) {
      try {
        lkg.write(configStore.getSaved());
      } catch (e) {
        slog(`lkg write failed: ${String(e)}`, "warn");
      }
    }
  }, 30_000).unref();

  // A crash-looping machine relaunches INTO safe mode; say so plainly.
  if (configStore.inSafeMode) {
    setTimeout(() => supervisor.notice("Running in safe mode — the reliable floor."), 3000);
  }
  return true;
}

// Proxy hands tool calls through main (avoids renderer CORS; adds the bearer token).
ipcMain.handle("windytalk:hands", async (_evt, { tool, args }) => {
  // Safe mode = hands off (contract safe_mode.nature); exit_safe_mode is the
  // one thing that re-enables them as a unit.
  if (configStore?.inSafeMode) {
    return { ok: false, error: "denied", result: "hands are off in safe mode" };
  }
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
