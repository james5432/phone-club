#!/usr/bin/env python3
"""
add_member.py - provision one Phone Club handset end to end.

  python add_member.py --name Femke --mac 0C383E1A2B3C
  python add_member.py --name Femke --mac 0C383E1A2B3C --dry-run

Steps performed:
  1. Find the next free extension in the kid pool (101-199).
  2. Generate a SIP password.
  3. Render the per-MAC .cfg from your template (validated before anything
     is created remotely) and check <mac>.cfg is not already in R2.
  4. Create the VoIP.ms sub-account (<account>_<name>) with that extension.
  5. Upload the .cfg to the R2 bucket (key = <mac>.cfg). Never overwrites.
  6. Poll VoIP.ms until the phone registers, then print a summary.

Secrets come from environment variables so nothing sensitive lives in this file.
They live in phoneclub.env (gitignored); run `source phoneclub.env` first:

  export VOIPMS_API_USER="you@example.com"     # VoIP.ms portal login (API user)
  export VOIPMS_API_PASS="your-api-password"   # set under Main Menu > SOAP/REST API
  export R2_ENDPOINT="https://<accountid>.r2.cloudflarestorage.com"
  export R2_ACCESS_KEY_ID="..."                # R2 API token access key
  export R2_SECRET_ACCESS_KEY="..."            # R2 API token secret
  export R2_BUCKET="phone-club-prov"
  export PHONE_CFG_TEMPLATE="phone.cfg.template"
"""

import argparse
import os
import re
import secrets
import string
import sys
import time

import requests
import boto3
from botocore.exceptions import ClientError

# --- fixed project settings ---------------------------------------------------
VOIPMS_API_URL = "https://voip.ms/api/v1/rest.php"
EXT_POOL       = [str(n) for n in range(101, 200)]   # child extensions
REG_TIMEOUT_S  = 150                                 # how long to wait for registration

# --- instance settings + secrets from environment -----------------------------
# Account number and server are not secrets, but they are club-specific: they
# live in phoneclub.env so this repo stays generic (and publishable).
VOIPMS_ACCOUNT  = os.environ.get("VOIPMS_ACCOUNT")   # main account number
VOIPMS_SERVER   = os.environ.get("VOIPMS_SERVER")    # e.g. your VoIP.ms POP, e.g. amsterdam.voip.ms
VOIPMS_API_USER = os.environ.get("VOIPMS_API_USER")
VOIPMS_API_PASS = os.environ.get("VOIPMS_API_PASS")
R2_ENDPOINT     = os.environ.get("R2_ENDPOINT")
R2_ACCESS_KEY   = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_KEY   = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET       = os.environ.get("R2_BUCKET", "phone-club-prov")
TEMPLATE_PATH   = os.environ.get("PHONE_CFG_TEMPLATE", "phone.cfg.template")


def die(msg):
    # SystemExit with a string prints it to stderr and exits 1 when uncaught,
    # and lets gui.py catch the same failures and show them in the browser.
    raise SystemExit(f"error: {msg}")


# --- VoIP.ms API --------------------------------------------------------------
def voipms(method, **params):
    """Call a VoIP.ms REST method and return the parsed JSON, raising on failure."""
    query = {
        "api_username": VOIPMS_API_USER,
        "api_password": VOIPMS_API_PASS,
        "method": method,
        "content_type": "json",
        **params,
    }
    # NOTE: must be GET - the VoIP.ms REST endpoint returns HTTP 500 for
    # form-POST bodies (verified 2026-07). Credentials ride the query string;
    # TLS covers them in transit.
    try:
        resp = requests.get(VOIPMS_API_URL, params=query, timeout=30)
    except requests.exceptions.RequestException as e:
        # sanitize: requests exceptions can embed the full request URL,
        # query-string credentials included
        raise RuntimeError(f"VoIP.ms {method}: {type(e).__name__} "
                           f"(network problem)") from None
    if resp.status_code != 200:
        # not raise_for_status(): its message embeds the full request URL,
        # query-string credentials included
        raise RuntimeError(f"VoIP.ms {method} returned HTTP {resp.status_code}")
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"VoIP.ms {method} returned: {data.get('status')}")
    return data


