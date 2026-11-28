# -*- coding: utf-8 -*-
"""取注册页用户名区（含域名下拉）的真实 DOM。"""
from __future__ import annotations

import json
import sys
import tempfile
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.WARNING)

from playwright.sync_api import sync_playwright

PROXY = "http://127.0.0.1:7000"
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode"

profile = tempfile.mkdtemp(prefix="iso-", dir=TMP)
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=profile, channel="msedge", headless=False,
        args=["--incognito", f"--proxy-server={PROXY}",
              "--disable-blink-features=AutomationControlled",
              "--no-first-run"],
        viewport={"width": 1400, "height": 950}, locale="en-US")
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    pg.goto("https://account.proton.me/signup?plan=free&language=en",
            wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_selector("#username", timeout=60000)
    time.sleep(4)

    info = pg.evaluate("""() => {
        const btns = [...document.querySelectorAll('button')].map(b => ({
            t: (b.innerText || '').replace(/\\n/g, ' ').trim().slice(0, 30),
            aria: b.getAttribute('aria-label'),
            id: b.id,
            cls: (b.className || '').slice(0, 40),
        })).filter(b => b.t || b.aria);
        const input = document.querySelector('#username');
        let wrap = input ? input.parentElement : null;
        for (let i = 0; i < 3 && wrap && wrap.parentElement; i++) wrap = wrap.parentElement;
        return { btns, wrap: wrap ? wrap.outerHTML.slice(0, 1200) : null };
    }""")
    print(json.dumps(info, ensure_ascii=False, indent=1)[:2500])
    ctx.close()
