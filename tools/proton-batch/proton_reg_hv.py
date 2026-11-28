# -*- coding: utf-8 -*-
"""Proton 注册 + 拼图人机校验：完整闭环（失败判据用页面文案，不看状态码）。"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("reg")

from playwright.sync_api import sync_playwright

from proton_batch.store import Store
from proton_batch.pw_captcha import PwCaptcha

PROXY = "http://127.0.0.1:7000"
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode"
ALPHA = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

username = "oahcom" + "".join(random.choice("0123456789") for _ in range(6))
password = "".join(random.choice(ALPHA) for _ in range(14)) + "9!aZ"
log.info("目标 %s@proton.me", username)

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
    log.info("已提交，等待人机校验")

    hv_seen = False
    for _ in range(10):
        time.sleep(3)
        try:
            has = pg.evaluate("() => !!document.querySelector('iframe[title=\"Captcha\"]')")
        except Exception:  # noqa: BLE001
            has = False
        if has:
            hv_seen = True
            break
    log.info("captcha 出现: %s", hv_seen)

    if hv_seen:
        try:
            solver = PwCaptcha(pg, log, "data/work")
            res = solver.solve()
            log.info("求解结果: %s", json.dumps(res, ensure_ascii=False)[:200])
            ok_click = solver.submit()
            log.info("已提交验证码: %s", ok_click)
        except Exception as exc:  # noqa: BLE001
            log.error("求解异常: %s", str(exc)[:150])

    for i in range(10):
        time.sleep(4)
        try:
            st = pg.evaluate("""() => ({
                url: location.href,
                text: document.body.innerText.replace(/\\n+/g, ' | ').slice(0, 300),
                captcha: !!document.querySelector('iframe[title="Captcha"]')
            })""")
        except Exception as exc:  # noqa: BLE001
            st = {"err": str(exc)[:50]}
        log.info("[%d] %s", i, json.dumps(st, ensure_ascii=False)[:240])
        text = str(st.get("text", ""))
        if "abusive traffic" in text:
            log.error("出口被拒（abusive traffic）")
            break
        if not st.get("captcha") and i >= 2:
            break

    pg.screenshot(path="data/work/reg_hv.png", full_page=True)

    # 会话核验
    pg.goto("https://account.proton.me/mail?u=0", wait_until="domcontentloaded", timeout=60000)
    time.sleep(8)
    body = pg.inner_text("body")[:200].replace("\n", " | ")
    logged = "To continue to Proton Mail" not in body
    print("MAIL:", body[:150])
    print("SESSION:", logged)
    print("EMAIL:", f"{username}@proton.me", "PW:", password)
    if logged:
        store = Store("data/accounts.db")
        acc_id = store.add_proton(username, f"{username}@proton.me", password, None)
        store.update(acc_id, proton_status="verified")
        print("SAVED id=", acc_id)
    ctx.close()
