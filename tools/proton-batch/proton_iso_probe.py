# -*- coding: utf-8 -*-
"""Proton 注册页结构探测（隔离浏览器内）。"""
from __future__ import annotations

import json
import sys
import tempfile
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("probe")

from playwright.sync_api import sync_playwright
from proton_batch.clash import Clash, is_node
from proton_batch.config import load_config

PROXY = "http://127.0.0.1:7000"
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode"


def pick_node() -> str:
    c = Clash(load_config(None), log)
    names = [n for n in c.members() if is_node(n)]
    cur = c.current()
    start = names.index(cur) + 1 if cur in names else 0
    for k in range(min(len(names), 15)):
        name = names[(start + k) % len(names)]
        if c.switch(name):
            ip = c.egress_ip()
            if ip and not str(ip).startswith("err"):
                log.info("出口 %s (%s)", name[:34], ip)
                return str(ip)
    raise RuntimeError("no node")


def dump(pg, tag):
    print(f"===== {tag} ===== {pg.url[:90]}")
    for f in pg.frames:
        try:
            inputs = f.eval_on_selector_all(
                "input", "els => els.filter(e => e.offsetWidth>0).map(e => e.id+'|'+e.name+'|'+e.type+'|'+(e.placeholder||''))")
            btns = f.eval_on_selector_all(
                "button", "els => els.filter(e => e.offsetWidth>0).map(e => (e.innerText||'').trim().slice(0,18)).filter(Boolean)")
            if inputs or btns:
                print(f"  frame[{f.name or f.url[:40]}]")
                print("    inputs:", inputs)
                print("    buttons:", btns)
        except Exception:  # noqa: BLE001
            pass
    try:
        print("  body:", pg.inner_text("body")[:260].replace("\n", " | "))
    except Exception:  # noqa: BLE001
        pass


pick_node()
profile = tempfile.mkdtemp(prefix="iso-", dir=TMP)
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=profile, channel="msedge", headless=False,
        args=["--incognito", f"--proxy-server={PROXY}",
              "--disable-blink-features=AutomationControlled",
              "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
              "--no-first-run"],
        viewport={"width": 1280, "height": 900}, locale="en-US",
        timezone_id="America/New_York")
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    pg.goto("https://account.proton.me/signup?plan=free&language=en",
            wait_until="domcontentloaded", timeout=90000)
    time.sleep(8)
    dump(pg, "注册页")
    pg.screenshot(path="data/work/proton_signup.png", full_page=True)
    print("screenshot -> data/work/proton_signup.png")
    ctx.close()
