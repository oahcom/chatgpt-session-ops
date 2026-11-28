# -*- coding: utf-8 -*-
"""Clash Verge（mihomo）节点控制：出口被封时换节点。

mihomo 在 Windows 上把 external-controller 绑到命名管道，所以直接讲 HTTP over pipe。
管道名与 secret 从 Clash Verge 的 config.yaml 里读，读不到再用 config.json 的 network.clash。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

VERGE_CFG = Path.home() / "AppData/Roaming/io.github.clash-verge-rev.clash-verge-rev/config.yaml"

# 组里除了真节点还有「剩余流量/套餐到期」这类信息条目，切过去等于断网
NOT_NODE = ("剩余流量", "套餐", "到期", "官网", "重置", "距离", "流量", "DIRECT", "REJECT",
            "GLOBAL", "自动选择", "故障转移", "负载均衡")


def is_node(name: str) -> bool:
    return not any(k in name for k in NOT_NODE)


def _verge_settings() -> tuple[str | None, str]:
    try:
        text = VERGE_CFG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, ""
    pipe = re.search(r"^external-controller-pipe:\s*(\S+)\s*$", text, re.M)
    secret = re.search(r"^secret:\s*(\S+)\s*$", text, re.M)
    return (pipe.group(1).strip() if pipe else None,
            secret.group(1).strip() if secret else "")


class Clash:
    def __init__(self, cfg: dict, log):
        c = (cfg.get("network") or {}).get("clash") or {}
        pipe, secret = _verge_settings()
        self.pipe = c.get("pipe") or pipe
        self.secret = c.get("secret") or secret or ""
        self.group = c.get("group") or "🚀节点选择"
        self.proxy = c.get("proxy") or "http://127.0.0.1:7000"
        self.log = log
        self._members: list[str] | None = None

    # ------------------------------------------------------------ 底层

    def _call(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        if not self.pipe:
            raise RuntimeError("找不到 mihomo controller 管道")
        raw_body = json.dumps(body).encode() if body is not None else b""
        head = (f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
                f"Authorization: Bearer {self.secret}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(raw_body)}\r\nConnection: close\r\n\r\n").encode()
        with open(self.pipe, "r+b", buffering=0) as f:
            f.write(head + raw_body)
            buf = b""
            while True:
                try:
                    chunk = f.read(8192)
                except Exception:
                    break
                if not chunk:
                    break
                buf += chunk
        head_end = buf.find(b"\r\n\r\n")
        header = buf[:head_end] if head_end >= 0 else b""
        payload = buf[head_end + 4:] if head_end >= 0 else buf
        if b"chunked" in header.lower():
            out, rest = [], payload
            while rest:
                line, _, rest = rest.partition(b"\r\n")
                try:
                    n = int(line.strip() or b"0", 16)
                except ValueError:
                    break
                if n == 0:
                    break
                out.append(rest[:n])
                rest = rest[n + 2:]
            payload = b"".join(out)
        try:
            return json.loads(payload.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------ 对外

    def current(self) -> str | None:
        try:
            return (self._call(f"/proxies/{urllib.parse.quote(self.group, safe='')}")
                    .get("now"))
        except Exception:  # noqa: BLE001 - 换节点是增强项，失败不该炸主流程
            return None

    def members(self) -> list[str]:
        """组内真实节点（排除 自动选择/地区组 这类子策略组）。只读，不切换。"""
        if self._members is None:
            data = self._call("/proxies").get("proxies") or {}
            groups = ("Selector", "URLTest", "Fallback", "LoadBalance")
            mine = (data.get(self.group) or {}).get("all") or []
            self._members = [n for n in mine
                             if (data.get(n) or {}).get("type") not in groups
                             and n not in ("DIRECT", "REJECT")]
        return self._members

    def switch(self, name: str) -> bool:
        try:
            self._call(f"/proxies/{urllib.parse.quote(self.group, safe='')}", "PUT",
                       {"name": name})
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.warning("切换节点失败(%s): %s", name, str(exc)[:100])
            return False

    def next_node(self) -> str | None:
        """换下一个出口。用途：每个账号一条干净出口（HV 方式按 IP 分配）。"""
        try:
            names = self.members()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("读取节点列表失败: %s", str(exc)[:100])
            return None
        names = [n for n in names if is_node(n)]
        if not names:
            return None
        cur = self.current()
        start = names.index(cur) + 1 if cur in names else 0
        for k in range(len(names)):
            name = names[(start + k) % len(names)]
            if self.switch(name):
                return name
        return None

    def egress_ip(self) -> str:
        op = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": self.proxy, "https": self.proxy}))
        try:
            with op.open("https://api.ipify.org", timeout=20) as r:
                return r.read().decode().strip()
        except Exception as exc:  # noqa: BLE001
            return f"err:{type(exc).__name__}"
