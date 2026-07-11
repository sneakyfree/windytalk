// OS-LEVEL notification from the resurrection service itself (contract
// staleness_tiers.tier2_wedged: disk-full is surfaced by the SERVICE, never the
// in-app UI, which may be down). Best-effort, never throws; deduped through the
// tmpdir (usually tmpfs, so it works even when the data disk is full).
import { execFile } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const DEDUPE_WINDOW_MS = 60 * 60 * 1000; // at most one identical nag per hour

export function notifyOs(
  title: string,
  body: string,
  platform: NodeJS.Platform = process.platform,
): void {
  if (recentlyNotified(title)) return;
  try {
    if (platform === "linux") {
      execFile("notify-send", ["--app-name=Windy Talk", title, body], swallow);
    } else if (platform === "darwin") {
      const script = `display notification ${aq(body)} with title ${aq(title)}`;
      execFile("osascript", ["-e", script], swallow);
    } else if (platform === "win32") {
      execFile("msg", ["*", "/TIME:30", `${title}: ${body}`], swallow);
    }
  } catch {
    // Notification is best-effort; the log line is the fallback.
  }
  console.log(`[windytalk-notify] ${title}: ${body}`);
}

function swallow(): void {}

function aq(s: string): string {
  return `"${s.replace(/[\\"]/g, "")}"`;
}

function recentlyNotified(title: string): boolean {
  const key = title.replace(/[^a-zA-Z0-9]/g, "_").slice(0, 60);
  const marker = path.join(os.tmpdir(), `windytalk-notify-${key}`);
  try {
    const st = fs.statSync(marker);
    if (Date.now() - st.mtimeMs < DEDUPE_WINDOW_MS) return true;
  } catch {
    // no marker yet
  }
  try {
    fs.writeFileSync(marker, String(Date.now()));
  } catch {
    // tmpdir unwritable too — notify anyway
  }
  return false;
}
