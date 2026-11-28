# -*- coding: utf-8 -*-
"""登录 Proton 网页并给收件箱截图，判断为何列表不渲染。"""
from __future__ import annotations

import sys
import tempfile
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("inbox")

from playwright.sync_api import sync_playwright
from proton_batch.store import Store, decrypt_secret

PROXY = "http://127.0.0.1:7000"
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode"
ACC = int(sys.argv[1]) if len(sys.argv) > 1 else 71

store = Store("data/accounts.db")
row = next(r for r in store.list(limit=3000) if r["id"] == ACC)
email, password = row["proton_email"], decrypt_secret(row["proton_password"] or "")
log.info("账号 %s", email)

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
    pg.goto("https://account.proton.me/login", wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_selector("#username", timeout=60000)
    pg.fill("#username", email)
    pg.get_by_role("button", name="Continue", exact=True).click(timeout=20000)
    pg.wait_for_selector("#password", timeout=60000)
    pg.fill("#password", password)
    pg.get_by_role("button", name="Sign in", exact=True).click(timeout=20000)
    time.sleep(10)
    log.info("登录后: %s", pg.url[:70])

    pg.goto("https://mail.proton.me/u/0/inbox", wait_until="domcontentloaded", timeout=90000)
    for i in range(8):
        time.sleep(8)
        try:
            body = pg.inner_text("body")[:300].replace("\n", " | ")
        except Exception as exc:  # noqa: BLE001
            body = f"<{str(exc)[:50]}>"
        log.info("[%d] %s", i, body[:200])
        if i == 3:
            pg.screenshot(path="data/work/inbox_probe.png", full_page=True)
    pg.screenshot(path="data/work/inbox_final.png", full_page=True)
    print("screenshots -> data/work/inbox_probe.png, inbox_final.png")
    ctx.close()
