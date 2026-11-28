# -*- coding: utf-8 -*-
"""Proton 网页登录（两步表单）。真实键盘输入，不用 JS 赋值。"""
from __future__ import annotations

import logging
import time

from .browser_driver import BrowseDriver, BrowserError

SUBMIT_SELS = ('button[type="submit"]', 'button:has-text("继续")',
               'button:has-text("Continue")', 'button:has-text("Sign in")',
               'button:has-text("登录")')


def _submit(d: BrowseDriver, log: logging.Logger) -> bool:
    for sel in SUBMIT_SELS:
        try:
            d.click_sel(sel)
            return True
        except BrowserError:
            continue
    log.warning("找不到提交按钮")
    return False


def login(d: BrowseDriver, username: str, password: str, log: logging.Logger,
          timeout: int = 90) -> bool:
    """登录 account.proton.me；已登录同一账号则直接返回 True。"""
    try:
        d.navigate("https://account.proton.me/login")
    except BrowserError as exc:
        log.warning("打开登录页失败: %s", str(exc)[:90])
        return False
    time.sleep(4)

    url = d.page_state().get("url") or ""
    if "/u/" in url:
        log.info("已是登录态")
        return True

    try:
        d.fill_sel("#username", username)
        time.sleep(0.5)
        _submit(d, log)
    except BrowserError as exc:
        log.warning("填用户名失败: %s", str(exc)[:110])
        return False

    # 第二步：密码
    for _ in range(15):
        time.sleep(2)
        try:
            d.fill_sel("#password", password)
            break
        except BrowserError:
            continue
    else:
        log.warning("密码框未出现，当前 URL: %s", (d.page_state().get("url") or "")[:80])
        return False

    time.sleep(0.5)
    _submit(d, log)

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        url = d.page_state().get("url") or ""
        if "/u/" in url:
            log.info("登录成功: %s", url[:70])
            return True
    log.warning("登录超时，当前 URL: %s", (d.page_state().get("url") or "")[:80])
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    driver = BrowseDriver(logging.getLogger("login"), None, "edge-this-computer")
    raise SystemExit(0)
