# -*- coding: utf-8 -*-
"""用户名候选生成：prefix + 随机数字（首位非 0）。"""
from __future__ import annotations

import secrets
import string


def make_username(prefix: str, digits: int = 6, rng=None) -> str:
    if digits < 1:
        raise ValueError("digits 必须 >= 1")
    pick = rng.choice if rng is not None else secrets.choice
    body = [pick(string.digits[1:9])]  # 1-8，避免首位 0
    body.extend(pick(string.digits) for _ in range(digits - 1))
    return f"{prefix}{''.join(body)}"


def candidates(prefix: str, digits: int = 6, count: int = 5, rng=None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    guard = 0
    while len(out) < count and guard < count * 50:
        guard += 1
        name = make_username(prefix, digits, rng)
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out
