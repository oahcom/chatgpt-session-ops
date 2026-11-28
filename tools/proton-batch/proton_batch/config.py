# -*- coding: utf-8 -*-
"""配置加载：默认值 + 用户 config.json 深合并。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULTS = {
    "proton": {
        "base_url": "https://account.proton.me/api",
        "signup_page": "https://account.proton.me/mail/signup",
        "appversion": "web-account@5.0.0.0",
        "apiversion": "4",
        "locale": "en_US",
        "available_path": "/core/v4/users/available",
        "verification_path": "/core/v4/auth/verification",
        "verification_captcha_path": "/core/v4/auth/verification/captcha",
        "verification_email_path": "/core/v4/auth/verification/email",
        "verification_email_code_path": "/core/v4/auth/verification/email/code",
        "verification_sms_path": "/core/v4/auth/verification/sms",
        "verification_sms_code_path": "/core/v4/auth/verification/sms/code",
        "signup_path": "/auth/v4/users",
        "auth_path": "/auth/v4",
        "username_prefix": "oahcom",
        "username_digits": 6,
        "username_attempts": 5,
        "password_length": 24,
        "mode": "api",             # api | browser（实测只有 browser 能过 Proton 风控）
        "verification": "auto",
        "recovery_email": "",
        "plan": "free",
        "login_verify": True,
        "extra_create_fields": {},
    },
    "chatgpt": {
        "auth_base": "https://auth.openai.com",
        "site_base": "https://chatgpt.com",
        "client_id": "app_2SKx67EdpoN0lwt8JhFJvOcnwvbnCkQa",
        "authorize_path": "/api/accounts/authorize",
        "register_path": "/api/accounts/user/register",
        "otp_send_path": "/api/accounts/email-otp/send",
        "otp_validate_path": "/api/accounts/email-otp/validate",
        "create_account_path": "/api/accounts/create_account",
        "sentinel_path": "/backend-api/sentinel/chat-requirements",
        "password_length": 20,
        "display_name": "Oah Com",
        "birthdate": "1990-01-01",
        "otp_timeout_sec": 240,
        "otp_poll_interval_sec": 6,
        "code_source": "imap",     # imap | proton-web | tempmail
        "sentinel": False,         # 注册链路不需要 sentinel，开启会 401
        "extra_create_fields": {},
    },
    "browser": {
        "browse_path": None,
        "browser": "chrome-this-computer",
        "keep_open": True,
    },
    "captcha": {
        "provider": "2captcha",
        "api_key": "",
        "yescaptcha_endpoint": "https://api.yescaptcha.com",
        "hcaptcha_sitekey": "",
        "hcaptcha_pageurl": "https://account.proton.me/mail/signup",
        "arkose_publickey": "",
        "arkose_surl": "https://client-api.arkoselabs.com",
        "arkose_pageurl": "https://auth.openai.com/create-account",
        "timeout_sec": 240,
        "poll_interval_sec": 5,
    },
    "sms": {
        "provider": "sms-activate",
        "endpoint": "https://api.sms-activate.org/stubs/handler_api.php",
        "api_key": "",
        "service": "protonmail",
        "country": 0,
        "timeout_sec": 600,
        "poll_interval_sec": 10,
    },
    "mail": {
        "host": "127.0.0.1",
        "port": 1143,
        "ssl": False,
        "username": "",
        "password": "",
        "mailbox": "INBOX",
        "from_contains": "",
        "subject_contains": "",
        "poll_interval_sec": 5,
        "timeout_sec": 300,
        "code_regex": r"(?<!\d)(\d{6})(?!\d)",
    },
    "network": {
        "proxies": [],
        "proxy_file": None,
        "proxy_cooldown_sec": 600,
        "rotate_on_fail": True,
        "timeout_sec": 45,
        "retries": 2,
        "retry_backoff_sec": 5,
        "delay_between_accounts_sec": [45, 120],
        "max_concurrency": 1,
    },
    "storage": {
        "data_dir": "data",
        "db_path": "data/accounts.db",
        "log_dir": "data/logs",
    },
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    """加载配置。找不到用户配置时使用内置默认值（并提示）。"""
    candidate = path or os.environ.get("PROTON_BATCH_CONFIG") or "config.json"
    p = Path(candidate)
    overrides: dict = {}
    if p.is_file():
        overrides = json.loads(p.read_text(encoding="utf-8-sig"))
        root = p.resolve().parent
    else:
        root = Path(__file__).resolve().parent.parent
    cfg = deep_merge(DEFAULTS, overrides)
    cfg["_config_path"] = str(p.resolve()) if p.is_file() else None
    cfg["_root"] = str(root)
    cfg["_config_found"] = p.is_file()
    return cfg


def resolve_path(cfg: dict, value: str) -> Path:
    p = Path(value)
    if p.is_absolute():
        return p
    return Path(cfg["_root"]) / p


def require(cfg: dict, section: str, key: str, purpose: str) -> str:
    value = cfg.get(section, {}).get(key)
    if not value:
        raise SystemExit(f"配置缺失：{section}.{key}（{purpose}）")
    return str(value)
