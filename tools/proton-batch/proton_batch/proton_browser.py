# -*- coding: utf-8 -*-
"""Proton 注册 —— 真实浏览器 UI 驱动（实测可行，2026-11）。

实测事实：
- 纯 HTTP `/api/core/v4/users` 会被 `potentially abusive traffic` 拦；
- 页面上任何 JS 赋值 + dispatchEvent 的输入同样被判为合成流量，
  必须用真实键盘事件（browse `type` / `click --ref`）；
- 注册第二步弹出 Human Verification，CAPTCHA 是自研拼图，不是 hCaptcha；
  Email 通道对 mail.tm 域名已封（`Email address verification temporarily disabled`）；
- 通过后依次是：恢复短语 → 显示名 → /apps。

因此本模块只做一件事：用真实输入把这条 UI 路径走通。
"""
from __future__ import annotations

import json
import logging
import re
import time

from .browser_driver import BrowseDriver, BrowserError
from .captcha_solver import PuzzleSolver
from .proton_logout import logout

BLOCK_MARKERS = (
    "potentially abusive traffic",
    "blocked any further signups",
    "too many attempts",
)

USER_FRAME = 'iframe[title="Email address"]'
PWD_SEL = "#password"
PWD_CONFIRM_SEL = "#password-confirm"
SUBMIT_SEL = 'button:has-text("Start using Proton Mail now")'
NEW_ACCOUNT_SEL = 'button:has-text("create a new account")'
SUBMIT_LABELS = ("Start using Proton Mail now", "下一步", "Next", "Create account")
NEXT_LABELS = ("下一步", "Next")
FAIL_MARKERS = ("没有通过验证", "did not pass", "验证超时", "重试", "retry")
OK_MARKERS = ("recovery phrase", "secure your account", "set a display name")


class NetworkRejected(RuntimeError):
    """Proton 明确网络级反滥用拒绝（422 abusive）。绝不可重试掩盖。"""


# 兼容旧名
ProtonBlocked = NetworkRejected


class HvRequired(RuntimeError):
    """服务端签发人机校验挑战（422 Human verification required）。

    ui_ready=False 表示挑战票据已签发但前端未展示（dialog 未打开）。
    """

    def __init__(self, message: str, *, ui_ready: bool, details: dict | None = None):
        super().__init__(message)
        self.ui_ready = ui_ready
        self.details = details or {}


class SubmissionStalled(RuntimeError):
    """提交后状态不明（无明确服务端结果）。不得自动重提。"""


class TransportError(RuntimeError):
    """浏览器/网络层失败，与风控无关。"""


class SubmissionStatus:
    SUCCESS = "SUCCESS"
    HV_REQUIRED = "HV_REQUIRED"
    NETWORK_REJECTED = "NETWORK_REJECTED"
    SUBMISSION_STALLED = "SUBMISSION_STALLED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"


class ChallengeUnavailable(RuntimeError):
    """本次 HV 只提供 Email/SMS，没有 CAPTCHA 页签 —— 换号重试即可。"""


def _work_dir(cfg: dict):
    from pathlib import Path
    storage = cfg.get("storage") or {}
    root = Path(cfg.get("_root") or ".")
    rel = storage.get("work_dir") or "data/work"
    p = Path(rel)
    return p if p.is_absolute() else root / p


