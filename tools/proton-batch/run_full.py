# -*- coding: utf-8 -*-
"""一条出口跑完整链路：新建 Proton 邮箱 -> 同会话读码 -> 注册 ChatGPT -> 存 token。

纪律：全程不换节点、不二次登录（Proton 会话就是注册时的那个）。
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import tempfile
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("full")

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from proton_batch.store import Store
from proton_batch.clash import Clash, is_node
from proton_batch.config import load_config

PROXY = "http://127.0.0.1:7000"
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode"
WORK = "data/work"
ALPHA = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_username() -> str:
    return "oahcom" + "".join(random.choice("0123456789") for _ in range(6))


def new_password(n: int = 14) -> str:
    return "".join(random.choice(ALPHA) for _ in range(n)) + "9!aZ"


def proton_signup(pg, username: str, password: str) -> tuple[int, str]:
    posts = []
    pg.on("response", lambda r: posts.append((r.status, r.text()[:300]))
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
        raise RuntimeError("用户名输入框不可见")
    frame.fill("#username", username)
    pg.fill("#password", password)
    time.sleep(0.8)
    pg.fill("#password-confirm", password)
    time.sleep(0.5)
    pg.get_by_role("button", name="Start using Proton Mail now", exact=False).click(timeout=20000)
    for _ in range(15):
        time.sleep(2)
        if posts:
            break
    if not posts:
        return 0, "no-post"
    return posts[0][0], posts[0][1]


def proton_wait_code(pg, timeout: int = 300) -> str:
    """在已登录的 Proton 会话里轮询收件箱，取 6 位码。"""
    pg.goto("https://mail.proton.me/u/0/inbox", wait_until="domcontentloaded", timeout=90000)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(6)
        try:
            rows = pg.query_selector_all('.item-container:not([data-element-id*="placeholder"])')
            if rows:
                rows[0].click()
                time.sleep(4)
                text = pg.inner_text("body")
                hit = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
                if hit:
                    return hit.group(1)
        except Exception as exc:  # noqa: BLE001
            log.warning("读信重试: %s", str(exc)[:70])
    raise TimeoutError("收件箱 300s 内无验证码")


def main() -> int:
    store = Store("data/accounts.db")
    clash = Clash(load_config(None), log)
    log.info("固定出口: %s (%s)", clash.current()[:36], clash.egress_ip())

    username = new_username()
    password = new_password()
    chat_pw = new_password(12)
    log.info("目标邮箱: %s@proton.me", username)

    profile = tempfile.mkdtemp(prefix="iso-", dir=TMP)
    os.makedirs(WORK, exist_ok=True)
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

        status, body = proton_signup(pg, username, password)
        print(f"PROTON {status} {body[:200]}")
        if status != 200 or "1000" not in body:
            log.error("邮箱注册失败，停止")
            ctx.close()
            return 1

        acc_id = store.add_proton(username, f"{username}@proton.me", password, None)
        store.update(acc_id, proton_status="verified", proton_user_id=username)
        store.event(acc_id, "proton", "info", f"隔离浏览器创建成功 node={clash.current()[:30]}")
        log.info("已入库 id=%s", acc_id)

        email = f"{username}@proton.me"

        # 同会话注册 ChatGPT
        cp = ctx.new_page()
        cp.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        cp.get_by_role("button", name="Sign up for free", exact=False).first.click(timeout=25000)
        cp.wait_for_selector("#mobile-auth-email", state="visible", timeout=60000)
        cp.fill("#mobile-auth-email", email)
        cp.get_by_role("button", name="Continue", exact=True).first.click(timeout=20000)
        log.info("已提交 ChatGPT 邮箱")
        cp.wait_for_selector('input[name="code"]', timeout=90000)
        print("OTP URL:", cp.url[:90])

        code = proton_wait_code(pg)
        log.info("收到验证码 %s", code)
        cp.bring_to_front()
        cp.fill('input[name="code"]', code)
        cp.get_by_role("button", name="Continue", exact=True).first.click(timeout=20000)
        time.sleep(9)
        print("验证后 URL:", cp.url[:90])

        if cp.query_selector('input[type="password"]'):
            cp.fill('input[type="password"]', chat_pw)
            try:
                cp.get_by_role("button", name="Continue", exact=True).first.click(timeout=8000)
            except PWTimeout:
                cp.keyboard.press("Enter")
            time.sleep(9)
            print("密码后 URL:", cp.url[:90])

        for _ in range(4):
            if not (cp.query_selector('input[name="name"]') or cp.query_selector('input[name="birthdate"]')):
                break
            if cp.query_selector('input[name="name"]'):
                cp.fill('input[name="name"]', "Alex Chen")
            if cp.query_selector('input[name="birthdate"]'):
                cp.fill('input[name="birthdate"]', "1994-06-12")
            try:
                cp.get_by_role("button", name="Continue", exact=True).first.click(timeout=8000)
            except PWTimeout:
                cp.keyboard.press("Enter")
            time.sleep(9)
            print("资料后 URL:", cp.url[:90])

        try:
            cp.wait_for_url(re.compile(r"chatgpt\.com/?$"), timeout=120000)
        except PWTimeout:
            print("最终 URL:", cp.url[:100])
        time.sleep(5)
        sess = cp.evaluate("""async () => {
            const r = await fetch('/api/auth/session', {headers: {'Accept': 'application/json'}});
            return await r.json();
        }""")
        token = (sess or {}).get("accessToken") or ""
        state_path = f"{WORK}/cgpt_{acc_id}.json"
        ctx.storage_state(path=state_path)
        if token:
            open(state_path.replace(".json", ".token"), "w", encoding="utf-8").write(token)
            json.dump(sess, open(state_path.replace(".json", ".session.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            store.update(acc_id, chatgpt_status="registered", chatgpt_email=email,
                         chatgpt_password=chat_pw, chatgpt_session=state_path,
                         chatgpt_account_id=((sess or {}).get("account") or {}).get("id"))
            store.event(acc_id, "chatgpt", "info", f"注册完成 token_len={len(token)}")
        else:
            store.update(acc_id, chatgpt_status="session_missing", chatgpt_email=email,
                         chatgpt_password=chat_pw)
        log.info("token 长度 %d", len(token))
        print("EMAIL:", email, "PROTON_PW:", password, "CGPT_PW:", chat_pw)
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
