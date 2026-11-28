# -*- coding: utf-8 -*-
"""代理池：轮换 + 失败冷却 + 线程安全。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class ProxyPool:
    def __init__(self, proxies: list[str] | None = None, cooldown_sec: int = 600,
                 proxy_file: str | os.PathLike | None = None, root: Path | None = None):
        items = list(proxies or [])
        if proxy_file:
            items.extend(_load_proxy_file(Path(root or ".") / proxy_file))
        # 去重保序
        self._all = list(dict.fromkeys([p.strip() for p in items if p and p.strip()]))
        self._cooldown = {p: 0.0 for p in self._all}
        self._cursor = 0
        self._cooldown_sec = cooldown_sec
        self._lock = threading.Lock()

    def __bool__(self) -> bool:
        return bool(self._all)

    def acquire(self) -> str | None:
        if not self._all:
            return None
        with self._lock:
            now = time.time()
            n = len(self._all)
            for i in range(n):
                proxy = self._all[(self._cursor + i) % n]
                if self._cooldown.get(proxy, 0.0) <= now:
                    self._cursor = (self._cursor + i + 1) % n
                    return proxy
            return None  # 全部冷却中

    def report(self, proxy: str | None, ok: bool) -> None:
        if not proxy:
            return
        with self._lock:
            if ok:
                self._cooldown[proxy] = 0.0
            else:
                self._cooldown[proxy] = time.time() + self._cooldown_sec

    def state(self) -> list[dict]:
        now = time.time()
        return [
            {"proxy": p, "cooldown_remaining": max(0, int(self._cooldown[p] - now))}
            for p in self._all
        ]


def _load_proxy_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("proxies", [])
        return [str(x) for x in data]
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def to_requests_map(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    if proxy.startswith("socks5h://") or proxy.startswith("socks5://"):
        return {"http": proxy, "https": proxy}
    if "://" not in proxy:
        proxy = "http://" + proxy
    return {"http": proxy, "https": proxy}
