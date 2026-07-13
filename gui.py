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
import threading
import time

from flask import Flask, jsonify, request

import add_member as am

PORT = 8765

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
<title>Phone Club provisioning</title>
<style>
  :root { --accent:#2563eb; --ok:#16a34a; --bad:#dc2626; --muted:#6b7280; }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,system-ui,sans-serif; margin:0; background:#f3f4f6; color:#111827; }
  header { background:#1e293b; color:#fff; padding:14px 22px; font-size:18px; font-weight:600; }
  header small { font-weight:400; color:#94a3b8; margin-left:10px; }
  main { max-width:840px; margin:22px auto 60px; padding:0 16px; display:grid; gap:16px; }
  .card { background:#fff; border-radius:10px; padding:16px 20px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
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
  .cardhead { display:flex; justify-content:space-between; align-items:baseline; }
  .linkbtn { font-size:12px; padding:3px 12px; border-radius:6px; border:1px solid #d1d5db; background:#fff; cursor:pointer; color:#374151; }
  #golive { display:none; margin-top:14px; padding:14px 16px; border-radius:8px; background:#eef2ff; border:1px solid #c7d2fe; }
  #golive h3 { margin:0 0 8px; font-size:14px; }
  #golive ol { margin:0 0 12px 18px; padding:0; font-size:14px; line-height:1.7; }
  #announce { width:100%; box-sizing:border-box; padding:9px 10px; border:1px solid #c7d2fe; border-radius:8px; font-size:14px; font-family:inherit; resize:vertical; min-height:52px; }
  #copyBtn { margin-top:8px; }
  form { display:grid; grid-template-columns:1fr 1fr 120px; gap:12px; }
  label { display:grid; gap:4px; font-size:13px; font-weight:600; color:#374151; }
  input { padding:9px 10px; border:1px solid #d1d5db; border-radius:8px; font-size:15px; }
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
<header>&#128222; Phone Club <small>handset provisioning</small></header>
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
    `<tr><td>${m.account}</td><td>${m.extension || '-'}</td><td>${m.description}</td>
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
