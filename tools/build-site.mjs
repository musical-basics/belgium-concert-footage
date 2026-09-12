#!/usr/bin/env node
/**
 * Generate site/reels.json — the only thing the showcase page needs from the
 * upload manifest. Kept as a separate committed file (rather than inlined into
 * index.html) so the page stays hand-editable and a re-upload only regenerates
 * data, never markup.
 *
 *   node tools/build-site.mjs
 */
import fs from "node:fs/promises";
import path from "node:path";
import { REPO, readManifest } from "./lib/stream.mjs";

const OUT = path.join(REPO, "site", "reels.json");

const manifest = await readManifest();
const reels = Object.values(manifest.reels ?? {})
  .filter((r) => r.uid)
  .sort((a, b) => a.order - b.order)
  .map((r) => ({
    uid: r.uid,
    title: r.title,
    composer: r.composer || null,
    duration: Math.round(r.duration ?? 0),
    thumbnail: r.thumbnail,
    portrait: (r.height ?? 0) > (r.width ?? 0),
  }));

if (!reels.length) throw new Error("no uploaded reels in the manifest — run tools/showcase-stream.mjs first");

const total = reels.reduce((a, r) => a + r.duration, 0);
await fs.mkdir(path.dirname(OUT), { recursive: true });
await fs.writeFile(OUT, JSON.stringify({ reels, totalSeconds: total }, null, 2) + "\n");
console.log(`site/reels.json — ${reels.length} reels, ${Math.round(total / 60)} min`);
