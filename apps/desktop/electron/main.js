// Electron main process — the thin shell hosting the Windy Talk face + voice
// client (voice-session.v1). The renderer does all the work; this just frames a
// transparent always-on-top window and loads the page.
import { fileURLToPath } from "node:url";
import path from "node:path";

import { BrowserWindow, app } from "electron";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEMO = process.env.WINDYTALK_DEMO || ""; // e.g. "speaking" for a screenshot

const SHOT = process.env.WINDYTALK_SHOT || "";

function createWindow() {
  const win = new BrowserWindow({
    width: 340,
    height: 500,
    frame: false,
    transparent: !SHOT, // offscreen shots render opaque; the live app is transparent
    resizable: false,
    alwaysOnTop: !SHOT,
    show: !SHOT,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      offscreen: !!SHOT, // headless render for the screenshot path
    },
  });
  const query = DEMO ? `?demo=${encodeURIComponent(DEMO)}` : "";
  win.loadFile(path.join(__dirname, "..", "renderer", "index.html"), {
    search: query.replace(/^\?/, ""),
  });

  // Screenshot mode (verify the face renders): WINDYTALK_SHOT=/path.png
  if (SHOT) {
    win.webContents.on("did-finish-load", async () => {
      await new Promise((r) => setTimeout(r, 900)); // let the canvas animate a few frames
      try {
        const img = await win.webContents.capturePage();
        const fs = await import("node:fs");
        fs.writeFileSync(SHOT, img.toPNG());
      } catch (e) {
        console.error("capture failed:", e);
      }
      app.quit();
    });
    setTimeout(() => app.quit(), 8000); // hard safety: never hang
  }
  return win;
}

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
