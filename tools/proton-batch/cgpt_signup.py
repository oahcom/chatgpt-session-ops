# -*- coding: utf-8 -*-
"""ChatGPT 注册（官方 UI）+ 无痕上下文 + 独立出口 + 凭据留存。

流程：signup -> email -> OTP(Proton 网页) -> 密码 -> 姓名/生日 -> chatgpt.com
产物：storage_state（cookie）、access_token、session json 落盘，并写库。

说明：不调用任何私有注册接口；全部走页面真实输入与真实点击。
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
log = logging.getLogger("signup")

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from proton_batch.store import Store, decrypt_secret
from proton_batch.clash import Clash
from proton_batch.config import load_config

PROXY = {"server": "http://127.0.0.1:7000"}
WORK = "data/work"


def dump(pg, tag):
    try:
        print(f"===== {tag} =====\nURL: {pg.url}")
        print("inputs:", pg.eval_on_selector_all(
            "input", "els => els.filter(e => e.offsetWidth > 0).map(e => e.id + '|' + e.name + '|' + e.type)"))
        print("buttons:", pg.eval_on_selector_all(
            "button", "els => els.filter(e => e.offsetWidth > 0).map(e => (e.innerText || '').trim().slice(0, 24)).filter(Boolean)"))
        print("body:", pg.inner_text("body")[:400].replace("\n", " | "))
    except Exception as exc:  # noqa: BLE001
        print(f"[{tag}] dump 失败: {str(exc)[:100]}")


def proton_inbox_code(browser, email: str, password: str) -> str:
    """独立上下文登录 Proton 并读最新邮件的 6 位码。"""
    ctx = browser.new_context(locale="en-US", viewport={"width": 1400, "height": 950})
    pg = ctx.new_page()
    try:
        pg.goto("https://account.proton.me/login", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector("#username", timeout=45000)
        pg.fill("#username", email)
        pg.get_by_role("button", name="Continue", exact=True).click(timeout=20000)
        pg.wait_for_selector("#password", timeout=45000)
        pg.fill("#password", password)
        pg.get_by_role("button", name="Sign in", exact=True).click(timeout=20000)
        time.sleep(8)

        pg.goto("https://mail.proton.me/u/0/inbox", wait_until="domcontentloaded", timeout=60000)
        # 列表先渲染 placeholder，真正邮件行出现才有 data-element-id
        pg.wait_for_selector('.item-container:not([data-element-id*="placeholder"])', timeout=180000)
        time.sleep(2)
        rows = pg.query_selector_all('.item-container:not([data-element-id*="placeholder"])')
        log.info("收件箱可见邮件: %d 封", len(rows))
        rows[0].click()
        time.sleep(6)
        text = pg.inner_text("body")
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        if not m:
            text = pg.eval_on_selector_all(
                '[data-testid="message-content:body"], .message-content',
                "els => els.map(e => e.innerText || '').join('\\n')")[0] or text
            m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        if not m:
            raise TimeoutError("邮件正文里没有 6 位码")
        return m.group(1)
    finally:
        ctx.close()


def main(account_id: int) -> int:
    cfg = load_config(None)
    store = Store("data/accounts.db")
    row = next(r for r in store.list(limit=3000) if r["id"] == account_id)
    email = row["proton_email"]
    proton_pw = decrypt_secret(row["proton_password"] or "")
    chat_pw = decrypt_secret(row.get("chatgpt_password") or "") or ""
    if not chat_pw:
        chat_pw = "Pg" + os.urandom(6).hex().capitalize() + "!9"
    log.info("账号 %s（id=%s）", email, account_id)

    node = Clash(cfg, log).next_node()
    log.info("出口 -> %s", node)
    log.info("出口 IP -> %s", Clash(cfg, log).egress_ip())

    os.makedirs(WORK, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=False, proxy=PROXY,
                              args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(locale="en-US", timezone_id="America/New_York",
                            viewport={"width": 1280, "height": 900})
        pg = ctx.new_page()
        pg.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        pg.mouse.wheel(0, 400)
        time.sleep(2)

        pg.get_by_role("button", name="Sign up for free", exact=False).first.click(timeout=20000)
        pg.wait_for_selector('#mobile-auth-email', state="visible", timeout=45000)
        pg.fill("#mobile-auth-email", email)
        pg.get_by_role("button", name="Continue", exact=True).first.click(timeout=15000)
        log.info("已提交邮箱")

        pg.wait_for_selector('input[name="code"]', timeout=90000)
        dump(pg, "OTP 页")

        code = proton_inbox_code(b, email, proton_pw)
        log.info("收到验证码 %s", code)

        pg.bring_to_front()
        pg.fill('input[name="code"]', code)
        pg.get_by_role("button", name="Continue", exact=True).first.click(timeout=15000)
        log.info("已提交验证码")
        time.sleep(10)
        dump(pg, "验证后")

        # 密码页
        if pg.query_selector('input[type="password"]'):
            pg.fill('input[type="password"]', chat_pw)
            try:
                pg.get_by_role("button", name="Continue", exact=True).first.click(timeout=8000)
            except PWTimeout:
                pg.keyboard.press("Enter")
            log.info("已设置密码")
            time.sleep(8)
            dump(pg, "密码后")

        # 姓名/生日
        for _ in range(3):
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

        pg.wait_for_url(re.compile(r"chatgpt\.com/?$"), timeout=90000)
        time.sleep(6)
        dump(pg, "完成")

        sess = pg.evaluate("""async () => {
            const r = await fetch('/api/auth/session', {headers: {'Accept': 'application/json'}});
            return await r.json();
        }""")
        token = (sess or {}).get("accessToken") or ""

        state_path = f"{WORK}/cgpt_{account_id}.json"
        ctx.storage_state(path=state_path)

        if token:
            with open(f"{WORK}/cgpt_{account_id}.token", "w", encoding="utf-8") as f:
                f.write(token)
            with open(f"{WORK}/cgpt_{account_id}.session.json", "w", encoding="utf-8") as f:
                json.dump(sess, f, ensure_ascii=False, indent=1)
        log.info("access_token 长度 %d；storage_state -> %s", len(token), state_path)

        store.update(account_id,
                     chatgpt_status="registered" if token else "session_missing",
                     chatgpt_email=email,
                     chatgpt_password=chat_pw,
                     chatgpt_session=state_path,
                     chatgpt_account_id=((sess or {}).get("account") or {}).get("id"),
                     chatgpt_error=None)
        store.event(account_id, "chatgpt", "info",
                    f"UI 注册完成 token_len={len(token)} node={node}")

        ctx.close()
        b.close()
        return 0 if token else 1


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 60))
