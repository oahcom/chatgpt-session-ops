#!/usr/bin/env python3
"""install_mcp.py — 给任意账号一键安装 远程MCP(RMC) / ChatGPT MCP(SO MCP) 连接器。

用法:
    python install_mcp.py <acctN>              # 装两个（RMC + SO MCP）
    python install_mcp.py <acctN> --rmc        # 只装远程 MCP
    python install_mcp.py <acctN> --so-mcp     # 只装 ChatGPT MCP

前置:
    Keycloak 口令: 环境变量 RMC_KC_PASS 优先；未设置则读脚本同目录 .kc-pass 文件（口令不写源码、不进参数）。
    Keycloak 用户名: 环境变量 RMC_KC_USER 优先，其次 .kc-user 文件，默认 owner。
    （注意 RMC 用 realm rmc、SO MCP 用 realm so-mcp；两侧同名账号可通用。）
    代理走 chatgpt_send.py 的统一出口（proxy.json / CHATGPT_PROXY）；
    Keycloak 表单登录固定直连（mcp.oachom0831.xyz 不走代理，2026-11-27 实测代理 TLS 会断）。

每个连接器的流程（references/mcp-oauth-connector-contract.md 实测契约）:
    1. 已装检查：list_accessible 里已有同 base_url 且 ACTIVE + actions>0 的 link → 跳过
    2. oauth_config 发现 → 创建（UI 同款 payload）
    3. 回读校验 supported_auth[0].type == "OAUTH"（200 本身不算数）；
       不符即删半成品并失败（fail-closed）
    4. links/oauth → Keycloak 表单登录(owner) → 回调落库（同一进程内连续完成，code 有时效）
    5. list_accessible 三项校验：link 存在 + auth_status=="ACTIVE" + actions 非空

任一步失败即停并打印原始响应；退出码 0=全部完成，2=失败。
"""
import html
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chatgpt_send as cs  # noqa: E402

CB = "https://chatgpt.com/connector_platform_oauth_redirect"
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TARGETS = {
    "--rmc": ("RMC", "https://mcp.oachom0831.xyz/mcp"),
    "--so-mcp": ("SO MCP", "https://mcp.oachom0831.xyz/so-mcp"),
}


