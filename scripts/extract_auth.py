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

# P2-CDP：CDP 适配器延迟加载。
# 模块级 `from cdp import CDPClient` 会把「解析参数」「judge_error」这类纯逻辑
# 一并绑上浏览器依赖——无 CDP 的环境（CI、Linux 服务器）连 --help 都起不来。
# 这里保留 `CDPClient` 这个名字：它是模块级可替换点（tests/test_hardening.py
# 用 p1.CDPClient 对 CDP 打桩），默认 None 表示「尚未解析」。
CDPClient = None


class CdpUnavailable(RuntimeError):
    """CDP 浏览器适配器不可用——延迟到真正需要浏览器时才判定。"""


_CDP_HINT = (
    "CDP_UNAVAILABLE\n"
    "本操作需要 CDP 浏览器适配器。\n"
    "cdp 模块未安装，或运行路径未配置（CHATGPT_CDP_DIR）。\n"
    "未执行任何浏览器操作。"
)


def _cdp_client():
    """按需导入 cdp.CDPClient。只有真正要连浏览器时才可能失败。"""
    cdp_dir = os.environ.get("CHATGPT_CDP_DIR") or os.path.join(
        os.path.expanduser("~"), ".workbuddy", "skills", "browser-automation-cdp", "scripts")
    if cdp_dir not in sys.path:
        sys.path.insert(0, cdp_dir)
    try:
        from cdp import CDPClient as _client
    except ModuleNotFoundError as e:
        # 区分「cdp 本身不存在」与「cdp 内部依赖损坏」：否则后者会被误报成
        # 未安装，排查方向直接错。
        if e.name == "cdp":
            raise CdpUnavailable("%s（查找路径：%s）" % (_CDP_HINT, cdp_dir)) from e
        raise CdpUnavailable("CDP 模块内部依赖缺失：%s" % e.name) from e
    except Exception as e:
        raise CdpUnavailable(
            "CDP 模块初始化失败：%s: %s" % (type(e).__name__, e)) from e
    return _client

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


def _cookie_domain_allowed(domain):
    """DECISION-156：只允许 OpenAI 自有域，精确域名或后缀，不做字符串包含。

    `notchatgpt.com` / `evil-openai.com` / `chatgpt.com.evil.io` 都必须被拒。
    与 chatgpt_send.py 的同名判定保持同一语义（两处独立实现，避免脚本互相依赖）。
    """
    d = (domain or "").strip().lower().lstrip(".")
    if not d:
        return False
    for base in ("chatgpt.com", "openai.com"):
        if d == base or d.endswith("." + base):
            return True
    return False

def chatgpt_cookies(c):
    """取完整 cookie（含 HttpOnly），只保留 ChatGPT / OpenAI 域。"""
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
        if _cookie_domain_allowed(ck.get("domain")):
            out.append(ck)
    return out


def main():
    argv = sys.argv[1:]
    port = 9222
    if "-h" in argv or "--help" in argv:
        print("usage: python extract_auth.py [--port 9222]", flush=True)
        print("  从已登录的调试浏览器里导出 ChatGPT cookies 与 access_token。", flush=True)
        print("  运行前需先启动调试端口：python start_chrome.py --port 9222", flush=True)
        print("  退出码：0 成功 / 2 用法或参数错误 / 3 缺少 CDP 浏览器适配器", flush=True)
        return 0
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

    client_cls = CDPClient or _cdp_client()
    c = client_cls(host="127.0.0.1", port=port, timeout=30)
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
    # 退出码契约（不得扩大捕获范围）：
    #   0 成功 / 2 用法或参数错误 / 3 缺少 CDP 浏览器适配器
    # 只捕获 CdpUnavailable。其他异常一律按原语义抛出，不得被映射成 3——
    # 否则真实的程序缺陷会被伪装成「环境没装好」，排查方向直接错。
    try:
        sys.exit(main())
    except CdpUnavailable as e:
        print(str(e), file=sys.stderr, flush=True)
        sys.exit(3)
