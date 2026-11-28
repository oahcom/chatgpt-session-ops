# -*- coding: utf-8 -*-
"""临时邮箱：mail.tm（Proton 注册时的 recovery email 收码用）。

协议：
    GET  /domains                      -> 可用域名
    POST /accounts {address,password}  -> 建箱
    POST /token    {address,password}  -> Bearer token
    GET  /messages                     -> 列表
    GET  /messages/{id}                -> 正文
"""
from __future__ import annotations

import logging
import random
import re
import string
import time

import requests

from .proxy import ProxyPool, to_requests_map


class TempMailError(RuntimeError):
    pass


class MailTm:
    def __init__(self, logger: logging.Logger, pool: ProxyPool | None = None,
                 timeout: int = 25):
        self.log = logger
        self.timeout = timeout
        self.proxies = to_requests_map(pool.acquire()) if pool else {}
        self.address = ""
        self.password = ""
        self.token = ""
        self._seen: set[str] = set()

    def create(self, prefix: str = "oahcom") -> str:
        r = requests.get("https://api.mail.tm/domains", timeout=self.timeout,
                         proxies=self.proxies,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        domains = (r.json().get("hydra:member") or [])
        if not domains:
            raise TempMailError("mail.tm 无可用域名")
        domain = domains[0]["domain"]
        self.address = f"{prefix}{random.randint(100000, 999999)}@{domain}"
        self.password = "Pw-" + "".join(random.choices(string.ascii_letters + string.digits, k=18))

        r2 = requests.post("https://api.mail.tm/accounts",
                           json={"address": self.address, "password": self.password},
                           timeout=self.timeout, proxies=self.proxies,
                           headers={"Content-Type": "application/json",
                                    "User-Agent": "Mozilla/5.0"})
        if r2.status_code >= 400:
            raise TempMailError(f"建箱失败 {r2.status_code}: {r2.text[:200]}")

        r3 = requests.post("https://api.mail.tm/token",
                           json={"address": self.address, "password": self.password},
                           timeout=self.timeout, proxies=self.proxies,
                           headers={"Content-Type": "application/json",
                                    "User-Agent": "Mozilla/5.0"})
        r3.raise_for_status()
        self.token = r3.json()["token"]
        self.log.info("临时邮箱: %s", self.address)
        return self.address

    def wait_code(self, since_ts: float | None = None, pattern: str | None = None,
                  timeout: int = 180) -> str:
        pattern = pattern or r"\b(\d{6})\b"
        deadline = time.time() + timeout
        headers = {"Authorization": "Bearer " + self.token, "User-Agent": "Mozilla/5.0"}
        while time.time() < deadline:
            time.sleep(5)
            try:
                r = requests.get("https://api.mail.tm/messages", headers=headers,
                                 timeout=self.timeout, proxies=self.proxies)
                for msg in r.json().get("hydra:member", []):
                    if msg["id"] in self._seen:
                        continue
                    d = requests.get(f"https://api.mail.tm/messages/{msg['id']}",
                                     headers=headers, timeout=self.timeout,
                                     proxies=self.proxies).json()
                    body = " ".join([d.get("subject") or ""]
                                    + (d.get("text") or []) + (d.get("html") or []))
                    hit = re.search(pattern, body)
                    self._seen.add(msg["id"])
                    if hit:
                        return hit.group(1)
            except requests.RequestException:
                continue
        raise TempMailError(f"临时邮箱 {self.address} 未收到验证码")
