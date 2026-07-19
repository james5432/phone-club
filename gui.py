#!/usr/bin/env python3
"""Phone Club provisioning GUI - a localhost web front-end for add_member.py.

Run it from a terminal where phoneclub.env has been sourced:

    source phoneclub.env && .venv/bin/python gui.py

then open http://127.0.0.1:8765

Safety model:
  * binds to 127.0.0.1 only - never reachable from the network
  * the Provision button stays disabled until a dry run of the exact same
    details succeeds; changing any field disarms it again
  * same add-only guarantees as add_member.py: the R2 key is checked before
    the sub-account is created, and nothing is ever overwritten or deleted
  * SIP passwords are generated server-side and never appear in the UI,
    the logs, or any API response
"""

import hashlib
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request

import add_member as am

PORT = 8765

# Provisioning Worker (for the heartbeat/uptime card) - same env vars the
# bootstrap tooling uses; all Worker calls happen server-side only.
PROV_SERVER_URL = (os.environ.get("PROV_SERVER_URL") or "").rstrip("/")
PROV_HTTP_USER  = os.environ.get("PROV_HTTP_USER")
PROV_HTTP_PASS  = os.environ.get("PROV_HTTP_PASS")

app = Flask(__name__)

state_lock = threading.Lock()
job = {"status": "idle", "log": [], "details": None}   # one job at a time
armed = None                          # details of the last successful dry run


def log(msg):
    job["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")


def token_for(name, mac, ext):
    return hashlib.sha256(f"{name}|{mac}|{ext}".encode()).hexdigest()[:16]


def env_flags():
    return {
        "voipms": bool(am.VOIPMS_ACCOUNT and am.VOIPMS_SERVER
                       and am.VOIPMS_API_USER and am.VOIPMS_API_PASS),
        "r2": bool(am.R2_ENDPOINT and am.R2_ACCESS_KEY and am.R2_SECRET_KEY),
        "template": os.path.exists(am.TEMPLATE_PATH),
    }


def get_members():
    data = am.voipms("getSubAccounts")
    members = []
    for a in data.get("accounts", []):
        members.append({
            "account": a.get("account") or a.get("username") or "?",
            "extension": str(a.get("internal_extension", "")).strip(),
            "description": a.get("description", ""),
        })
    members.sort(key=lambda m: m["extension"] or "999")
    return members


@app.get("/api/state")
def api_state():
    env = env_flags()
    out = {"env": env, "members": [], "next_ext": None,
           "job_status": job["status"], "error": None}
    if env["voipms"]:
        try:
            out["members"] = get_members()
            used = {m["extension"] for m in out["members"]}
            out["next_ext"] = am.next_extension(used)
        except SystemExit as e:
            out["error"] = str(e)          # e.g. extension pool exhausted
        except Exception as e:
            msg = str(e) or type(e).__name__
            if "ip_not_enabled" in msg:
                msg += (" — this machine's IP isn't whitelisted: update it at "
                        "voip.ms → Main Menu → SOAP and REST/JSON API")
            elif "invalid_credentials" in msg:
                msg += (" — check VOIPMS_API_USER / VOIPMS_API_PASS in "
                        "phoneclub.env (API password, not portal password)")
            out["error"] = msg
    return jsonify(out)


@app.get("/api/registration")
def api_registration():
    account = request.args.get("account", "")
    try:
        return jsonify(ok=True, registered=am.registration_status(account))
    except Exception as e:
        return jsonify(ok=False, error=type(e).__name__)


_mac_owner_cache = {}   # mac -> sip account, learned from the bucket's configs


def _mac_owner_map():
    """Map <mac> -> sip account (e.g. 521431_Rody) via the per-MAC configs in R2.

    Config bodies are read server-side only to find the account name and never
    leave this function (they contain SIP passwords). Cached per MAC: each
    config is read at most once per GUI process.
    """
    s3 = am.r2_client()
    pat = re.compile(re.escape(am.VOIPMS_ACCOUNT).encode() + rb"_[A-Za-z0-9]+")
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=am.R2_BUCKET):
        for obj in page.get("Contents", []):
            m = re.fullmatch(r"([0-9a-f]{12})\.cfg", obj["Key"])
            if not m or m.group(1) in _mac_owner_cache:
                continue
            body = s3.get_object(Bucket=am.R2_BUCKET, Key=obj["Key"])["Body"].read()
            hit = pat.search(body)
            if hit:
                _mac_owner_cache[m.group(1)] = hit.group(0).decode()
    return dict(_mac_owner_cache)