def used_extensions():
    """Return the set of internal extensions already assigned to sub-accounts."""
    data = voipms("getSubAccounts")
    return {str(a.get("internal_extension", "")).strip() for a in data.get("accounts", [])}


def next_extension(used):
    """Return the first extension in EXT_POOL not present in `used`."""
    for ext in EXT_POOL:
        if ext not in used:
            return ext
    die("no free extensions left in the pool (101-199)")


def validate_name(raw):
    """Return the cleaned member name, or die if it can't be a sub-account username."""
    name = raw.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name):
        die(f"name must be letters/digits only (it becomes the sub-account "
            f"username), got: {raw!r}")
    return name


def validate_extension(raw, used):
    """Return the cleaned extension, or die if it's malformed or taken."""
    ext = raw.strip()
    if not re.fullmatch(r"[12][0-9][0-9]", ext):
        die(f"extension must match the dial plan (101-299), got: {raw!r}")
    if ext in used:
        die(f"extension {ext} is already assigned to a sub-account")
    return ext


def make_password(length=16):
    alphabet = string.ascii_letters + string.digits   # no symbols: safe in XML and SIP
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_subaccount(name, extension, password):
    """
    Create sub-account <account>_<name> with the given internal extension.

    NOTE: createSubAccount's required fields occasionally change. Before first real
    run, cross-check this dict against https://voip.ms/m/apidocs.php and adjust.
    Run with --dry-run first so nothing is created until you've confirmed it.
    """
    params = {
        "username":            name,          # becomes <account>_<name>
        "password":            password,
        "auth_type":           "1",           # 1 = username/password
        "device_type":         "2",           # generic IP phone
        "protocol":            "1",           # 1 = SIP
        "description":         f"Phone Club - {name}",
        "internal_extension":  extension,
        "internal_voicemail":  "0",
        "internal_dialtime":   "20",
        "internal_ringtime":   "15",
        "lock_international":   "1",           # block international dialling
        "international_route":  "1",
        "music_on_hold":       "default",
        "allowed_codecs":      "ulaw;g722",
        "dtmf_mode":           "AUTO",
        "nat":                 "yes",
    }
    voipms("createSubAccount", **params)
    return f"{VOIPMS_ACCOUNT}_{name}"


def registration_status(account):
    """Return True if the given full sub-account is currently registered."""
    try:
        data = voipms("getRegistrationStatus", account=account)
    except RuntimeError:
        return False
    if str(data.get("registered", "")).lower() == "yes":
        return True
    return bool(data.get("registrations"))


# --- config rendering ---------------------------------------------------------
def render_cfg(mac, sip_user, sip_pass, extension):
    """
    Fill the per-MAC config from your template.

    Build phone.cfg.template from a KNOWN-GOOD exported .cfg: take a working phone's
    config and replace the five values that differ per handset with these tokens:
        {{SIP_USER}}    -> full account, e.g. <account>_Femke
        {{SIP_PASS}}    -> SIP password
        {{SIP_SERVER}}  -> your VoIP.ms POP, e.g. amsterdam.voip.ms
        {{EXTENSION}}   -> 103
        {{MAC}}         -> the phone's MAC
    Everything shared (quiet hours, UseVPN=0, AllowIPCall=0, timezone) stays in
    common.xml, so this template only carries the account-specific line.
    """
    if not os.path.exists(TEMPLATE_PATH):
        die(f"template not found: {TEMPLATE_PATH} "
            f"(create it from a working .cfg, see render_cfg docstring)")
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()
    return (template
            .replace("{{SIP_USER}}",   sip_user)
            .replace("{{SIP_PASS}}",   sip_pass)
            .replace("{{SIP_SERVER}}", VOIPMS_SERVER)
            .replace("{{EXTENSION}}",  extension)
            .replace("{{MAC}}",        mac))


