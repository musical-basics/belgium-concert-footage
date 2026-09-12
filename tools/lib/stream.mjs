/**
 * Cloudflare Stream helpers for the public showcase.
 *
 * Deliberately dependency-free (this repo has no npm root): fetch + node
 * builtins only. Credentials come from .env.local at the repo root, which is
 * gitignored — see .env.example for the two keys it needs.
 */
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const MANIFEST = path.join(REPO, "tools", "out", "stream-manifest.json");

/** .env.local values, with real environment variables winning (for CI). */
export async function loadEnv() {
  const env = {};
  try {
    const raw = await fs.readFile(path.join(REPO, ".env.local"), "utf8");
    for (const line of raw.split("\n")) {
      const m = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(line.trim());
      if (m) env[m[1]] = m[2].trim().replace(/^["']|["']$/g, "");
    }
  } catch (err) {
    if (err.code !== "ENOENT") throw err;
  }
  for (const k of ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_STREAM_TOKEN", "SHOWCASE_ORIGINS"]) {
    if (process.env[k]) env[k] = process.env[k];
  }
  for (const k of ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_STREAM_TOKEN"]) {
    if (!env[k]) throw new Error(`${k} is not set — copy .env.example to .env.local and fill it in`);
  }
  return env;
}

/** One Stream API call. Throws on a non-success envelope so callers can't
 *  mistake Cloudflare's 200-with-errors for a win. */
export async function api(env, pathname, init = {}) {
  const res = await fetch(
    `https://api.cloudflare.com/client/v4/accounts/${env.CLOUDFLARE_ACCOUNT_ID}/stream${pathname}`,
    {
      ...init,
      headers: {
        Authorization: `Bearer ${env.CLOUDFLARE_STREAM_TOKEN}`,
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    },
  );
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.success === false) {
    const why = (body.errors ?? []).map((e) => `${e.code} ${e.message}`).join("; ");
    throw new Error(`${init.method ?? "GET"} ${pathname}: HTTP ${res.status} ${why}`);
  }
  return body.result;
}

export async function readManifest() {
  try {
    return JSON.parse(await fs.readFile(MANIFEST, "utf8"));
  } catch (err) {
    if (err.code !== "ENOENT") throw err;
    return { reels: {} };
  }
}

export async function writeManifest(m) {
  await fs.mkdir(path.dirname(MANIFEST), { recursive: true });
  await fs.writeFile(MANIFEST, JSON.stringify(m, null, 2) + "\n");
}

/** Origins the player is allowed to run on, from SHOWCASE_ORIGINS (comma-separated
 *  hostnames, no scheme — that is the shape Stream wants). */
export function origins(env) {
  return (env.SHOWCASE_ORIGINS ?? "")
    .split(",")
    .map((s) => s.trim().replace(/^https?:\/\//, "").replace(/\/$/, ""))
    .filter(Boolean);
}
