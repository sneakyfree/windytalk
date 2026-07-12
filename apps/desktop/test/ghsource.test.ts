// P2 tests: the GitHub-Releases UpdateSource (contract self_update.source —
// channel-head = newest non-prerelease Release). The source only FETCHES; the
// end-to-end case proves it composes with selfupdate.ts's guard pipeline using
// a real Ed25519 test key, so the correctness core is proven before Grant's
// real key lands.
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { test } from "node:test";

import {
  GithubReleasesSource,
  artifactAssetName,
  pickChannelHead,
  repoFromChannel,
  type GhRelease,
} from "../electron/control/ghsource.js";
import { applyUpdate, type ReleaseArtifact } from "../electron/control/selfupdate.js";

// -- pure helpers -----------------------------------------------------------------

test("repoFromChannel parses the pinned channel and rejects others", () => {
  assert.equal(repoFromChannel("github-releases:sneakyfree/windytalk"), "sneakyfree/windytalk");
  assert.equal(repoFromChannel(), "sneakyfree/windytalk"); // the embedded UPDATE_CHANNEL
  assert.equal(repoFromChannel("https://evil.example/releases"), null);
  assert.equal(repoFromChannel("github-releases:no-slash"), null);
});

test("pickChannelHead: newest SEMVER among non-draft non-prerelease semver tags", () => {
  const r = (tag: string, extra: Partial<GhRelease> = {}): GhRelease =>
    ({ tag_name: tag, draft: false, prerelease: false, assets: [], ...extra });
  // out of list order + drafts/prereleases/garbage present
  const head = pickChannelHead([
    r("v1.1.0"),
    r("v9.9.9", { draft: true }),
    r("v2.0.0-rc.1", { prerelease: true }),
    r("nightly-build"),
    r("v1.10.0"), // semver-newer than 1.9 despite '10'
    r("v1.9.0"),
  ]);
  assert.equal(head?.tag_name, "v1.10.0");
  assert.equal(pickChannelHead([r("garbage"), r("v1", { draft: false })]), null);
});

test("artifactAssetName: the one naming rule, v-stripped, fail-closed", () => {
  assert.equal(artifactAssetName("v1.2.0", "win32", "x64"), "windytalk-1.2.0-win-x64.exe");
  // universal2: BOTH mac arches resolve to the same single artifact
  assert.equal(artifactAssetName("1.2.0", "darwin", "arm64"), "windytalk-1.2.0-mac-universal.dmg");
  assert.equal(artifactAssetName("1.2.0", "darwin", "x64"), "windytalk-1.2.0-mac-universal.dmg");
  assert.equal(artifactAssetName("v1.2.0", "linux", "x64"), "windytalk-1.2.0-linux-x86_64.AppImage");
  assert.equal(artifactAssetName("1.2.0", "win32", "ia32"), null);
  assert.equal(artifactAssetName("1.2.0", "linux", "arm64"), null);
});

// -- a faked GitHub over injected fetch ---------------------------------------------

const API = "https://api.github.com/repos/sneakyfree/windytalk";
const CDN = "https://cdn.test";

function fakeGithub(routes: Map<string, () => Response>): typeof fetch {
  return (async (input: RequestInfo | URL) => {
    const hit = routes.get(String(input));
    return hit ? hit() : new Response("not found", { status: 404 });
  }) as typeof fetch;
}

function releasesFixture(artifact: Buffer, signature: Buffer) {
  const assetName = "windytalk-1.2.0-linux-x86_64.AppImage";
  const release: GhRelease = {
    tag_name: "v1.2.0",
    draft: false,
    prerelease: false,
    assets: [
      { name: assetName, browser_download_url: `${CDN}/${assetName}`, size: artifact.length },
      { name: `${assetName}.sig`, browser_download_url: `${CDN}/${assetName}.sig`, size: signature.length },
    ],
  };
  const routes = new Map<string, () => Response>([
    [`${API}/releases?per_page=30`, () => new Response(JSON.stringify([release]))],
    [`${API}/releases/tags/v1.2.0`, () => new Response(JSON.stringify(release))],
    [`${CDN}/${assetName}`, () => new Response(new Uint8Array(artifact))],
    [`${CDN}/${assetName}.sig`, () => new Response(new Uint8Array(signature))],
  ]);
  return { routes, release, assetName };
}

function testSource(routes: Map<string, () => Response>, opts: Record<string, unknown> = {}) {
  return new GithubReleasesSource({
    fetchImpl: fakeGithub(routes),
    platform: "linux",
    arch: "x64",
    ...opts,
  });
}

