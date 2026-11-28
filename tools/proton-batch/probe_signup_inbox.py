# -*- coding: utf-8 -*-
"""注册 Proton 后，在同一个已认证会话里打开收件箱并截图，定位「列表不渲染」的原因。"""
from __future__ import annotations

import random
import sys
import tempfile
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("probe")

from playwright.sync_api import sync_playwright
from proton_batch.store import Store

PROXY = "http://127.0.0.1:7000"
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode"
ALPHA = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

username = "oahcom" + "".join(random.choice("0123456789") for _ in range(6))
password = "".join(random.choice(ALPHA) for _ in range(14)) + "9!aZ"
log.info("新账号 %s", username)

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

    posts = []
    pg.on("response", lambda r: posts.append((r.status, r.text()[:160]))
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
    for _ in range(12):
        time.sleep(2)
        if posts:
            break
    print("SIGNUP:", posts[0] if posts else "no-post")
    print("URL:", pg.url[:80])

    store = Store("data/accounts.db")
    acc_id = store.add_proton(username, f"{username}@proton.me", password, None)
    store.update(acc_id, proton_status="verified")
    print("id=", acc_id, "email=", f"{username}@proton.me", "pw=", password)

    time.sleep(5)
    pg.screenshot(path="data/work/s1_after_signup.png", full_page=True)

    pg.goto("https://mail.proton.me/u/0/inbox", wait_until="domcontentloaded", timeout=90000)
    for i in range(6):
        time.sleep(10)
        try:
            body = pg.inner_text("body")[:200].replace("\n", " | ")
            rows = pg.eval_on_selector_all(".item-container", "e => e.length")
        except Exception as exc:  # noqa: BLE001
            body, rows = f"<{str(exc)[:40]}>", -1
        print(f"[{i}] rows={rows} url={pg.url[:60]} body={body[:120]}")
    pg.screenshot(path="data/work/s2_inbox.png", full_page=True)
    print("shots -> s1_after_signup.png s2_inbox.png")
    ctx.close()
