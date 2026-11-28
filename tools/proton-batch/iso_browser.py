# -*- coding: utf-8 -*-
"""真·隔离浏览器：每次全新 profile + 全新进程 + 独立出口，并在页面内自证出口 IP。

与 browser.new_context() 的区别：
- new_context 只是同一浏览器进程里的独立存储分区（共享 profile 目录、共享进程指纹）；
- 这里是 launch_persistent_context 指向**新建的临时 user-data-dir**，等于一次全新的浏览器安装，
  再加 --incognito 得到无痕窗口，进程退出即删目录，不留任何 cookie/缓存/指纹。

用法：python iso_browser.py [--keep]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("iso")

from playwright.sync_api import sync_playwright

from proton_batch.clash import Clash, is_node
from proton_batch.config import load_config

PROXY = "http://127.0.0.1:7000"
TMP_ROOT = r"C:\Users\Administrator\AppData\Local\Temp\opencode"


def pick_node() -> tuple[str, str]:
    c = Clash(load_config(None), log)
    names = [n for n in c.members() if is_node(n)]
    cur = c.current()
    start = names.index(cur) + 1 if cur in names else 0
    for k in range(min(len(names), 15)):
        name = names[(start + k) % len(names)]
        if c.switch(name):
            ip = c.egress_ip()
            if ip and not str(ip).startswith("err"):
                return name, str(ip)
    raise RuntimeError("没有可用出口")


def main(keep: bool) -> int:
    node, clash_ip = pick_node()
    log.info("出口节点: %s", node[:40])
    log.info("Clash 侧出口 IP: %s", clash_ip)

    os.makedirs(TMP_ROOT, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="iso-", dir=TMP_ROOT)
    log.info("全新 profile: %s", profile)

    args = [
        "--incognito",
        f"--proxy-server={PROXY}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        # WebRTC 不走代理的 UDP 会泄漏真实公网 IP，必须掐掉
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile,
            channel="msedge",
            headless=False,
            args=args,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()

        pg.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=60000)
        page_ip = pg.inner_text("body").strip()
        log.info("页面内看到的出口 IP: %s", page_ip)

        probe = pg.evaluate("""() => ({
            webdriver: navigator.webdriver,
            cookies: document.cookie,
            lang: navigator.language,
            tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
            ua: navigator.userAgent,
            plugins: navigator.plugins.length,
        })""")
        log.info("指纹自检: %s", json.dumps(probe, ensure_ascii=False)[:300])

        # WebRTC 泄漏检查（真实 IP 会在这里露出来）
        rtc = pg.evaluate("""async () => {
            const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]});
            const cands = [];
            pc.createDataChannel('x');
            pc.onicecandidate = e => { if (e.candidate) cands.push(e.candidate.candidate); };
            await pc.setLocalDescription(await pc.createOffer());
            await new Promise(r => setTimeout(r, 3000));
            pc.close();
            return cands;
        }""")
        log.info("ICE 候选: %s", json.dumps(rtc, ensure_ascii=False)[:400])

        ok = page_ip.find(clash_ip) >= 0
        log.info("出口一致性: %s", "OK" if ok else "不一致")
        ctx.close()

    if not keep:
        shutil.rmtree(profile, ignore_errors=True)
        log.info("profile 已删除")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main("--keep" in sys.argv))
