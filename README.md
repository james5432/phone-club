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

No surnames, no addresses, no call recordings. Call metadata (who called whom,
when) exists at the VoIP provider as with any phone service. One admin holds
the credentials; nobody else has access. When a family leaves the club, their
sub-account is retired and their handset config removed by hand.

## How it works

```
sign-up ──> add_member.py / gui.py ──> VoIP.ms sub-account   (identity + dial permissions)
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
2. Fanvil H2U-V2 handsets (or adjust the config for your model). Set
   "Update Mode" to "Update at Time Interval" on each phone once.
3. `cp phoneclub.env.example phoneclub.env`, fill it in, `chmod 600` it.
4. `python3 -m venv .venv && .venv/bin/pip install requests boto3 flask`
5. Export a working phone's config, build `phone.cfg.template` from it
   (see the docstring in `add_member.py`), and put your shared settings
   in `common.xml`.
6. `source phoneclub.env && .venv/bin/python gui.py` — then dry-run first,
   always.

## License

[MIT](LICENSE). Use it for your own village.
