# -*- coding: utf-8 -*-
"""Proton 邮箱 UI 收码（不依赖 API token / IMAP / Bridge）。

为什么走 UI：Proton 的 AUTH cookie 是 HttpOnly，页面内 fetch 拿不到 uid；
API 直连又需要合法 appversion（实测陈旧版本回 5003）。
浏览器里已经登录的会话反而最可靠：打开收件箱、点最新邮件、读正文取码。

适用前提：浏览器当前登录的就是目标账号（run_all 串行注册时天然成立）。
"""
from __future__ import annotations

import logging
import re
import time

from .browser_driver import BrowseDriver

CODE_PATTERNS = [
    r"(?:code|验证码|verification code)[^\d]{0,40}(\d{6})",
    r"\b(\d{6})\b",
]


class ProtonWebMail:
    def __init__(self, cfg: dict, driver: BrowseDriver, logger: logging.Logger,
                 username: str | None = None, password: str | None = None):
        self.cfg = cfg["proton"]
        self.driver = driver
        self.log = logger
        self.username = username
        self.password = password
        self._open = False

    def open_inbox(self) -> None:
        if self._open:
            return
        if self.username and self.password:
            from .proton_login import login
            if not login(self.driver, self.username, self.password, self.log):
                self.log.warning("Proton 登录失败，收件箱可能不可用")
        self.driver.navigate("https://mail.proton.me/u/0/inbox")
        self._open = True
        time.sleep(3)

    def refresh(self) -> None:
        self.driver.eval_js("""(function(){
            const b = document.querySelector('[data-testid="navigation-link:refresh-folder"]');
            if (b) { b.click(); return 'refreshed'; }
            return 'no-refresh';
        })()""")

    def _open_latest(self) -> str:
        return str(self.driver.eval_js("""(async () => {
            const rows = [...document.querySelectorAll('.item-container')];
            if (!rows.length) return 'NO_ROWS';
            const row = rows[0];
            const link = row.querySelector('a') || row;
            link.click();
            await new Promise(r => setTimeout(r, 2500));
            return 'OPENED';
        })()"""))

    def _body_text(self) -> str:
        return str(self.driver.eval_js("""(function(){
            const nodes = document.querySelectorAll('[data-testid*="message-content"], .message-content, article');
            if (nodes.length) return [...nodes].map(n => n.innerText).join('\\n').slice(0, 8000);
            return (document.body.innerText || '').slice(0, 8000);
        })()"""))

    def wait_code(self, since_ts: float | None = None, timeout: int = 180,
                  pattern: str | None = None) -> str:
        self.open_inbox()
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            self.refresh()
            time.sleep(3)
            self._open_latest()
            text = self._body_text()
            if text and text != last:
                last = text
                for pat in ([pattern] if pattern else CODE_PATTERNS):
                    hit = re.search(pat, text, re.I)
                    if hit:
                        self.log.info("从 Proton 邮件取到验证码")
                        return hit.group(1)
            time.sleep(4)
        raise TimeoutError(f"Proton 邮箱 {timeout}s 内未取到验证码")
