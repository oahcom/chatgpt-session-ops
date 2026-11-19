# -*- coding: utf-8 -*-
"""extract_auth.py — 从本机调试浏览器（默认 Edge）提取 ChatGPT 凭据。

    python extract_auth.py                 # 端口 9222（CHATGPT_CDP_PORT 可覆盖）
    python extract_auth.py --port 9333

产出（都在脚本目录，落盘即 chmod 600）：
    cookies.json       仅 chatgpt.com / openai.com 的 cookie（含 HttpOnly）
    access_token.txt   /api/auth/session 返回的 accessToken

注意：只落 ChatGPT 域下的 cookie。旧版把浏览器里**所有站点**的 cookie 都写到
cookies.json，等于顺手备份了一遍全站登录态——没有必要，也不该做。
"""
import json
import os
import sys

_CDP_DIR = os.environ.get("CHATGPT_CDP_DIR") or os.path.join(
    os.path.expanduser("~"), ".workbuddy", "skills", "browser-automation-cdp", "scripts")
sys.path.insert(0, _CDP_DIR)
from cdp import CDPClient  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_JS = """(function(){
  try {
    var x = new XMLHttpRequest();
    x.open('GET', '/api/auth/session', false);   // 同步 XHR：evaluate 不 await promise
    x.send();
    var j = JSON.parse(x.responseText || '{}') || {};
    return j.accessToken || '';
  } catch (e) { return ''; }
})()"""


def _harden(path):
    if os.name != "posix":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def chatgpt_cookies(c):
    """取完整 cookie（含 HttpOnly），只保留 ChatGPT 域。"""
    cookies = []
    try:
        c.send("Network.enable", {})
        r = c.send("Network.getAllCookies", {})
        cookies = r.get("cookies") or r.get("result", {}).get("cookies") or []
    except Exception as e:
        print("Network.getAllCookies 失败（%s），改用 Storage.getCookies" % e, flush=True)
    if not cookies:
        try:
            r = c.send("Storage.getCookies", {})
            cookies = r.get("cookies") or []
        except Exception as e:
            print("Storage.getCookies 也失败：%s" % e, flush=True)
    out = []
    for ck in cookies:
        domain = (ck.get("domain") or "").lstrip(".").lower()
        if "chatgpt.com" in domain or "openai.com" in domain:
            out.append(ck)
    return out


def main():
    argv = sys.argv[1:]
    port = 9222
    if "--port" in argv:
        i = argv.index("--port")
        if i + 1 >= len(argv):
            print("usage: python extract_auth.py [--port 9222]", flush=True)
            return 2
        try:
            port = int(argv[i + 1])
        except ValueError:
            print("ERROR: --port 需要整数", flush=True)
            return 2
    else:
        port = int(os.environ.get("CHATGPT_CDP_PORT") or 9222)

    c = CDPClient(host="127.0.0.1", port=port, timeout=30)
    try:
        c.ensure_page(url_contains="chatgpt.com")
    except Exception as e:
        print("ERROR: CDP 端口 %d 不可用：%s" % (port, e), flush=True)
        print("提示：先启动已登录的调试浏览器（python start_chrome.py --port %d）" % port, flush=True)
        return 2

    cookies = chatgpt_cookies(c)
    print("chatgpt/openai cookies:", len(cookies))
    names = sorted({ck.get("name") for ck in cookies if ck.get("name")})
    print("names:", ", ".join(names))
    if not cookies:
        print("WARN: 没拿到任何 ChatGPT cookie：确认这个浏览器已登录 chatgpt.com", flush=True)

    out = os.path.join(BASE, "cookies.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    _harden(out)
    print("saved", out)

    token = ""
    try:
        token = (c.evaluate(TOKEN_JS).get("value") or "").strip()
    except Exception as e:
        print("WARN: 取 accessToken 失败：%s" % e, flush=True)
    if token:
        tp = os.path.join(BASE, "access_token.txt")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(token)
        _harden(tp)
        print("saved %s (%d chars)" % (tp, len(token)))
    else:
        print("WARN: /api/auth/session 没返回 accessToken（可能未登录或页面不在 chatgpt.com）", flush=True)
        print("      cookies.json 已写好，但 API 直连仍需要 access_token.txt", flush=True)

    header = "; ".join("%s=%s" % (ck.get("name"), ck.get("value", "")) for ck in cookies)
    print("header length:", len(header))
    return 0 if cookies else 1


if __name__ == "__main__":
    sys.exit(main())
