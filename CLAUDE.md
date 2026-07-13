# The Phone Club — provisioning

A small closed VoIP network that lets children in one Dutch village call only
other approved club members. No internet, apps, or outside numbers. Run by one
admin. This repo holds the tooling to add a new handset end to end.

## Key facts

- VoIP provider: VoIP.ms. The main account number, server, and Worker URL are
  club-specific and live in `phoneclub.env` (`VOIPMS_ACCOUNT`, `VOIPMS_SERVER`)
  — this repo is public, so keep instance identifiers out of it.
- Sub-account naming: `<account>_<Name>`. Child extensions 101–199, parent 200.
- Dial plan: `^[12][0-9][0-9]$`.
- Provisioning: Cloudflare R2 bucket + Worker (HTTP Basic Auth).
- Config split: shared `common.xml` + one per-MAC `.cfg` (object key = `<mac>.cfg`).
- Phones: Fanvil H2U-V2. "Update Mode" must be set to "Update at Time Interval"
  or auto-provisioning never fires.
- `common.xml` holds quiet hours (DND 21:00–08:00), `UseVPN=0`, `AllowIPCall=0`,
  timezone UTC+1.

## Secrets — never in this repo

VoIP.ms API and Cloudflare R2 credentials live ONLY in environment variables
(see the header of `add_member.py`). Before launching Claude Code, run
`source phoneclub.env` in the same terminal so scripts inherit the variables.
Never read, print, commit, or paste these values.

## How to add a handset

1. Set up the environment once:
   `python3 -m venv .venv && source .venv/bin/activate && pip install requests boto3`
2. Make sure `phone.cfg.template` exists — built from a known-good exported `.cfg`
   with the five per-phone values replaced by `{{SIP_USER}}`, `{{SIP_PASS}}`,
   `{{SIP_SERVER}}`, `{{EXTENSION}}`, `{{MAC}}`.
3. Factory-fresh phone? Render the seed config once (`python make_bootstrap.py`,
   needs PROV_* env vars) and import `bootstrap.cfg` via the phone's web UI
   (System → Configuration → Import), then reboot it. This sets the
   provisioning server, Basic Auth, and Update Mode in one go.
4. Dry run first: `python add_member.py --name <Name> --mac <MAC> --dry-run`.
5. Only after a clean dry run, run it for real (drop `--dry-run`).

## Rules — do NOT

- Do not run the real (non-dry-run) provisioning without my explicit go-ahead.
- Do not delete or deprovision sub-accounts, and never delete or overwrite R2
  objects. Provisioning only ever adds.
- Do not print or commit SIP passwords, API keys, or R2 keys.
- Do not widen international dialling or change the quiet-hours window without asking.
- 112 emergency calling does NOT work on this network. Never imply it does in any
  parent-facing text.
- A rendered `.cfg` contains a SIP password — treat all `*.cfg` files as sensitive.

## Files

- `add_member.py` — provision one handset: pick extension → render `.cfg` and
  check the R2 key is free → create sub-account → upload → poll until registered.
- `gui.py` — localhost web GUI wrapping add_member.py (run
  `source phoneclub.env && .venv/bin/python gui.py`, open
  http://127.0.0.1:8765). Provision button only arms after a clean dry run
  of the same details; never shows SIP passwords; needs `pip install flask`.
- `phone.cfg.template` — per-MAC template with the `{{...}}` tokens above.
- `bootstrap.cfg.template` + `make_bootstrap.py` — render `bootstrap.cfg`, the
  one-time seed config imported into each new phone's web UI (provisioning
  server + Basic Auth + Update Mode). The rendered file holds the Worker's
  Basic Auth credentials (gitignored via `*.cfg`). FlashMode/FlashProtocol
  enum values still need verifying against a hand-configured phone's export.
- `common.xml` — shared Fanvil config pushed to every phone.
- `phoneclub.env` — environment variables (gitignored; never read into context).
- `default_user_config.xml-2.cfg` / `-4.cfg` — raw phone exports from two
  members' working handsets (sources of common.xml's call behaviour and of
  `phone.cfg.template` respectively). Fanvil omits passwords on export, so
  they hold no SIP secrets, but they contain member names and LAN details:
  gitignored via `*.cfg`, never commit them.
- `phoneclub.env.example` — committed template for `phoneclub.env`.
- `README.md`, `LICENSE` — public-facing; README is parent-facing text
  (the 112 rule applies to it).
