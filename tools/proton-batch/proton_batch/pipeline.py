# -*- coding: utf-8 -*-
"""编排：Proton 批量注册 -> ChatGPT 批量注册 -> 落库。

两种 Proton 模式：
- api     ：纯 HTTP（v4）。实测 2026-11 端点漂移 + 版本校验，仅作兼容保留。
- browser ：真实浏览器（browse CLI）。实测可行路径；风控命中时会立即中止整批。

ChatGPT 收码来源（chatgpt.code_source）：
- imap        ：任意 IMAP（Proton Bridge / 其他邮箱）
- proton-web  ：浏览器里已登录的 Proton 收件箱（串行 run_all 时最省事）
- tempmail    ：mail.tm 一次性邮箱（OpenAI 会拒这类域名，仅调试用）
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import passwords
from .browser_driver import BrowseDriver
from .captcha import CaptchaSolver
from .clash import Clash
from .chatgpt_api import ChatGPTSignup, ChatGPTError
from .config import resolve_path
from .httpclient import Http, HttpError, jitter_delay
from .mailbox import Mailbox, MailError
from .proton_api import ProtonClient, ProtonError
from .proton_browser import (ChallengeUnavailable, HvRequired, NetworkRejected,
                             ProtonBrowserSignup, SubmissionStalled, TransportError)
from .proton_webmail import ProtonWebMail
from .proxy import ProxyPool
from .sms import SmsProvider
from .store import Store, decrypt_secret
from .tempmail import MailTm, TempMailError
from .usernamegen import candidates


class Pipeline:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.log = logger
        storage = cfg["storage"]
        self.data_dir = resolve_path(cfg, storage["data_dir"])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(resolve_path(cfg, storage["db_path"]))

        self.pool = ProxyPool(
            cfg["network"]["proxies"],
            cfg["network"]["proxy_cooldown_sec"],
            cfg["network"]["proxy_file"],
            Path(cfg["_root"]),
        )
        if self.pool:
            self.log.info("代理池: %d 个节点", len(self.pool.state()))
        else:
            self.log.info("HTTP 模式未配代理；浏览器模式走系统代理（Clash 7000）")

        self._driver_obj: BrowseDriver | None = None

    # ------------------------------------------------------------ 服务

    def _driver(self) -> BrowseDriver:
        if self._driver_obj is None:
            bcfg = self.cfg.get("browser") or {}
            self._driver_obj = BrowseDriver(
                self.log, bcfg.get("browse_path"), bcfg.get("browser", "chrome-this-computer"))
        return self._driver_obj

    def _services(self, tag: str):
        """API 模式的服务组合（测试与兼容用）。"""
        http = Http(self.cfg, self.pool, self.log, tag)
        solver = CaptchaSolver(self.cfg, self.log)
        sms = SmsProvider(self.cfg, self.log)
        mail = Mailbox(self.cfg, self.log)
        proton = ProtonClient(self.cfg, http, solver, sms, mail, self.log)
        return http, proton

    def _code_provider(self, row: dict):
        source = (self.cfg["chatgpt"].get("code_source") or "imap").lower()
        if source == "proton-web":
            return ProtonWebMail(self.cfg, self._driver(), self.log,
                                 row.get("proton_username"),
                                 decrypt_secret(row.get("proton_password") or ""))
        if source == "tempmail":
            mail = MailTm(self.log, self.pool)
            mail.create()
            return mail
        return Mailbox(self.cfg, self.log)

    # ------------------------------------------------------------ Proton

    def run_proton(self, count: int, prefix: str | None = None) -> list[int]:
        if (self.cfg["proton"].get("mode") or "api").lower() == "browser":
            return self._run_proton_browser(count, prefix)
        return self._run_proton_api(count, prefix)

    def _new_credentials(self, prefix: str | None) -> tuple[str, str, int]:
        prefix = prefix or self.cfg["proton"]["username_prefix"]
        digits = int(self.cfg["proton"]["username_digits"])
        username = candidates(prefix, digits, 1)[0]
        password = passwords.generate(int(self.cfg["proton"]["password_length"]))
        return username, password, digits

    def _run_proton_browser(self, count: int, prefix: str | None) -> list[int]:
        driver = self._driver()
        done: list[int] = []
        attempts = int(self.cfg["proton"].get("username_attempts") or 3)
        rotator = None
        if ((self.cfg.get("network") or {}).get("clash") or {}).get("enabled", True):
            rotator = Clash(self.cfg, self.log)          # 每账号一条干净出口
            self.log.info("出口节点: %s (ip=%s)", rotator.current(), rotator.egress_ip())

        for index in range(1, count + 1):
            self.log.info("===== Proton(browser) %d/%d =====", index, count)
            if rotator and index > 1:
                node = rotator.next_node()
                if node:
                    self.log.info("换出口: %s (ip=%s)", node, rotator.egress_ip())
            for attempt in range(1, attempts + 1):
                username, password, _ = self._new_credentials(prefix)
                self.log.info("目标账号: %s（第 %d 次尝试）", username, attempt)
                account_id = self.store.add_proton(
                    username, f"{username}@proton.me", password, None)
                signup = ProtonBrowserSignup(self.cfg, driver, None, self.log)
                node_tries = 0
                while True:
                    try:
                        result = signup.signup(username, password)
                    except ChallengeUnavailable as exc:
                        # 该出口不给 CAPTCHA（只有 Email/SMS）或地址发码用尽：换出口重来
                        if rotator and node_tries < 6:
                            node_tries += 1
                            node = rotator.next_node()
                            if node:
                                self.log.warning("挑战不可用（%s），换出口 %s (ip=%s)",
                                                 exc, node, rotator.egress_ip())
                                continue
                        self.store.update(account_id, proton_status="skipped",
                                          proton_error=str(exc)[:200])
                        self.store.event(account_id, "proton", "warn", str(exc)[:200])
                        self.log.warning("挑战类型不可用，换号：%s", exc)
                        break
                    except NetworkRejected as exc:
                        # 明确网络级拒绝：立即终止整批，不换节点、不重试
                        self.store.update(account_id, proton_status="network_rejected",
                                          proton_error=str(exc)[:400])
                        self.store.event(account_id, "proton", "error", str(exc)[:400])
                        self.log.error("网络级反滥用拒绝，终止整批：%s", exc)
                        raise
                    except HvRequired as exc:
                        # 服务端已签发挑战：不重提、不猜协议，保留诊断信息后暂停
                        status = "hv_presented" if exc.ui_ready else "hv_ui_not_visible"
                        self.store.update(account_id, proton_status=status,
                                          proton_error=str(exc)[:400])
                        self.store.event(account_id, "proton", "warn", str(exc)[:400])
                        self.log.error("需要人机校验（ui_visible=%s，状态=%s），暂停：%s",
                                       exc.ui_ready, status, exc)
                        break
                    except SubmissionStalled as exc:
                        # 状态不明：不重提同一请求
                        self.store.update(account_id, proton_status="stalled",
                                          proton_error=str(exc)[:400])
                        self.store.event(account_id, "proton", "warn", str(exc)[:400])
                        self.log.error("提交状态停滞，不重提：%s", exc)
                        break
                    except TransportError as exc:
                        msg = f"TransportError: {exc}"
                        self.store.update(account_id, proton_status="failed",
                                          proton_error=msg[:400], attempts=1)
                        self.store.event(account_id, "proton", "error", msg[:400])
                        self.log.error("传输层失败: %s", msg)
                        break
                    except Exception as exc:  # noqa: BLE001 - 单账号失败不掩盖
                        msg = f"{exc.__class__.__name__}: {exc}"
                        self.store.update(account_id, proton_status="failed",
                                          proton_error=msg[:400], attempts=1)
                        self.store.event(account_id, "proton", "error", msg[:400])
                        self.log.error("Proton 失败: %s", msg)
                        break
                    recovery = result.get("recovery")
                self.store.update(
                    account_id, proton_status="verified",
                    proton_recovery=recovery or None,
                    recovery_status="present" if recovery else "missing",
                    credential_status="complete" if recovery else "incomplete",
                    proton_user_id="")
                self.store.event(account_id, "proton", "info",
                                 f"浏览器注册成功 recovery={'present' if recovery else 'missing'}")
                done.append(account_id)
                break

            if index < count:
                time.sleep(jitter_delay(self.cfg, self.log))
        return done

    def _run_proton_api(self, count: int, prefix: str | None) -> list[int]:
        prefix = prefix or self.cfg["proton"]["username_prefix"]
        digits = int(self.cfg["proton"]["username_digits"])
        created: list[int] = []
        http, proton = self._services("proton")

        for index in range(1, count + 1):
            self.log.info("===== Proton(api) %d/%d =====", index, count)
            http.rotate_proxy()
            account_id = None
            try:
                name = proton.pick_username(lambda n: candidates(prefix, digits, n))
                password = passwords.generate(int(self.cfg["proton"]["password_length"]))
                account_id = self.store.add_proton(
                    name, f"{name}@proton.me", password, http._proxy)
                self.store.event(account_id, "proton", "info", "开始创建")

                result = proton.register(name, password)
                self.store.update(account_id, proton_status="created",
                                  proton_user_id=result.get("user_id") or "")

                if self.cfg["proton"].get("login_verify", True):
                    login = proton.login(name, password)
                    self.store.update(
                        account_id, proton_status="verified",
                        proton_user_id=login.get("user_id") or result.get("user_id") or "")
                created.append(account_id)
            except (ProtonError, HttpError, MailError, OSError) as exc:
                message = f"{exc.__class__.__name__}: {exc}"
                self.log.error("Proton 失败: %s", message)
                if account_id:
                    self.store.update(account_id, proton_status="failed",
                                      proton_error=message[:500], attempts=1)
            if index < count:
                time.sleep(jitter_delay(self.cfg, self.log))
        return created

    # ------------------------------------------------------------ ChatGPT

    def run_chatgpt(self, limit: int | None = None) -> list[int]:
        pending = [row for row in self.store.list(limit=1000)
                   if row["proton_status"] in ("created", "verified")
                   and row["chatgpt_status"] in ("none", "failed")]
        pending.sort(key=lambda r: r["id"])
        if limit:
            pending = pending[:limit]
        if not pending:
            self.log.info("没有待注册 ChatGPT 的 Proton 账号")
            return []

        done: list[int] = []
        client = ChatGPTSignup(self.cfg, self.log, self.pool)

        for index, row in enumerate(pending, 1):
            email = row["proton_email"] or f"{row['proton_username']}@proton.me"
            self.log.info("===== ChatGPT %d/%d (%s) =====", index, len(pending), email)
            account_id = row["id"]
            try:
                password = passwords.generate(int(self.cfg["chatgpt"]["password_length"]))
                self.store.update(account_id, chatgpt_email=email,
                                  chatgpt_password=password, chatgpt_status="pending",
                                  chatgpt_error=None)
                provider = self._code_provider(row)
                result = client.signup(email, password, provider)
                self.store.update(
                    account_id, chatgpt_status="created",
                    chatgpt_account_id=result.get("account_id") or "",
                    chatgpt_session=result.get("access_token") or "",
                    chatgpt_error=None)
                self.store.event(account_id, "chatgpt", "info", "注册完成并有 accessToken")
                done.append(account_id)
            except (ChatGPTError, MailError, TimeoutError, TempMailError) as exc:
                msg = f"{exc.__class__.__name__}: {exc}"
                self.log.error("ChatGPT 失败: %s", msg)
                self.store.update(account_id, chatgpt_status="failed",
                                  chatgpt_error=msg[:500],
                                  attempts=int(row.get("attempts") or 0) + 1)
                self.store.event(account_id, "chatgpt", "error", msg[:500])
            if index < len(pending):
                time.sleep(jitter_delay(self.cfg, self.log))
        return done

    def run_all(self, count: int) -> dict:
        """串行：注册一个 Proton 就立刻注册 ChatGPT（浏览器会话不串号）。"""
        proton_done, chatgpt_done = [], []
        for i in range(1, count + 1):
            self.log.info("##### 账号 %d/%d #####", i, count)
            ids = self.run_proton(1)
            proton_done.extend(ids)
            if ids:
                chatgpt_done.extend(self.run_chatgpt(limit=1))
            if i < count:
                time.sleep(jitter_delay(self.cfg, self.log))
        return {"proton": proton_done, "chatgpt": chatgpt_done}

    # ------------------------------------------------------------ 导出

    def export_rows(self) -> list[dict]:
        rows = []
        for row in self.store.list(limit=100000):
            rows.append({
                "id": row["id"],
                "proton_email": row["proton_email"],
                "proton_password": decrypt_secret(row["proton_password"]),
                "proton_status": row["proton_status"],
                "chatgpt_email": row["chatgpt_email"],
                "chatgpt_password": decrypt_secret(row["chatgpt_password"]),
                "chatgpt_status": row["chatgpt_status"],
                "created_at": row["created_at"],
            })
        return rows