class ProtonBrowserSignup:
    def __init__(self, cfg: dict, driver: BrowseDriver, mail_provider, logger: logging.Logger):
        self.cfg = cfg["proton"]
        self._full_cfg = cfg
        self.mail_cfg = cfg.get("mail") or {}
        self.d = driver
        self.mail = mail_provider          # 保留兼容；当前路径不需要外部邮箱
        self.log = logger
        self.solver = PuzzleSolver(driver, logger, _work_dir(cfg))
        self.recovery: str | None = None

    # ------------------------------------------------------------ 工具

    def _state(self) -> dict:
        st = self.d.state()
        text = (st.get("txt") or "").lower()
        for marker in BLOCK_MARKERS:
            if marker in text:
                raise ProtonBlocked(
                    "Proton 风控拦截（页面原文: %s）" % marker)
        return st

    def _ref(self, role: str, *names: str, prefix: bool = False) -> str | None:
        try:
            snap = self.d.snapshot()
        except BrowserError:
            return None
        for name in names:
            ref = self.d.find_ref(snap, role, name, prefix=prefix)
            if ref:
                return ref
        return None

    def _observe_hv_ui(self) -> dict:
        """观察 HV 挑战 UI：是否存在、是否可见、页签是否渲染。

        返回 {"present", "visible", "initialized", "tabs"}；不推断验证是否通过。
        """
        st = self.d.page_state()
        return {
            "present": bool(st.get("dialogExists")),
            "visible": bool(st.get("dialogVisible")),
            "initialized": bool(st.get("challengeUiInitialized")),
            "tabs": [str(t) for t in (st.get("tabs") or [])],
            "tabCount": int(st.get("tabCount") or 0),
        }

    def _select_captcha_tab(self) -> bool:
        """HV 页签可能是 CAPTCHA / Email / SMS；只处理 CAPTCHA。"""
        for _ in range(6):
            ui = self._observe_hv_ui()
            if not ui["visible"]:
                time.sleep(1.2)
                continue
            if any("CAPTCHA" in t.upper() for t in ui["tabs"]):
                try:
                    self.d.click_sel('[role="tab"]:has-text("CAPTCHA")')
                    time.sleep(1.0)
                except BrowserError as exc:
                    self.log.warning("点 CAPTCHA tab 失败: %s", str(exc)[:100])
                return True
            return False
        return False

    def _outcome(self) -> str | None:
        """ok / failed / None —— 失败文案在 captcha iframe 里，只能看 snapshot。"""
        st = self.d.page_state()
        if "signup" not in (st.get("url") or ""):
            return "ok"
        try:
            snap = self.d.snapshot().lower()
        except BrowserError:
            return None
        for marker in OK_MARKERS:
            if marker in snap:
                return "ok"
        for marker in FAIL_MARKERS:
            if marker in snap:
                return "failed"
        return None

    def _username_value(self) -> str | None:
        try:
            v = self.d.eval_expr(
                "() => { const e = document.querySelector('input'); return e ? e.value : null; }",
                frame=USER_FRAME)
        except BrowserError:
            return None
        return v if isinstance(v, str) else None

    # ------------------------------------------------------------ 表单

    def _wait_user_input(self, timeout: int = 25) -> bool:
        """用户名 input 在 iframe 里（主文档的 #username 是隐藏镜像），必须进 frame 等。"""
        js = """
try {
  await page.frameLocator(%r).locator('input').first()
    .waitFor({ state: 'visible', timeout: %d });
  return { ok: true };
} catch (e) { return { ok: false }; }
""" % (USER_FRAME, timeout * 1000)
        try:
            res = self.d.execute(js, timeout=timeout + 15)
        except BrowserError:
            return False
        return bool(isinstance(res, dict) and res.get("ok"))

    def _prepare_form(self) -> bool:
        """等注册表单就绪；已登录态先登出再回到注册页。"""
        for i in range(20):
            if self._wait_user_input(timeout=8):
                return True
            try:
                self.d.click_sel(NEW_ACCOUNT_SEL)
                self.log.info("检测到已登录态，先登出再重新注册")
                if logout(self.d, self.log):
                    self.d.navigate(self.cfg["signup_page"])
                    time.sleep(3.0)
                else:
                    time.sleep(2.5)
            except BrowserError:
                time.sleep(1.0)
        raise BrowserError("用户名输入框未出现")

    def _type_username(self, username: str) -> None:
        """iframe 里的 input 用 frameLocator 聚焦 + 真实键盘输入（CDP 事件，不是 JS 赋值）。"""
        js = """
const inp = page.frameLocator(%r).locator('input').first();
await inp.click();
await page.keyboard.press('Control+A');
await page.keyboard.type(%s, { delay: 55 });
await page.waitForTimeout(250);
return { ok: true };
""" % (USER_FRAME, json.dumps(username))
        got = None
        for attempt in range(4):
            try:
                self.d.execute(js, timeout=120)
            except BrowserError as exc:
                self.log.warning("输入用户名失败(%d): %s", attempt + 1, str(exc)[:110])
                time.sleep(1.2)
                continue
            time.sleep(0.5)
            got = self._username_value()
            if got == username:
                self.log.info("用户名写入成功（%d 次）", attempt + 1)
                return
            self.log.warning("用户名不匹配 %r，重试", got)
            time.sleep(0.6)
        raise BrowserError(f"用户名写入失败: {got!r}")

    def _field_len(self, dom_id: str) -> int:
        v = self.d.eval_js(
            f"JSON.stringify((document.getElementById({dom_id!r})||{{value:''}}).value.length)")
        try:
            return int(v)
        except (TypeError, ValueError):
            return -1

    def _field_exists(self, dom_id: str) -> bool:
        try:
            return bool(self.d.eval_expr(
                f"() => !!document.getElementById({dom_id!r})"))
        except BrowserError:
            return False

    def _fill_all_passwords(self, password: str, tries: int = 2) -> int:
        """能填的密码框都填到正确长度；先 fill，不行再用真实键盘。"""
        want = len(password)
        for attempt in range(tries):
            for sel, dom_id in ((PWD_SEL, "password"), (PWD_CONFIRM_SEL, "password-confirm")):
                if not self._field_exists(dom_id) or self._field_len(dom_id) == want:
                    continue
                try:
                    self.d.fill_sel(sel, password)
                    time.sleep(0.35)
                except BrowserError as exc:
                    self.log.debug("%s fill 失败: %s", dom_id, str(exc)[:80])
                if self._field_len(dom_id) != want:
                    try:
                        self.d.press_sel(sel, "Control+A")
                        self.d.type_sel(sel, password, delay=12)
                        time.sleep(0.4)
                    except BrowserError as exc:
                        self.log.debug("%s 键盘写入失败: %s", dom_id, str(exc)[:80])
            ok = self._field_len("password") == want and (
                not self._field_exists("password-confirm")
                or self._field_len("password-confirm") == want)
            if ok:
                break
            time.sleep(0.6)
        n_pw = self._field_len("password")
        n_cf = self._field_len("password-confirm")
        self.log.info("密码框: password=%s confirm=%s (want %d)", n_pw, n_cf, want)
        return int(n_pw == want) + int(n_cf == want)

    def _submit_and_classify(self, password: str) -> str:
        """提交一次并分类：SUCCESS / 明确拒绝抛 NetworkRejected / 挑战抛 HvRequired /
        停滞抛 SubmissionStalled / 传输异常抛 TransportError。

        一次调用只点一次按钮，不重复提交。
        """
        st = self.d.page_state()
        if st.get("dialogVisible"):
            raise HvRequired("人机校验界面已可见", ui_ready=True)

        if st.get("hasConfirm") and not st.get("confirmLen"):
            self._fill_all_passwords(password)

        self._capture("start")
        try:
            self.d.click_sel(SUBMIT_SEL)
        except BrowserError as exc:
            self._capture("stop")
            raise TransportError(f"提交按钮不可点: {str(exc)[:120]}") from exc

        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(1.5)
            st = self.d.page_state()
            if st.get("dialogVisible"):
                self._capture("stop")
                self.log.info("提交分类: HV_REQUIRED（UI 可见）tabs=%s", st.get("tabs"))
                raise HvRequired("人机校验界面已可见", ui_ready=True)
            if st.get("buttonLoading") or st.get("buttonDisabled"):
                continue
            break

        st = self.d.page_state()
        status, body = self._users_exchange(self._capture("stop"))
        details = self._parse_details(body)
        self._save_evidence(st, status, body)
        self.log.info(
            "提交分类依据: http=%s error=%s methods=%s dialogExists=%s dialogVisible=%s "
            "dialogOpenAttr=%s challengeUiInitialized=%s tabCount=%s buttonLoading=%s "
            "usernameInvalid=%s errors=%s",
            status, details.get("Error"), details.get("HumanVerificationMethods"),
            st.get("dialogExists"), st.get("dialogVisible"), st.get("dialogOpenAttr"),
            st.get("challengeUiInitialized"), st.get("tabCount"), st.get("buttonLoading"),
            st.get("usernameInvalid"), st.get("errors"))

        err = (details.get("Error") or "").lower()
        if status == 422 and "abusive" in err:
            raise NetworkRejected(f"网络级反滥用拒绝（HTTP 422）: {details.get('Error')}")
        if status == 422 and "human verification" in err:
            raise HvRequired(
                "服务端要求人机校验（HTTP 422）",
                ui_ready=bool(st.get("dialogVisible")),
                details=details)
        self._check_blocked()
        if st.get("usernameInvalid"):
            raise SubmissionStalled(
                f"客户端校验失败: {st.get('usernameError')}（未提交到服务端）")
        if status is None:
            raise SubmissionStalled(
                "无明确服务端结果：按钮 Loading/disabled 且未捕获到 /api/core/v4/users 响应")
        raise SubmissionStalled(f"创建请求返回 HTTP {status}，未进入人机校验")

    @staticmethod
    def _parse_details(body: str) -> dict:
        """从 422 响应体里取 Error 与 Details（脱敏后使用）。"""
        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError:
            return {"Error": (body or "")[:200]}
        details = dict(data.get("Details") or {})
        details["Error"] = data.get("Error")
        return details

    # ------------------------------------------------------------ 诊断支撑

    def _capture(self, action: str) -> dict:
        try:
            if action == "start":
                return self.d._obj("network", {
                    "action": "start", "urlFilter": "proton.me",
                    "methods": ["POST"], "maxDurationMs": 180000}, timeout=60) or {}
            return self.d._obj("network", {"action": "stop"}, timeout=120) or {}
        except BrowserError as exc:
            self.log.debug("network %s 失败: %s", action, str(exc)[:100])
            return {}

    @staticmethod
    def _users_exchange(cap: dict) -> tuple[int | None, str]:
        for ex in (cap.get("exchanges") or []):
            url = ((ex.get("request") or {}).get("url") or "")
            if "/api/core/v4/users" in url:
                resp = ex.get("response") or {}
                return resp.get("status"), ((resp.get("body") or {}).get("text") or "")
        return None, ""

    def _save_evidence(self, st: dict, status: int | None, body: str) -> None:
        """失败现场留档：页面状态 + 服务端原文。"""
        try:
            path = _work_dir(self._full_cfg) / "submit_evidence.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "http_status": status,
                "response_body": body[:2000],
                "page_state": st,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            self.log.info("现场已留档: %s", path)
        except OSError as exc:
            self.log.warning("现场留档失败: %s", str(exc)[:100])

    def _check_blocked(self) -> None:
        """abusive 提示只在 snapshot 的 alert 里出现，page_state 的 txt 看不到。"""
        try:
            snap = self.d.snapshot()
        except BrowserError:
            return
        low = snap.lower()
        for marker in BLOCK_MARKERS:
            if marker in low:
                raise ProtonBlocked(f"Proton 风控拦截（{marker}）")

    # ------------------------------------------------------------ 后续步骤

    def _read_recovery(self) -> str | None:
        """点 Show 展开恢复短语，从正文里抓 12 个单词。"""
        ref = self._ref("button", "Show")
        if ref:
            self.d.click_ref(ref)
            time.sleep(1.2)
        text = (self.d.state().get("txt") or "")
        for line in text.splitlines():
            words = line.split()
            if len(words) == 12 and all(re.fullmatch(r"[a-z]{3,9}", w) for w in words):
                return " ".join(words)
        m = re.search(r"((?:[a-z]{3,9} ){11}[a-z]{3,9})", text)
        return m.group(1) if m else None

    def _email_challenge(self, password: str) -> None:
        """HV Email 形态：填真实邮箱 → 收码 → 提交。需要 mail.* IMAP 可用。"""
        from .mailbox import Mailbox

        address = (self.mail_cfg.get("address") or self.mail_cfg.get("username") or "").strip()
        if not address:
            raise ChallengeUnavailable("HV 为 Email 形态，但未配置 mail.address")
        mailbox = Mailbox({"mail": self.mail_cfg}, self.log)

        try:
            self.d.click_sel('dialog [role="tab"]:has-text("Email")')
            time.sleep(1.0)
        except BrowserError:
            pass

        filled = False
        for sel in ('dialog input[type="email"]', 'dialog input'):
            try:
                self.d.fill_sel(sel, address)
                filled = True
                break
            except BrowserError:
                continue
        if not filled:
            raise BrowserError("HV Email 输入框未出现")

        time.sleep(0.6)
        try:
            self.d.click_sel('dialog button:has-text("Get verification code")')
        except BrowserError as exc:
            raise BrowserError(f"点 Get verification code 失败: {str(exc)[:100]}")

        # 域名被拉黑 / 非 Proton 地址限制 会弹 alert；成功也会弹 "Code sent to ..."
        time.sleep(5)
        alert = re.search(r'- alert "([^"]{10,200})"', self.d.snapshot())
        if alert:
            text = alert.group(1)
            if re.search(r"code sent", text, re.I):
                self.log.info("验证码已发送: %s", text[:90])
            elif re.search(r"too many verification codes", text, re.I):
                raise ChallengeUnavailable("该收件地址发码次数已用尽，换出口重试")
            elif re.search(r"not valid|disabled|non-?proton|cannot|too many", text, re.I):
                raise BrowserError(f"Proton 拒绝该邮箱: {text[:120]}")
            else:
                self.log.warning("未识别的提示: %s", text[:120])

        since = time.time()
        code = mailbox.wait_code(since_ts=since)
        self.log.info("收到 Proton 验证码")

        typed = False
        for sel in ('dialog input[type="text"]', 'dialog input:not([type="email"])',
                    'dialog input'):
            try:
                self.d.fill_sel(sel, code)
                typed = True
                break
            except BrowserError:
                continue
        if not typed:
            raise BrowserError("验证码输入框未出现")

        time.sleep(0.4)
        for sel in ('dialog button:has-text("Verify")', 'dialog button:has-text("Confirm")',
                    'dialog button:has-text("Continue")', 'dialog button:has-text("下一步")',
                    'dialog button:has-text("Next")'):
            try:
                self.d.click_sel(sel)
                return
            except BrowserError:
                continue
        raise BrowserError("找不到验证码提交按钮")

    def _finish_steps(self, display_name: str) -> str:
        """恢复短语 → 显示名 → 离开 signup。返回最终 URL。"""
        deadline = time.time() + 180
        while time.time() < deadline:
            st = self.d.page_state()
            url = st.get("url") or ""
            if not url:
                time.sleep(1.5)
                continue
            if "signup" not in url:
                return url
            txt = (st.get("txt") or "").lower()

            if "loading" in txt or "creating your account" in txt or "preparing" in txt:
                time.sleep(2)
                continue

            if "recovery phrase" in txt:
                phrase = self._read_recovery()
                if phrase and phrase != self.recovery:
                    self.recovery = phrase
                    self.log.info("恢复短语: %s", phrase)
                ref = self._ref("checkbox", "checkbox")
                if ref:
                    self.d.click_ref(ref)
                    time.sleep(0.8)

            if "display name" in txt:
                ref = self._ref("textbox", "Display name")
                if ref:
                    self.d.fill_ref(ref, display_name)
                    time.sleep(0.4)

            ref = self._ref("button", "Continue", *NEXT_LABELS, "Get started", prefix=True)
            if ref:
                self.d.click_ref(ref)
                time.sleep(4)
                continue
            time.sleep(2)

        return (self.d.page_state().get("url") or "")

    # ------------------------------------------------------------ 主流程

    def signup(self, username: str, password: str) -> dict:
        d = self.d
        try:
            d.close_proton_tabs()
            d.navigate(self.cfg["signup_page"])
        except BrowserError as exc:
            self.log.warning("浏览器无响应，重启 relay：%s", str(exc)[:80])
            d.restart()
            time.sleep(3)
            d.navigate(self.cfg["signup_page"])
        time.sleep(3.0)

        self._prepare_form()
        self._type_username(username)
        if not self._fill_all_passwords(password):
            raise BrowserError("密码输入框未出现")

        try:
            self._submit_and_classify(password)
        except HvRequired as exc:
            if not exc.ui_ready:
                raise
            ui = self._observe_hv_ui()
            self.log.info("HV UI 可见（initialized=%s tabs=%s），进入验证流程",
                          ui["initialized"], ui["tabs"])

        if not self._select_captcha_tab():
            self.log.info("HV 为 Email 形态，走收码通道")
            self._email_challenge(password)
            return self._after_challenge(username)

        time.sleep(1.2)
        solved = self.solver.solve()
        if not solved.get("ok"):
            raise BrowserError(f"验证码未解出: {solved.get('reason')}")

        ref = self._ref("button", *NEXT_LABELS, prefix=True)
        if not ref:
            raise BrowserError("验证码后找不到下一步按钮")
        d.click_ref(ref)
        self.log.info("已提交验证码，等待结果")
        return self._after_challenge(username)

    def _after_challenge(self, username: str) -> dict:
        outcome = None
        deadline = time.time() + 60
        while time.time() < deadline:
            outcome = self._outcome()
            if outcome:
                break
            time.sleep(1.5)
        if outcome != "ok":
            raise BrowserError(f"人机校验未通过（outcome={outcome}）")

        final_url = self._finish_steps(display_name=username)
        if "signup" in final_url:
            raise BrowserError(f"仍停留在注册页: {final_url[:90]}")
        self.log.info("注册完成: %s", final_url[:90])
        return {"username": username, "email": f"{username}@proton.me",
                "recovery": self.recovery, "final_url": final_url}
