# -*- coding: utf-8 -*-
"""隔离浏览器内注册 Proton：填表 -> 提交 -> 观测人机校验形态。"""
from __future__ import annotations

import os
import random
import re
import sys
import tempfile
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("signup")

from playwright.sync_api import sync_playwright

from proton_batch.clash import Clash, is_node
from proton_batch.config import load_config

PROXY = "http://127.0.0.1:7000"
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode"
ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


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
    try:
        print("  inputs:", pg.eval_on_selector_all(
            "input", "els => els.filter(e=>e.offsetWidth>0).map(e => e.id+'|'+e.name+'|'+e.type)"))
        print("  buttons:", pg.eval_on_selector_all(
            "button", "els => els.filter(e=>e.offsetWidth>0).map(e => (e.innerText||'').trim().slice(0,20)).filter(Boolean)"))
        print("  iframes:", [f.url[:70] for f in pg.frames if f != pg.main_frame])
        print("  body:", pg.inner_text("body")[:400].replace("\n", " | "))
    except Exception as exc:  # noqa: BLE001
        print("  dump 失败:", str(exc)[:90])


username = "oahcom" + "".join(random.choice("0123456789") for _ in range(6))
password = "".join(random.choice(ALPHABET) for _ in range(14)) + "9!aZ"
log.info("目标: %s", username)
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
    pg.wait_for_selector("#username", timeout=60000)
    time.sleep(3)

    # 用户名真实输入框在 iframe[title="Email address"] 内，主文档里是隐藏镜像
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
    log.info("用户名输入框在: %s", (frame.name or frame.url)[:60])

    frame.fill("#username", username)
    pg.fill("#password", password)
    time.sleep(0.8)
    try:
        pg.wait_for_selector("#password-confirm", state="visible", timeout=15000)
        pg.fill("#password-confirm", password)
        log.info("已填确认密码")
    except Exception:  # noqa: BLE001
        log.warning("没有确认密码框")
    time.sleep(0.5)
    submitted = []
    errs = []
    pg.on("response", lambda r: submitted.append((r.status, r.url[:80], r.text()[:200] if "/api/core/v4/users" in r.url else ""))
          if "/api/core/v4/users" in r.url else None)
    pg.on("console", lambda m: errs.append(f"{m.type}: {m.text[:90]}") if m.type == "error" else None)

    try:
        pg.get_by_role("button", name="Start using Proton Mail now", exact=False).click(timeout=20000)
        log.info("已点击提交按钮(get_by_role)")
    except Exception as exc:  # noqa: BLE001
        log.error("点击失败: %s", str(exc)[:90])
    time.sleep(6)
    if not submitted:
        log.info("无 POST，尝试回车提交")
        pg.click("#password-confirm")
        pg.keyboard.press("Enter")
        time.sleep(8)
    state = pg.evaluate("""() => {
        const f = document.querySelector('form');
        const bad = [...document.querySelectorAll('[aria-invalid="true"]')].map(e => e.id);
        const btn = [...document.querySelectorAll('button')].find(b => /Start using Proton/i.test(b.innerText||''));
        return {form: !!f, bad, btnDisabled: btn ? btn.disabled : null, btnType: btn ? btn.type : null};
    }""")
    print("  FORM:", state)
    for st, url, body in submitted:
        print(f"  SUBMIT {st} {url} :: {body[:200]}")
    for e in errs[-5:]:
        print("  CONSOLE", e)
    dump(pg, "提交后")
    pg.screenshot(path="data/work/proton_hv.png", full_page=True)
    print("username:", username, "password:", password)
    print("screenshot -> data/work/proton_hv.png")
    time.sleep(2)
    ctx.close()