test("channelHead + fetchArtifact happy path returns verbatim version, bytes, sig", async () => {
  const artifact = Buffer.from("new build bytes");
  const { privateKey } = crypto.generateKeyPairSync("ed25519");
  const signature = crypto.sign(null, artifact, privateKey);
  const { routes } = releasesFixture(artifact, signature);

  const src = testSource(routes);
  assert.equal(await src.channelHead(), "v1.2.0");
  const got = await src.fetchArtifact("v1.2.0");
  assert.equal(got.version, "v1.2.0"); // verbatim: applyUpdate's equality check holds
  assert.deepEqual(got.data, artifact);
  assert.deepEqual(got.signature, signature);
});

test("fetchArtifact fails CLOSED: missing sig, size mismatch, wrong platform, size cap", async () => {
  const artifact = Buffer.from("bytes");
  const sig = Buffer.alloc(64);
  {
    const { routes, release, assetName } = releasesFixture(artifact, sig);
    release.assets = release.assets.filter((a) => a.name === assetName); // drop the .sig
    routes.set(`${API}/releases/tags/v1.2.0`, () => new Response(JSON.stringify(release)));
    await assert.rejects(testSource(routes).fetchArtifact("v1.2.0"), /missing its signature/);
  }
  {
    const { routes, release } = releasesFixture(artifact, sig);
    release.assets[0].size = artifact.length + 1; // CDN served fewer bytes than declared
    routes.set(`${API}/releases/tags/v1.2.0`, () => new Response(JSON.stringify(release)));
    await assert.rejects(testSource(routes).fetchArtifact("v1.2.0"), /size mismatch/);
  }
  {
    const { routes } = releasesFixture(artifact, sig);
    await assert.rejects(
      testSource(routes, { platform: "linux", arch: "arm64" }).fetchArtifact("v1.2.0"),
      /no artifact published/,
    );
  }
  {
    const { routes } = releasesFixture(artifact, sig);
    await assert.rejects(
      testSource(routes, { maxArtifactBytes: 2 }).fetchArtifact("v1.2.0"),
      /size cap/,
    );
  }
});

test("releaseByTag tolerates a hand-typed version with/without the v prefix", async () => {
  const artifact = Buffer.from("bytes");
  const { routes } = releasesFixture(artifact, Buffer.alloc(64));
  // only the v-prefixed tag route exists; ask without the prefix
  const got = await testSource(routes).fetchArtifact("1.2.0");
  // filenames are v-stripped either way, so the same asset matches
  assert.deepEqual(got.data, artifact);
});

test("END-TO-END: GithubReleasesSource through the REAL guard pipeline", async () => {
  const artifact = Buffer.from("the 1.2.0 build");
  const { publicKey, privateKey } = crypto.generateKeyPairSync("ed25519");
  const pem = publicKey.export({ type: "spki", format: "pem" }) as string;
  const signature = crypto.sign(null, artifact, privateKey);
  const { routes } = releasesFixture(artifact, signature);

  const staged: ReleaseArtifact[] = [];
  const deps = {
    source: testSource(routes),
    publicKeyPem: pem,
    currentVersion: "1.0.0",
    freeBytes: () => Number.MAX_SAFE_INTEGER,
    stage: async (a: ReleaseArtifact) => { staged.push(a); },
  };

  // happy path: head discovered, signature verifies, staged
  const ok = await applyUpdate(deps);
  assert.deepEqual(ok, { ok: true, result: "restarting" });
  assert.equal(staged.length, 1);
  assert.equal(staged[0].version, "v1.2.0");

  // anti-rollback THROUGH the source: explicit non-head target refused pre-download
  const down = await applyUpdate({ ...deps, currentVersion: "1.5.0" });
  assert.deepEqual(down, { ok: false, error: "downgrade refused" });

  // tampered artifact: SAME-LENGTH byte flip (a length change is already caught
  // by the size-mismatch guard) — only the signature can catch this -> refused
  const evil = Buffer.from("the 1.2.0 bUild");
  routes.set(`${CDN}/windytalk-1.2.0-linux-x86_64.AppImage`, () => new Response(new Uint8Array(evil)));
  const tampered = await applyUpdate(deps);
  assert.equal(tampered.ok, false);
  if (!tampered.ok) assert.equal(tampered.error, "unsigned or untrusted update");
  assert.equal(staged.length, 1); // nothing new staged
});
