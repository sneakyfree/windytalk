// Slice-5 tests: the self-update guards (contract self_update). The feature is
// INERT until Grant embeds the key, so the INERT path is asserted with the real
// (empty) embedded key, and every GUARD is exercised with an injected TEST key
// + source so the correctness core is proven now, not when the key lands.
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  applyUpdate,
  checkAntiRollback,
  clearUpdateState,
  compareSemver,
  parseSemver,
  precheckDisk,
  readUpdateState,
  rollbackDecision,
  verifySignature,
  writeUpdateState,
  type ReleaseArtifact,
  type UpdateSource,
  type UpdateState,
} from "../electron/control/selfupdate.js";
import { EMBEDDED_UPDATE_PUBLIC_KEY, updateConfigured } from "../electron/control/update-key.js";

// A test signing keypair (Ed25519) — stands in for Grant's real trust root.
const { publicKey, privateKey } = crypto.generateKeyPairSync("ed25519");
const TEST_PUB = publicKey.export({ type: "spki", format: "pem" }).toString();

function sign(data: Buffer): Buffer {
  return crypto.sign(null, data, privateKey);
}

function artifact(version: string, body = "the new build bytes"): ReleaseArtifact {
  const data = Buffer.from(`${version}:${body}`);
  return { version, data, signature: sign(data) };
}

function source(head: string, arts: Record<string, ReleaseArtifact>): UpdateSource {
  return {
    channelHead: async () => head,
    fetchArtifact: async (v) => arts[v] ?? artifact(v),
  };
}

// -- INERT (the shipped state) ---------------------------------------------------

test("INERT by construction: the embedded key is empty, so updateConfigured() is false", () => {
  assert.equal(EMBEDDED_UPDATE_PUBLIC_KEY, "");
  assert.equal(updateConfigured(), false);
});

test("INERT applyUpdate: no source -> 'no update source configured', never fakes success", async () => {
  const res = await applyUpdate({
    source: null,
    currentVersion: "0.1.0",
    freeBytes: () => Number.MAX_SAFE_INTEGER,
    stage: async () => assert.fail("must NOT stage while inert"),
  });
  assert.deepEqual(res, { ok: false, error: "no update source configured" });
});

test("INERT verifySignature: with the empty embedded key, nothing ever verifies", () => {
  const a = artifact("9.9.9");
  assert.equal(verifySignature(EMBEDDED_UPDATE_PUBLIC_KEY, a.data, a.signature), false);
});

// -- semver ----------------------------------------------------------------------

test("semver: parse + strict compare incl. prerelease ordering", () => {
  assert.equal(compareSemver("1.2.3", "1.2.4"), -1);
  assert.equal(compareSemver("1.3.0", "1.2.9"), 1);
  assert.equal(compareSemver("2.0.0", "2.0.0"), 0);
  assert.equal(compareSemver("1.0.0", "1.0.0-rc1"), 1, "release > prerelease");
  assert.equal(compareSemver("v1.0.0", "1.0.0"), 0, "leading v tolerated");
  assert.equal(parseSemver("not-a-version"), null);
  assert.throws(() => compareSemver("x", "1.0.0"), /unparseable/);
});

// -- signature -------------------------------------------------------------------

test("signature: a correctly-signed artifact verifies; tamper or wrong key fails", () => {
  const a = artifact("1.2.0");
  assert.equal(verifySignature(TEST_PUB, a.data, a.signature), true);
  const tampered = Buffer.from(a.data.toString() + "x");
  assert.equal(verifySignature(TEST_PUB, tampered, a.signature), false, "tampered payload fails");
  const { publicKey: other } = crypto.generateKeyPairSync("ed25519");
  const otherPem = other.export({ type: "spki", format: "pem" }).toString();
  assert.equal(verifySignature(otherPem, a.data, a.signature), false, "wrong key fails");
});

// -- anti-rollback ---------------------------------------------------------------

test("anti-rollback: only the channel-head installs (downgrade + signed-intermediate refused)", () => {
  const head = "1.5.0";
  assert.deepEqual(checkAntiRollback("1.4.0", "1.5.0", head), { ok: true }, "head installs");
  assert.deepEqual(checkAntiRollback("1.4.0", "1.3.0", head), { ok: false, error: "downgrade refused" }, "older than current");
  assert.deepEqual(checkAntiRollback("1.4.0", "1.4.0", head), { ok: false, error: "downgrade refused" }, "equal to current");
  assert.deepEqual(
    checkAntiRollback("1.4.0", "1.4.5", head),
    { ok: false, error: "downgrade refused" },
    "signed INTERMEDIATE (>current but <head) refused — the round-2 downgrade attack",
  );
});

// -- disk ------------------------------------------------------------------------

test("disk precheck: needs room for the A/B pair + margin; fails closed", () => {
  const artifactBytes = 100 * 1024 * 1024; // 100 MB
  assert.equal(precheckDisk(1000 * 1024 * 1024, artifactBytes), true);
  assert.equal(precheckDisk(150 * 1024 * 1024, artifactBytes), false, "not enough for the pair");
});

