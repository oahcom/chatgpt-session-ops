# -*- coding: utf-8 -*-
"""密码生成：加密安全随机 + 拒绝采样 + Fisher-Yates。"""
from __future__ import annotations

import secrets
import string

UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
LOWER = "abcdefghijkmnopqrstuvwxyz"
DIGITS = "23456789"
SPECIAL = "!@#$%^&*-_=+"
ALL = UPPER + LOWER + DIGITS + SPECIAL


def _pick(alphabet: str) -> str:
    n = len(alphabet)
    limit = 256 - (256 % n)
    while True:
        b = secrets.randbelow(256)
        if b < limit:
            return alphabet[b % n]


def generate(length: int = 24) -> str:
    if length < 8:
        raise ValueError("密码长度至少 8")
    required = [_pick(UPPER), _pick(LOWER), _pick(DIGITS), _pick(SPECIAL)]
    rest = [_pick(ALL) for _ in range(length - len(required))]
    chars = required + rest
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def looks_strong(value: str) -> bool:
    return (len(value) >= 8
            and any(c in string.ascii_uppercase for c in value)
            and any(c in string.ascii_lowercase for c in value)
            and any(c.isdigit() for c in value)
            and any(not c.isalnum() for c in value))
