# -*- coding: utf-8 -*-
"""browse CLI 驱动。

实测要点（2026-11）：
- `--ref` 用位置参数传会被解析成 tab，元素操作一律走 `--json '<object>'`；
- `--tab` 用 flag 传时只接受 JSON 里的 tab 才能稳定绑定元素；
- 跨域 iframe 内的输入只能 `click --ref` + `type`（无 ref）或 Playwright frame eval；
- 合成事件（JS setter + dispatchEvent）会被 Proton 判成 potentially abusive traffic，
  所以用户名必须用真实键盘 `type`。
"""
from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path


class BrowserError(RuntimeError):
    pass


def _style_visible(style: dict | None) -> bool:
    """单个元素的可见性（不含几何）。opacity 走 CSS 过渡，只要求 > 0。"""
    if not style:
        return False
    if style.get("display") == "none":
        return False
    if style.get("visibility") in ("hidden", "collapse"):
        return False
    try:
        opacity = float(style.get("opacity", 1))
    except (TypeError, ValueError):
        opacity = 1.0
    return opacity > 0


def dialog_visible(state: dict) -> bool:
    """弹窗是否真的可见：自身 + 全部祖先的样式，且布局矩形非零。

    不能用 `dialog[open]`：Proton 的 modal 用 CSS 控制显示，`open` 属性可能缺失。
    """
    if not state.get("dialogExists"):
        return False
    if not _style_visible(state.get("dialogStyle")):
        return False
    for ancestor in state.get("ancestors") or []:
        if not _style_visible(ancestor):
            return False
    rect = state.get("dialogRect") or {}
    return (rect.get("width") or 0) > 0 and (rect.get("height") or 0) > 0


def challenge_ui_initialized(state: dict) -> bool:
    """挑战 UI 已初始化到可观察状态 —— 不代表验证已通过。"""
    return (bool(state.get("dialogExists")) and dialog_visible(state)
            and int(state.get("tabCount") or 0) >= 1)


def _first_json(text: str):
    i = (text or "").find("{")
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except json.JSONDecodeError:
        return None


