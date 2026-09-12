#!/usr/bin/env node
/**
 * Publish the finished reels in output/ to Cloudflare Stream for the public
 * showcase site.
 *
 * The opposite of the masterclass uploader in the monorepo: these videos are
 * PUBLIC, so `requiresignedurls` is deliberately NOT set. The protection here
 * is allowedOrigins — the player refuses to run unless it is embedded on one
 * of SHOWCASE_ORIGINS. That is set in a second pass (--set-origins) because the
 * Vercel domain does not exist until the site has been deployed once.
 *
 * tus rather than the simple upload endpoint: every reel exceeds that
 * endpoint's 200MB cap (they run 130-460MB), and tus is resumable across the
 * ~5GB total.
 *
 *   node tools/showcase-stream.mjs                  # plan only
 *   node tools/showcase-stream.mjs --execute
 *   node tools/showcase-stream.mjs --execute --limit 1
 *   node tools/showcase-stream.mjs --set-origins    # apply SHOWCASE_ORIGINS
 *   node tools/showcase-stream.mjs --refresh        # re-pull metadata
 */
import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import { REPO, loadEnv, api, readManifest, writeManifest, origins } from "./lib/stream.mjs";

const OUTPUT = path.join(REPO, "output");
// tus requires every non-final chunk to be a multiple of 256 KiB.
const CHUNK = 96 * 1024 * 1024;
const b64 = (s) => Buffer.from(s, "utf8").toString("base64");
const mb = (n) => `${(n / 1e6).toFixed(0)}MB`;

/** The 16 numbered reels, read from their sibling .plan.json (the render
 *  pipeline's own record of what each reel is). */
async function discoverReels() {
  const names = (await fs.readdir(OUTPUT)).filter((f) => /^\d\d_.+\.mp4$/.test(f)).sort();
  const reels = [];
  for (const file of names) {
    const slug = file.replace(/\.mp4$/, "");
    const mp4 = path.join(OUTPUT, file);
    let plan = {};
    try {
      plan = JSON.parse(await fs.readFile(path.join(OUTPUT, `${slug}.plan.json`), "utf8"));
    } catch (err) {
      if (err.code !== "ENOENT") throw err;
    }
    reels.push({
      slug,
      file: mp4,
      bytes: (await fs.stat(mp4)).size,
      order: Number(slug.slice(0, 2)),
      title: titleOf(plan, slug),
      composer: composerOf(plan),
    });
  }
  return reels;
}

/** Plan titles are authoritative where they look real. Reels 14-16 still carry
 *  scratch metadata ("four seasons" / composer "asdf"), so fall back to a
 *  title-cased slug and drop the junk composer rather than publish it. */
function titleOf(plan, slug) {
  const t = (plan.title ?? "").trim();
  const looksReal = t && t.toLowerCase() !== t;
  if (looksReal) return t;
  const base = (t || slug.slice(3).replace(/-/g, " ")).replace(/-/g, " ");
  return base.replace(/\b[a-z]/g, (c) => c.toUpperCase());
}

function composerOf(plan) {
  const c = (plan.composer ?? "").trim();
  return !c || c.toLowerCase() === "asdf" ? "" : c;
}

async function createUpload({ env, bytes, name }) {
  const res = await fetch(
    `https://api.cloudflare.com/client/v4/accounts/${env.CLOUDFLARE_ACCOUNT_ID}/stream`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.CLOUDFLARE_STREAM_TOKEN}`,
        "Tus-Resumable": "1.0.0",
        "Upload-Length": String(bytes),
        // name is base64 per the tus spec. No `requiresignedurls` here: these
        // are public on purpose.
        "Upload-Metadata": `name ${b64(name)}`,
      },
    },
  );
  if (res.status !== 201) {
    throw new Error(`create: HTTP ${res.status} ${(await res.text()).slice(0, 300)}`);
  }
  const location = res.headers.get("location");
  const uid = res.headers.get("stream-media-id");
  if (!location || !uid) throw new Error("create: missing Location or stream-media-id");
  return { location, uid };
}

/** Streams the file up a chunk at a time rather than buffering it: a 458MB
 *  reel held whole in memory is avoidable waste. */
async function patchChunks({ env, location, file, bytes, onProgress }) {
  const fh = await fs.open(file, "r");
  try {
    let offset = 0;
    const buf = Buffer.allocUnsafe(Math.min(CHUNK, bytes));
    while (offset < bytes) {
      const len = Math.min(CHUNK, bytes - offset);
      const { bytesRead } = await fh.read(buf, 0, len, offset);
      if (bytesRead !== len) throw new Error(`short read @${offset}: ${bytesRead} of ${len}`);
      const res = await fetch(location, {
        method: "PATCH",
        headers: {
          // The tus Location stays on api.cloudflare.com, which authenticates
          // every method — the bearer token is required on PATCH too.
          Authorization: `Bearer ${env.CLOUDFLARE_STREAM_TOKEN}`,
          "Tus-Resumable": "1.0.0",
          "Upload-Offset": String(offset),
          "Content-Type": "application/offset+octet-stream",
        },
        body: buf.subarray(0, len),
      });
      if (res.status !== 204) {
        throw new Error(`patch @${offset}: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
      }
      const next = Number(res.headers.get("upload-offset"));
      // Trust the server's offset, not our arithmetic: a short write we ignored
      // would silently corrupt the tail of the video.
      if (!Number.isFinite(next) || next <= offset) throw new Error(`patch @${offset}: no progress`);
      offset = next;
      onProgress?.(offset, bytes);
    }
  } finally {
    await fh.close();
  }
}

