# -*- coding: utf-8 -*-
"""ChatGPT 邮箱注册客户端（实测协议，2026-11 验证）。

顺序（缺一步就拿不到 session）：
    GET  https://chatgpt.com/api/auth/csrf                     -> csrfToken
    POST https://chatgpt.com/api/auth/signin/openai?...         -> authorize URL
    GET  authorize URL（跟随 302）                              -> auth.openai.com cookie
    POST https://auth.openai.com/api/accounts/user/register     -> continue_url
    GET  continue_url                                           -> 触发发信
    POST .../email-otp/validate {code}                          -> continue_url
    GET  continue_url                                           -> about-you cookie
    POST .../create_account {name,birthdate}                    -> callback URL
    GET  callback URL                                           -> 会话 cookie
    GET  https://chatgpt.com/api/auth/session                   -> accessToken

注意（踩过的坑）：
- 注册链路**不需要** sentinel PoW；把 sentinel 放前面会 401。
- register 只拒「一次性邮箱域名」，回 400 account_creation_failed；
  用 @proton.me 这类正常域名可通过。这个判断必须原样报告，不能重试掩盖。
- 必须逐跳收 Set-Cookie：chatgpt.com 与 auth.openai.com 是两个域。
"""
from __future__ import annotations

import json
import logging
import random
import time
import uuid
from typing import Protocol
from urllib.parse import urlencode

import requests

from .httpclient import HttpError
from .proxy import ProxyPool, to_requests_map


class CodeProvider(Protocol):
    def wait_code(self, since_ts: float | None = None,
                  pattern: str | None = None) -> str: ...


