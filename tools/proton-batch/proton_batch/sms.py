# -*- coding: utf-8 -*-
"""接码：sms-activate 协议。

  GET  .../handler_api.php?api_key=K&action=getNumber&service=protonmail&country=0
       -> ACCESS_NUMBER:<id>:<phone>  |  NO_NUMBERS  |  NO_BALANCE
  GET  ...&action=setStatus&status=1&id=<id>        # 已就绪
  GET  ...&action=getStatus&id=<id>                 # STATUS_WAIT_CODE / STATUS_OK:<code>
  GET  ...&action=setStatus&status=6&id=<id>        # 完成
  GET  ...&action=setStatus&status=8&id=<id>        # 取消
"""
from __future__ import annotations

import logging
import time

import requests

from .logging_util import redact


class SmsError(RuntimeError):
    pass


class SmsProvider:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg["sms"]
        self.log = logger
        self.api_key = self.cfg.get("api_key") or ""
        self.endpoint = self.cfg.get("endpoint", "https://api.sms-activate.org/stubs/handler_api.php")
        self.timeout = int(self.cfg.get("timeout_sec") or 600)
        self.interval = int(self.cfg.get("poll_interval_sec") or 10)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _api(self, **params) -> str:
        params = {"api_key": self.api_key, **params}
        resp = requests.get(self.endpoint, params=params, timeout=60)
        return resp.text.strip()

    def acquire(self, service: str | None = None, country: int | None = None) -> tuple[str, str]:
        service = service or self.cfg.get("service", "protonmail")
        country = self.cfg.get("country", 0) if country is None else country
        for _ in range(3):
            text = self._api(action="getNumber", service=service, country=country)
            if text.startswith("ACCESS_NUMBER:"):
                _, order_id, phone = text.split(":", 2)
                self.log.info("接码号码就绪 id=%s phone=%s****", order_id, phone[:4])
                self._api(action="setStatus", status=1, id=order_id)
                return order_id, phone
            if text == "NO_BALANCE":
                raise SmsError("sms-activate 余额不足")
            if text in ("NO_NUMBERS", "NO_NUMBER"):
                time.sleep(3)
                continue
            raise SmsError(f"取号失败: {redact(text)}")
        raise SmsError("取号失败：连续无可用号码")

    def wait_code(self, order_id: str) -> str:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(self.interval)
            text = self._api(action="getStatus", id=order_id)
            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                self.log.info("收到短信验证码 id=%s", order_id)
                return code
            if text in ("STATUS_WAIT_CODE", "STATUS_WAIT_RETRY"):
                continue
            if text in ("STATUS_CANCEL", "NO_ACTIVATION"):
                raise SmsError(f"订单失效: {text}")
        raise SmsError("等待短信验证码超时")

    def finish(self, order_id: str) -> None:
        try:
            self._api(action="setStatus", status=6, id=order_id)
        except requests.RequestException:
            pass

    def cancel(self, order_id: str) -> None:
        try:
            self._api(action="setStatus", status=8, id=order_id)
        except requests.RequestException:
            pass
