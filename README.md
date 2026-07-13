# The Phone Club 📞

A tiny, closed telephone network for children in one village. Each child gets
a simple desk phone that can call **only the other club members** — nothing
else. No internet, no apps, no screens, no outside numbers in or out. Just a
handset, a short number, and their friends.

This repository contains the complete tooling and configuration that runs the
network. It is public on purpose: any parent (or a technically minded friend)
can read exactly what these phones can and cannot do.

> ## ⚠️ Geen 112 / No emergency calls
>
> **Deze telefoons kunnen GEEN 112 (of enig ander alarmnummer) bellen.**
> Het netwerk is volledig gesloten: alleen clubleden zijn bereikbaar.
> Zorg dat kinderen weten dat ze voor noodgevallen een gewone telefoon
> of mobiel moeten gebruiken.
>
> **These phones CANNOT call 112 (or any other emergency number).**
> The network is fully closed: only club members are reachable. Make sure
> children know to use a regular phone or mobile in an emergency.

## What the phones can do

- Call other club members by dialling a three-digit number (101–199 for
  children, 200 for the parent line).
- Ring, be answered, be hung up. That's the whole feature list.

## What the phones cannot do

- **No outside calls**, in either direction — the numbers only exist inside
  the club's private VoIP account. There is nothing to prank-call and no way
  for strangers to ring a child.
- **No internet access** for the user: the handset uses the home network only
  to reach the phone server, and it cannot browse, message, or run apps.
- **No calls during quiet hours** — every phone automatically enforces
  Do Not Disturb from 21:00 to 08:00.
- **No IP dialling, no international dialling** — both are switched off in
  configuration, and international calling is additionally locked on the
  provider side per account.

## What data exists about each child

For each member the system stores exactly three things:

| Data | Where it lives | Why |
|---|---|---|
| First name | VoIP.ms (EU server) | forms the account name, e.g. `<account>_Femke` |
| Extension number | VoIP.ms | the number friends dial |
| Phone's MAC address | Cloudflare R2 | so the handset fetches its own config |

No surnames, no addresses, no call recordings. Call metadata is retained by
the VoIP provider as with any phone service: per-call records of caller,
callee, time, duration, and technical details including the handset's IP
address. One admin — and nobody else — has access to the VoIP.ms account,
which is protected by two-factor authentication. When a family leaves the
club, their sub-account is retired and their handset config removed by hand.

## How it works

```
sign-up (Baserow) ──> add_member.py / gui.py ──> VoIP.ms sub-account   (identity + dial permissions)
                       │
                       └────────────> Cloudflare R2 + Worker (phone config, HTTP Basic Auth)
                                            │
                       Fanvil H2U-V2 phone ─┘  (fetches config on a timer, then registers)
```

- Each handset is a Fanvil H2U-V2 hotel-style phone. On boot (and on a timer)
  it fetches two config files from a password-protected provisioning server:
  [`common.xml`](common.xml) — identical for every phone: quiet hours, the
  three-digit dial plan, IP-calling off, timezone — and a per-phone file with
  its own account and a randomly generated 95-bit SIP password.
- [`add_member.py`](add_member.py) provisions a new handset end to end and is
  deliberately **add-only**: it refuses to overwrite an existing phone's
  config, never deletes anything, and never prints passwords.
- [`gui.py`](gui.py) is a localhost web front-end for the same code, with a
  dry-run-first workflow: the real Provision button stays locked until a
  dry run of the exact same details has passed.

## Security & privacy

**Transport security.** Provisioning traffic (handset ↔ config server) runs
over HTTPS with club credentials, so SIP passwords never cross the internet
in the clear. The calls themselves, however, use plain SIP/RTP **without
encryption** — like classic telephony, call audio between a handset and the
VoIP server is in principle readable by operators of the network path. We
state this openly rather than imply otherwise; for this threat model
(children calling club friends) we judged it acceptable, and SIP-TLS/SRTP
hardening is on the wish list.

