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
class VoipmsTimeout(RuntimeError):
    """A VoIP.ms call timed out. The request may still have been processed
    server-side - callers that create things should re-check before failing."""


def voipms(method, timeout=30, **params):
    """Call a VoIP.ms REST method and return the parsed JSON, raising on failure.

    Write calls should pass a longer `timeout`: VoIP.ms is sometimes slow to
    answer creates/updates even when they succeed (observed 2026-07: a
    createSubAccount took >30s to answer but was processed).
    """
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
        resp = requests.get(VOIPMS_API_URL, params=query, timeout=timeout)
    except requests.exceptions.Timeout as e:
        # distinct type so callers can tell "gave up waiting" (request may
        # still have landed) from a hard network failure
        raise VoipmsTimeout(f"VoIP.ms {method}: {type(e).__name__} "
                            f"(network problem)") from None
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


def get_subaccounts():
    """Return the list of sub-account dicts from VoIP.ms."""
    return voipms("getSubAccounts").get("accounts", [])


def used_extensions(accounts=None):
    """Return the set of internal extensions already assigned to sub-accounts."""
    if accounts is None:
        accounts = get_subaccounts()
    return {str(a.get("internal_extension", "")).strip() for a in accounts}


def find_subaccount(accounts, sip_user):
    """Return the sub-account dict whose full name matches sip_user, or None."""
    for a in accounts:
        if str(a.get("account", "")).lower() == sip_user.lower():
            return a
    return None


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


def _subaccount_params(name, extension, password):
    """
    Field set shared by createSubAccount and setSubAccount.

    NOTE: the required fields occasionally change. Before first real run,
    cross-check this dict against https://voip.ms/m/apidocs.php and adjust.
    Run with --dry-run first so nothing is created until you've confirmed it.
    """
    return {
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


def create_subaccount(name, extension, password):
    """Create sub-account <account>_<name> with the given internal extension.

    Recovers from a read timeout: VoIP.ms sometimes processes the create but
    answers slowly, so on timeout we re-query before declaring failure.
    """
    sip_user = f"{VOIPMS_ACCOUNT}_{name}"
    params = {"username": name, **_subaccount_params(name, extension, password)}
    try:
        voipms("createSubAccount", timeout=90, **params)
    except VoipmsTimeout:
        print("  createSubAccount timed out - checking whether the sub-account "
              "was created anyway ...")
        time.sleep(10)
        if find_subaccount(get_subaccounts(), sip_user) is None:
            raise
        print(f"  createSubAccount timed out, but {sip_user} exists on "
              f"VoIP.ms - continuing.")
    return sip_user


def reset_subaccount(existing, name, extension, password):
    """Point an existing (orphaned) sub-account at this run's SIP password.

    Used when an earlier run created the sub-account but died before uploading
    the phone config: that run's password is lost (VoIP.ms never returns
    passwords), so we set a fresh one to keep the rendered .cfg in sync.
    Only call this after checking that no uploaded .cfg references the
    sub-account (see cfg_referencing_user).
    """
    sub_id = existing.get("id")
    if not sub_id:
        die(f"cannot re-use {VOIPMS_ACCOUNT}_{name}: getSubAccounts returned no id")
    voipms("setSubAccount", timeout=90, id=sub_id,
           **_subaccount_params(name, extension, password))


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
    # Guard against a misconfigured bucket name first: HeadObject returns 404
    # for a MISSING BUCKET too, which would make the free-key check pass
    # vacuously and the later upload fail after the sub-account was created
    # (observed 2026-07-18 with a stale R2_BUCKET value).
    try:
        s3.head_bucket(Bucket=R2_BUCKET)
    except ClientError:
        die(f"R2 bucket '{R2_BUCKET}' not found or not accessible - "
            f"check R2_BUCKET in phoneclub.env")
    try:
        s3.head_object(Bucket=R2_BUCKET, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return
        raise
    die(f"'{key}' already exists in bucket '{R2_BUCKET}'. Provisioning never "
        f"overwrites - if this handset is being re-issued, sort that out by hand "
        f"in the R2 dashboard first.")


def cfg_referencing_user(s3, sip_user):
    """Return the key of the first .cfg in the bucket mentioning sip_user, else None.

    Guard for the sub-account re-use path: if any uploaded phone config
    references the sub-account, it belongs to a (possibly working) handset and
    we must not reset its password. Read-only.
    """
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".cfg"):
                continue
            body = s3.get_object(Bucket=R2_BUCKET, Key=key)["Body"].read()
            if sip_user.encode() in body:
                return key
    return None


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
    sip_user = f"{VOIPMS_ACCOUNT}_{name}"

    accounts = get_subaccounts()
    used = used_extensions(accounts)
    existing = find_subaccount(accounts, sip_user)

    if existing:
        # Likely an orphan from an earlier run that died between creating the
        # sub-account and uploading the config. Adopt its extension; whether
        # it's safe to re-use is checked further down, before any writes.
        ext = str(existing.get("internal_extension", "")).strip()
        if not ext:
            die(f"sub-account {sip_user} already exists but has no internal "
                f"extension - fix it by hand in the VoIP.ms portal")
        if args.ext and args.ext.strip() != ext:
            die(f"sub-account {sip_user} already exists with extension {ext}; "
                f"--ext {args.ext} conflicts - drop --ext to re-use it")
    else:
        ext = validate_extension(args.ext, used) if args.ext else next_extension(used)

    password = make_password()

    print(f"  name       : {name}")
    print(f"  mac        : {mac}")
    print(f"  extension  : {ext}")
    print(f"  sip user   : {sip_user}")
    print(f"  server     : {VOIPMS_SERVER}")
    if existing:
        print(f"  note       : sub-account already exists on VoIP.ms - will re-use it")

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
        if existing:
            print(f"[dry-run] would re-use existing sub-account {sip_user} "
                  f"(resetting its SIP password) and upload {key}. Nothing changed.")
        else:
            print(f"[dry-run] would create sub-account {sip_user} and upload {key}. "
                  "Nothing changed.")
        return

    # Check the R2 key is free before creating the sub-account, so a re-used
    # MAC aborts cleanly instead of leaving an orphan sub-account behind.
    s3 = r2_client()
    ensure_key_free(s3, key)

    if existing:
        # Safe to re-use only if no uploaded phone config references the
        # sub-account - otherwise it may belong to a working handset, and
        # resetting its password would break that phone.
        ref = cfg_referencing_user(s3, sip_user)
        if ref:
            die(f"sub-account {sip_user} already exists and '{ref}' in bucket "
                f"'{R2_BUCKET}' references it - that looks like a live handset. "
                f"Re-issuing a phone is a manual job, sort it out in the "
                f"portals first.")
        print(f"\nsub-account {sip_user} already exists (probably an earlier "
              f"timed-out run) - re-using it and resetting its SIP password ...")
        reset_subaccount(existing, name, ext, password)
        account = sip_user
    else:
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
