# -*- coding: utf-8 -*-
"""Solve ChatGPT proof-of-work challenge (extracted from 7078.691a92af09.js)."""
import base64
import json
import math
import os
import random
import time


def fnv1a_hex(s: str) -> str:
    """FNV-1a 32-bit + avalanche，返回 8 位十六进制（对齐 JS 的 >>>0 语义）。

    注意：JS 的 charCodeAt 按 UTF-16 码元迭代，这里按 Python 码点迭代。
    BMP 内字符（含中文）一致；emoji 等非 BMP 字符会差一个码元，指纹里别放。
    """
    t = 0x811C9DC5
    for ch in s:
        t ^= ord(ch)
        t = (t * 0x1000193) & 0xFFFFFFFF
    t ^= (t >> 16)
    t = (t * 0x85EBCA6B) & 0xFFFFFFFF
    t ^= (t >> 13)
    t = (t * 0xC2B2AE35) & 0xFFFFFFFF
    t ^= (t >> 16)
    return format(t, "08x")


def js_string_from_bytes(data: bytes) -> str:
    """btoa(String.fromCharCode(...new TextEncoder().encode(json)))"""
    return base64.b64encode(data).decode("ascii")


def encode_payload(arr) -> str:
    """_(e): JSON.stringify -> UTF-8 -> fromCharCode -> btoa"""
    txt = json.dumps(arr, separators=(",", ":"), ensure_ascii=False)
    return js_string_from_bytes(txt.encode("utf-8"))


def solve(seed: str, difficulty: str, fp=None) -> str:
    """Return the proof token (gAAAAAB...), per web app algorithm."""
    if fp is None:
        fp = build_fingerprint()
    n0 = time.time() * 1000
    for r in range(500000):
        fp[3] = r
        fp[9] = int(round(time.time() * 1000 - n0))
        i = encode_payload(fp)
        h = fnv1a_hex(seed + i)
        if h[: len(difficulty)] <= difficulty:
            return "gAAAAAB" + i + "~S"
    # 旧版这里返回一个写死的假 proof —— 服务端必然 403，却看不出是解算失败。
    # 宁可返回空串让上层显式失败，也不要拿假 token 去撞风控。
    print("WARN: PoW 未在 50 万次内收敛（difficulty=%s），proof 置空" % (difficulty,), flush=True)
    return ""


def build_fingerprint(browser_facts: dict = None) -> list:
    """Replicate f() from the web app. Values are spoofable; server checks hash only."""
    b = browser_facts or {}
    ua = b.get("ua", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.4341.60")
    script_src = b.get("script_src", "/cdn/assets/async/7078.691a92af09.js")
    # 注意：这里的 UUID 是前端 build id（/c/<build-id>/_next/），不是会话 id，
    # 也不属于任何账号；页面在时由调用方从 script_src 覆盖。
    build = b.get("build", "/c/6afcc453-295c-83e8-8e24-3a089ff20e22/_next/")
    hw = b.get("hw", 16)
    now_ms = time.time() * 1000
    date_str = time.strftime("%a %b %d %Y %H:%M:%S GMT+0800 (中国标准时间)")
    nav_keys = ["userAgent", "language", "platform", "vendor", "webdriver"]
    doc_keys = ["visibilityState", "readyState", "hidden", "fonts", "documentElement"]
    win_keys = ["chrome", "visualViewport", "speechSynthesis", "trustedTypes", "localStorage"]
    proto_key = random.choice(nav_keys)
    return [
        2160,                       # screen w+h approx (1920+1080 could be; use plausible)
        date_str,                   # String(new Date)
        None,                       # jsHeapSizeLimit
        0.5,                        # nonce placeholder
        ua,                         # navigator.userAgent
        script_src,                 # random script src
        build,                      # data-build
        "zh-CN",                    # navigator.language
        "zh-CN,zh",                 # languages
        0.5,                        # elapsed placeholder
        "%s\\u2212%s" % (proto_key, "true"),   # a[10]
        proto_key,                  # random nav proto key
        random.choice(doc_keys),    # random doc key
        random.choice(win_keys),    # random win key
        now_ms % 100000,            # performance.now()
        "t",                        # (0,a.a)() unknown -> short string
        "",                         # URL search keys
        hw,                         # hardwareConcurrency
        int(now_ms),                # timeOrigin
        0, 0, 0, 0, 0, 0, 0,        # "ai"/"createPRNG"/"cache"/"data"/"solana"/"dump"/"InstallTrigger" in window
    ]


if __name__ == "__main__":
    import sys
    seed = sys.argv[1] if len(sys.argv) > 1 else "0.24039406050357925"
    diff = sys.argv[2] if len(sys.argv) > 2 else "07a120"
    t0 = time.time()
    tok = solve(seed, diff)
    print("token:", tok[:80], "...")
    print("len:", len(tok), "time: %.2fs" % (time.time() - t0))
    # verify
    body = tok[len("gAAAAAB"):]
    if body.endswith("~S"):
        body = body[:-2]
    h = fnv1a_hex(seed + body)
    print("hash:", h, "prefix:", h[: len(diff)], "<=?", diff, h[: len(diff)] <= diff)
