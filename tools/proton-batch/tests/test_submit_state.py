# -*- coding: utf-8 -*-
"""提交状态机离线用例（不触网、不需要浏览器）。

对应裁决（2026-11-27 会话 6b08f1d0）：
  422 abusive                -> NETWORK_REJECTED
  422 Human verification     -> HV_REQUIRED
  422 缺少 Details           -> 不猜测，按无明确结果处理
  dialog 存在但未打开        -> HV_REQUIRED(ui_ready=False)，不认定验证完成
  Loading 超时且无响应       -> SUBMISSION_STALLED
  网络/传输异常              -> TRANSPORT_ERROR
  反滥用拒绝                 -> 不触发自动节点轮换
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from proton_batch.browser_driver import BrowserError  # noqa: E402
from proton_batch.proton_browser import (  # noqa: E402
    HvRequired, NetworkRejected, ProtonBrowserSignup, SubmissionStalled, TransportError)

BASE_STATE = {
    "url": "https://account.proton.me/signup",
    "txt": "",
    "hasConfirm": True,
    "confirmLen": 24,
    "dialogVisible": False,
    "hvRequired": False,
    "dialogExists": True,
    "dialogOpenAttr": False,
    "challengeUiInitialized": False,
    "tabCount": 2,
    "tabs": ["Email", "SMS"],
    "buttonLoading": True,
    "buttonDisabled": True,
    "usernameValuePresent": True,
    "usernameInvalid": False,
    "usernameError": None,
    "errors": [],
    "passwordComplete": True,
    "failed": False,
    "timeoutMsg": False,
}

HV_BODY = (
    '{"Code":"x","Error":"Human verification required","Details":{'
    '"HumanVerificationToken":"T","HumanVerificationMethods":["sms","email","coupon"],'
    '"Direct":1,"Title":"Human Verification",'
    '"WebUrl":"https://verify.proton.me/?methods=sms%2Cemail%2Ccoupon&token=T",'
    '"ExpiresAt":1795751018}}'
)
ABUSE_BODY = ('{"Code":"x","Error":"We are detecting potentially abusive traffic '
              'coming from your network and have blocked any further signups.","Details":{}}')


class StubDriver:
    """按顺序回放 page_state；记录点击次数。"""

    def __init__(self, states, capture=None, click_error=None):
        self._states = list(states)
        self._capture = capture or {}
        self._click_error = click_error
        self.clicks = 0

    def page_state(self):
        return dict(self._states.pop(0) if len(self._states) > 1 else self._states[0])

    def click_sel(self, selector):
        self.clicks += 1
        if self._click_error:
            raise self._click_error

    def _obj(self, cmd, payload, timeout=90):
        if cmd != "network":
            return {}
        return self._capture if payload.get("action") == "stop" else {}

    def snapshot(self):
        return ""


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    """观察器按 1.5s 轮询：压掉 sleep，并让 time.time 每次前进 2s。"""
    monkeypatch.setattr("proton_batch.proton_browser.time.sleep", lambda *_: None)
    clock = {"t": 1_000_000.0}

    def _tick():
        clock["t"] += 2.0
        return clock["t"]

    monkeypatch.setattr("proton_batch.proton_browser.time.time", _tick)


def make_signup(driver, tmp_path):
    cfg = {
        "proton": {"signup_page": "https://account.proton.me/signup"},
        "storage": {"work_dir": str(tmp_path)},
        "_root": str(tmp_path),
    }
    return ProtonBrowserSignup(cfg, driver, None, logging.getLogger("t"))


def users_capture(status, body):
    return {"exchanges": [{
        "request": {"method": "POST",
                    "url": "https://account.proton.me/api/core/v4/users"},
        "response": {"status": status, "body": {"text": body}},
    }]}


def run(driver, tmp_path):
    return make_signup(driver, tmp_path)._submit_and_classify("pw")


def test_422_abusive_is_network_rejected(tmp_path):
    driver = StubDriver([BASE_STATE], capture=users_capture(422, ABUSE_BODY))
    with pytest.raises(NetworkRejected):
        run(driver, tmp_path)


def test_422_hv_is_hv_required_not_rejected(tmp_path):
    driver = StubDriver([BASE_STATE], capture=users_capture(422, HV_BODY))
    with pytest.raises(HvRequired) as exc:
        run(driver, tmp_path)
    assert exc.value.ui_ready is False            # dialog 存在但未打开
    assert exc.value.details["HumanVerificationMethods"] == ["sms", "email", "coupon"]


def test_422_without_details_is_not_guessed(tmp_path):
    driver = StubDriver([BASE_STATE], capture=users_capture(422, '{"Error":"weird"}'))
    with pytest.raises(SubmissionStalled):
        run(driver, tmp_path)


def test_dialog_open_is_hv_required_ui_ready(tmp_path):
    opened = dict(BASE_STATE, dialogVisible=True, dialogOpenAttr=True,
                  challengeUiInitialized=True)
    driver = StubDriver([opened])
    with pytest.raises(HvRequired) as exc:
        run(driver, tmp_path)
    assert exc.value.ui_ready is True
    assert driver.clicks == 0                     # 已打开就不再点


def test_loading_timeout_is_stalled_and_clicks_once(tmp_path):
    driver = StubDriver([BASE_STATE], capture={"exchanges": []})
    with pytest.raises(SubmissionStalled):
        run(driver, tmp_path)
    assert driver.clicks == 1                     # 不重复提交


def test_client_validation_error_is_stalled(tmp_path):
    invalid = dict(BASE_STATE, buttonLoading=False, buttonDisabled=False,
                   usernameInvalid=True, usernameError="Email is not valid")
    driver = StubDriver([invalid], capture={"exchanges": []})
    with pytest.raises(SubmissionStalled) as exc:
        run(driver, tmp_path)
    assert "Email is not valid" in str(exc.value)


def test_click_failure_is_transport_error(tmp_path):
    driver = StubDriver([BASE_STATE], click_error=BrowserError("button is disabled"))
    with pytest.raises(TransportError):
        run(driver, tmp_path)


def test_other_status_is_stalled(tmp_path):
    driver = StubDriver([dict(BASE_STATE, buttonLoading=False, buttonDisabled=False)],
                        capture=users_capture(500, '{"Error":"boom"}'))
    with pytest.raises(SubmissionStalled):
        run(driver, tmp_path)


def test_evidence_written_on_failure(tmp_path):
    driver = StubDriver([BASE_STATE], capture=users_capture(422, ABUSE_BODY))
    with pytest.raises(NetworkRejected):
        run(driver, tmp_path)
    assert (tmp_path / "submit_evidence.json").exists()


def test_rotation_only_between_accounts_not_on_rejection():
    """换出口只发生在账号之间（HV 方式按 IP 分配）；被拒绝时不得换节点重试。"""
    src = (ROOT / "proton_batch" / "pipeline.py").read_text(encoding="utf-8")
    assert "if rotator and index > 1:" in src
    # NetworkRejected 分支必须是终止（raise），不能出现 next_node
    start = src.index("except NetworkRejected")
    end = src.index("except ", start + 10)
    block = src[start:end]
    assert "raise" in block
    assert "next_node" not in block
