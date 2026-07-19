// ============================================================================
// VOIP Phone Club - Cloudflare Worker: serve R2 provisioning files w/ Basic Auth
// ----------------------------------------------------------------------------
// Phones fetch:  https://<worker>.workers.dev/<mac>.cfg   and the common file.
// This Worker checks HTTP Basic Auth, then streams the file from the R2 bucket.
//
// Requires (set in the dashboard, see the setup steps):
//   * R2 binding named           BUCKET     -> your provisioning bucket
//   * Plain-text/secret variable  PROV_USER  -> Basic Auth username
//   * Secret variable             PROV_PASS  -> Basic Auth password
//   * D1 binding named            DB         -> heartbeat log (phone-heartbeats)
//
// Auth here is a SECOND layer. The device .cfg files are also AES-encrypted
// with the DSC tool, so even a bad actor past this layer only gets ciphertext.
//
// Heartbeat log: every authenticated fetch of a per-MAC file doubles as proof
// that the phone was powered and online. We record (mac, timestamp) in D1 and
// keep 30 days, so the admin can spot phones that keep dropping offline.
// One-time D1 setup (dashboard -> D1 -> phone-heartbeats -> Console):
//
//   CREATE TABLE IF NOT EXISTS heartbeats (mac TEXT NOT NULL, ts INTEGER NOT NULL);
//   CREATE INDEX IF NOT EXISTS idx_heartbeats_mac_ts ON heartbeats (mac, ts);
//   CREATE INDEX IF NOT EXISTS idx_heartbeats_ts ON heartbeats (ts);
// ============================================================================

const RETENTION_DAYS = 30;

export default {
  async fetch(request, env, ctx) {
    // Phones only ever GET (HEAD is harmless too). Reject everything else.
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    // --- HTTP Basic Auth ---------------------------------------------------
    const provided = request.headers.get("Authorization") || "";
    const expected = "Basic " + btoa(`${env.PROV_USER}:${env.PROV_PASS}`);
    if (provided !== expected) {
      return new Response("Authentication required", {
        status: 401,
        headers: { "WWW-Authenticate": 'Basic realm="phone-club"' },
      });
    }

    // --- Map the URL path to an object key in the bucket -------------------
    // "/0c383e7fbefb.cfg" -> "0c383e7fbefb.cfg"
    const url = new URL(request.url);
    const key = decodeURIComponent(url.pathname.replace(/^\/+/, ""));
    if (!key) return new Response("Not found", { status: 404 });

    // --- Heartbeat queries (admin, same Basic Auth) ------------------------
    if (key === "heartbeats") {
      return heartbeatReport(env, url);
    }

    // --- Record the heartbeat ----------------------------------------------
    // A fetch of <mac>.cfg or <mac>.boot identifies the phone; log it even if
    // the object doesn't exist (the request itself proves the phone is up).
    // waitUntil: logging never delays - and can never break - provisioning.
    const m = key.match(/^([0-9a-f]{12})\.(cfg|boot)$/i);
    if (m && env.DB) {
      ctx.waitUntil(logHeartbeat(env, m[1].toLowerCase()));
    }

    // --- Fetch from R2 -----------------------------------------------------
    const object = await env.BUCKET.get(key);
    if (!object) return new Response("Not found", { status: 404 });

    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set("etag", object.httpEtag);
    // Config changes are picked up via the file's Version bump, so make sure
    // the edge never serves a stale copy after you upload a new version.
    headers.set("Cache-Control", "no-store");

    return new Response(request.method === "HEAD" ? null : object.body, {
      headers,
    });
  },
};

async function logHeartbeat(env, mac) {
  try {
    const now = Math.floor(Date.now() / 1000);
    await env.DB.prepare("INSERT INTO heartbeats (mac, ts) VALUES (?1, ?2)")
      .bind(mac, now).run();
    // Occasionally prune anything past retention (cheap, indexed on ts).
    if (Math.random() < 0.02) {
      await env.DB.prepare("DELETE FROM heartbeats WHERE ts < ?1")
        .bind(now - RETENTION_DAYS * 86400).run();
    }
  } catch (e) {
    // Monitoring must never break provisioning: swallow and move on.
  }
}

async function heartbeatReport(env, url) {
  if (!env.DB) {
    return Response.json(
      { ok: false, error: "no D1 binding named DB - add it in Settings > Bindings" },
      { status: 500 });
  }
  const mac = (url.searchParams.get("mac") || "").toLowerCase();
  const hours = Math.min(
    parseInt(url.searchParams.get("hours") || "168", 10) || 168, 24 * RETENTION_DAYS);
  const since = Math.floor(Date.now() / 1000) - hours * 3600;
  try {
    if (mac) {
      // Raw timestamps for one phone - callers compute gaps/uptime from these.
      const rows = await env.DB.prepare(
        "SELECT ts FROM heartbeats WHERE mac = ?1 AND ts >= ?2 ORDER BY ts")
        .bind(mac, since).all();
      return Response.json(
        { ok: true, mac, hours, ts: rows.results.map(r => r.ts) });
    }
    // Overview: last-seen + beat count per phone in the window.
    const rows = await env.DB.prepare(
      "SELECT mac, MAX(ts) AS last_seen, COUNT(*) AS beats " +
      "FROM heartbeats WHERE ts >= ?1 GROUP BY mac ORDER BY mac")
      .bind(since).all();
    return Response.json({ ok: true, hours, phones: rows.results });
  } catch (e) {
    return Response.json(
      { ok: false, error: "db error - did you run the CREATE TABLE from the header comment?" },
      { status: 500 });
  }
}