@app.get("/api/heartbeats")
def api_heartbeats():
    """Hourly uptime-ribbon data for every provisioned phone (last 7 days).

    Merges the Worker's heartbeat log (keyed by MAC) with the member list
    (keyed by account) so the UI can label rows 'Rody (104)'.
    """
    if not (PROV_SERVER_URL and PROV_HTTP_USER and PROV_HTTP_PASS):
        return jsonify(ok=False, error="PROV_* env vars not set - restart gui.py "
                                       "from a terminal where phoneclub.env is sourced")
    hours = 168
    now = int(time.time())
    start = now - hours * 3600
    try:
        owners = _mac_owner_map()
        exts = {m["account"]: m["extension"] for m in get_members()}
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}")

    phones = []
    for mac, account in sorted(owners.items(), key=lambda kv: exts.get(kv[1], "999")):
        try:
            r = requests.get(f"{PROV_SERVER_URL}/heartbeats",
                             params={"mac": mac, "hours": hours},
                             auth=(PROV_HTTP_USER, PROV_HTTP_PASS), timeout=20)
            ts = r.json().get("ts", []) if r.status_code == 200 else []
        except requests.RequestException:
            return jsonify(ok=False, error="heartbeat endpoint unreachable")
        cells = [0] * hours
        for t in ts:
            i = (t - start) // 3600
            if 0 <= i < hours:
                cells[i] = 1
        # uptime measured from the phone's first beat in the window, so a
        # phone provisioned yesterday isn't punished for the empty week before
        first = ts[0] if ts else None
        covered = [c for i, c in enumerate(cells)
                   if first is not None and start + (i + 1) * 3600 > first]
        phones.append({
            "mac": mac,
            "name": account.split("_", 1)[-1],
            "ext": exts.get(account, ""),
            "last_seen": ts[-1] if ts else None,
            "uptime": round(100 * sum(covered) / len(covered)) if covered else None,
            "cells": cells,
        })
    return jsonify(ok=True, hours=hours, now=now, phones=phones)


