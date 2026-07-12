// The concrete UpdateSource for the contract-pinned channel
// "github-releases:sneakyfree/windytalk" (self_update.source): channel-head =
// the newest NON-prerelease, non-draft GitHub Release with a parseable semver
// tag. This file only FETCHES — every trust decision (signature, anti-rollback,
// disk) stays in selfupdate.ts's guard pipeline, and the whole path remains
// inert until the trust root is embedded (update-key.ts), because main.js only
// constructs this source when updateConfigured().
import { parseSemver, compareSemver, type ReleaseArtifact, type UpdateSource } from "./selfupdate.js";
import { UPDATE_CHANNEL } from "./update-key.js";

export interface GhAsset {
  name: string;
  browser_download_url: string;
  size: number;
}

export interface GhRelease {
  tag_name: string;
  draft: boolean;
  prerelease: boolean;
  assets: GhAsset[];
}

/** "github-releases:owner/repo" -> "owner/repo"; anything else -> null. */
export function repoFromChannel(channel: string = UPDATE_CHANNEL): string | null {
  const m = /^github-releases:([\w.-]+\/[\w.-]+)$/.exec(channel);
  return m ? m[1] : null;
}

/**
 * The channel-head: newest by SEMVER (not list order — the API is
 * newest-created-first, which an out-of-order re-publish could skew) among
 * non-draft, non-prerelease releases with parseable semver tags.
 */
export function pickChannelHead(releases: GhRelease[]): GhRelease | null {
  let head: GhRelease | null = null;
  for (const r of releases) {
    if (r.draft || r.prerelease || !parseSemver(r.tag_name)) continue;
    if (!head || compareSemver(r.tag_name, head.tag_name) > 0) head = r;
  }
  return head;
}

/**
 * The ONE artifact-naming rule, shared with scripts/publish-release.sh:
 *   windytalk-<version>-win-x64.exe | -mac-universal.dmg | -linux-x86_64.AppImage
 * (version rendered without a leading 'v'; detached signature = name + '.sig').
 * Unsupported platform/arch -> null (fail closed, honest error upstream).
 */
export function artifactAssetName(
  version: string,
  platform: NodeJS.Platform = process.platform,
  arch: string = process.arch,
): string | null {
  const bare = version.replace(/^v/, "");
  if (platform === "win32" && arch === "x64") return `windytalk-${bare}-win-x64.exe`;
  if (platform === "darwin") return `windytalk-${bare}-mac-universal.dmg`; // universal2: any arch
  if (platform === "linux" && arch === "x64") return `windytalk-${bare}-linux-x86_64.AppImage`;
  return null;
}

const MAX_SIG_BYTES = 4096; // Ed25519 detached sig is 64 bytes; anything big is wrong

export interface GhSourceOptions {
  repo?: string; // default: from UPDATE_CHANNEL
  fetchImpl?: typeof fetch; // injected in tests
  platform?: NodeJS.Platform;
  arch?: string;
  apiTimeoutMs?: number;
  downloadTimeoutMs?: number;
  maxArtifactBytes?: number;
}

export class GithubReleasesSource implements UpdateSource {
  private readonly repo: string;
  private readonly fetchImpl: typeof fetch;
  private readonly platform: NodeJS.Platform;
  private readonly arch: string;
  private readonly apiTimeoutMs: number;
  private readonly downloadTimeoutMs: number;
  private readonly maxArtifactBytes: number;

  constructor(opts: GhSourceOptions = {}) {
    const repo = opts.repo ?? repoFromChannel();
    if (!repo) throw new Error("update channel misconfigured");
    this.repo = repo;
    this.fetchImpl = opts.fetchImpl ?? fetch;
    this.platform = opts.platform ?? process.platform;
    this.arch = opts.arch ?? process.arch;
    this.apiTimeoutMs = opts.apiTimeoutMs ?? 10_000;
    this.downloadTimeoutMs = opts.downloadTimeoutMs ?? 300_000;
    this.maxArtifactBytes = opts.maxArtifactBytes ?? 2 * 1024 * 1024 * 1024;
  }

  private async api(pathname: string): Promise<unknown> {
    const res = await this.fetchImpl(`https://api.github.com/repos/${this.repo}${pathname}`, {
      headers: {
        accept: "application/vnd.github+json",
        // GitHub's API requires a UA; keep it content-free.
        "user-agent": "windytalk-updater",
      },
      signal: AbortSignal.timeout(this.apiTimeoutMs),
    });
    if (!res.ok) throw new Error(`update channel http ${res.status}`);
    return res.json();
  }

  async channelHead(): Promise<string | null> {
    const releases = (await this.api("/releases?per_page=30")) as GhRelease[];
    if (!Array.isArray(releases)) throw new Error("update channel malformed response");
    return pickChannelHead(releases)?.tag_name ?? null;
  }

  private async releaseByTag(tag: string): Promise<GhRelease> {
    try {
      return (await this.api(`/releases/tags/${encodeURIComponent(tag)}`)) as GhRelease;
    } catch {
      // tolerate a hand-typed version missing/carrying the 'v' prefix
      const alt = tag.startsWith("v") ? tag.slice(1) : `v${tag}`;
      return (await this.api(`/releases/tags/${encodeURIComponent(alt)}`)) as GhRelease;
    }
  }

  private async download(url: string, maxBytes: number): Promise<Buffer> {
    const res = await this.fetchImpl(url, {
      headers: { "user-agent": "windytalk-updater" },
      signal: AbortSignal.timeout(this.downloadTimeoutMs),
    });
    if (!res.ok) throw new Error(`artifact download http ${res.status}`);
    const data = Buffer.from(await res.arrayBuffer());
    if (data.length > maxBytes) throw new Error("artifact exceeds size cap");
    return data;
  }

  async fetchArtifact(version: string): Promise<ReleaseArtifact> {
    const release = await this.releaseByTag(version);
    const wanted = artifactAssetName(version, this.platform, this.arch);
    if (!wanted) throw new Error("no artifact published for this platform");
    const asset = release.assets.find((a) => a.name === wanted);
    const sig = release.assets.find((a) => a.name === `${wanted}.sig`);
    if (!asset) throw new Error("no artifact published for this platform");
    if (!sig) throw new Error("release is missing its signature asset");
    if (asset.size > this.maxArtifactBytes) throw new Error("artifact exceeds size cap");

    const data = await this.download(asset.browser_download_url, this.maxArtifactBytes);
    if (data.length !== asset.size) throw new Error("artifact size mismatch");
    const signature = await this.download(sig.browser_download_url, MAX_SIG_BYTES);
    // version: verbatim as requested, so applyUpdate's string-equality
    // artifact-vs-target check holds by construction.
    return { version, data, signature };
  }
}