async function pollReady({ env, uid, timeoutMs = 30 * 60 * 1000 }) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const r = await api(env, `/${uid}`);
    if (r.status?.state === "ready") return r;
    if (r.status?.state === "error") throw new Error(`encode failed: ${r.status?.errReasonText}`);
    await new Promise((res) => setTimeout(res, 5000));
  }
  throw new Error("timed out waiting for encode");
}

/** What the site needs about one video, pulled from the Stream record. */
function entry(reel, r) {
  return {
    slug: reel.slug,
    order: reel.order,
    title: reel.title,
    composer: reel.composer,
    uid: r.uid,
    duration: r.duration,
    width: r.input?.width ?? null,
    height: r.input?.height ?? null,
    thumbnail: r.thumbnail ?? null,
    playback: r.playback?.hls ?? null,
    requireSignedURLs: r.requireSignedURLs ?? false,
    allowedOrigins: r.allowedOrigins ?? [],
    bytes: reel.bytes,
    uploadedAt: new Date().toISOString(),
  };
}

async function setOrigins(env, manifest) {
  const allowed = origins(env);
  if (!allowed.length) {
    throw new Error("SHOWCASE_ORIGINS is empty — set it in .env.local (e.g. belgium.vercel.app)");
  }
  console.log(`locking playback to: ${allowed.join(", ")}\n`);
  for (const e of Object.values(manifest.reels)) {
    const r = await api(env, `/${e.uid}`, {
      method: "POST",
      body: JSON.stringify({ allowedOrigins: allowed }),
    });
    e.allowedOrigins = r.allowedOrigins ?? allowed;
    console.log(`  ${e.slug.padEnd(36)} ${(e.allowedOrigins ?? []).join(", ")}`);
  }
  await writeManifest(manifest);
  console.log(`\nupdated ${Object.keys(manifest.reels).length} videos`);
}

async function refresh(env, manifest) {
  for (const e of Object.values(manifest.reels)) {
    const r = await api(env, `/${e.uid}`);
    Object.assign(e, {
      duration: r.duration,
      width: r.input?.width ?? e.width,
      height: r.input?.height ?? e.height,
      thumbnail: r.thumbnail ?? e.thumbnail,
      playback: r.playback?.hls ?? e.playback,
      requireSignedURLs: r.requireSignedURLs ?? false,
      allowedOrigins: r.allowedOrigins ?? [],
    });
    console.log(`  ${e.slug.padEnd(36)} ${r.status?.state}`);
  }
  await writeManifest(manifest);
}

async function main() {
  const { values } = parseArgs({
    options: {
      execute: { type: "boolean", default: false },
      limit: { type: "string" },
      "set-origins": { type: "boolean", default: false },
      refresh: { type: "boolean", default: false },
    },
    strict: false,
  });
  const env = await loadEnv();
  const manifest = await readManifest();
  manifest.reels ??= {};

  if (values["set-origins"]) return setOrigins(env, manifest);
  if (values.refresh) return refresh(env, manifest);

  const reels = await discoverReels();
  const todo = reels.filter((r) => !manifest.reels[r.slug]?.uid);
  const total = reels.reduce((a, r) => a + r.bytes, 0);
  console.log(`${reels.length} reels in output/ (${(total / 1e9).toFixed(2)} GB)`);
  console.log(`${reels.length - todo.length} already on Stream, ${todo.length} to upload`);
  console.log(`account: ${env.CLOUDFLARE_ACCOUNT_ID.slice(0, 8)}…   signed URLs: NO (public showcase)\n`);

  if (!values.execute) {
    console.log("DRY RUN — pass --execute to upload");
    for (const r of todo) {
      console.log(`  ${r.slug.padEnd(36)} ${mb(r.bytes).padStart(6)}  ${r.title}${r.composer ? ` — ${r.composer}` : ""}`);
    }
    return;
  }

  const slice = values.limit ? todo.slice(0, Number(values.limit)) : todo;
  let ok = 0, failed = 0;

  for (const [i, reel] of slice.entries()) {
    process.stdout.write(`[${i + 1}/${slice.length}] ${reel.slug} ${mb(reel.bytes)} — ${reel.title}\n`);
    try {
      const { location, uid } = await createUpload({ env, bytes: reel.bytes, name: reel.title });
      await patchChunks({
        env, location, file: reel.file, bytes: reel.bytes,
        onProgress: (off, tot) =>
          process.stdout.write(`\r    upload ${((off / tot) * 100).toFixed(0)}%   `),
      });
      process.stdout.write("\r    encoding…            ");
      const r = await pollReady({ env, uid });
      manifest.reels[reel.slug] = entry(reel, r);
      await writeManifest(manifest);                 // checkpoint after each
      process.stdout.write(`\r    ready  uid=${uid}  ${r.duration?.toFixed(0)}s\n`);
      ok++;
    } catch (err) {
      process.stdout.write(`\r    FAILED: ${err.message}\n`);
      failed++;
    }
  }
  console.log(`\n${ok} uploaded, ${failed} failed`);
  if (failed) process.exitCode = 1;
}

main().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
