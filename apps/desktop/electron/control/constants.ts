// control.mcp.v1 — the pinned numbers (contracts/control.mcp.v1.json is
// authoritative; every value here is normative there). One module so prod and
// tests share the same constants and a drifted number is a one-line diff.

/** Fixed control port (security.port). Never silently bind another. */
export const CONTROL_PORT = 8782;

/** Bearer-token header (security.token.header). */
export const TOKEN_HEADER = "x-windytalk-control-token";

/** Test-only env override for the token (security.token.env_override). */
export const TOKEN_ENV_OVERRIDE = "WINDYTALK_CONTROL_TOKEN";

/** Supervisor bumps the heartbeat every 5 s (resurrection.heartbeat.cadence). */
export const HEARTBEAT_INTERVAL_MS = 5_000;

/** OS service checks every 15 s (resurrection.heartbeat.cadence). */
export const WATCH_INTERVAL_MS = 15_000;

/** Relaunch budget after SIGKILL (resurrection.heartbeat.cadence). */
export const RELAUNCH_BUDGET_S = 45;

/** tier1_dead: mtime > 30 s AND pid absent-by-identity => the process is gone. */
export const STALE_DEAD_S = 30;

/** tier2_wedged: pid present-by-identity but mtime > 90 s => maybe wedged. */
export const STALE_WEDGE_S = 90;

/** single_instance ack: holder must ack within 3 s, one retry. */
export const ACK_TIMEOUT_MS = 3_000;
export const ACK_RETRIES = 1;

/** service_backoff: max 3 relaunches per 300 s ... */
export const BACKOFF_MAX_RELAUNCHES = 3;
export const BACKOFF_WINDOW_S = 300;

/** ... then drop to 1 attempt per 5 min. */
export const BACKOFF_SLOW_INTERVAL_S = 300;

/** Backoff counter resets after 300 s of continuous fresh heartbeat. */
export const BACKOFF_FRESH_RESET_S = 300;

/**
 * Identity tolerance when matching a live process's start time against the
 * heartbeat/lock record {pid, started_at, exe} (staleness_tiers.identity_aware).
 * OS clocks report process start at ~1 s resolution (ps lstart, /proc ticks).
 */
export const IDENTITY_START_TOLERANCE_S = 2;

/** Request-body cap on :8782, same DoS guard as hands/surface.py. */
export const MAX_BODY_BYTES = 64 * 1024;
