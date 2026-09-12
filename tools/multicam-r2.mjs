#!/usr/bin/env node
/**
 * Publish the three stationary camera proxies to R2 for the showcase's
 * multi-cam view.
 *
 * Why R2 and not Stream, when the reels are on Stream: the multi-cam panes are
 * kept in sync by driving `currentTime` on each <video> (the same master +
 * drift-correction the editor uses in app.js). Stream's iframe player exposes
 * no such clock, and its adaptive-bitrate switching stalls on quality changes,
 * which is exactly what pulls the panes apart. Plain mp4 over HTTP gives exact
 * seeking and free egress.
 *
 * Uploaded whole rather than cut per reel: the proxies are already faststart
 * (moov at byte 32), so a pane can range-seek straight to a reel's `in` second
 * without fetching the 900MB ahead of it. That makes a cutting pass pointless.
 *
 *   node tools/multicam-r2.mjs             # plan only
 *   node tools/multicam-r2.mjs --execute
 *   node tools/multicam-r2.mjs --force     # re-upload even if present
 */
import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import { REPO, loadEnv } from "./lib/stream.mjs";
import { putLargeFile, headObject } from "./lib/r2.mjs";

const PREFIX = "belgium-concert/multicam";
// The 3 stationary angles. The 5D 2 is deliberately absent: it is a roving
// camera with gaps (31 clips in editor/sync.json), so a pane for it would go
// dark mid-song and need the editor's coverage badge to explain itself.
const ANGLES = [
  { id: "back", label: "Back Camera", proxy: "back.mp4", audio: true },
  { id: "livestream", label: "Livestream", proxy: "livestream.mp4" },
  { id: "piano", label: "Next to Piano", proxy: "piano.mp4" },
];

const { values } = parseArgs({
  options: { execute: { type: "boolean", default: false }, force: { type: "boolean", default: false } },
  strict: false,
});

const env = await loadEnv();
for (const k of ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_MULTICAM", "MULTICAM_BASE"]) {
  if (!env[k]) throw new Error(`${k} is not set — see .env.example`);
}
const bucket = env.R2_BUCKET_MULTICAM;

const plan = [];
for (const a of ANGLES) {
  const file = path.join(REPO, "proxies", a.proxy);
  const bytes = (await fs.stat(file)).size;
  const key = `${PREFIX}/${a.proxy}`;
  const existing = await headObject({ env, bucket, key });
  plan.push({ ...a, file, bytes, key, existing });
}

const total = plan.reduce((s, p) => s + p.bytes, 0);
console.log(`bucket: ${bucket}   public base: ${env.MULTICAM_BASE}`);
console.log(`${plan.length} angles, ${(total / 1e9).toFixed(2)} GB\n`);
for (const p of plan) {
  const state = p.existing === p.bytes ? "up to date" : p.existing ? `differs (${p.existing} remote)` : "missing";
  console.log(`  ${p.proxy.padEnd(16)} ${(p.bytes / 1e6).toFixed(0).padStart(4)} MB   ${state}`);
}

const todo = plan.filter((p) => values.force || p.existing !== p.bytes);
if (!values.execute) {
  console.log(`\nDRY RUN — ${todo.length} to upload. Pass --execute.`);
  process.exit(0);
}
if (!todo.length) console.log("\nnothing to upload");

for (const p of todo) {
  process.stdout.write(`\n${p.proxy} -> ${p.key}\n`);
  await putLargeFile({
    env, bucket, key: p.key, file: p.file,
    contentType: "video/mp4",
    // Immutable content under a stable key: cache hard, it is 900MB.
    cacheControl: "public, max-age=31536000, immutable",
    onProgress: (o, t) => process.stdout.write(`\r  ${((o / t) * 100).toFixed(0)}%   `),
  });
  process.stdout.write("\r  done      \n");
}

// The view descriptor the site reads: angle list + where each reel starts, so a
// pane can seek straight to the song instead of playing from the concert's top.
const reels = [];
for (const f of (await fs.readdir(path.join(REPO, "output"))).filter((n) => /^\d\d_.+\.plan\.json$/.test(n)).sort()) {
  const d = JSON.parse(await fs.readFile(path.join(REPO, "output", f), "utf8"));
  if (typeof d.in === "number" && typeof d.out === "number") {
    reels.push({ slug: f.replace(/\.plan\.json$/, ""), in: d.in, out: d.out });
  }
}
const out = {
  base: `${env.MULTICAM_BASE}/${PREFIX}`,
  angles: plan.map((p) => ({ id: p.id, label: p.label, file: p.proxy, audio: !!p.audio })),
  reels,
};
await fs.writeFile(path.join(REPO, "site", "multicam.json"), JSON.stringify(out, null, 2) + "\n");
console.log(`\nsite/multicam.json — ${out.angles.length} angles, ${reels.length} reel ranges`);
