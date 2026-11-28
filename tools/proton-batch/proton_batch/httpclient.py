# -*- coding: utf-8 -*-
"""HTTP 客户端：统一出口代理、重试、JSON、限速退避。"""
from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests

from .logging_util import redact
from .proxy import ProxyPool, to_requests_map


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class Http:
    def __init__(self, cfg: dict, pool: ProxyPool, logger: logging.Logger, tag: str = "http"):
        self.cfg = cfg
        self.pool = pool
        self.log = logger
        self.tag = tag
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": cfg["user_agent"],
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._proxy: str | None = None

    # ------------------------------------------------------------ 代理

    def rotate_proxy(self) -> str | None:
        self._proxy = self.pool.acquire() if self.pool else None
        proxies = to_requests_map(self._proxy)
        self.session.proxies = proxies or {}
        if self._proxy:
            self.log.debug("[%s] 出口代理 -> %s", self.tag, self._proxy)
        return self._proxy

    def mark_proxy(self, ok: bool) -> None:
        if self.pool:
            self.pool.report(self._proxy, ok)

    # ------------------------------------------------------------ 请求

    def request(self, method: str, url: str, *, retries: int | None = None,
                backoff: float | None = None, **kwargs) -> requests.Response:
        net = self.cfg["network"]
        retries = net["retries"] if retries is None else retries
        backoff = net["retry_backoff_sec"] if backoff is None else backoff
        kwargs.setdefault("timeout", net["timeout_sec"])
        kwargs.setdefault("allow_redirects", False)

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            if attempt == 0 and self._proxy is None and self.pool:
                self.rotate_proxy()
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                self.mark_proxy(False)
                self.log.warning("[%s] %s %s 网络异常(%s)，第 %d/%d 次",
                                 self.tag, method, _short(url), exc.__class__.__name__,
                                 attempt + 1, retries + 1)
                if attempt < retries:
                    if net.get("rotate_on_fail") and self.pool:
                        self.rotate_proxy()
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise HttpError(f"网络异常: {exc}") from exc

            status = resp.status_code
            if status == 429:
                wait = _retry_after(resp) or backoff * (attempt + 2)
                self.log.warning("[%s] 429 限速，等待 %.0fs", self.tag, wait)
                if attempt < retries:
                    time.sleep(wait)
                    continue
                raise HttpError("429 限速", status, resp.text[:500])
            if 500 <= status < 600 and attempt < retries:
                self.log.warning("[%s] %s %s -> %d，重试", self.tag, method, _short(url), status)
                time.sleep(backoff * (attempt + 1))
                continue

            self.mark_proxy(status < 500)
            if status >= 500:
                raise HttpError(f"服务端异常 {status}", status, resp.text[:500])
            return resp

        raise HttpError(f"请求失败: {last_exc}")

    def json(self, method: str, url: str, expect: tuple[int, ...] = (200,), **kwargs) -> Any:
        resp = self.request(method, url, **kwargs)
        if resp.status_code not in expect:
            raise HttpError(
                f"{method} {_short(url)} -> {resp.status_code}: {redact(resp.text[:400])}",
                resp.status_code, resp.text[:2000])
        if not resp.text:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise HttpError(f"响应不是 JSON: {redact(resp.text[:200])}", resp.status_code) from exc


def _retry_after(resp: requests.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return max(1.0, min(value, 3600.0))


def _short(url: str) -> str:
    return url.split("?")[0][:90]


def jitter_delay(cfg: dict, logger: logging.Logger | None = None) -> float:
    lo, hi = cfg["network"]["delay_between_accounts_sec"]
    value = random.uniform(float(lo), float(hi))
    if logger:
        logger.info("账号间隔 %.0fs", value)
    return value
