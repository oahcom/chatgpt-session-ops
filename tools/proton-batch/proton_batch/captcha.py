# -*- coding: utf-8 -*-
"""打码：2captcha / yescaptcha，支持 hCaptcha 与 Arkose FunCaptcha。

协议（实测口径）：
  2captcha      POST https://2captcha.com/in.php  (form)  -> {"status":1,"request":"<task_id>"}
                GET  https://2captcha.com/res.php?action=get&id=<task_id>&json=1
   yescaptcha   POST {endpoint}/createTask {"clientKey":..,"task":{...}}
                POST {endpoint}/getTaskResult {"clientKey":..,"taskId":..}
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import requests

from .logging_util import redact


class CaptchaError(RuntimeError):
    pass


class CaptchaSolver:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg["captcha"]
        self.ua = cfg["user_agent"]
        self.log = logger
        self.provider = (self.cfg.get("provider") or "2captcha").lower()
        self.api_key = self.cfg.get("api_key") or ""
        self.timeout = int(self.cfg.get("timeout_sec") or 240)
        self.interval = int(self.cfg.get("poll_interval_sec") or 5)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------ 对外

    def solve_hcaptcha(self, sitekey: str | None = None, pageurl: str | None = None,
                       proxy: str | None = None) -> str:
        sitekey = sitekey or self.cfg.get("hcaptcha_sitekey")
        pageurl = pageurl or self.cfg.get("hcaptcha_pageurl")
        if not sitekey:
            raise CaptchaError("缺少 hcaptcha_sitekey")
        self.log.info("求解 hCaptcha (provider=%s)", self.provider)
        if self.provider == "2captcha":
            task_id = self._2c_submit({
                "method": "hcaptcha",
                "sitekey": sitekey,
                "pageurl": pageurl,
                "userAgent": self.ua,
            })
            return self._2c_poll(task_id)
        return self._yes_submit_poll({
            "type": "HCaptchaTaskProxyless",
            "websiteURL": pageurl,
            "websiteKey": sitekey,
        })

    def solve_arkose(self, publickey: str | None = None, surl: str | None = None,
                     pageurl: str | None = None) -> str:
        publickey = publickey or self.cfg.get("arkose_publickey")
        surl = surl or self.cfg.get("arkose_surl")
        pageurl = pageurl or self.cfg.get("arkose_pageurl")
        if not publickey:
            raise CaptchaError("缺少 arkose_publickey")
        self.log.info("求解 Arkose FunCaptcha (provider=%s)", self.provider)
        if self.provider == "2captcha":
            task_id = self._2c_submit({
                "method": "funcaptcha",
                "publickey": publickey,
                "surl": surl,
                "pageurl": pageurl,
                "userAgent": self.ua,
            })
            return self._2c_poll(task_id)
        return self._yes_submit_poll({
            "type": "FunCaptchaTaskProxyless",
            "websiteURL": pageurl,
            "websitePublicKey": publickey,
            "funcaptchaApiJSSubdomain": surl,
        })

    def solve_any(self, solve: Callable[[], str], attempts: int = 3) -> str:
        last: Exception | None = None
        for i in range(attempts):
            try:
                return solve()
            except CaptchaError as exc:
                last = exc
                self.log.warning("打码失败(%d/%d): %s", i + 1, attempts, exc)
                time.sleep(2)
        raise CaptchaError(f"打码连续失败: {last}")

    # ------------------------------------------------------------ 2captcha

    def _2c_submit(self, data: dict) -> str:
        payload = {"key": self.api_key, "json": 1, **data}
        resp = requests.post("https://2captcha.com/in.php", data=payload, timeout=60)
        body = _json_or_text(resp)
        if not isinstance(body, dict) or str(body.get("status")) != "1":
            raise CaptchaError(f"2captcha 提交失败: {redact(str(body))}")
        task_id = str(body["request"])
        self.log.debug("2captcha task_id=%s", task_id)
        return task_id

    def _2c_poll(self, task_id: str) -> str:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(self.interval)
            resp = requests.get(
                "https://2captcha.com/res.php",
                params={"key": self.api_key, "action": "get", "id": task_id, "json": 1},
                timeout=60)
            body = _json_or_text(resp)
            if isinstance(body, dict):
                if str(body.get("status")) == "1":
                    return str(body["request"])
                if body.get("request") == "CAPCHA_NOT_READY":
                    continue
                raise CaptchaError(f"2captcha 失败: {redact(str(body))}")
        raise CaptchaError("2captcha 超时")

    # ------------------------------------------------------------ yescaptcha

    def _yes_submit_poll(self, task: dict) -> str:
        endpoint = self.cfg.get("yescaptcha_endpoint", "https://api.yescaptcha.com").rstrip("/")
        resp = requests.post(f"{endpoint}/createTask",
                             json={"clientKey": self.api_key, "task": task}, timeout=60)
        body = _json_or_text(resp)
        if not isinstance(body, dict) or body.get("errorId"):
            raise CaptchaError(f"yescaptcha 提交失败: {redact(str(body))}")
        task_id = body.get("taskId")
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(self.interval)
            resp = requests.post(f"{endpoint}/getTaskResult",
                                 json={"clientKey": self.api_key, "taskId": task_id}, timeout=60)
            body = _json_or_text(resp)
            if not isinstance(body, dict):
                continue
            if body.get("status") == "ready":
                solution = body.get("solution") or {}
                return str(solution.get("gRecaptchaResponse") or solution.get("token") or "")
            if body.get("errorId"):
                raise CaptchaError(f"yescaptcha 失败: {redact(str(body))}")
        raise CaptchaError("yescaptcha 超时")


def _json_or_text(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return resp.text
