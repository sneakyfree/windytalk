// Preload — the ONLY bridge between the sandboxed renderer and Node/Electron.
// contextIsolation is on and nodeIntegration is off, so the renderer cannot read
// process.env or reach the network's hands port on its own. We expose exactly two
// things on window.windytalk:
//   • cfg  — engine/hands URLs + build info from env (fixes the "URL unconfigurable"
//            bug: WINDYTALK_ENGINE_URL etc. now actually reach the renderer)
//   • hands.invoke(tool, args) — a tool call routed through the MAIN process via IPC,
//            so it never hits the browser's CORS preflight (fixes "tool calls die
//            on preflight"), and the hands bearer token stays out of the renderer.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("windytalk", {
  cfg: {
    engineUrl: process.env.WINDYTALK_ENGINE_URL || "ws://127.0.0.1:8788",
    handsUrl: process.env.WINDYTALK_HANDS_URL || "http://127.0.0.1:8781",
    appVersion: process.env.WINDYTALK_APP_VERSION || "0.1.0",
    demo: process.env.WINDYTALK_DEMO || "",
    autoMic: process.env.WINDYTALK_AUTO_MIC === "1",
  },
  hands: {
    invoke: (tool, args) => ipcRenderer.invoke("windytalk:hands", { tool, args }),
  },
  // control.mcp.v1 status bus: the renderer pushes engine/mic status up to the
  // supervisor (Layer 1 + get_health feed on it) and receives supervisor
  // commands (reconnect, safe-mode overlay, a plain-English notice).
  control: {
    pushStatus: (s) => ipcRenderer.send("windytalk:status", s),
    onCommand: (cb) => ipcRenderer.on("windytalk:cmd", (_e, cmd) => cb(cmd)),
  },
  quit: () => ipcRenderer.send("windytalk:quit"),
});
