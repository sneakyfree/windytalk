// On-disk locations pinned by control.mcp.v1 (security.token.storage,
// resurrection.heartbeat.path, resurrection.single_instance). Everything not
// pinned lives beside the pinned files. WINDYTALK_CONTROL_DIR (tests only)
// redirects the whole tree to a temp dir, mirroring the token env override.
import os from "node:os";
import path from "node:path";

export interface ControlPaths {
  /** ~/.windytalk (macOS/Linux) | %APPDATA%\WindyTalk (Windows). */
  configDir: string;
  /** Heartbeat lives in %LOCALAPPDATA% on Windows (pinned), configDir elsewhere. */
  stateDir: string;
  token: string;
  portFile: string;
  heartbeat: string;
  /** Lock-file CONTENT {pid, started_at, exe}, written at acquire. */
  instanceLock: string;
  /** The kernel-released exclusivity primitive: unix socket path (POSIX). */
  instanceSocket: string;
  /** Watcher relaunch spec, written by the resurrection installer. */
  resurrectionSpec: string;
  /** Watcher backoff state (service_backoff). */
  resurrectionState: string;
}

export function controlPaths(
  platform: NodeJS.Platform = process.platform,
  env: NodeJS.ProcessEnv = process.env,
): ControlPaths {
  const override = env.WINDYTALK_CONTROL_DIR;
  let configDir: string;
  let stateDir: string;
  if (override) {
    configDir = override;
    stateDir = override;
  } else if (platform === "win32") {
    const appData = env.APPDATA || path.join(os.homedir(), "AppData", "Roaming");
    const localAppData = env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
    configDir = path.join(appData, "WindyTalk");
    stateDir = path.join(localAppData, "WindyTalk");
  } else {
    configDir = path.join(os.homedir(), ".windytalk");
    stateDir = configDir;
  }
  return {
    configDir,
    stateDir,
    token: path.join(configDir, "control-token"),
    portFile: path.join(configDir, "control-port"),
    heartbeat: path.join(stateDir, "heartbeat"),
    instanceLock: path.join(stateDir, "instance.lock"),
    instanceSocket:
      platform === "win32"
        ? windowsPipeName(env)
        : path.join(stateDir, "instance.sock"),
    resurrectionSpec: path.join(stateDir, "resurrection.json"),
    resurrectionState: path.join(stateDir, "resurrection-state.json"),
  };
}

// Windows named pipes are a global namespace: scope per user (and per test
// override dir) so two sessions never collide on the lock.
function windowsPipeName(env: NodeJS.ProcessEnv): string {
  const scope = env.WINDYTALK_CONTROL_DIR || env.USERNAME || "default";
  const safe = scope.replace(/[^a-zA-Z0-9._-]/g, "_");
  return `\\\\.\\pipe\\windytalk-instance-${safe}`;
}
