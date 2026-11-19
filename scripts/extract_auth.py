# -*- coding: utf-8 -*-
"""Extract cookies from Edge via CDP, call ChatGPT backend API directly."""
import json
import os
import sys

_CDP_DIR = os.environ.get("CHATGPT_CDP_DIR") or os.path.join(
    os.path.expanduser("~"), ".workbuddy", "skills", "browser-automation-cdp", "scripts")
sys.path.insert(0, _CDP_DIR)
from cdp import CDPClient

BASE = os.path.dirname(os.path.abspath(__file__))


def main():
    c = CDPClient(host="127.0.0.1", port=9222, timeout=30)
    c.ensure_page(url_contains="chatgpt.com")

    # get all cookies including httpOnly via Network domain
    c.send("Network.enable", {})
    r = c.send("Network.getAllCookies", {})
    cookies = r.get("cookies") or r.get("result", {}).get("cookies") or []
    print("total cookies:", len(cookies))

    # also try Browser.getCookies
    if not cookies:
        try:
            r2 = c.send("Browser.getCookies", {})
            cookies = r2.get("cookies") or []
            print("browser cookies:", len(cookies))
        except Exception as e:
            print("Browser.getCookies err", e)

    interesting = [ck for ck in cookies if any(k in ck.get("name", "").lower() for k in
                   ("session", "cf_", "oai", "_secure", "chatgpt", "intercom", "user"))]
    print("interesting cookie names:", [ck.get("name") for ck in interesting])

    out = os.path.join(BASE, "cookies.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    if os.name == "posix":  # 凭据落盘收权限，别让同机其他用户直接读走
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
    print("saved", out)

    # build cookie header for chatgpt.com
    chat_cookies = [ck for ck in cookies if "chatgpt.com" in (ck.get("domain") or "") or "openai.com" in (ck.get("domain") or "")]
    header = "; ".join("%s=%s" % (ck["name"], ck["value"]) for ck in chat_cookies)
    print("chatgpt cookie count:", len(chat_cookies))
    print("header length:", len(header))
    return 0


if __name__ == "__main__":
    sys.exit(main())