class ChatGPTError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class ChatGPTSignup:
    def __init__(self, cfg: dict, logger: logging.Logger, pool: ProxyPool | None = None):
        self.cfg = cfg["chatgpt"]
        self.net = cfg["network"]
        self.ua = cfg["user_agent"]
        self.log = logger
        self.pool = pool
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.ua,
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.device_id = str(uuid.uuid4())
        self.logging_id = str(uuid.uuid4())

    # ------------------------------------------------------------ 出口

    def _apply_proxy(self) -> None:
        proxy = self.pool.acquire() if self.pool else None
        self.session.proxies = to_requests_map(proxy) or {}
        if proxy:
            self.log.debug("ChatGPT 出口 -> %s", proxy)

    def _req(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.net["timeout_sec"])
        try:
            return self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise ChatGPTError(f"网络异常: {exc.__class__.__name__}: {exc}") from exc

    def _json(self, resp: requests.Response, step: str) -> dict:
        try:
            data = resp.json()
        except ValueError:
            raise ChatGPTError(f"{step} 返回非 JSON ({resp.status_code}): "
                               f"{resp.text[:200]}", resp.status_code) from None
        if resp.status_code >= 400:
            raise ChatGPTError(f"{step} -> {resp.status_code}: "
                               f"{json.dumps(data, ensure_ascii=False)[:400]}",
                               resp.status_code, data)
        return data

    # ------------------------------------------------------------ 步骤

    def _api_headers(self, referer: str) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://auth.openai.com",
            "Referer": referer,
        }

    def bootstrap(self, email: str) -> str:
        csrf = self._req("GET", "https://chatgpt.com/api/auth/csrf",
                         headers={"Accept": "*/*", "Referer": "https://chatgpt.com/"})
        csrf_token = self._json(csrf, "csrf").get("csrfToken")
        if not csrf_token:
            raise ChatGPTError("未取得 csrfToken", csrf.status_code)

        params = {
            "prompt": "login",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": self.logging_id,
            "ext-passkey-client-capabilities": "0101",
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
        resp = self._req("POST", "https://chatgpt.com/api/auth/signin/openai?" + urlencode(params),
                         data={"callbackUrl": "https://chatgpt.com/",
                               "csrfToken": csrf_token, "json": "true"},
                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                  "Accept": "*/*", "Origin": "https://chatgpt.com",
                                  "Referer": "https://chatgpt.com/"})
        auth_url = self._json(resp, "signin").get("url")
        if not auth_url:
            raise ChatGPTError("未取得 authorize URL", resp.status_code)

        page = self._req("GET", auth_url, allow_redirects=True,
                         headers={"Accept": "text/html,application/xhtml+xml,"
                                            "application/xml;q=0.9,*/*;q=0.8",
                                  "Referer": "https://chatgpt.com/"})
        # 只要拿到 auth.openai.com 的 cookie 链即可，页面内容不解释
        self.log.info("authorize -> %s (%d)", page.url[:80], page.status_code)
        return page.url

    def register(self, email: str, password: str) -> str:
        resp = self._req("POST", "https://auth.openai.com/api/accounts/user/register",
                         json={"password": password, "username": email},
                         headers=self._api_headers(
                             "https://auth.openai.com/create-account/password"))
        data = self._json(resp, "user/register")
        cont = data.get("continue_url")
        if not cont:
            raise ChatGPTError(f"register 未返回 continue_url: {data}", resp.status_code, data)
        return cont

    def send_otp_page(self, continue_url: str) -> None:
        self._req("GET", continue_url, allow_redirects=True,
                  headers={"Accept": "text/html,*/*",
                           "Referer": "https://auth.openai.com/create-account/password"})

    def resend_otp(self) -> None:
        self._req("POST", "https://auth.openai.com/api/accounts/email-otp/resend",
                  headers={"Accept": "*/*", "Origin": "https://auth.openai.com",
                           "Referer": "https://auth.openai.com/email-verification"})

    def validate_otp(self, code: str) -> str:
        resp = self._req("POST", "https://auth.openai.com/api/accounts/email-otp/validate",
                         json={"code": code},
                         headers=self._api_headers(
                             "https://auth.openai.com/email-verification"))
        data = self._json(resp, "email-otp/validate")
        cont = data.get("continue_url")
        if not cont:
            raise ChatGPTError(f"OTP 校验未返回 continue_url: {data}", resp.status_code, data)
        self._req("GET", cont, allow_redirects=True,
                  headers={"Accept": "text/html,*/*",
                           "Referer": "https://auth.openai.com/email-verification"})
        return cont

    def create_account(self, name: str | None = None,
                       birthdate: str | None = None) -> dict:
        body = {
            "name": name or self.cfg["display_name"],
            "birthdate": birthdate or self._random_birthdate(),
        }
        body.update(self.cfg.get("extra_create_fields") or {})
        resp = self._req("POST", "https://auth.openai.com/api/accounts/create_account",
                         json=body,
                         headers=self._api_headers("https://auth.openai.com/about-you"))
        data = self._json(resp, "create_account")
        callback = (data.get("page") or {}).get("payload", {}).get("url") \
            or data.get("continue_url")
        if not callback:
            raise ChatGPTError(f"create_account 未返回回调地址: {data}", resp.status_code, data)
        self._req("GET", callback, allow_redirects=True,
                  headers={"Accept": "text/html,*/*",
                           "Referer": "https://auth.openai.com/"})
        return data

    def session(self) -> dict:
        resp = self._req("GET", "https://chatgpt.com/api/auth/session",
                         headers={"Accept": "application/json",
                                  "Referer": "https://chatgpt.com/"})
        if resp.status_code != 200:
            return {}
        try:
            return resp.json() or {}
        except ValueError:
            return {}

    # ------------------------------------------------------------ 完整流程

    def signup(self, email: str, password: str, provider: CodeProvider,
               resend_rounds: int = 2) -> dict:
        self._apply_proxy()
        self.log.info("ChatGPT 注册开始: %s", email)
        self.bootstrap(email)
        cont = self.register(email, password)
        self.send_otp_page(cont)

        code = None
        since = time.time()
        for attempt in range(resend_rounds + 1):
            if attempt:
                self.log.info("重发验证码 (%d/%d)", attempt, resend_rounds)
                self.resend_otp()
            try:
                code = provider.wait_code(since_ts=since)
                break
            except Exception as exc:  # 超时类异常都按「本轮未收到」处理
                self.log.warning("本轮未收到验证码: %s", exc)
        if not code:
            raise ChatGPTError("多轮重发后仍未收到验证码")

        self.log.info("收到验证码")
        self.validate_otp(code)
        self.create_account()
        sess = self.session()
        if not sess.get("accessToken"):
            raise ChatGPTError("注册流程完成但未取得 accessToken")
        return {
            "email": email,
            "access_token": sess.get("accessToken"),
            "account_id": (sess.get("account") or {}).get("id")
                          or (sess.get("user") or {}).get("id"),
        }

    @staticmethod
    def _random_birthdate() -> str:
        return f"{random.randint(1990, 2000)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