**Passwords.** Each phone gets its own randomly generated 95-bit SIP password
(see `make_password()` in [`add_member.py`](add_member.py)). It exists in
exactly two places — the VoIP provider and that phone's own config file — and
the tooling never prints, logs, or displays it.

**Sign-ups and membership administration** are powered by
[Baserow](https://baserow.io), a Dutch tool whose cloud infrastructure is
hosted in Germany, within the EU — chosen for GDPR compliance. Parents'
contact details live there — not in this repository and not on the phone
network.

**AVG/GDPR.**

- *Legal basis*: a parent signs up their own child — consent is explicit and
  given at sign-up, and can be withdrawn by leaving the club.
- *Data minimization*: the phone network itself stores only the three items
  in the table above.
- *Processors*: VoIP.ms (Canadian provider; our account uses an EU-located
  server), Cloudflare (config hosting), Baserow (membership, German servers).
- *Your rights*: any parent can ask the admin exactly what is stored about
  their child, and have it deleted. When a family leaves, the sub-account and
  handset config are removed.
- *Call metadata* is retained by the VoIP provider as with any telephone
  service: per-call records of caller, callee, time, duration, and technical
  details including the handset's IP address (visible to the admin in the
  portal under Call Detail Records). No call content is or can be recorded
  by the club.

**Trust model & auditing.** The club is run by one admin, who holds the
provider credentials — that is the honest trust boundary, and we prefer
stating it to hiding it. Only that admin has access to the VoIP.ms account,
and the account is protected with two-factor authentication. The guardrails
around it:

- The provisioning tooling is public and **add-only**: it is incapable of
  deleting or overwriting anything, so scripted mistakes can't take the
  network down or hijack an existing phone.
- Every configuration and tooling change is visible in this repository's
  public git history.
- Destructive actions (retiring a member) are deliberately manual, done by
  hand in the provider portals.
- There is currently **no tamper-proof log of admin actions** — call detail
  records exist at the VoIP provider, but portal actions are logged only by
  the providers themselves. A public provisioning log (append-only through
  git history) is a planned improvement; security-minded parents are warmly
  invited to review this repo and suggest more.

## Verifying our claims

Every claim above is checkable in this repo: the dial plan and quiet hours
are in [`common.xml`](common.xml), the add-only and no-password-printing
guarantees in [`add_member.py`](add_member.py), and the per-phone template in
[`phone.cfg.template`](phone.cfg.template). What's *not* in the repo:
credentials, SIP passwords, children's data, and our club-specific account
identifiers (see `phoneclub.env.example` for the shape of those).

## Running your own

The tooling is generic — any group could run their own club:

1. A VoIP.ms account (or adapt `voipms()` for another provider with
   sub-accounts and internal extensions), a Cloudflare account with an R2
   bucket, and a Basic-Auth Worker in front of it for provisioning.
2. Fanvil H2U-V2 handsets (or adjust the config for your model). For each
   new phone, render the seed config (`make_bootstrap.py`) and import it
   once via the phone's web UI — it sets the provisioning server, its
   credentials, and the update mode in one step.
3. `cp phoneclub.env.example phoneclub.env`, fill it in, `chmod 600` it.
4. `python3 -m venv .venv && .venv/bin/pip install requests boto3 flask`
5. Export a working phone's config, build `phone.cfg.template` from it
   (see the docstring in `add_member.py`), and put your shared settings
   in `common.xml`.
6. `source phoneclub.env && .venv/bin/python gui.py` — then dry-run first,
   always.

## License

[MIT](LICENSE). Use it for your own village.

---

**Babbel en Bel Club** — een kleinschalig community-project beheerd door een
ouder uit Austerlitz.

[neem contact op met de beheerder](mailto:babbelenbel@gmail.com) ·
[privacybeleid](https://docs.google.com/document/d/1TRrpMPhnbv9TUyjCaIjnKierF3-NXXew8WhN52PkazE/edit)
