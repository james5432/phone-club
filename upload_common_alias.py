#!/usr/bin/env python3
"""Upload common.xml to R2 under the Fanvil CommonConfig filename.

Fanvil H2U-V2 phones request the shared config as F0V2UV200000.cfg (observed
in the Worker logs, 2026-07-18), not as common.xml. This uploads the current
common.xml content under that key so auto-provisioned phones actually receive
the shared settings (dial plan, instant 3-digit dialing, quiet hours, no IP
calls, timezone).

Add-only, like all provisioning tooling: refuses to overwrite an existing
object. If common.xml changes later, delete the old alias by hand in the R2
dashboard first, then re-run this.

Usage:  source phoneclub.env && .venv/bin/python upload_common_alias.py
"""

import os
import sys

import boto3
from botocore.exceptions import ClientError

FANVIL_COMMON_KEY = "F0V2UV200000.cfg"
COMMON_XML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "common.xml")


def main():
    endpoint = os.environ.get("R2_ENDPOINT")
    access = os.environ.get("R2_ACCESS_KEY_ID")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET", "phone-club-prov")
    if not all([endpoint, access, secret]):
        raise SystemExit("error: R2_* env vars not set (source phoneclub.env first)")

    with open(COMMON_XML, "rb") as f:
        body = f.read()

    s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access,
                      aws_secret_access_key=secret, region_name="auto")
    try:
        s3.head_object(Bucket=bucket, Key=FANVIL_COMMON_KEY)
        raise SystemExit(f"error: '{FANVIL_COMMON_KEY}' already exists in "
                         f"'{bucket}' - this tool never overwrites. If you are "
                         f"updating common.xml, delete the old object by hand "
                         f"in the R2 dashboard first.")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
            raise

    s3.put_object(Bucket=bucket, Key=FANVIL_COMMON_KEY, Body=body,
                  ContentType="text/plain")
    print(f"uploaded '{FANVIL_COMMON_KEY}' ({len(body)} bytes) to bucket '{bucket}'")
    print("reboot a phone (or wait for its next poll) to pick it up")


if __name__ == "__main__":
    main()
