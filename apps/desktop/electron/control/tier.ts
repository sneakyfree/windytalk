// tier_resolution (contract tiers.tier_resolution) — the SINGLE normative
// algorithm computing a tool's EFFECTIVE tier for a given call, authoritative
// over any per-tool prose. A documented extension of hands/tiers.py: value-
// conditional resolvers (set_autonomy / set_volume), the always_confirm_floor,
// and autonomy bands. This module is the single source of truth both the
// dispatch gate and the tier-matrix tests read — the recurring trap (§6 of the
// build notes: set_autonomy vs set_volume(0) collisions) has exactly one home.

export type Tier = "auto_allow" | "ask_first" | "always_confirm";

/** Static tiers from the frozen contract (per-tool `tier` field). */
export const STATIC_TIER: Record<string, Tier> = {
  get_health: "auto_allow",
  get_status: "auto_allow",
  get_config: "auto_allow",
  get_logs: "auto_allow",
  list_audio_devices: "auto_allow",
  run_selftest: "auto_allow",
  get_capabilities: "auto_allow",
  check_for_update: "auto_allow",
  reconnect: "auto_allow",
  enter_safe_mode: "auto_allow",
  exit_safe_mode: "ask_first",
  repair_resurrection: "ask_first",
  restart_engine: "ask_first",
  restart_app: "ask_first",
  clear_cache: "ask_first",
  set_audio_input: "ask_first",
  set_audio_output: "ask_first",
  set_volume: "ask_first",
  set_engine_url: "ask_first",
  set_brain: "ask_first",
  set_wake_mode: "ask_first",
  set_autonomy: "ask_first",
  reset_to_defaults: "always_confirm",
  apply_update: "always_confirm",
  // account/billing (rev.8, ADR-060 §7) — tri-state unsupported until wired.
  get_account: "auto_allow",
  get_billing_summary: "auto_allow",
  open_account_portal: "ask_first",
  logout_account: "ask_first",
};

/** security.always_confirm_floor.always — unconditional members. */
const FLOOR_ALWAYS = new Set([
  "set_engine_url",
  "set_brain",
  "reset_to_defaults",
  "apply_update",
  "exit_safe_mode",
]);

export interface TierContext {
  currentAutonomy: number;
  /** Session-scoped always-allow grants — granted by the USER via the confirmer. */
  sessionGrants: ReadonlySet<string>;
}

export type TierDecision =
  /** Execute immediately (also: notify-after at autonomy 0-2, see notify_after). */
  | { action: "allow"; notify_after: boolean }
  /** One confirmation; the confirmer MAY offer a session grant. */
  | { action: "confirm"; session_grant_allowed: true }
  /** Floor: confirm EVERY invocation; no session grant is ever offered. */
  | { action: "confirm"; session_grant_allowed: false };

/**
 * Apply tier_resolution steps 1-4 in order. `args` supplies the values the
 * conditional rules read (set_autonomy.level, set_volume.level,
 * set_wake_mode.hands_free).
 */
export function resolveTier(
  tool: string,
  args: Record<string, unknown>,
  ctx: TierContext,
): TierDecision {
  const notifyAfter = ctx.currentAutonomy <= 2;

  // -- step1_base: static tier, UNLESS a value-conditional resolver FULLY
  //    REPLACES it.
  let tier: Tier = STATIC_TIER[tool] ?? "ask_first";
  if (tool === "set_autonomy") {
    const level = Number(args.level);
    tier = level <= ctx.currentAutonomy ? "auto_allow" : "always_confirm";
  } else if (tool === "set_volume") {
    tier = Number(args.level) === 0 ? "always_confirm" : "auto_allow";
  }

  // -- step2_floor: floor membership ends resolution (steps 3-4 do NOT apply).
  if (onFloor(tool, args, ctx)) {
    return { action: "confirm", session_grant_allowed: false };
  }

  if (tier === "auto_allow") return { action: "allow", notify_after: notifyAfter };
  if (tier === "always_confirm") {
    // Non-floor always_confirm (unreached by the current tool table, but the
    // algorithm covers it): per-invocation confirm, no session upgrade.
    return { action: "confirm", session_grant_allowed: false };
  }

  // -- step3_autonomy: ask_first at autonomy >= 7 = a standing user grant.
  if (ctx.currentAutonomy >= 7) return { action: "allow", notify_after: false };

  // -- step4_session_grant: a USER-granted session upgrade may stand.
  if (ctx.sessionGrants.has(tool)) return { action: "allow", notify_after: notifyAfter };
  return { action: "confirm", session_grant_allowed: true };
}

/** security.always_confirm_floor membership for THIS call (tool, condition). */
function onFloor(tool: string, args: Record<string, unknown>, ctx: TierContext): boolean {
  if (FLOOR_ALWAYS.has(tool)) return true;
  // conditional members:
  if (tool === "set_autonomy") return Number(args.level) > ctx.currentAutonomy; // RAISING only
  if (tool === "set_volume") return Number(args.level) === 0; // muting strands
  if (tool === "set_wake_mode") return args.hands_free === true; // always-listen escalates privacy
  return false;
}
