# -*- coding: utf-8 -*-
"""探测：切到 @protonmail.com 域名 + 提交后的人机校验形态。"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("hv")

from playwright.sync_api import sync_playwright

PROXY = "http://127.0.0.1:7000"
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode"
ALPHA = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

username = "oahcom" + "".join(random.choice("0123456789") for _ in range(6))
password = "".join(random.choice(ALPHA) for _ in range(14)) + "9!aZ"
log.info("目标 %s@protonmail.com", username)

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
    pg.on("response", lambda r: posts.append((r.status, r.text()[:400]))
          if "/api/core/v4/users" in r.url else None)

    pg.goto("https://account.proton.me/signup?plan=free&language=en",
            wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_selector("#username", timeout=60000)
    time.sleep(3)

    # 域名下拉：点开并列出选项
    for sel in ('button:has-text("@proton.me")', 'button:has-text("proton.me")'):
        try:
            pg.locator(sel).first.click(timeout=6000)
            log.info("点开域名下拉: %s", sel)
            break
        except Exception:  # noqa: BLE001
            continue
    time.sleep(1.5)
    opts = pg.eval_on_selector_all(
        "[role=option], [role=menuitem], li",
        "els => els.filter(e=>e.offsetWidth>0).map(e => (e.innerText||'').trim()).filter(Boolean).slice(0,12)")
    print("域名选项:", opts)

    for label in ("@protonmail.com", "protonmail.com"):
        try:
            pg.get_by_text(label, exact=False).first.click(timeout=5000)
            log.info("已选 %s", label)
            break
        except Exception:  # noqa: BLE001
            continue
    time.sleep(1)
    shown = pg.eval_on_selector_all(
        "button", "els => els.map(e => (e.innerText||'').trim()).filter(t => t.includes('@'))")
    print("当前域名按钮:", shown)

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
    log.info("已提交")

    for i in range(10):
        time.sleep(4)
        try:
            st = pg.evaluate("""() => ({
                url: location.href,
                iframes: [...document.querySelectorAll('iframe')].map(f => (f.title||'') + '|' + (f.src||'').slice(0,70)),
                dialogs: [...document.querySelectorAll('dialog,[role=dialog]')].filter(d => d.offsetWidth>0)
                          .map(d => (d.innerText||'').replace(/\\n/g,' | ').slice(0,200)),
                body: document.body.innerText.slice(0, 200).replace(/\\n/g,' | ')
            })""")
        except Exception as exc:  # noqa: BLE001
            st = {"err": str(exc)[:60]}
        print(f"[{i}] {json.dumps(st, ensure_ascii=False)[:420]}")
        if posts:
            print("   POST:", posts[0][0], posts[0][1][:200])
            break
    pg.screenshot(path="data/work/hv_probe.png", full_page=True)
    print("EMAIL:", f"{username}@protonmail.com", "PW:", password)
    ctx.close()
