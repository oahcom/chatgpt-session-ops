# -*- coding: utf-8 -*-
"""登出当前 Proton 会话。

注册之间必须登出：已登录态会让后续注册先命中「检测到已登录态」，
且 HV 方式退化成 SMS/Manual（不再给 Email）。
"""
from __future__ import annotations

import logging
import re
import time

from .browser_driver import BrowseDriver, BrowserError


def logout(d: BrowseDriver, log: logging.Logger) -> bool:
    """打开账号菜单点登出；返回是否已离开登录态。"""
    try:
        d.navigate("https://account.proton.me/u/0/dashboard")
    except BrowserError as exc:
        log.warning("打开账号页失败: %s", str(exc)[:90])
        return False
    time.sleep(5)

    for _ in range(3):
        try:
            snap = d.snapshot()
        except BrowserError as exc:
            log.warning("snapshot 失败: %s", str(exc)[:90])
            time.sleep(3)
            continue
        line = next((ln for ln in snap.splitlines() if "@proton.me" in ln), None)
        if line:
            m = re.search(r"\[ref=(e\d+)", line)
            if m:
                try:
                    d.click_ref(m.group(1))
                    time.sleep(2.5)
                    break
                except BrowserError as exc:
                    log.warning("点账号菜单失败: %s", str(exc)[:90])
        time.sleep(2)

    for label in ("Sign out", "Log out"):
        try:
            d.click_sel(f'button:has-text("{label}"), a:has-text("{label}")')
            time.sleep(5)
            url = d.page_state().get("url") or ""
            log.info("已登出，当前 URL: %s", url[:80])
            return "/u/" not in url
        except BrowserError:
            continue

    log.warning("找不到登出按钮")
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    driver = BrowseDriver(logging.getLogger("logout"), None, "edge-this-computer")
    raise SystemExit(0 if logout(driver, logging.getLogger("logout")) else 1)
