# -*- coding: utf-8 -*-
"""连续尝试 Proton 注册（最多 3 次）：每次都记录 users 接口原文 + 页面文案 + 会话结果。"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("try3")

from playwright.sync_api import sync_playwright

from proton_batch.pw_captcha import PwCaptcha

PROXY = "http://127.0.0.1:7000"
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode"
ALPHA = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ATTEMPTS = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def one_attempt(pg) -> dict:
    username = "oahcom" + "".join(random.choice("0123456789") for _ in range(6))
    password = "".join(random.choice(ALPHA) for _ in range(14)) + "9!aZ"
    posts = []
    pg.on("response", lambda r: posts.append((r.status, r.text()[:260]))
          if "/api/core/v4/users" in r.url else None)

    pg.goto("https://account.proton.me/signup?plan=free&language=en",
            wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_selector("#username", timeout=60000)
    time.sleep(3)
    frame = None
    for f in pg.frames:
        try:
            el = f.query_selector("#username")
            if el and el.is_visible():
                frame = f
                break
        except Exception:  # noqa: BLE001
            continue
    frame.fill("#username", username)
    pg.fill("#password", password)
    time.sleep(0.8)
    pg.fill("#password-confirm", password)
    time.sleep(0.5)
    pg.get_by_role("button", name="Start using Proton Mail now", exact=False).click(timeout=20000)

    hv = False
    for _ in range(8):
        time.sleep(3)
        try:
            hv = pg.evaluate("() => !!document.querySelector('iframe[title=\"Captcha\"]')")
        except Exception:  # noqa: BLE001
            hv = False
        if hv or posts:
            break
    solved = None
    if hv:
        try:
            solver = PwCaptcha(pg, log, "data/work")
            solved = solver.solve()
            solver.submit()
        except Exception as exc:  # noqa: BLE001
            solved = {"err": str(exc)[:80]}
    time.sleep(10)
    try:
        text = pg.inner_text("body")[:220].replace("\n", " | ")
    except Exception:  # noqa: BLE001
        text = "?"
    return {"user": username, "pw": password, "hv": hv, "solved": solved,
            "post": posts[0] if posts else None, "text": text}


profile = tempfile.mkdtemp(prefix="iso-", dir=TMP)
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=profile, channel="msedge", headless=False,
        args=["--incognito", f"--proxy-server={PROXY}",
              "--disable-blink-features=AutomationControlled",
              "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
              "--no-first-run"],
        viewport={"width": 1400, "height": 950}, locale="en-US",
        timezone_id="America/New_York")
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()

    for i in range(ATTEMPTS):
        log.info("=== 第 %d 次 ===", i + 1)
        r = one_attempt(pg)
        print(json.dumps(r, ensure_ascii=False)[:600])
        if r["post"] and r["post"][0] == 200 and "2028" not in r["post"][1]:
            pg.goto("https://account.proton.me/mail?u=0", wait_until="domcontentloaded", timeout=60000)
            time.sleep(8)
            body = pg.inner_text("body")[:160].replace("\n", " | ")
            ok = "To continue to Proton Mail" not in body
            print("SESSION:", ok, "|", body[:120])
            if ok:
                from proton_batch.store import Store
                st = Store("data/accounts.db")
                acc = st.add_proton(r["user"], f"{r['user']}@proton.me", r["pw"], None)
                st.update(acc, proton_status="verified")
                print("SAVED id=", acc, "email=", f"{r['user']}@proton.me", "pw=", r["pw"])
                break
        time.sleep(4)
    ctx.close()
