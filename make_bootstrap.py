#!/usr/bin/env python3
"""Render bootstrap.cfg - the one-time seed config for factory-fresh handsets.

    source phoneclub.env && python make_bootstrap.py

Then, on each NEW phone: web UI > System > Configuration > Import
bootstrap.cfg, reboot, and the phone fetches its full config from the
provisioning server on its next poll.

Writes ./bootstrap.cfg, which contains the provisioning server's Basic Auth
credentials: it is gitignored (*.cfg) and chmod 600 - treat it like
phoneclub.env. This script prints no secret values.
"""

import os
import sys

TEMPLATE = "bootstrap.cfg.template"
OUT = "bootstrap.cfg"


def die(msg):
    raise SystemExit(f"error: {msg}")


def main():
    url = os.environ.get("PROV_SERVER_URL")
    user = os.environ.get("PROV_HTTP_USER")
    password = os.environ.get("PROV_HTTP_PASS")
    if not all([url, user, password]):
        die("PROV_SERVER_URL / PROV_HTTP_USER / PROV_HTTP_PASS not set "
            "(see phoneclub.env.example)")
    if not os.path.exists(TEMPLATE):
        die(f"template not found: {TEMPLATE}")

    with open(TEMPLATE, encoding="utf-8") as f:
        text = f.read()
    text = (text
            .replace("{{PROV_SERVER_URL}}", url)
            .replace("{{PROV_HTTP_USER}}", user)
            .replace("{{PROV_HTTP_PASS}}", password))
    if "{{" in text:
        die("unreplaced tokens remain in template")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(OUT, 0o600)
    print(f"wrote {OUT} ({len(text)} bytes, contains credentials - do not share).")
    print("Import it on the new phone: web UI > System > Configuration > "
          "Import, then reboot the phone.")


if __name__ == "__main__":
    main()