// -- the full pipeline (with the TEST key) ---------------------------------------

test("pipeline: signed head installs; verify happens BEFORE staging", async () => {
  const staged: string[] = [];
  const res = await applyUpdate({
    source: source("1.6.0", { "1.6.0": artifact("1.6.0") }),
    publicKeyPem: TEST_PUB,
    currentVersion: "1.5.0",
    freeBytes: () => Number.MAX_SAFE_INTEGER,
    stage: async (a) => { staged.push(a.version); },
  });
  assert.deepEqual(res, { ok: true, result: "restarting" });
  assert.deepEqual(staged, ["1.6.0"]);
});

test("pipeline: an UNSIGNED (wrong-key) artifact is refused and NEVER staged", async () => {
  const { privateKey: evil } = crypto.generateKeyPairSync("ed25519");
  const data = Buffer.from("1.6.0:malicious");
  const badArtifact: ReleaseArtifact = { version: "1.6.0", data, signature: crypto.sign(null, data, evil) };
  let staged = false;
  const res = await applyUpdate({
    source: source("1.6.0", { "1.6.0": badArtifact }),
    publicKeyPem: TEST_PUB,
    currentVersion: "1.5.0",
    freeBytes: () => Number.MAX_SAFE_INTEGER,
    stage: async () => { staged = true; },
  });
  assert.deepEqual(res, { ok: false, error: "unsigned or untrusted update" });
  assert.equal(staged, false, "a bad signature must never reach staging");
});

test("pipeline: a signed but OLDER build is refused (run_selftest is NOT the integrity gate)", async () => {
  const res = await applyUpdate({
    source: source("1.6.0", { "1.4.0": artifact("1.4.0") }),
    publicKeyPem: TEST_PUB,
    currentVersion: "1.5.0",
    requestedVersion: "1.4.0",
    freeBytes: () => Number.MAX_SAFE_INTEGER,
    stage: async () => assert.fail("must not stage a downgrade"),
  });
  assert.deepEqual(res, { ok: false, error: "downgrade refused" });
});

test("pipeline: insufficient disk fails CLOSED, never half-stages", async () => {
  let staged = false;
  const res = await applyUpdate({
    source: source("1.6.0", { "1.6.0": artifact("1.6.0", "x".repeat(1024)) }),
    publicKeyPem: TEST_PUB,
    currentVersion: "1.5.0",
    freeBytes: () => 1024, // way too little
    stage: async () => { staged = true; },
  });
  assert.deepEqual(res, { ok: false, error: "insufficient disk" });
  assert.equal(staged, false);
});

test("pipeline: 'latest' resolves to the channel-head", async () => {
  const staged: string[] = [];
  const res = await applyUpdate({
    source: source("2.0.0", { "2.0.0": artifact("2.0.0") }),
    publicKeyPem: TEST_PUB,
    currentVersion: "1.9.0",
    requestedVersion: "latest",
    freeBytes: () => Number.MAX_SAFE_INTEGER,
    stage: async (a) => { staged.push(a.version); },
  });
  assert.equal(res.ok, true);
  assert.deepEqual(staged, ["2.0.0"]);
});

// -- out-of-process rollback decision + state ------------------------------------

test("rollbackDecision: attested -> commit; within window -> wait; past deadline unattested -> rollback", () => {
  const state: UpdateState = {
    pending: true, fromVersion: "1.5.0", toVersion: "1.6.0",
    previousBinary: "/app/1.5.0", newBinary: "/app/1.6.0", deadlineMs: 10_000,
  };
  assert.equal(rollbackDecision(state, true, 5_000), "commit", "attested inside window commits");
  assert.equal(rollbackDecision(state, true, 20_000), "commit", "attested is always commit");
  assert.equal(rollbackDecision(state, false, 5_000), "wait", "unattested but still inside 60 s");
  assert.equal(rollbackDecision(state, false, 10_001), "rollback", "deadline passed unattested -> flip back");
});

test("update-state: atomic round-trip; clear removes it", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-upd-"));
  const state: UpdateState = {
    pending: true, fromVersion: "1.0.0", toVersion: "1.1.0",
    previousBinary: "/a", newBinary: "/b", deadlineMs: 123, attested: false,
  };
  writeUpdateState(dir, state);
  assert.deepEqual(readUpdateState(dir), state);
  clearUpdateState(dir);
  assert.equal(readUpdateState(dir), null);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("SAFETY: a build that crashes on boot never sets attested -> rollback fires (out-of-process, app can't suppress)", () => {
  // The core out-of-process guarantee: the DECISION lives in the watcher and
  // keys off the ABSENCE of an attestation the crashed build could not write.
  const state: UpdateState = {
    pending: true, fromVersion: "1.5.0", toVersion: "1.6.0-broken",
    previousBinary: "/app/good", newBinary: "/app/broken", deadlineMs: 1_000,
  };
  // The broken build never boots, so attested stays false through the deadline.
  assert.equal(rollbackDecision(state, false, 1_001), "rollback");
});