def _renews_in_minutes(next_str):
    """Best-effort minutes until the phone must re-register.

    VoIP.ms reports register_next in an unspecified server timezone. The
    phones register with RegisterTTL 3600s (phone.cfg.template), so the true
    deadline is always within the next hour: try each whole-hour timezone
    offset and accept the single one that lands in that window. Returns None
    when the inference is ambiguous or the timestamp unparseable.
    """
    try:
        naive = datetime.strptime(next_str, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    now = datetime.now(timezone.utc)
    hits = []
    for h in range(-12, 15):
        cand = naive.replace(tzinfo=timezone(timedelta(hours=h)))
        delta = (cand - now).total_seconds()
        if -120 < delta <= 3720:      # small margin for clock skew
            hits.append(delta)
    positive = [d for d in hits if d > 0]
    if len(hits) == 1:
        return max(0, int(hits[0] // 60))
    if len(positive) == 1:      # skew margin caught the neighbouring hour too
        return int(positive[0] // 60)
    return None


@app.get("/api/phone_details")
def api_phone_details():
    """Registration details for one sub-account, for the member row detail view.

    Returns only what VoIP.ms reports about the phone's registration - no
    credentials or config content ever pass through here.
    """
    account = request.args.get("account", "")
    try:
        data = am.voipms("getRegistrationStatus", account=account)
    except Exception as e:
        return jsonify(ok=False, error=type(e).__name__)
    regs = []
    for r in (data.get("registrations") or []):
        ua = str(r.get("register_useragent", ""))
        parts = ua.split()
        # e.g. "Fanvil H2U-V2 2.12.20.2 0c383e841c47" -> firmware 2.12.20.2
        firmware = parts[2] if len(parts) >= 3 and parts[0].lower() == "fanvil" else ""
        regs.append({
            "ip":        r.get("register_ip", ""),
            "port":      r.get("register_port", ""),
            "server":    r.get("server_name", ""),
            "transport": r.get("register_transport", ""),
            "next":      r.get("register_next", ""),
            "renews_in_min": _renews_in_minutes(r.get("register_next", "")),
            "useragent": ua,
            "firmware":  firmware,
        })
    return jsonify(ok=True,
                   registered=str(data.get("registered", "")).lower() == "yes",
                   registrations=regs)


@app.post("/api/dryrun")
def api_dryrun():
    global armed
    if job["status"] == "running":
        return jsonify(ok=False, error="a provisioning job is already running"), 409
    p = request.get_json(force=True)
    try:
        name = am.validate_name(p.get("name") or "")
        mac = am.normalise_mac(p.get("mac") or "")
        members = get_members()
        used = {m["extension"] for m in members}
        ext_raw = (p.get("ext") or "").strip()
        ext = am.validate_extension(ext_raw, used) if ext_raw else am.next_extension(used)
        sip_user = f"{am.VOIPMS_ACCOUNT}_{name}"
        if any(m["account"].lower() == sip_user.lower() for m in members):
            raise SystemExit(f"error: sub-account {sip_user} already exists")

        checks = [f"name and MAC valid, extension {ext} is free"]
        cfg = am.render_cfg(mac, sip_user, "DRY-RUN-PLACEHOLDER", ext)
        checks.append(f"template renders OK ({len(cfg)} bytes)")
        key = f"{mac}.cfg"
        if not env_flags()["r2"]:
            raise SystemExit("error: R2_* env vars not set")
        am.ensure_key_free(am.r2_client(), key)
        checks.append(f"'{key}' is free in bucket '{am.R2_BUCKET}'")

        with state_lock:
            armed = {"token": token_for(name, mac, ext),
                     "name": name, "mac": mac, "ext": ext}
        return jsonify(ok=True, name=name, mac=mac, ext=ext, sip_user=sip_user,
                       key=key, checks=checks, token=armed["token"])
    except SystemExit as e:
        return jsonify(ok=False, error=str(e))
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}")


def run_provision(d):
    name, mac, ext = d["name"], d["mac"], d["ext"]
    sip_user = f"{am.VOIPMS_ACCOUNT}_{name}"
    key = f"{mac}.cfg"
    try:
        password = am.make_password()
        cfg = am.render_cfg(mac, sip_user, password, ext)
        s3 = am.r2_client()
        am.ensure_key_free(s3, key)

        log(f"creating sub-account {sip_user} (ext {ext}) ...")
        account = am.create_subaccount(name, ext, password)
        log("sub-account created")

        am.upload_to_r2(s3, key, cfg)
        log(f"uploaded {key} to bucket '{am.R2_BUCKET}'")
        log("waiting for the phone to register - power it on and plug in ethernet")

        deadline = time.time() + am.REG_TIMEOUT_S
        while time.time() < deadline:
            if am.registration_status(account):
                log(f"{name} (ext {ext}) is LIVE and registered")
                job["status"] = "registered"
                return
            time.sleep(10)
            log(f"not registered yet ({max(0, int(deadline - time.time()))}s left) ...")
        log(f"not registered after {am.REG_TIMEOUT_S}s. The account and config are in "
            f"place - check power/ethernet and that Update Mode is 'Update at Time "
            f"Interval'. The phone will pick up the config on its next poll.")
        job["status"] = "uploaded_only"
    except SystemExit as e:
        log(str(e))
        job["status"] = "failed"
    except Exception as e:
        log(f"failed: {type(e).__name__}: {e}")
        job["status"] = "failed"


@app.post("/api/provision")
def api_provision():
    global armed
    p = request.get_json(force=True)
    with state_lock:
        if job["status"] == "running":
            return jsonify(ok=False, error="a provisioning job is already running"), 409
        if not armed or p.get("token") != armed["token"]:
            return jsonify(ok=False,
                           error="not armed - run a dry run of these exact details first"), 400
        details, armed = armed, None
        job["status"] = "running"
        job["log"] = []
        job["details"] = {"name": details["name"], "ext": details["ext"],
                          "mac": details["mac"]}
    threading.Thread(target=run_provision, args=(details,), daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/job")
def api_job():
    return jsonify(status=job["status"], log=job["log"], details=job["details"])


@app.get("/")
def index():
    return PAGE


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Babbel en Bel Admin</title>
<style>
  :root { --accent:#2563eb; --ok:#16a34a; --bad:#dc2626; --muted:#6b7280; }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,system-ui,sans-serif; margin:0; background:#f3f4f6; color:#111827; }
  header { background:#F94892; color:#fff; padding:14px 22px; font-size:18px; font-weight:600; }
  header small { font-weight:400; color:rgba(255,255,255,.85); margin-left:10px; }
  main { max-width:840px; margin:22px auto 60px; padding:0 16px; display:grid; gap:16px; }
  .card { background:#fff; border-radius:10px; padding:16px 20px; box-shadow:0 1px 3px rgba(0,0,0,.08); overflow-x:auto; }
  .card h2 { margin:0 0 12px; font-size:15px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
  .chips { display:flex; gap:8px; flex-wrap:wrap; }
  .chip { padding:4px 12px; border-radius:999px; font-size:13px; font-weight:600; }
  .chip.ok  { background:#dcfce7; color:#166534; }
  .chip.bad { background:#fee2e2; color:#991b1b; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th { text-align:left; color:var(--muted); font-weight:600; padding:6px 8px; border-bottom:1px solid #e5e7eb; }
  td { padding:7px 8px; border-bottom:1px solid #f1f5f9; }
  .reg-yes { color:var(--ok); font-weight:700; }
  .reg-no  { color:var(--bad); font-weight:700; }
  .reg-wait { color:var(--muted); }
  tr.mrow { cursor:pointer; }
  tr.mrow:hover td { background:#f8fafc; }
  tr.drow td { background:#f8fafc; padding:10px 14px; }
  .kv { display:grid; grid-template-columns:auto 1fr; gap:2px 18px; font-size:13px; margin:0; }
  .kv dt { color:var(--muted); font-weight:600; }
  .kv dd { margin:0; font-family:ui-monospace,monospace; }
  .ribbonrow { display:flex; align-items:center; gap:10px; margin:7px 0; }
  .riblabel { flex:0 0 110px; font-size:13px; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ribbon { display:flex; gap:1px; flex:1; min-width:0; }
  .ribbon i { flex:1 1 0; height:16px; border-radius:1px; background:#e5e7eb; }
  .ribbon i.on { background:#22c55e; }
  .ribmeta { flex:0 0 120px; font-size:12px; color:var(--muted); text-align:right; }
  .cardhead { display:flex; justify-content:space-between; align-items:baseline; }
  .linkbtn { font-size:12px; padding:3px 12px; border-radius:6px; border:1px solid #d1d5db; background:#fff; cursor:pointer; color:#374151; }
  #golive { display:none; margin-top:14px; padding:14px 16px; border-radius:8px; background:#eef2ff; border:1px solid #c7d2fe; }
  #golive h3 { margin:0 0 8px; font-size:14px; }
  #golive ol { margin:0 0 12px 18px; padding:0; font-size:14px; line-height:1.7; }
  #announce { width:100%; box-sizing:border-box; padding:9px 10px; border:1px solid #c7d2fe; border-radius:8px; font-size:14px; font-family:inherit; resize:vertical; min-height:52px; }
  #copyBtn { margin-top:8px; }
  form { display:grid; grid-template-columns:2fr 2fr 1fr; gap:12px; }
  label { display:grid; gap:4px; font-size:13px; font-weight:600; color:#374151; min-width:0; }
  input { padding:9px 10px; border:1px solid #d1d5db; border-radius:8px; font-size:15px; width:100%; min-width:0; }
  @media (max-width:700px) { form { grid-template-columns:1fr; } }
  input:focus { outline:2px solid var(--accent); border-color:transparent; }
  .buttons { grid-column:1/-1; display:flex; gap:10px; align-items:center; }
  button.action { padding:10px 18px; border-radius:8px; border:none; font-size:15px; font-weight:600; cursor:pointer; }
  #dryrunBtn { background:#e0e7ff; color:#3730a3; }
  #provisionBtn { background:var(--accent); color:#fff; }
  button.action:disabled { opacity:.4; cursor:not-allowed; }
  #result { margin-top:14px; padding:12px 14px; border-radius:8px; font-size:14px; display:none; white-space:pre-wrap; }
  #result.good { display:block; background:#f0fdf4; border:1px solid #bbf7d0; }
  #result.err  { display:block; background:#fef2f2; border:1px solid #fecaca; }
  #log { background:#0f172a; color:#e2e8f0; border-radius:8px; padding:12px 14px; font:13px ui-monospace,monospace; white-space:pre-wrap; min-height:60px; max-height:320px; overflow-y:auto; display:none; }
  .hint { font-size:13px; color:var(--muted); }
</style>
</head>
<body>
<header>&#128222; Babbel en Bel Admin <small>handset provisioning</small></header>
<main>

  <div class="card">
    <h2>Environment</h2>
    <div class="chips" id="chips">loading &hellip;</div>
  </div>

  <div class="card">
    <div class="cardhead">
      <h2>Current members</h2>
      <button class="linkbtn" id="recheck">&#8635; check again</button>
    </div>
    <table id="members"><thead>
      <tr><th>Sub-account</th><th>Ext</th><th>Description</th><th>Online</th></tr>
    </thead><tbody></tbody></table>
    <p class="hint" id="membersHint"></p>
  </div>

  <div class="card">
    <div class="cardhead">
      <h2>Uptime &mdash; last 7 days</h2>
      <button class="linkbtn" id="hbRefresh">&#8635; refresh</button>
    </div>
    <div id="ribbons" class="hint">loading &hellip;</div>
    <p class="hint" id="hbHint"></p>
  </div>

  <div class="card">
    <h2>New handset</h2>
    <form id="form" onsubmit="return false">
      <label>Child's name
        <input id="name" placeholder="Femke" autocomplete="off">
      </label>
      <label>MAC address (on the sticker)
        <input id="mac" placeholder="0C:38:3E:1A:2B:3C" autocomplete="off">
      </label>
      <label>Extension
        <input id="ext" placeholder="auto" autocomplete="off">
      </label>
      <div class="buttons">
        <button class="action" id="dryrunBtn">Dry run</button>
        <button class="action" id="provisionBtn" disabled>Provision</button>
        <span class="hint" id="armHint">Provision unlocks after a clean dry run.</span>
      </div>
    </form>
    <div id="result"></div>
    <div id="log"></div>
    <div id="golive">
      <h3>&#127881; Next steps</h3>
      <ol>
        <li>Deliver the phone and test it at the member's home (call someone!)</li>
        <li>Announce on the Signal channel &mdash; message below, edit as you like</li>
        <li>Add the extension to the directory (until that's automated)</li>
      </ol>
      <textarea id="announce" rows="2"></textarea>
      <br>
      <button class="linkbtn" id="copyBtn">&#128203; Copy message</button>
      <span class="hint" id="copyHint"></span>
    </div>
  </div>

</main>
<script>
const $ = id => document.getElementById(id);
let token = null, polling = null;

function disarm() {
  token = null;
  $('provisionBtn').disabled = true;
  $('armHint').textContent = 'Provision unlocks after a clean dry run.';
}
['name','mac','ext'].forEach(id => $(id).addEventListener('input', disarm));

async function loadState() {
  const s = await (await fetch('/api/state')).json();
  $('chips').innerHTML = [
    ['VoIP.ms credentials', s.env.voipms],
    ['R2 credentials', s.env.r2],
    ['phone.cfg.template', s.env.template],
  ].map(([n, ok]) =>
    `<span class="chip ${ok ? 'ok' : 'bad'}">${n}: ${ok ? 'OK' : 'missing'}</span>`
  ).join('');
  const tb = $('members').querySelector('tbody');
  tb.innerHTML = s.members.map(m =>
    `<tr class="mrow" data-account="${m.account}" title="click for phone details">
     <td>${m.account}</td><td>${m.extension || '-'}</td><td>${m.description}</td>
     <td class="regcell" data-account="${m.account}"><span class="reg-wait">&hellip;</span></td></tr>`
  ).join('');
  sweepRegistration();
  $('membersHint').textContent = s.error ? s.error :
    (s.next_ext ? `Next free extension: ${s.next_ext}` : '');
  if (s.next_ext) $('ext').placeholder = `auto (${s.next_ext})`;
  const blocked = !(s.env.voipms && s.env.r2 && s.env.template);
  $('dryrunBtn').disabled = blocked || s.job_status === 'running';
  if (blocked) $('armHint').textContent =
    'Fix the environment above, then restart gui.py from a terminal where phoneclub.env is sourced.';
  if (s.job_status === 'running') pollJob();
}

async function sweepRegistration() {
  const cells = [...document.querySelectorAll('.regcell')];
  await Promise.all(cells.map(async cell => {
    cell.innerHTML = '<span class="reg-wait">&hellip;</span>';
    try {
      const r = await (await fetch('/api/registration?account=' +
        encodeURIComponent(cell.dataset.account))).json();
      cell.innerHTML = r.ok
        ? (r.registered ? '<span class="reg-yes">&#9679; online</span>'
                        : '<span class="reg-no">&#9679; offline</span>')
        : '<span class="hint">error</span>';
    } catch {
      cell.innerHTML = '<span class="hint">error</span>';
    }
  }));
}
$('recheck').addEventListener('click', sweepRegistration);

$('members').querySelector('tbody').addEventListener('click', async e => {
  const row = e.target.closest('tr.mrow');
  if (!row) return;
  const next = row.nextElementSibling;
  if (next && next.classList.contains('drow')) { next.remove(); return; }
  document.querySelectorAll('tr.drow').forEach(el => el.remove());
  const d = document.createElement('tr');
  d.className = 'drow';
  d.innerHTML = '<td colspan="4">loading details &hellip;</td>';
  row.after(d);
  const cell = d.firstElementChild;
  try {
    const r = await (await fetch('/api/phone_details?account=' +
      encodeURIComponent(row.dataset.account))).json();
    if (!r.ok) { cell.textContent = 'lookup failed: ' + r.error; return; }
    if (!r.registrations.length) {
      cell.innerHTML =
        '<span class="reg-no">No active registration.</span> ' +
        '<span class="hint">The phone is off, unplugged or offline &mdash; or its last ' +
        'lease just expired. Note: a registration can linger up to an hour after a ' +
        'phone goes offline, and reappears within a minute of it coming back.</span>';
      return;
    }
    cell.innerHTML = r.registrations.map(g => `
      <dl class="kv">
        <dt>Status</dt><dd><span class="reg-yes">registered</span></dd>
        <dt>Phone is at</dt><dd>${g.ip}:${g.port} (${g.transport})</dd>
        <dt>VoIP server</dt><dd>${g.server}</dd>
        <dt>Firmware</dt><dd>${g.firmware || '?'}</dd>
        <dt>User agent</dt><dd>${g.useragent}</dd>
        <dt>Lease</dt><dd>${g.renews_in_min != null
          ? 'renews within ~' + g.renews_in_min + ' min'
          : g.next}</dd>
      </dl>`).join('');
  } catch {
    cell.textContent = 'lookup failed';
  }
});

function fmtAgo(sec) {
  if (sec < 90) return 'just now';
  if (sec < 5400) return Math.round(sec / 60) + ' min ago';
  if (sec < 129600) return Math.round(sec / 3600) + ' h ago';
  return Math.round(sec / 86400) + ' d ago';
}

async function loadHeartbeats() {
  $('ribbons').textContent = 'loading …';
  try {
    const r = await (await fetch('/api/heartbeats')).json();
    if (!r.ok) { $('ribbons').textContent = r.error; return; }
    if (!r.phones.length) {
      $('ribbons').textContent = 'No provisioned phones with heartbeat data yet.';
      return;
    }
    $('ribbons').innerHTML = r.phones.map(p => {
      const cells = p.cells.map((c, i) => {
        const t = new Date((r.now - (r.hours - i) * 3600) * 1000);
        return `<i class="${c ? 'on' : ''}" title="${t.toLocaleString()}"></i>`;
      }).join('');
      const meta = p.last_seen
        ? `${p.uptime != null ? p.uptime + '% · ' : ''}${fmtAgo(r.now - p.last_seen)}`
        : 'no beats yet';
      return `<div class="ribbonrow">
        <span class="riblabel" title="${p.mac}">${p.name} (${p.ext || '?'})</span>
        <span class="ribbon">${cells}</span>
        <span class="ribmeta">${meta}</span></div>`;
    }).join('');
    $('hbHint').textContent = 'Each cell is one hour; green means the phone checked in. ' +
      'Phones poll hourly, so one grey cell can be timing jitter — look for patterns, not pixels.';
  } catch {
    $('ribbons').textContent = 'failed to load heartbeat data';
  }
}
$('hbRefresh').addEventListener('click', loadHeartbeats);
loadHeartbeats();

function showGoLive(details) {
  if (!details) return;
  $('announce').value =
    `${details.name}'s phone is now live! You can call them on ${details.ext}. \u{1F4DE}`;
  $('golive').style.display = 'block';
}
$('copyBtn').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('announce').value);
    $('copyHint').textContent = 'copied!';
  } catch {
    $('announce').select();
    document.execCommand('copy');
    $('copyHint').textContent = 'copied!';
  }
  setTimeout(() => $('copyHint').textContent = '', 2000);
});

$('dryrunBtn').addEventListener('click', async () => {
  disarm();
  const res = $('result');
  res.className = ''; res.style.display = 'block'; res.textContent = 'running checks ...';
  const r = await (await fetch('/api/dryrun', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: $('name').value, mac: $('mac').value, ext: $('ext').value})
  })).json();
  if (r.ok) {
    token = r.token;
    res.className = 'good';
    res.textContent = 'Dry run clean:\n  • ' + r.checks.join('\n  • ') +
      `\n\nWill create ${r.sip_user} (ext ${r.ext}) and upload ${r.key}. Nothing changed yet.`;
    $('provisionBtn').disabled = false;
    $('armHint').textContent = 'Armed. Provision will make real changes.';
  } else {
    res.className = 'err';
    res.textContent = r.error;
  }
});

$('provisionBtn').addEventListener('click', async () => {
  if (!token) return;
  if (!confirm('Provision for real? This creates the sub-account and uploads the config.')) return;
  $('provisionBtn').disabled = true;
  $('dryrunBtn').disabled = true;
  $('result').style.display = 'none';
  $('golive').style.display = 'none';
  const r = await (await fetch('/api/provision', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({token})
  })).json();
  if (!r.ok) { alert(r.error); $('dryrunBtn').disabled = false; return; }
  token = null;
  pollJob();
});

function pollJob() {
  if (polling) return;
  $('log').style.display = 'block';
  polling = setInterval(async () => {
    const j = await (await fetch('/api/job')).json();
    $('log').textContent = j.log.join('\n');
    $('log').scrollTop = $('log').scrollHeight;
    if (j.status !== 'running') {
      clearInterval(polling); polling = null;
      $('dryrunBtn').disabled = false;
      $('armHint').textContent = j.status === 'registered'
        ? 'Done - phone is live.' : 'Job finished - see log.';
      if (j.status === 'registered' || j.status === 'uploaded_only') showGoLive(j.details);
      loadState();
    }
  }, 2000);
}

loadState();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    flags = env_flags()
    missing = [k for k, v in flags.items() if not v]
    if missing:
        print(f"warning: not ready ({', '.join(missing)}) - did you "
              f"`source phoneclub.env` in this terminal?")
    print(f"Phone Club GUI: http://127.0.0.1:{PORT}  (Ctrl-C to stop)")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
