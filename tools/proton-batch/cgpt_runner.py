# -*- coding: utf-8 -*-
"""批量注册 ChatGPT：每账号 = 新出口 + 无痕上下文 + 单次 Proton API 登录。

纪律（用户口径）：
- 每次注册换一条新出口节点，节点用过即记入 used_nodes.json 不再复用；
- 浏览器一律独立 new_context()（无痕，无历史 cookie）；
- 已风控的账号直接丢弃，记入 discarded.json，不再重试。

用法：python cgpt_runner.py [max_accounts]
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
log = logging.getLogger("runner")

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from proton_batch.store import Store, decrypt_secret
from proton_batch.clash import Clash, is_node
from proton_batch.config import load_config

PROXY = {"server": "http://127.0.0.1:7000"}
WORK = "data/work"
USED_NODES = f"{WORK}/used_nodes.json"
DISCARDED = f"{WORK}/discarded.json"
VER_ACCOUNT = "web-account@5.0.445.0"
VER_MAIL = "web-mail@5.0.143.7"


def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _save(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def pick_node(clash: Clash) -> tuple[str, str]:
    """取一条没用过、且真能连到 Proton 的出口。"""
    import requests

    used = set(_load(USED_NODES, []))
    names = [n for n in clash.members() if is_node(n)]
    cur = clash.current()
    start = names.index(cur) + 1 if cur in names else 0
    for k in range(len(names)):
        name = names[(start + k) % len(names)]
        if name in used:
            continue
        if not clash.switch(name):
            continue
        ip = clash.egress_ip()
        if not ip or str(ip).startswith("err"):
            continue
        try:
            r = requests.get("https://account.proton.me/", timeout=12,
                             proxies={"http": PROXY["server"], "https": PROXY["server"]})
            if r.status_code >= 500:
                continue
        except Exception:  # noqa: BLE001 - 这条出口到不了 Proton
            continue
        used.add(name)
        _save(USED_NODES, sorted(used))
        return name, str(ip)
    raise RuntimeError("没有可用出口了")


def pm_login(email: str, password: str):
    import protonmail.constants as C
    import protonmail.client as PC
    from protonmail import ProtonMail

    for mod in (C, PC):
        setattr(mod, "PM_APP_VERSION_ACCOUNT", VER_ACCOUNT)
        if hasattr(mod, "PM_APP_VERSION_MAIL"):
            setattr(mod, "PM_APP_VERSION_MAIL", VER_MAIL)
    C.DEFAULT_HEADERS["x-pm-appversion"] = VER_ACCOUNT

    pm = ProtonMail()
    pm.session.headers["x-pm-appversion"] = VER_ACCOUNT
    pm.session.proxies = {"http": PROXY["server"], "https": PROXY["server"]}
    pm.login(email, password)          # 只登一次，不换节点重试
    return pm


def wait_code(pm, timeout: int = 240) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msgs = pm.get_messages()
        except Exception as exc:  # noqa: BLE001
            log.warning("取信列表失败: %s", str(exc)[:80])
            time.sleep(8)
            continue
        for m in (msgs or [])[:3]:
            subj = getattr(m, "subject", "") or ""
            sender = str(getattr(getattr(m, "sender", None), "address", "") or "")
            if not re.search(r"openai|code|verif", f"{sender} {subj}", re.I):
                continue
            try:
                body = getattr(pm.read_message(m), "body", "") or ""
            except Exception as exc:  # noqa: BLE001
                log.warning("读信失败: %s", str(exc)[:80])
                continue
            hit = re.search(r"(?<!\d)(\d{6})(?!\d)", body)
            if hit:
                log.info("验证码 %s（%s | %s）", hit.group(1), sender[:30], subj[:40])
                return hit.group(1)
        time.sleep(8)
    raise TimeoutError("收件箱 240s 内无验证码")


def dump(pg, tag):
    try:
        print(f"  [{tag}] {pg.url[:82]}")
        print("   btn:", pg.eval_on_selector_all(
            "button", "els => els.filter(e => e.offsetWidth>0).map(e => (e.innerText||'').trim().slice(0,18)).filter(Boolean)"))
        print("   txt:", pg.inner_text("body")[:200].replace("\n", " | "))
    except Exception as exc:  # noqa: BLE001
        print(f"  [{tag}] dump 失败 {str(exc)[:60]}")


def register(browser, email: str, code_getter, chat_pw: str, state_path: str):
    ctx = browser.new_context(locale="en-US", timezone_id="America/New_York",
                              viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    try:
        pg.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        pg.mouse.wheel(0, 400)
        time.sleep(2)
        pg.get_by_role("button", name="Sign up for free", exact=False).first.click(timeout=25000)
        pg.wait_for_selector("#mobile-auth-email", state="visible", timeout=60000)
        pg.fill("#mobile-auth-email", email)
        pg.get_by_role("button", name="Continue", exact=True).first.click(timeout=20000)
        log.info("已提交邮箱")
        pg.wait_for_selector('input[name="code"]', timeout=90000)
        dump(pg, "OTP 页")

        code = code_getter()
        pg.bring_to_front()
        pg.fill('input[name="code"]', code)
        pg.get_by_role("button", name="Continue", exact=True).first.click(timeout=20000)
        time.sleep(9)
        dump(pg, "验证后")

        if pg.query_selector('input[type="password"]'):
            pg.fill('input[type="password"]', chat_pw)
            try:
                pg.get_by_role("button", name="Continue", exact=True).first.click(timeout=8000)
            except PWTimeout:
                pg.keyboard.press("Enter")
            time.sleep(9)
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
            time.sleep(9)
            dump(pg, "资料后")

        pg.wait_for_url(re.compile(r"chatgpt\.com/?$"), timeout=120000)
        time.sleep(6)
        dump(pg, "完成")
        sess = pg.evaluate("""async () => {
            const r = await fetch('/api/auth/session', {headers: {'Accept': 'application/json'}});
            return await r.json();
        }""")
        token = (sess or {}).get("accessToken") or ""
        ctx.storage_state(path=state_path)
        if token:
            open(state_path.replace(".json", ".token"), "w", encoding="utf-8").write(token)
            json.dump(sess, open(state_path.replace(".json", ".session.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        return token, sess
    finally:
        ctx.close()


def main(limit: int) -> int:
    store = Store("data/accounts.db")
    clash = Clash(load_config(None), log)
    discarded = _load(DISCARDED, {})

    rows = [r for r in store.list(limit=3000)
            if r["proton_status"] == "verified" and (r.get("chatgpt_status") or "none") == "none"
            and str(r["id"]) not in discarded]
    rows.sort(key=lambda r: r["id"])
    log.info("候选账号 %d 个: %s", len(rows), [r["id"] for r in rows][:limit])

    done = 0
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=False, proxy=PROXY,
                              args=["--disable-blink-features=AutomationControlled"])
        for row in rows[:limit]:
            acc = row["id"]
            email = row["proton_email"]
            log.info("===== id=%s %s =====", acc, email)
            pm = None
            for attempt in range(3):
                try:
                    node, ip = pick_node(clash)
                except RuntimeError as exc:
                    log.error("出口耗尽: %s", exc)
                    break
                log.info("出口 %s (%s)", node[:34], ip)
                try:
                    pm = pm_login(email, decrypt_secret(row["proton_password"] or ""))
                    break
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)[:140]
                    if "Too many recent logins" in msg:
                        log.error("Proton 风控限流: %s", msg[:90])
                        discarded[str(acc)] = {"reason": "proton_login_limited", "ts": time.strftime("%F %T")}
                        _save(DISCARDED, discarded)
                        store.update(acc, chatgpt_status="discarded", chatgpt_error=msg)
                        store.event(acc, "chatgpt", "error", f"丢弃: {msg[:120]}")
                        break
                    log.warning("出口不可用（%s），换节点重试", msg[:70])
            if pm is None:
                continue

            chat_pw = f"Qz{os.urandom(5).hex().capitalize()}!7a"
            state_path = f"{WORK}/cgpt_{acc}.json"
            try:
                token, sess = register(b, email, lambda: wait_code(pm), chat_pw, state_path)
            except Exception as exc:  # noqa: BLE001
                msg = f"{exc.__class__.__name__}: {str(exc)[:120]}"
                log.error("注册失败: %s", msg)
                if "TimeoutError" in msg or "验证码" in msg:
                    discarded[str(acc)] = {"reason": "收件箱无验证码（疑似邮箱风控）",
                                           "ts": time.strftime("%F %T")}
                    _save(DISCARDED, discarded)
                    store.update(acc, chatgpt_status="discarded", chatgpt_error=msg)
                    store.event(acc, "chatgpt", "error", f"丢弃: {msg}")
                else:
                    store.update(acc, chatgpt_status="failed", chatgpt_error=msg)
                    store.event(acc, "chatgpt", "error", msg)
                continue

            if token:
                store.update(acc, chatgpt_status="registered", chatgpt_email=email,
                             chatgpt_password=chat_pw, chatgpt_session=state_path,
                             chatgpt_account_id=((sess or {}).get("account") or {}).get("id"),
                             chatgpt_error=None)
                store.event(acc, "chatgpt", "info", f"注册完成 node={node[:30]}")
                log.info("注册成功: %s（token %d）", email, len(token))
                done += 1
            else:
                store.update(acc, chatgpt_status="session_missing",
                             chatgpt_error="流程走完但无 accessToken")
                store.event(acc, "chatgpt", "warn", "完成但无 accessToken")
        b.close()
    log.info("本轮成功 %d 个", done)
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2))
