// The self-update TRUST ROOT (contract self_update.source). This is a real
// cryptographic trust root: whoever holds the PRIVATE half can push code to
// every install. The private half is Grant's alone (never in the repo, never
// on a build box unattended); the PUBLIC half is embedded HERE for
// verify-before-stage.
//
// FORCED-HONEST / INERT BY CONSTRUCTION: until Grant embeds the public key,
// EMBEDDED_UPDATE_PUBLIC_KEY stays "" and the feature is safe-OFF —
// check_for_update reports 'no update source configured' and apply_update
// refuses with the same. The tools are built, wired, and tested; they simply
// cannot act without the trust root. Never fake success.
//
// Grant's REMAINING real-world action before self-update goes live (self_update
// .source): generate the keypair, paste the PUBLIC key PEM below, and wire the
// signing step into GitHub Release publishing. Nothing else in the code changes.

/** Ed25519 SPKI PEM of the update-signing public key. "" = INERT. */
export const EMBEDDED_UPDATE_PUBLIC_KEY = "";

/** The update CHANNEL: GitHub Releases; head = newest non-prerelease Release. */
export const UPDATE_CHANNEL = "github-releases:sneakyfree/windytalk";

/** Configured iff the trust root exists. The single inert/live switch. */
export function updateConfigured(publicKeyPem: string = EMBEDDED_UPDATE_PUBLIC_KEY): boolean {
  return publicKeyPem.trim().length > 0;
}
