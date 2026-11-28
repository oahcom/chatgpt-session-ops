# -*- coding: utf-8 -*-
"""Proton 注册客户端（account.proton.me /api，v4）。

链路：
    GET  /core/v4/users/available?Name=...        用户名可用性
    POST /core/v4/auth/verification                取 human verification 方法
    POST /core/v4/auth/verification/captcha        hCaptcha 换 HV token
    POST /core/v4/auth/verification/email[/code]   邮件验证分支
    POST /core/v4/auth/verification/sms[/code]     短信验证分支
    POST /auth/v4/users                            创建账号（带 HV 头）
    POST /auth/v4                                  登录校验

服务端约定：HTTP 200 + 业务码。成功码 1000；用户名被占 12106/12111；
HV 失败 9001/12087/2026。所有非 1000 一律按失败处理，不做「猜成功」。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .captcha import CaptchaError, CaptchaSolver
from .httpclient import Http, HttpError
from .mailbox import Mailbox, MailError
from .sms import SmsError, SmsProvider

CODE_OK = 1000
CODE_USERNAME_TAKEN = {12106, 12111, 12113}

HV_HEADER = "x-pm-human-verification-token"
HV_TYPE_HEADER = "x-pm-human-verification-token-type"


class ProtonError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.code = code
        self.payload = payload


class ProtonClient:
    def __init__(self, cfg: dict, http: Http, solver: CaptchaSolver,
                 sms: SmsProvider, mailbox: Mailbox, logger: logging.Logger):
        self.cfg = cfg["proton"]
        self.http = http
        self.solver = solver
        self.sms = sms
        self.mailbox = mailbox
        self.log = logger
        self.hv: dict | None = None

    # ------------------------------------------------------------ 基础

    def _url(self, path_key: str) -> str:
        return self.cfg["base_url"].rstrip("/") + self.cfg[path_key]

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Accept": "application/vnd.protonmail.v1+json",
            "Content-Type": "application/json",
            "x-pm-appversion": self.cfg["appversion"],
            "x-pm-apiversion": self.cfg["apiversion"],
            "x-pm-locale": self.cfg["locale"],
            "Origin": "https://account.proton.me",
            "Referer": self.cfg["signup_page"],
        }
        if self.hv:
            headers[HV_HEADER] = self.hv["token"]
            headers[HV_TYPE_HEADER] = self.hv["type"]
        headers.update(extra or {})
        return headers

    def _call(self, method: str, path_key: str, body: dict | None = None,
              params: dict | None = None) -> dict:
        url = self._url(path_key)
        kwargs = {"headers": self._headers(), "params": params}
        if method != "GET":
            kwargs["data"] = json.dumps(body or {}).encode()
        resp = self.http.request(method, url, **kwargs)
        try:
            data = resp.json()
        except ValueError:
            raise ProtonError(f"非 JSON 响应 ({resp.status_code})", None,
                              resp.text[:500]) from None
        code = data.get("Code")
        if code != CODE_OK:
            raise ProtonError(
                f"{path_key} 失败 Code={code} Error={data.get('Error')}",
                code, data.get("Details"))
        return data

    # ------------------------------------------------------------ 探测

    def probe(self) -> dict:
        """端点存活探测：只发只读请求，不改任何状态。"""
        url = self._url("available_path")
        resp = self.http.request("GET", url, headers=self._headers(),
                                 params={"Name": self.cfg["username_prefix"] + "000000"})
        return {"status": resp.status_code, "body": resp.text[:300]}

    # ------------------------------------------------------------ 用户名

    def check_available(self, name: str) -> bool:
        try:
            self._call("GET", "available_path", params={"Name": name})
            return True
        except ProtonError as exc:
            if exc.code in CODE_USERNAME_TAKEN:
                return False
            # 其余业务码不是「重名」，是接口/风控问题，必须冒泡
            raise

    def pick_username(self, pygen) -> str:
        attempts = int(self.cfg["username_attempts"])
        for name in pygen(attempts):
            if self.check_available(name):
                self.log.info("用户名可用: %s", name)
                return name
            self.log.info("用户名已占用: %s", name)
            time.sleep(1.5)
        raise ProtonError(f"连续 {attempts} 个候选用户名不可用")

    # ------------------------------------------------------------ 人机验证

    def _hv_set(self, token: str, token_type: str) -> None:
        self.hv = {"token": token, "type": token_type}
        self.log.debug("HV token 就绪 type=%s", token_type)

    def start_verification(self) -> list[str]:
        data = self._call("POST", "verification_path", {})
        details = data.get("Details") or {}
        methods = details.get("Methods") or []
        token = details.get("HumanVerificationToken")
        token_type = details.get("HumanVerificationTokenType")
        if token and token_type:
            self._hv_set(token, token_type)
        self.log.info("可用验证方式: %s", ",".join(methods) or "(none)")
        return methods

    def solve_captcha(self) -> None:
        if not self.solver.enabled:
            raise ProtonError("需要 hCaptcha，但未配置 captcha.api_key", None,
                              {"methods": ["captcha"]})
        token = self.solver.solve_any(lambda: self.solver.solve_hcaptcha())
        data = self._call("POST", "verification_captcha_path",
                          {"Token": token, "TokenType": "hcaptcha"})
        details = data.get("Details") or {}
        self._hv_set(details.get("HumanVerificationToken", ""),
                     details.get("HumanVerificationTokenType", "captcha"))

    def solve_email(self, email: str | None = None) -> None:
        email = email or self.cfg.get("recovery_email")
        if not email:
            raise ProtonError("需要邮件验证，但未配置 proton.recovery_email")
        since = time.time()
        self._call("POST", "verification_email_path", {"Email": email, "Type": "email"})
        code = self.mailbox.wait_code(since_ts=since)
        data = self._call("POST", "verification_email_code_path",
                          {"Email": email, "Code": code})
        details = data.get("Details") or {}
        self._hv_set(details.get("HumanVerificationToken", ""),
                     details.get("HumanVerificationTokenType", "email"))

    def solve_sms(self) -> None:
        if not self.sms.enabled:
            raise ProtonError("需要短信验证，但未配置 sms.api_key")
        order_id, phone = self.sms.acquire()
        try:
            self._call("POST", "verification_sms_path", {"Phone": phone})
            code = self.sms.wait_code(order_id)
            data = self._call("POST", "verification_sms_code_path", {"Code": code})
        finally:
            self.sms.cancel(order_id)
        details = data.get("Details") or {}
        self._hv_set(details.get("HumanVerificationToken", ""),
                     details.get("HumanVerificationTokenType", "sms"))

    def ensure_human_verification(self, methods: list[str] | None = None) -> None:
        if self.hv:
            return
        methods = methods if methods is not None else self.start_verification()
        mode = (self.cfg.get("verification") or "auto").lower()

        order = {
            "captcha": ["captcha"],
            "email": ["email"],
            "sms": ["sms"],
            "auto": ["captcha", "email", "sms"],
        }.get(mode, ["captcha", "email", "sms"])

        errors = []
        for method in order:
            if methods and method not in methods:
                continue
            try:
                if method == "captcha":
                    self.solve_captcha()
                elif method == "email":
                    self.solve_email()
                else:
                    self.solve_sms()
                if self.hv:
                    return
            except (CaptchaError, MailError, SmsError, ProtonError) as exc:
                errors.append(f"{method}: {exc}")
                self.log.warning("%s 验证失败: %s", method, exc)
        detail = "; ".join(errors) if errors else "服务端未提供可用的验证方式"
        raise ProtonError(f"人机验证未完成: {detail}")

    # ------------------------------------------------------------ 创建 / 登录

    def register(self, name: str, password: str) -> dict:
        self.ensure_human_verification()
        body = {
            "Name": name,
            "Password": password,
            "Email": f"{name}@proton.me",
            "RecoveryEmail": self.cfg.get("recovery_email") or None,
            "Payload": {},
            "Referrer": self.cfg["signup_page"],
        }
        extra = self.cfg.get("extra_create_fields") or {}
        body.update(extra)
        data = self._call("POST", "signup_path", body)
        user = data.get("User") or {}
        user_id = data.get("UserID") or user.get("ID") or ""
        if not user_id:
            self.log.warning("创建接口未返回 UserID，需登录校验确认")
        return {"user_id": user_id, "raw_code": data.get("Code")}

    def login(self, name: str, password: str) -> dict:
        url = self.cfg["base_url"].rstrip("/") + self.cfg["auth_path"]
        # 登录不需要 HV token；带上反而可能被服务端当成重复验证
        headers = self._headers()
        headers.pop(HV_HEADER, None)
        headers.pop(HV_TYPE_HEADER, None)
        resp = self.http.request(
            "POST", url, headers=headers,
            data=json.dumps({"Username": name, "Password": password}).encode())
        try:
            data = resp.json()
        except ValueError:
            raise ProtonError(f"登录返回非 JSON ({resp.status_code})") from None
        if data.get("Code") != CODE_OK:
            raise ProtonError(f"登录失败 Code={data.get('Code')} "
                              f"Error={data.get('Error')}", data.get("Code"))
        return {
            "uid": data.get("UID"),
            "user_id": (data.get("User") or {}).get("ID"),
            "access_token": data.get("AccessToken"),
        }
