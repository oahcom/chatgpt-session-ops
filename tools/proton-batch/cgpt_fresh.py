# -*- coding: utf-8 -*-
"""对指定新账号做 ChatGPT 注册（不换节点，用当前出口；Proton 首次登录读码）。

用法：python cgpt_fresh.py <account_id>
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, r"D:\code\chatgpt-session-ops\tools\proton-batch")

import cgpt_runner as R
from playwright.sync_api import sync_playwright
from proton_batch.store import Store, decrypt_secret

acc = int(sys.argv[1])
store = Store("data/accounts.db")
row = next(r for r in store.list(limit=3000) if r["id"] == acc)
email = row["proton_email"]
R.log.info("账号 %s（id=%s）", email, acc)

pm = R.pm_login(email, decrypt_secret(row["proton_password"] or ""))
R.log.info("Proton API 首次登录成功")

chat_pw = f"Qz{os.urandom(5).hex().capitalize()}!7a"
state_path = f"{R.WORK}/cgpt_{acc}.json"

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=False, proxy=R.PROXY,
                          args=["--disable-blink-features=AutomationControlled"])
    token, sess = R.register(b, email, lambda: R.wait_code(pm), chat_pw, state_path)
    b.close()

if token:
    store.update(acc, chatgpt_status="registered", chatgpt_email=email,
                 chatgpt_password=chat_pw, chatgpt_session=state_path,
                 chatgpt_account_id=((sess or {}).get("account") or {}).get("id"),
                 chatgpt_error=None)
    store.event(acc, "chatgpt", "info", f"注册完成 token_len={len(token)}")
    R.log.info("成功，token %d", len(token))
else:
    store.update(acc, chatgpt_status="session_missing", chatgpt_error="无 accessToken")
    R.log.warning("流程完成但没有 accessToken")
