#!/usr/bin/env python3
"""Render the one-time seed config for factory-fresh handsets.

    source phoneclub.env && python make_bootstrap.py                 # Fanvil
    source phoneclub.env && python make_bootstrap.py --device ht801  # Grandstream

Fanvil: writes ./bootstrap.cfg. On each NEW phone: web UI > System >
Configuration > Import bootstrap.cfg, reboot, and the phone fetches its full
config from the provisioning server on its next poll.

Grandstream HT801: writes ./ht801-bootstrap.xml, using the Worker's second
login (PROV_HTTP_USER_2 / PROV_HTTP_PASS_2; the HT801 accepts at most 40
characters). On each NEW adapter: web UI > Maintenance > Upgrade > Config
File > upload ht801-bootstrap.xml, then reboot.

Both outputs contain the provisioning server's Basic Auth credentials: they
are gitignored and chmod 600 - treat them like phoneclub.env. This script
prints no secret values.
"""

import argparse
import os

DEVICES = {
    "fanvil": {
        "template": "bootstrap.cfg.template",
        "out": "bootstrap.cfg",
        "user_var": "PROV_HTTP_USER",
        "pass_var": "PROV_HTTP_PASS",
        "how": "web UI > System > Configuration > Import, then reboot the phone.",
    },
    "ht801": {
        "template": "ht801-bootstrap.xml.template",
        "out": "ht801-bootstrap.xml",
        "user_var": "PROV_HTTP_USER_2",
        "pass_var": "PROV_HTTP_PASS_2",
        "how": "web UI > Maintenance > Upgrade > Config File > upload, then reboot.",
    },
}
HT801_MAX_PASS = 40   # the HT801's config server password field refuses more


def die(msg):
    raise SystemExit(f"error: {msg}")


def main():
    ap = argparse.ArgumentParser(description="Render a handset seed config.")
    ap.add_argument("--device", choices=sorted(DEVICES), default="fanvil",
                    help="handset type (default: fanvil)")
    args = ap.parse_args()
    dev = DEVICES[args.device]

    url = os.environ.get("PROV_SERVER_URL")
    user = os.environ.get(dev["user_var"])
    password = os.environ.get(dev["pass_var"])
    if not all([url, user, password]):
        die(f"PROV_SERVER_URL / {dev['user_var']} / {dev['pass_var']} not set "
            "(see phoneclub.env.example)")
    if args.device == "ht801" and len(password) > HT801_MAX_PASS:
        die(f"{dev['pass_var']} is longer than {HT801_MAX_PASS} characters; "
            "the HT801 will not accept it")
    if not os.path.exists(dev["template"]):
        die(f"template not found: {dev['template']}")

    # The HT801 wants a bare host name: no scheme, no trailing slash.
    host = url.split("://", 1)[-1].rstrip("/")

    with open(dev["template"], encoding="utf-8") as f:
        text = f.read()
    text = (text
            .replace("{{PROV_SERVER_URL}}", url)
            .replace("{{PROV_HOST}}", host)
            .replace("{{PROV_HTTP_USER}}", user)
            .replace("{{PROV_HTTP_PASS}}", password))
    if "{{" in text:
        die("unreplaced tokens remain in template")

    out = dev["out"]
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(out, 0o600)
    print(f"wrote {out} ({len(text)} bytes, contains credentials - do not share).")
    print(f"Load it on the new {args.device}: {dev['how']}")


if __name__ == "__main__":
    main()
