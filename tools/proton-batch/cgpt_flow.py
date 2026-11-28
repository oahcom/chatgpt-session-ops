# -*- coding: utf-8 -*-
"""ChatGPT 注册全流程（官方 UI + Proton API 收码 + 无痕上下文 + 换出口）。

不碰任何私有注册接口；收码用 protonmail-api-client（SRP + PGP 由库完成）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("flow")

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from proton_batch.store import Store, decrypt_secret
from proton_batch.clash import Clash, is_node
from proton_batch.config import load_config

PROXY = {"server": "http://127.0.0.1:7000"}
WORK = "data/work"
VER_ACCOUNT = "web-account@5.0.445.0"
VER_MAIL = "web-mail@5.0.143.7"


def pick_node() -> str:
    c = Clash(load_config(None), logging.getLogger("pick"))
    names = [n for n in c.members() if is_node(n)]
    cur = c.current()
    start = names.index(cur) + 1 if cur in names else 0
    for k in range(min(len(names), 15)):
        name = names[(start + k) % len(names)]
        if c.switch(name):
            ip = c.egress_ip()
            if ip and not str(ip).startswith("err"):
                return f"{name[:32]} ({ip})"
    return "?"


def pm_login(email: str, password: str):
    import protonmail.constants as C
    import protonmail.client as PC
    from protonmail import ProtonMail

    for mod in (C, PC):
        setattr(mod, "PM_APP_VERSION_ACCOUNT", VER_ACCOUNT)
        if hasattr(mod, "PM_APP_VERSION_MAIL"):
            setattr(mod, "PM_APP_VERSION_MAIL", VER_MAIL)
    C.DEFAULT_HEADERS["x-pm-appversion"] = VER_ACCOUNT

    hosts = ["https://account.proton.me/api", "https://mail.proton.me/api",
             "https://api.protonmail.ch/api"]
    last = None
    for attempt, host in enumerate(hosts * 2):
        C.urls_api["mail"] = host
        PC.urls_api["mail"] = host
        pm = ProtonMail()
        pm.session.headers["x-pm-appversion"] = VER_ACCOUNT
        try:
            pm.login(email, password)
            log.info("Proton API 入口: %s", host)
            return pm
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("登录失败[%s @ %s]: %s", attempt, host, str(exc)[:90])
            if "Too many recent logins" in str(exc):
                raise
            pick_node()
            time.sleep(3)
    raise last


def wait_code(pm, since: float, timeout: int = 240) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msgs = pm.get_messages()
        except Exception as exc:  # noqa: BLE001
            log.warning("取信列表失败: %s", str(exc)[:90])
            time.sleep(6)
            continue
        if msgs:
            m = msgs[0]
            subj = getattr(m, "subject", "") or ""
            sender = str(getattr(getattr(m, "sender", None), "address", "") or "")
            if "openai" in sender.lower() or "openai" in subj.lower() or "code" in subj.lower():
                try:
                    full = pm.read_message(m)
                    body = getattr(full, "body", "") or ""
                except Exception as exc:  # noqa: BLE001
                    log.warning("读信失败: %s", str(exc)[:90])
                    body = ""
                hit = re.search(r"(?<!\d)(\d{6})(?!\d)", body or subj)
                if hit:
                    log.info("验证码来自: %s | %s", sender[:40], subj[:50])
                    return hit.group(1)
        time.sleep(6)
    raise TimeoutError("Proton 收件箱未见验证码")


def dump(pg, tag):
    try:
        print(f"  [{tag}] {pg.url[:80]}")
        print("   buttons:", pg.eval_on_selector_all(
            "button", "els => els.filter(e => e.offsetWidth>0).map(e => (e.innerText||'').trim().slice(0,20)).filter(Boolean)"))
        print("   body:", pg.inner_text("body")[:220].replace("\n", " | "))
    except Exception as exc:  # noqa: BLE001
        print(f"  [{tag}] dump 失败 {str(exc)[:70]}")


def main(account_id: int) -> int:
    store = Store("data/accounts.db")
    row = next(r for r in store.list(limit=3000) if r["id"] == account_id)
    email = row["proton_email"]
    proton_pw = decrypt_secret(row["proton_password"] or "")
    chat_pw = f"Qz{os.urandom(5).hex().capitalize()}!7a"
    log.info("账号 %s（id=%s）", email, account_id)
    if os.environ.get("CGPT_PIN_NODE"):
        c = Clash(load_config(None), log)
        log.info("出口固定: %s (%s)", c.current(), c.egress_ip())
    else:
        log.info("出口: %s", pick_node())

    pm = pm_login(email, proton_pw)
    log.info("Proton API 登录成功")

    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=False, proxy=PROXY,
                              args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(locale="en-US", timezone_id="America/New_York",
                            viewport={"width": 1280, "height": 900})
        pg = ctx.new_page()
        pg.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        pg.mouse.wheel(0, 500)
        time.sleep(2)

        pg.get_by_role("button", name="Sign up for free", exact=False).first.click(timeout=25000)
        pg.wait_for_selector("#mobile-auth-email", state="visible", timeout=60000)
        pg.fill("#mobile-auth-email", email)
        pg.get_by_role("button", name="Continue", exact=True).first.click(timeout=20000)
        log.info("已提交邮箱")
        pg.wait_for_selector('input[name="code"]', timeout=90000)
        dump(pg, "OTP 页")

        code = wait_code(pm, time.time())
        log.info("收到验证码 %s", code)
        pg.bring_to_front()
        pg.fill('input[name="code"]', code)
        pg.get_by_role("button", name="Continue", exact=True).first.click(timeout=20000)
        time.sleep(8)
        dump(pg, "验证后")

        if pg.query_selector('input[type="password"]'):
            pg.fill('input[type="password"]', chat_pw)
            try:
                pg.get_by_role("button", name="Continue", exact=True).first.click(timeout=8000)
            except PWTimeout:
                pg.keyboard.press("Enter")
            time.sleep(8)
            dump(pg, "密码后")

        for _ in range(4):
            name_in = pg.query_selector('input[name="name"]')
            bd_in = pg.query_selector('input[name="birthdate"]')
            if not (name_in or bd_in):
                break
            if name_in:
                pg.fill('input[name="name"]', "Alex Chen")
            if bd_in:
                pg.fill('input[name="birthdate"]', "1994-06-12")
            try:
                pg.get_by_role("button", name="Continue", exact=True).first.click(timeout=8000)
            except PWTimeout:
                pg.keyboard.press("Enter")
            time.sleep(8)
            dump(pg, "资料后")

        pg.wait_for_url(re.compile(r"chatgpt\.com/?$"), timeout=120000)
        time.sleep(6)
        dump(pg, "完成")

        sess = pg.evaluate("""async () => {
            const r = await fetch('/api/auth/session', {headers: {'Accept': 'application/json'}});
            return await r.json();
        }""")
        token = (sess or {}).get("accessToken") or ""

        os.makedirs(WORK, exist_ok=True)
        state_path = f"{WORK}/cgpt_{account_id}.json"
        ctx.storage_state(path=state_path)
        if token:
            open(f"{WORK}/cgpt_{account_id}.token", "w", encoding="utf-8").write(token)
            json.dump(sess, open(f"{WORK}/cgpt_{account_id}.session.json", "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)

        store.update(account_id,
                     chatgpt_status="registered" if token else "session_missing",
                     chatgpt_email=email,
                     chatgpt_password=chat_pw,
                     chatgpt_session=state_path,
                     chatgpt_account_id=((sess or {}).get("account") or {}).get("id"),
                     chatgpt_error=None)
        store.event(account_id, "chatgpt", "info", f"UI 注册完成 token_len={len(token)}")
        log.info("token 长度 %d；state -> %s", len(token), state_path)
        ctx.close()
        b.close()
        return 0 if token else 1


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 12))
