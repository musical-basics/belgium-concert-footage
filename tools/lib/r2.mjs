/**
 * Minimal S3/SigV4 client for Cloudflare R2 — dependency-free, adapted from the
 * ultimatepianist monorepo's tools/lib/r2.mjs.
 *
 * The multicam proxies are ~900MB each, so uploads go through multipart rather
 * than a single PUT: 64MB parts keep memory bounded and let a failed part retry
 * on its own instead of restarting a gigabyte.
 */
import crypto from "node:crypto";
import fs from "node:fs/promises";

const sha256hex = (b) => crypto.createHash("sha256").update(b).digest("hex");
const hmac = (k, m) => crypto.createHmac("sha256", k).update(m).digest();

/** One signed request. `query` is signed too (multipart needs uploadId/partNumber). */
export function signedRequest({ env, method, bucket, key, query = {}, body = Buffer.alloc(0), headers = {} }) {
  const host = `${env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com`;
  const canonicalPath =
    "/" + bucket + (key ? "/" + key.split("/").map(encodeURIComponent).join("/") : "");
  const canonicalQuery = Object.keys(query)
    .sort()
    .map((k) => `${encodeURIComponent(k)}=${encodeURIComponent(query[k])}`)
    .join("&");

  const amz = new Date().toISOString().replace(/[-:]|\.\d{3}/g, "");
  const ds = amz.slice(0, 8);
  const payloadHash = sha256hex(body);

  const all = {
    ...Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), String(v)])),
    host,
    "x-amz-content-sha256": payloadHash,
    "x-amz-date": amz,
  };
  const signedHeaders = Object.keys(all).sort().join(";");
  const canonicalHeaders = Object.keys(all).sort().map((k) => `${k}:${all[k]}\n`).join("");

  const canonical = `${method}\n${canonicalPath}\n${canonicalQuery}\n${canonicalHeaders}\n${signedHeaders}\n${payloadHash}`;
  const scope = `${ds}/auto/s3/aws4_request`;
  const sts = `AWS4-HMAC-SHA256\n${amz}\n${scope}\n${sha256hex(Buffer.from(canonical))}`;
  const signingKey = hmac(hmac(hmac(hmac(`AWS4${env.R2_SECRET_ACCESS_KEY}`, ds), "auto"), "s3"), "aws4_request");
  const signature = crypto.createHmac("sha256", signingKey).update(sts).digest("hex");

  return {
    url: `https://${host}${canonicalPath}${canonicalQuery ? "?" + canonicalQuery : ""}`,
    headers: {
      ...all,
      Authorization: `AWS4-HMAC-SHA256 Credential=${env.R2_ACCESS_KEY_ID}/${scope}, SignedHeaders=${signedHeaders}, Signature=${signature}`,
    },
  };
}

export async function putObject({ env, bucket, key, body, contentType, cacheControl }) {
  const headers = { "content-type": contentType };
  if (cacheControl) headers["cache-control"] = cacheControl;
  const { url, headers: h } = signedRequest({ env, method: "PUT", bucket, key, body, headers });
  const res = await fetch(url, { method: "PUT", headers: h, body });
  if (!res.ok) throw new Error(`PUT ${key}: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
  return res.headers.get("etag");
}

/** Size of an existing object, or null. Makes re-runs cheap. */
export async function headObject({ env, bucket, key }) {
  const { url, headers } = signedRequest({ env, method: "HEAD", bucket, key });
  const res = await fetch(url, { method: "HEAD", headers });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`HEAD ${key}: HTTP ${res.status}`);
  return Number(res.headers.get("content-length"));
}

const xml = (body, tag) => body.match(new RegExp(`<${tag}>([^<]+)</${tag}>`))?.[1];

/** Multipart upload of a file on disk. Reads one part at a time. */
export async function putLargeFile({ env, bucket, key, file, contentType, cacheControl, partSize = 64 * 1024 * 1024, onProgress }) {
  const size = (await fs.stat(file)).size;

  const headers = { "content-type": contentType };
  if (cacheControl) headers["cache-control"] = cacheControl;
  const start = signedRequest({ env, method: "POST", bucket, key, query: { uploads: "" }, headers });
  const startRes = await fetch(start.url, { method: "POST", headers: start.headers });
  if (!startRes.ok) throw new Error(`create multipart ${key}: HTTP ${startRes.status}`);
  const uploadId = xml(await startRes.text(), "UploadId");
  if (!uploadId) throw new Error(`create multipart ${key}: no UploadId`);

  const fh = await fs.open(file, "r");
  const parts = [];
  try {
    const buf = Buffer.allocUnsafe(Math.min(partSize, size));
    let offset = 0, n = 0;
    while (offset < size) {
      const len = Math.min(partSize, size - offset);
      const { bytesRead } = await fh.read(buf, 0, len, offset);
      if (bytesRead !== len) throw new Error(`short read @${offset}`);
      const part = buf.subarray(0, len);
      n += 1;
      const q = { partNumber: String(n), uploadId };
      const signed = signedRequest({ env, method: "PUT", bucket, key, query: q, body: part });
      const res = await fetch(signed.url, { method: "PUT", headers: signed.headers, body: part });
      if (!res.ok) throw new Error(`part ${n} of ${key}: HTTP ${res.status} ${(await res.text()).slice(0, 160)}`);
      parts.push({ n, etag: res.headers.get("etag") });
      offset += len;
      onProgress?.(offset, size);
    }
  } catch (err) {
    // Leaving a dangling multipart upload bills for the stored parts.
    const ab = signedRequest({ env, method: "DELETE", bucket, key, query: { uploadId } });
    await fetch(ab.url, { method: "DELETE", headers: ab.headers }).catch(() => {});
    await fh.close();
    throw err;
  }
  await fh.close();

  const body = Buffer.from(
    `<CompleteMultipartUpload>${parts
      .map((p) => `<Part><PartNumber>${p.n}</PartNumber><ETag>${p.etag}</ETag></Part>`)
      .join("")}</CompleteMultipartUpload>`,
  );
  const done = signedRequest({ env, method: "POST", bucket, key, query: { uploadId }, body });
  const doneRes = await fetch(done.url, { method: "POST", headers: done.headers, body });
  if (!doneRes.ok) throw new Error(`complete ${key}: HTTP ${doneRes.status} ${(await doneRes.text()).slice(0, 200)}`);
  return size;
}
