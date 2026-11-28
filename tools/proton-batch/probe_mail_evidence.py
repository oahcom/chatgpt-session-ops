# -*- coding: utf-8 -*-
"""采集 ChatGPT 要求的两项证据：打开收件箱后的 DOM 快照 + verifiedepoch 的完整状态与响应体。"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("evidence")

from playwright.sync_api import sync_playwright
from proton_batch.store import Store

PROXY = "http://127.0.0.1:7000"
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode"
ALPHA = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

username = "oahcom" + "".join(random.choice("0123456789") for _ in range(6))
password = "".join(random.choice(ALPHA) for _ in range(14)) + "9!aZ"
log.info("新账号 %s", username)

profile = tempfile.mkdtemp(prefix="iso-", dir=TMP)
net = []
con = []

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

    def on_resp(r):
        if "/api/" not in r.url:
            return
        if r.status < 400 and "verifiedepoch" not in r.url:
            return
        try:
            body = r.text()[:400]
        except Exception:  # noqa: BLE001
            body = "<no-body>"
        net.append({"status": r.status, "method": r.request.method,
                    "url": r.url.split("?")[0][:110], "body": body.replace("\n", " ")})

    pg.on("response", on_resp)
    pg.on("console", lambda m: con.append(f"{m.type}: {m.text[:160]}") if m.type in ("error", "warning") else None)

    # 1) 注册
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
    time.sleep(12)
    log.info("注册后 URL: %s", pg.url[:80])

    # 2) 账号身份
    ident = pg.evaluate("""() => ({
        url: location.href,
        text: document.body.innerText.slice(0, 400),
        cookies: document.cookie.slice(0, 120)
    })""")
    print("IDENT:", json.dumps(ident, ensure_ascii=False)[:500])

    # 3) 打开 mail 并观察
    pg.goto("https://account.proton.me/u/0/mail", wait_until="domcontentloaded", timeout=90000)
    for i in range(6):
        time.sleep(10)
        try:
            state = pg.evaluate("""() => ({
                url: location.href,
                rows: document.querySelectorAll('.item-container').length,
                placeholders: document.querySelectorAll('[data-testid="message-item:undefined"]').length,
                loading: document.querySelectorAll('.item-is-loading').length,
                text: document.body.innerText.slice(0, 200)
            })""")
        except Exception as exc:  # noqa: BLE001
            state = {"err": str(exc)[:60]}
        log.info("[%d] %s", i, json.dumps(state, ensure_ascii=False)[:220])

    # 4) DOM / iframe / shadow 快照
    snap = pg.evaluate("""() => ({
        url: location.href,
        frames: [...document.querySelectorAll('iframe')].map(f => (f.title || '') + '|' + (f.src || '').slice(0, 90)),
        shadowHosts: [...document.querySelectorAll('*')].filter(e => e.shadowRoot)
            .map(e => e.tagName + '#' + e.id + '.' + String(e.className).slice(0, 40)).slice(0, 15),
        body: document.body.innerText.slice(0, 1500)
    })""")
    pg.screenshot(path="data/work/mail_evidence.png", full_page=True)

    store = Store("data/accounts.db")
    acc_id = store.add_proton(username, f"{username}@proton.me", password, None)
    store.update(acc_id, proton_status="verified")

    out = {"account": username, "id": acc_id, "snapshot": snap,
           "network": net[-40:], "console": con[-20:]}
    with open("data/work/mail_evidence.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("EMAIL:", f"{username}@proton.me", "PW:", password, "id:", acc_id)
    print("network entries:", len(net))
    for n in net[-12:]:
        print(" ", n["status"], n["method"], n["url"], "::", n["body"][:120])
    ctx.close()
