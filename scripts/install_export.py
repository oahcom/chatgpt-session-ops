#!/usr/bin/env python3
"""Install a ChatGPT export JSON directly into an account slot.

The export file format (from the browser console export):
    {"token": "eyJ...", "cookies": [{"name":..., "value":..., "domain":...}, ...]}

This script converts and installs it to the layout chatgpt_send.py expects:
    accounts/<acctN>/cookies.json       bare cookie array
    accounts/<acctN>/access_token.txt   single line, NO trailing newline

Usage:
    python install_export.py <export.json> <acctN>

Rules enforced (accounts/README.md):
  - Identity is judged by JWT sub, never by file name. The decoded sub is
    printed so it can be compared with existing slots before reuse.
  - Only chatgpt.com / openai.com cookie domains are kept; others are dropped.
  - Existing slot files are backed up to cred-backup-<ts>/ before overwrite.
"""
import base64
import json
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ALLOWED_DOMAINS = ("chatgpt.com", "openai.com")


def die(msg):
    print("ERROR: " + msg, flush=True)
    sys.exit(2)


def decode_sub(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        d = json.loads(base64.urlsafe_b64decode(payload))
        return d.get("sub", ""), d.get("exp")
    except Exception:
        return "", None


def domain_allowed(d):
    d = (d or "").strip().lower().lstrip(".")
    return any(d == b or d.endswith("." + b) for b in ALLOWED_DOMAINS)


def main():
    if len(sys.argv) != 3:
        die("usage: python install_export.py <export.json> <acctN>")
    src, acct = sys.argv[1], sys.argv[2]
    if acct != "acct1" and not (acct.startswith("acct") and acct[4:].isdigit()):
        die("--account must be acct1, acct2, ... (acctN)")
    if not os.path.isfile(src):
        die("export file not found: " + src)

    data = json.load(open(src, encoding="utf-8"))
    token = (data.get("token") or data.get("accessToken") or "").strip()
    cookies = data.get("cookies") or []

    # --- self checks before touching disk ---
    if token.count(".") != 2:
        die("token is not a three-part JWT")
    sub, exp = decode_sub(token)
    if not sub:
        die("cannot decode sub from token")
    if not isinstance(cookies, list) or not cookies:
        die("cookies must be a non-empty array")
    kept, dropped = [], []
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name") or "value" not in c:
            die("cookie entry missing name/value: " + repr(c)[:80])
        if not c.get("domain"):
            die("cookie entry missing domain: " + str(c.get("name")))
        (kept if domain_allowed(c.get("domain")) else dropped).append(c)
    if not kept:
        die("all cookies were non-OpenAI domains; nothing to install")

    # --- slot path (acct1 shares the script root) ---
    outdir = BASE if acct == "acct1" else os.path.join(BASE, "accounts", acct)
    os.makedirs(outdir, exist_ok=True)

    # --- backup what is being replaced ---
    existing = [f for f in ("cookies.json", "access_token.txt")
                if os.path.exists(os.path.join(outdir, f))]
    if existing:
        bak = os.path.join(outdir, "cred-backup-" + time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(bak, exist_ok=True)
        for f in existing:
            shutil.copy2(os.path.join(outdir, f), os.path.join(bak, f))
        print("backup -> " + bak)

    # --- write in the exact layout the runtime expects ---
    cj_path = os.path.join(outdir, "cookies.json")
    at_path = os.path.join(outdir, "access_token.txt")
    with open(cj_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(kept, fh, ensure_ascii=False)
        fh.write("\n")
    with open(at_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(token)  # single line, no trailing newline

    print("installed -> " + outdir)
    print("identity sub=%s exp=%s" % (sub, exp))
    print("cookies kept=%d dropped_non_openai=%d" % (len(kept), len(dropped)))
    if dropped:
        for c in dropped[:5]:
            print("  dropped: %s domain=%s" % (c.get("name"), c.get("domain")))
    print("NEXT: verify with --new + PROBE_OK before trusting this slot")


if __name__ == "__main__":
    main()