def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def ensure_key_free(s3, key):
    """Die if <mac>.cfg already exists: provisioning only ever adds, never overwrites."""
    try:
        s3.head_object(Bucket=R2_BUCKET, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return
        raise
    die(f"'{key}' already exists in bucket '{R2_BUCKET}'. Provisioning never "
        f"overwrites - if this handset is being re-issued, sort that out by hand "
        f"in the R2 dashboard first.")


def upload_to_r2(s3, key, cfg_text):
    """Upload the rendered config to R2 as <mac>.cfg (call ensure_key_free first)."""
    s3.put_object(Bucket=R2_BUCKET, Key=key,
                  Body=cfg_text.encode("utf-8"),
                  ContentType="text/plain")


# --- MAC helpers --------------------------------------------------------------
def normalise_mac(raw):
    hexonly = re.sub(r"[^0-9A-Fa-f]", "", raw)
    if len(hexonly) != 12:
        die(f"MAC must be 12 hex digits, got: {raw!r}")
    return hexonly.lower()


# --- main ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Provision one Phone Club handset.")
    ap.add_argument("--name", required=True, help="child's name, e.g. Femke")
    ap.add_argument("--mac",  required=True, help="phone MAC (any separators)")
    ap.add_argument("--ext",  help="force a specific extension instead of auto-picking")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would happen without creating or uploading anything")
    args = ap.parse_args()

    if not all([VOIPMS_ACCOUNT, VOIPMS_SERVER, VOIPMS_API_USER, VOIPMS_API_PASS]):
        die("VOIPMS_* env vars not set (see phoneclub.env.example)")
    have_r2 = all([R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY])
    if not args.dry_run and not have_r2:
        die("R2_* env vars not set (see header comment)")

    name = validate_name(args.name)
    mac = normalise_mac(args.mac)

    used = used_extensions()
    ext = validate_extension(args.ext, used) if args.ext else next_extension(used)

    sip_user = f"{VOIPMS_ACCOUNT}_{name}"
    password = make_password()

    print(f"  name       : {name}")
    print(f"  mac        : {mac}")
    print(f"  extension  : {ext}")
    print(f"  sip user   : {sip_user}")
    print(f"  server     : {VOIPMS_SERVER}")

    # Render first: this validates the template BEFORE anything is created
    # remotely, so a failure here can't leave a half-provisioned handset.
    cfg = render_cfg(mac, sip_user, password, ext)
    key = f"{mac}.cfg"   # confirm this matches what your Worker/Fanvil requests

    if args.dry_run:
        print(f"\n[dry-run] template renders OK ({len(cfg)} bytes).")
        if have_r2:
            ensure_key_free(r2_client(), key)
            print(f"[dry-run] '{key}' is free in bucket '{R2_BUCKET}'.")
        else:
            print("[dry-run] R2_* env vars not set, skipped checking whether "
                  f"'{key}' already exists.")
        print(f"[dry-run] would create sub-account {sip_user} and upload {key}. "
              "Nothing changed.")
        return

    # Check the R2 key is free before creating the sub-account, so a re-used
    # MAC aborts cleanly instead of leaving an orphan sub-account behind.
    s3 = r2_client()
    ensure_key_free(s3, key)

    print("\ncreating sub-account ...")
    account = create_subaccount(name, ext, password)

    print("uploading config ...")
    upload_to_r2(s3, key, cfg)
    print(f"  uploaded   : {key} -> {R2_BUCKET}")

    print("waiting for the phone to register (power it on and plug in ethernet) ...")
    deadline = time.time() + REG_TIMEOUT_S
    while time.time() < deadline:
        if registration_status(account):
            print(f"\n  {name}, ext {ext}, LIVE and registered.")
            print(f"  SIP password lives only in the uploaded {key} (not shown here).")
            return
        time.sleep(10)

    print(f"\n  not registered yet after {REG_TIMEOUT_S}s. The account and config are "
          f"in place - check power/ethernet, or that Update Mode is 'Update at "
          f"Time Interval'. It will pick up the config on its next poll.")
    print(f"  SIP password lives only in the uploaded {key} (not shown here).")


if __name__ == "__main__":
    main()