class BrowseDriver:
    def __init__(self, logger: logging.Logger, browse_path: str | None = None,
                 browser: str = "chrome-this-computer"):
        default = Path.home() / ".local" / "bin" / "browse.exe"
        self.browse = str(Path(browse_path) if browse_path else default)
        if not Path(self.browse).exists():
            found = shutil.which("browse")
            if not found:
                raise BrowserError(f"找不到 browse CLI: {self.browse}")
            self.browse = found
        self.browser = browser
        self.log = logger
        self.tab: str | None = None

    # ------------------------------------------------------------ 基础

    def _run(self, args: list[str], timeout: int = 90) -> str:
        proc = subprocess.run([self.browse, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        if proc.returncode != 0:
            raise BrowserError((proc.stderr or proc.stdout or "").strip()[:300]
                               or f"browse {args[0]} 失败")
        return (proc.stdout or "").strip()

    def _json(self, args: list[str], timeout: int = 90):
        raw = self._run(args, timeout)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return _first_json(raw)

    def _obj(self, cmd: str, payload: dict, timeout: int = 90):
        payload = {"tab": int(self.tab) if str(self.tab).isdigit() else self.tab,
                   **payload}
        return self._json([cmd, "--json", json.dumps(payload, ensure_ascii=False)], timeout)

    # ------------------------------------------------------------ 导航

    def navigate(self, url: str) -> str:
        raw = self._run(["navigate", url, f"--browser={self.browser}"])
        data = json.loads(raw)
        self.tab = str(data.get("tab"))
        self.log.info("打开 %s (tab=%s, status=%s)", url[:70], self.tab, data.get("status"))
        return self.tab

    def tabs(self) -> list[dict]:
        data = _first_json(self._run(["tabs"])) or {}
        return data.get("tabs") or []

    def close_tab(self, tab: str | int) -> None:
        try:
            self._run(["tabs", "close", "--tab", str(tab)])
        except BrowserError:
            pass

    def close_proton_tabs(self, keep: str | None = None) -> int:
        n = 0
        for t in self.tabs():
            if "account.proton.me" in (t.get("url") or "") and str(t.get("tab")) != str(keep):
                self.close_tab(t["tab"])
                n += 1
        return n

    # ------------------------------------------------------------ 读

    def snapshot(self, tries: int = 3) -> str:
        last = ""
        for i in range(tries):
            try:
                return self._run(["snapshot", f"--tab={self.tab}"], timeout=45)
            except BrowserError as exc:
                last = str(exc)
                self.log.debug("snapshot 重试 %d: %s", i + 1, last[:120])
                time.sleep(1.2)
        raise BrowserError(f"snapshot 失败: {last[:160]}")

    def eval_js(self, code: str, timeout: int = 60):
        b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
        raw = self._run(["eval", f"eval(atob('{b64}'))", f"--tab={self.tab}"], timeout)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    def eval_expr(self, expression: str, frame: str | None = None, timeout: int = 60):
        payload = {"expression": expression}
        if frame:
            payload["frame"] = frame
        return self._obj("eval", payload, timeout)

    def frame_eval(self, frame: str, expression: str, timeout: int = 60):
        return self.eval_expr(expression, frame=frame, timeout=timeout)

    def state(self) -> dict:
        return self.eval_js("""JSON.stringify({
            url: location.href,
            txt: (document.body && document.body.innerText || '').slice(0, 1500),
            ids: [...document.querySelectorAll('input')].map(e=>e.id||e.name||e.type),
            btns: [...document.querySelectorAll('button')].map(b=>b.innerText.trim()).filter(Boolean).slice(0,30)
        })""") or {}

    def page_state(self) -> dict:
        """只读诊断快照：弹窗原始样式事实 + 按钮/表单/客户端校验。

        可见性判定放在 Python（`dialog_visible`），JS 只回传原始样式，便于离线测试。
        """
        for i in range(3):
            try:
                st = self.eval_js("""JSON.stringify((() => {
          const q = s => document.querySelector(s);
          const styleOf = el => {
            const cs = getComputedStyle(el);
            return { display: cs.display, visibility: cs.visibility, opacity: cs.opacity };
          };
          const dlg = q('dialog.human-verification-modal') || q('dialog');
          const btn = [...document.querySelectorAll('button')]
            .find(b => /Start using Proton Mail now|下一步/.test(b.innerText || ''));
          const errNodes = [...document.querySelectorAll('[class*=error],[role=alert]')]
            .map(e => (e.innerText || '').trim()).filter(Boolean);
          const uname = q('#username');
          const rect = dlg ? dlg.getBoundingClientRect() : null;
          const ancestors = [];
          for (let el = dlg && dlg.parentElement; el && el !== document.documentElement;
               el = el.parentElement) ancestors.push(styleOf(el));
          const tabs = dlg ? [...dlg.querySelectorAll('[role=tab]')] : [];
          return {
            url: location.href,
            txt: (document.body ? document.body.innerText : '').slice(0, 600),
            cap: document.querySelectorAll('iframe[src*="/core/v4/captcha"]').length,
            hasConfirm: !!q('#password-confirm'),
            confirmLen: (q('#password-confirm') || {}).value ? q('#password-confirm').value.length : 0,
            dialogExists: !!dlg,
            dialogOpenAttr: !!(dlg && dlg.hasAttribute('open')),
            dialogClass: dlg ? (dlg.className || '') : null,
            dialogStyle: dlg ? styleOf(dlg) : null,
            dialogRect: rect ? { width: rect.width, height: rect.height } : null,
            ancestors,
            tabCount: tabs.length,
            tabs: tabs.map(t => (t.innerText || '').trim()),
            buttonLoading: !!(btn && /Loading/i.test(btn.innerText || '')),
            buttonDisabled: !!(btn && btn.disabled),
            usernameValuePresent: !!(uname && uname.value),
            usernameInvalid: errNodes.some(t => /not valid|invalid/i.test(t)),
            usernameError: errNodes.find(t => /not valid|invalid/i.test(t)) || null,
            errors: errNodes.slice(0, 4),
            passwordComplete: !!(q('#password') && q('#password').value),
            failed: /没有通过验证|did not pass/.test(document.body.innerText),
            timeoutMsg: /验证超时|timed out/.test(document.body.innerText)
          };
        })())""")
                if st:
                    st["dialogVisible"] = dialog_visible(st)
                    st["challengeUiInitialized"] = challenge_ui_initialized(st)
                    return st
            except Exception:
                pass
            time.sleep(1.0)
        return {}

    def wait_for(self, predicate_js: str, timeout: int = 30, interval_ms: int = 400) -> bool:
        code = """
        (async () => {
          for (let i = 0; i < %d; i++) {
            try { if (eval(%s)) return true; } catch (e) {}
            await new Promise(r => setTimeout(r, %d));
          }
          return false;
        })()
        """ % (int(timeout * 1000 / interval_ms), json.dumps(predicate_js), interval_ms)
        return bool(self.eval_js(code, timeout=timeout + 10))

    # ------------------------------------------------------------ 写

    def click_sel(self, selector: str):
        return self._obj("click", {"selector": selector})

    def type_sel(self, selector: str, text: str, delay: int = 35):
        return self._obj("type", {"selector": selector, "text": text, "delay": delay})

    def fill_sel(self, selector: str, value: str):
        return self._obj("fill", {"selector": selector, "value": value})

    def press_sel(self, selector: str, key: str):
        return self._obj("press", {"selector": selector, "key": key})

    def click_ref(self, ref: str):
        return self._obj("click", {"ref": ref})

    def fill_ref(self, ref: str, value: str):
        return self._obj("fill", {"ref": ref, "value": value})

    def type_text(self, text: str, ref: str | None = None, delay: int = 35):
        payload: dict = {"text": text, "delay": delay}
        if ref:
            payload["ref"] = ref
        return self._obj("type", payload)

    def press(self, key: str):
        return self._obj("press", {"key": key})

    def drag(self, fx: float, fy: float, tx: float, ty: float, steps: int = 22):
        return self._obj("drag", {"from": {"x": fx, "y": fy},
                                  "to": {"x": tx, "y": ty}, "steps": steps})

    def execute(self, code: str, timeout: int = 90):
        return self._obj("execute", {"code": code}, timeout)

    def screenshot(self, path: str) -> None:
        self._run(["screenshot", path, f"--tab={self.tab}"], timeout=90)

    def restart(self) -> None:
        """relay 卡死时重启（agent 标签页保留）。"""
        self._run(["restart"], timeout=180)

    # ------------------------------------------------------------ 便捷

    @staticmethod
    def find_ref(snapshot_text: str, role: str, name: str, prefix: bool = False) -> str | None:
        """从 snapshot 里取 ref。prefix=True 时允许名字后带状态后缀（如 "xxx Loading"）。"""
        import re
        tail = r'[^"]*' if prefix else ''
        m = re.search(rf'{role}\s+"{re.escape(name)}{tail}"\s+\[ref=(e\d+)[^\]]*\]', snapshot_text)
        return m.group(1) if m else None

    def ref_of(self, role: str, name: str, prefix: bool = False) -> str | None:
        return self.find_ref(self.snapshot(), role, name, prefix=prefix)
