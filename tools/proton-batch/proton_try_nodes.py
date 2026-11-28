# -*- coding: utf-8 -*-
"""用 7000 代理逐条出口试 Proton 注册：每次全新无痕 profile，记录出口 IP 与服务端返回码。

用途：证明 422 Code 2028 是「出口网段被封」还是「某条节点被封」——换节点即换出口。
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import tempfile
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("try")

from playwright.sync_api import sync_playwright

from proton_batch.clash import Clash, is_node
from proton_batch.config import load_config

PROXY = "http://127.0.0.1:7000"
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode"
USED = r"data/work/used_nodes.json"
TRIES = int(sys.argv[1]) if len(sys.argv) > 1 else 3
ALPHA = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def load_used() -> set[str]:
    try:
        with open(USED, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:  # noqa: BLE001
        return set()


def save_used(s: set[str]) -> None:
    os.makedirs(os.path.dirname(USED), exist_ok=True)
    with open(USED, "w", encoding="utf-8") as f:
        json.dump(sorted(s), f, ensure_ascii=False, indent=1)


def next_node(c: Clash, used: set[str]) -> str | None:
    names = [n for n in c.members() if is_node(n) and n not in used]
    for name in names:
        if c.switch(name):
            ip = c.egress_ip()
            if ip and not str(ip).startswith("err"):
                used.add(name)
                save_used(used)
                log.info("出口 %s (%s)", name[:36], ip)
                return str(ip)
    return None


def attempt() -> tuple[str, str]:
    """一次完整注册尝试，返回 (页面看到的IP, 提交结果)。"""
    username = "oahcom" + "".join(random.choice("0123456789") for _ in range(6))
    password = "".join(random.choice(ALPHA) for _ in range(14)) + "9!aZ"
    profile = tempfile.mkdtemp(prefix="iso-", dir=TMP)
    result = ("?", "no-submit")
    try:
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

            pg.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=60000)
            page_ip = pg.inner_text("body").strip()

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
            if frame is None:
                return page_ip, "no-username-frame"
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
            result = posts[0] if posts else ("no-post", "")
            if not posts:
                pg.click("#password-confirm")
                pg.keyboard.press("Enter")
                time.sleep(6)
                result = posts[0] if posts else ("no-post", "")
            ctx.close()
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return page_ip, f"{result[0]} {result[1][:130]}"


c = Clash(load_config(None), log)
used = load_used()
log.info("当前出口: %s (%s)", c.current(), c.egress_ip())

for i in range(TRIES):
    if os.environ.get("NO_SWITCH"):
        ip = str(c.egress_ip())
        log.info("不换节点，用当前出口 %s (%s)", c.current()[:34], ip)
    else:
        ip = next_node(c, used)
    if not ip:
        log.error("没有未试过的节点了")
        break
    page_ip, res = attempt()
    print(f"[{i}] 节点IP={ip} 页面IP={page_ip} 提交={res}")
    if "422" not in str(res) and "no-" not in str(res):
        log.info("这条出口可用，停止试探")
        break
