# -*- coding: utf-8 -*-
"""IMAP 收码：Proton Bridge（127.0.0.1:1143）或任意 IMAP 服务器。

关键行为：
- 只采信「本次操作开始之后」到达的邮件，避免读到上一轮的旧验证码。
- 已消费的 Message-ID 记录在内存中，同一轮不会重复命中。
- 只解析纯文本/HTML 正文，正则取码；不落盘邮件原文。
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class MailError(RuntimeError):
    pass


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_code(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text or "")
    return match.group(1) if match else None


def message_text(msg) -> str:
    if msg.is_multipart():
        chunks = []
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() in ("text/plain", "text/html"):
                chunks.append(_decode_part(part))
        return "\n".join(chunks)
    return _decode_part(msg)


def _arrival_ts(msg) -> float:
    raw = msg.get("Date")
    if not raw:
        return 0.0
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError):
        return 0.0


class Mailbox:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg["mail"]
        self.log = logger
        self.seen: set[str] = set()

    # ------------------------------------------------------------ 连接

    def _connect(self) -> imaplib.IMAP4:
        host = self.cfg["host"]
        port = int(self.cfg["port"])
        if self.cfg.get("ssl"):
            conn = imaplib.IMAP4_SSL(host, port)
        else:
            conn = imaplib.IMAP4(host, port)
        if self.cfg.get("username"):
            conn.login(self.cfg["username"], self.cfg.get("password", ""))
        conn.select(self.cfg.get("mailbox", "INBOX"), readonly=True)
        return conn

    # ------------------------------------------------------------ 收码

    def wait_code(self, since_ts: float | None = None,
                  pattern: str | None = None) -> str:
        pattern = pattern or self.cfg.get("code_regex") or r"(?<!\d)(\d{6})(?!\d)"
        since_ts = since_ts or time.time()
        deadline = since_ts + int(self.cfg.get("timeout_sec") or 300)
        interval = int(self.cfg.get("poll_interval_sec") or 5)
        from_filter = (self.cfg.get("from_contains") or "").lower()
        subject_filter = (self.cfg.get("subject_contains") or "").lower()

        # IMAP SINCE 只到天粒度，往后多留一天避免时区边界漏信
        since_date = datetime.fromtimestamp(since_ts - 86400).strftime("%d-%b-%Y")

        while time.time() < deadline:
            try:
                conn = self._connect()
            except (OSError, imaplib.IMAP4.error) as exc:
                self.log.warning("IMAP 连接失败(%s)，重试", exc.__class__.__name__)
                time.sleep(interval)
                continue

            try:
                typ, data = conn.search(None, f'(SINCE {since_date})')
                ids = data[0].split() if data and data[0] else []
                for uid in reversed(ids[-60:]):
                    typ, fetched = conn.fetch(uid, "(RFC822)")
                    if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                        continue
                    msg = email.message_from_bytes(fetched[0][1], policy=email.policy.default)
                    msg_id = msg.get("Message-ID") or f"uid:{uid.decode()}"
                    if msg_id in self.seen:
                        continue
                    if _arrival_ts(msg) and _arrival_ts(msg) < since_ts - 30:
                        continue
                    if from_filter and from_filter not in (msg.get("From") or "").lower():
                        continue
                    if subject_filter and subject_filter not in (msg.get("Subject") or "").lower():
                        continue
                    code = extract_code(message_text(msg), pattern)
                    if code:
                        self.seen.add(msg_id)
                        self.log.info("收到邮件验证码（from=%s）", (msg.get("From") or "")[:60])
                        return code
                    self.seen.add(msg_id)
            except imaplib.IMAP4.error as exc:
                self.log.warning("IMAP 读取异常: %s", exc.__class__.__name__)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

            time.sleep(interval)

        raise MailError("等待邮件验证码超时")

    # ------------------------------------------------------------ 配置自检

    def probe(self, limit: int = 5) -> list[dict]:
        """只读自检：能否登录、收件箱是否可读、最近几封的主题。

        不返回正文，避免把验证码/隐私带进日志。
        """
        conn = self._connect()
        try:
            typ, data = conn.search(None, "ALL")
            ids = data[0].split() if data and data[0] else []
            out = []
            for uid in reversed(ids[-limit:]):
                typ, fetched = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                    continue
                msg = email.message_from_bytes(fetched[0][1], policy=email.policy.default)
                out.append({
                    "from": str(msg.get("From") or "")[:80],
                    "subject": str(msg.get("Subject") or "")[:80],
                    "date": str(msg.get("Date") or "")[:40],
                })
            return out
        finally:
            try:
                conn.logout()
            except Exception:
                pass
