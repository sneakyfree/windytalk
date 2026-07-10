// Electron main process — the shell hosting the Windy Talk control panel + voice
// client (voice-session.v1). The renderer does the voice work; main owns the window,
// the preload bridge, and the IPC handler that proxies hands tool calls (so the
// renderer never touches CORS and the hands token stays out of the sandbox).
import { fileURLToPath } from "node:url";
import path from "node:path";
import http from "node:http";

import { BrowserWindow, app, ipcMain } from "electron";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEMO = process.env.WINDYTALK_DEMO || "";
const SHOT = process.env.WINDYTALK_SHOT || "";
const HANDS_URL = process.env.WINDYTALK_HANDS_URL || "http://127.0.0.1:8781";
const HANDS_TOKEN = process.env.WINDYTALK_HANDS_TOKEN || "";

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
        const fs = await import("node:fs");
        fs.writeFileSync(SHOT, img.toPNG());
      } catch (e) {
        console.error("capture failed:", e);
      }
      app.quit();
    });
    setTimeout(() => app.quit(), 10000);
  }
  return win;
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

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