def _side_file(name):
    try:
        with open(os.path.join(_SCRIPT_DIR, name), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def kc_user():
    return (
        os.environ.get("RMC_KC_USER")
        or _side_file(".kc-user")
        or "owner"
    )


def die(msg):
    print("ERROR: " + msg, flush=True)
    sys.exit(2)


def proxy_url():
    return os.environ.get("CHATGPT_PROXY") or getattr(cs, "PROXY", None) or "http://127.0.0.1:7000"


def hidden_fields(frag):
    out = {}
    for m in re.findall(r"<input[^>]*>", frag, re.I):
        n = re.search(r'name=["\']([^"\']+)', m)
        if n:
            v = re.search(r'value=["\']([^"\']*)', m)
            out[n.group(1)] = html.unescape(v.group(1)) if v else ""
    return out


def main():
    if len(sys.argv) < 2 or not sys.argv[1].startswith("acct"):
        die("usage: python install_mcp.py <acctN> [--rmc] [--so-mcp]")
    acct = sys.argv[1]
    flags = [a for a in sys.argv[2:] if a in TARGETS]
    if not flags:
        flags = list(TARGETS)
    kc_pass = os.environ.get("RMC_KC_PASS") or _side_file(".kc-pass")
    if not kc_pass:
        die("Keycloak 口令未找到。设置 RMC_KC_PASS 环境变量，或在脚本目录放 .kc-pass 文件。")
    kc_user_name = kc_user()
    print("Keycloak 用户: %s" % kc_user_name, flush=True)

    api = cs.ChatGPTAPI(account=acct)
    api.warm_cookies()
    tok, proof = api.requirements()
    hdr = {}
    if tok:
        hdr["OpenAI-Sentinel-Chat-Requirements-Token"] = tok
    if proof:
        hdr["OpenAI-Sentinel-Proof-Token"] = proof

    def post(path, obj):
        return api.req("POST", cs.API_BASE + path, obj, hdr)

    # ---- 0. 现状盘点（已装检查 + 输出对照）----
    st, out = post("/backend-api/aip/connectors/links/list_accessible", {"principals": []})
    if st != 200:
        die("list_accessible -> %s %s" % (st, out[:200]))
    links = json.loads(out).get("links", [])
    healthy = {}
    for l in links:
        cid = l.get("connector_id")
        st2, out2 = api.req("GET", cs.API_BASE + "/backend-api/aip/connectors/" + cid, None, hdr)
        if st2 != 200:
            continue
        d = json.loads(out2)
        healthy[d.get("base_url") or ""] = (cid, d.get("name"), l.get("auth_status"), len(l.get("actions") or []))

    results = []
    for flag in flags:
        label, url = TARGETS[flag]
        name = "%s %s" % (label, acct)
        print("\n=== [%s] %s -> %s ===" % (acct, name, url), flush=True)
        hit = healthy.get(url)
        if hit and hit[2] == "ACTIVE" and hit[3] > 0:
            print("SKIP 已装且健康: cid=%s name=%s actions=%d" % (hit[0], hit[1], hit[3]), flush=True)
            results.append((name, "SKIP", hit[0]))
            continue

        # ---- 1. oauth_config 发现 ----
        st, out = post("/backend-api/aip/connectors/mcp/oauth_config", {"mcp_url": url})
        if st != 200:
            print("oauth_config -> %s %s" % (st, out[:300]), flush=True)
            results.append((name, "FAIL@oauth_config", ""))
            continue
        cfg = json.loads(out)["oauth_config"]

        # ---- 2. 创建（UI 同款 payload）----
        body = {"name": name, "description": "", "mcp_url": url, "logo_url": None,
                "auth_request": {"supported_auth": [cfg]}}
        st, out = post("/backend-api/aip/connectors/mcp", body)
        if st == 409:
            cid = (json.loads(out).get("detail") or {}).get("existing_connector_id")
            print("同名连接器已存在，认领继续: cid=%s" % cid, flush=True)
        elif st != 200:
            print("create -> %s %s" % (st, out[:300]), flush=True)
            results.append((name, "FAIL@create", ""))
            continue
        else:
            c = json.loads(out).get("connector") or json.loads(out)
            cid = c.get("id")
            auths = [a.get("type") for a in (c.get("supported_auth") or [])]
            print("created cid=%s supported_auth=%s" % (cid, auths), flush=True)
            if not auths or auths[0] != "OAUTH":
                print("校验失败: supported_auth[0].type != OAUTH，删除半成品", flush=True)
                api.req("DELETE", cs.API_BASE + "/backend-api/aip/connectors/" + cid, None, hdr)
                results.append((name, "FAIL@auth_type", cid))
                continue

        # ---- 3. OAuth 链接（同进程连做：code 有时效）----
        st, out = post("/backend-api/aip/connectors/links/oauth",
                       {"connector_id": cid, "name": "Primary", "callback_url": CB})
        if st != 200:
            print("links/oauth -> %s %s" % (st, out[:300]), flush=True)
            results.append((name, "FAIL@links_oauth", cid))
            continue
        redirect_url = json.loads(out)["redirect_url"]
        print("redirect_url: %s" % redirect_url[:160], flush=True)

        cj = http.cookiejar.CookieJar()
        # Keycloak 在自有服务器上走直连（实测 2026-11-27 本地代理隧道到 mcp.oachom0831.xyz TLS 会断）；
        # chatgpt.com 的任何请求一律走代理（fail-closed 纪律），按 URL 域名分流。

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None  # 3xx 以 HTTPError 冒出，交给 fetch 手动逐跳处理

        op_direct = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPCookieProcessor(cj))
        op_proxy = urllib.request.build_opener(
            _NoRedirect,
            urllib.request.ProxyHandler({"http": proxy_url(), "https": proxy_url()}),
            urllib.request.HTTPCookieProcessor(cj),
        )
        UA = getattr(cs, "UA", "Mozilla/5.0")

        def fetch(u, method="GET", data=None):
            # 手动逐跳跟随重定向：每一跳按域名选通道（chatgpt.com→代理，其余→直连），
            # 避免 urllib 自动跳转时沿用同一 opener 导致跨域分流失效。
            cur_url, cur_method, cur_data = u, method, data
            for _ in range(10):
                op = op_proxy if "chatgpt.com" in urllib.parse.urlparse(cur_url).netloc else op_direct
                rq = urllib.request.Request(
                    cur_url, data=cur_data, method=cur_method, headers={"User-Agent": UA})
                try:
                    with op.open(rq, timeout=90) as r:
                        st_, page_, loc = r.status, r.read().decode("utf-8", "replace"), r.url
                        redirs = [v for k, v in r.getheaders() if k.lower() == "location"]
                except urllib.error.HTTPError as e:
                    st_, page_, loc = e.code, e.read().decode("utf-8", "replace"), e.url
                    redirs = [v for k, v in e.headers.items() if k.lower() == "location"]
                except urllib.error.URLError as e:
                    # Cloudflare 偶发 TLS 掐断（实测 2026-11-27），同 URL 重试 2 次再认输
                    last_err = e
                    for retry in range(2):
                        try:
                            with op.open(rq, timeout=90) as r:
                                st_, page_, loc = (r.status,
                                                   r.read().decode("utf-8", "replace"), r.url)
                                redirs = [v for k, v in r.getheaders()
                                          if k.lower() == "location"]
                            break
                        except urllib.error.HTTPError as e2:
                            st_, page_, loc = (e2.code,
                                               e2.read().decode("utf-8", "replace"), e2.url)
                            redirs = [v for k, v in e2.headers.items()
                                      if k.lower() == "location"]
                            break
                        except urllib.error.URLError as e2:
                            last_err = e2
                    else:
                        return 0, "URLError: %s" % last_err.reason, cur_url
                if st_ in (301, 302, 303, 307, 308) and redirs:
                    cur_url = urllib.parse.urljoin(cur_url, redirs[0])
                    if st_ in (301, 302, 303):
                        cur_method, cur_data = "GET", None
                    continue
                return st_, page_, loc
            return 0, "redirect loop", cur_url

        st, page, cur = fetch(redirect_url)
        m = re.search(r'<form[^>]*action=["\']([^"\']+)["\'][^>]*>(.*?)</form>', page, re.S | re.I)
        if not m:
            diag = re.sub(r'\s+', ' ', page)[:400]
            print("Keycloak 表单未找到 (HTTP %s) final_url=%s 页面开头: %s" % (st, cur[:160], diag), flush=True)
            results.append((name, "FAIL@kc_form", cid))
            continue
        f = hidden_fields(m.group(2))
        f["username"] = kc_user_name
        f["password"] = kc_pass
        st, page, cur = fetch(html.unescape(m.group(1)), "POST",
                              urllib.parse.urlencode(f).encode(),
                              )
        for _ in range(3):
            m2 = re.search(r'<form[^>]*action=["\']([^"\']+)["\'][^>]*>(.*?)</form>', page, re.S | re.I)
            if not m2 or "kc-form-login" in m2.group(0):
                break
            f2 = hidden_fields(m2.group(2))
            for b in re.findall(r'<input[^>]*type=["\']submit["\'][^>]*>', m2.group(2), re.I):
                n = re.search(r'name=["\']([^"\']+)', b)
                if n:
                    f2.setdefault(n.group(1), "yes")
            st, page, cur = fetch(html.unescape(m2.group(1)), "POST",
                                  urllib.parse.urlencode(f2).encode())
        if "code=" not in cur:
            print("OAuth 回跳未拿到 code，最终 URL: %s" % cur[:120], flush=True)
            results.append((name, "FAIL@kc_login", cid))
            continue
        st, out = post("/backend-api/aip/connectors/links/oauth/callback",
                       {"full_redirect_url": cur})
        print("callback -> %s" % st, flush=True)
        if st != 200:
            print(out[:300], flush=True)
            results.append((name, "FAIL@callback", cid))
            continue

        # ---- 4. 三项校验 ----
        st, out = post("/backend-api/aip/connectors/links/list_accessible", {"principals": []})
        ok = None
        if st == 200:
            for l in json.loads(out).get("links", []):
                if l.get("connector_id") == cid and l.get("auth_status") == "ACTIVE" and (l.get("actions") or []):
                    ok = len(l.get("actions") or [])
                    break
        if ok:
            print("OK 通了: cid=%s auth_status=ACTIVE actions=%d" % (cid, ok), flush=True)
            results.append((name, "OK(%d tools)" % ok, cid))
        else:
            print("校验未过: link 非 ACTIVE 或 actions 为空", flush=True)
            results.append((name, "FAIL@verify", cid))

    print("\n==== %s 汇总 ====" % acct)
    for n, r, cid in results:
        print("  %-12s %-18s %s" % (n, r, cid))
    sys.exit(0 if all(r.startswith(("OK", "SKIP")) for _, r, _ in results) else 2)


if __name__ == "__main__":
    main()
