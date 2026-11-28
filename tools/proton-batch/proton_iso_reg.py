# -*- coding: utf-8 -*-
"""Proton 注册（protonmail.com 域名 + 诚实判定 + 人机校验观测）。

判定纪律：
- 200 不等于成功；必须页面无 "abusive traffic" 且能进入 /mail（非登录页）才算成功。
"""
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

    pg.goto("https://account.proton.me/signup?plan=free&language=en",
            wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_selector("#username", timeout=60000)
    time.sleep(3)

    # 用户名/域名控件都在 challenge iframe 内，主文档是隐藏镜像
    frame = None
    for f in pg.frames:
        try:
            el = f.query_selector("#username")
            if el and el.is_visible():
                frame = f
                break
        except Exception:  # noqa: BLE001
            continue
    if frame is None:
        log.error("找不到可见的用户名输入框")
        raise SystemExit(2)
    log.info("表单在: %s", (frame.name or frame.url)[:60])

    # 域名切到 protonmail.com
    frame.click("#select-domain", timeout=10000)
    time.sleep(1.2)
    opts = frame.eval_on_selector_all(
        "[role=option], [role=menuitem], li, button",
        "els => els.filter(e=>e.offsetWidth>0).map(e => (e.innerText||'').trim()).filter(t => t.includes('proton')).slice(0,10)")
    log.info("域名选项: %s", opts)
    picked = False
    for label in ("@protonmail.com", "protonmail.com"):
        try:
            frame.get_by_text(label, exact=True).first.click(timeout=5000)
            picked = True
            break
        except Exception:  # noqa: BLE001
            continue
    try:
        cur_domain = frame.inner_text("#select-value-4")
    except Exception:  # noqa: BLE001
        cur_domain = "?"
    log.info("当前域名: %s (picked=%s)", cur_domain, picked)

    frame.fill("#username", username)
    pg.fill("#password", password)
    time.sleep(0.8)
    pg.fill("#password-confirm", password)
    time.sleep(0.5)

    posts = []
    pg.on("response", lambda r: posts.append((r.status, r.text()[:300]))
          if "/api/core/v4/users" in r.url else None)
    pg.get_by_role("button", name="Start using Proton Mail now", exact=False).click(timeout=20000)
    log.info("已提交")

    blocked = False
    hv = None
    for i in range(8):
        time.sleep(4)
        try:
            st = pg.evaluate("""() => ({
                url: location.href,
                text: document.body.innerText.replace(/\\n+/g, ' | ').slice(0, 400),
                captcha: !!document.querySelector('iframe[title="Captcha"]'),
                dialogs: [...document.querySelectorAll('dialog,[role=dialog]')]
                          .filter(d => d.offsetWidth > 0).length
            })""")
        except Exception as exc:  # noqa: BLE001
            st = {"err": str(exc)[:50]}
        log.info("[%d] %s", i, json.dumps(st, ensure_ascii=False)[:260])
        text = str(st.get("text", ""))
        if "abusive traffic" in text or "blocked any further signups" in text:
            blocked = True
            break
        if st.get("captcha") or st.get("dialogs"):
            hv = st
            break

    print("POST:", posts[0] if posts else "none")
    print("BLOCKED:", blocked, "HV:", json.dumps(hv, ensure_ascii=False) if hv else None)

    # 会话核验：/mail 若是登录页说明没有会话
    if not blocked:
        pg.goto("https://account.proton.me/mail?u=0", wait_until="domcontentloaded", timeout=60000)
        time.sleep(6)
        body = pg.inner_text("body")[:160].replace("\n", " | ")
        print("MAIL PAGE:", body)
        logged = "To continue to Proton Mail" not in body
        print("SESSION:", logged)
        if logged:
            store = Store("data/accounts.db")
            acc_id = store.add_proton(username, f"{username}@protonmail.com", password, None)
            store.update(acc_id, proton_status="verified")
            print("SAVED id=", acc_id, "email=", f"{username}@protonmail.com", "pw=", password)

    pg.screenshot(path="data/work/reg_result.png", full_page=True)
    print("EMAIL:", f"{username}@protonmail.com", "PW:", password)
    ctx.close()
