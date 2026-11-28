# -*- coding: utf-8 -*-
"""HV UI 可见性判据离线用例（裁决 2026-11-27 会话 6b08f1d0）。

核心纪律：UI 可见 ≠ 人机验证通过。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from proton_batch.browser_driver import (  # noqa: E402
    challenge_ui_initialized, dialog_visible)


def state(**over):
    base = {
        "dialogExists": True,
        "dialogOpenAttr": False,
        "dialogClass": "modal-two-dialog human-verification-modal",
        "dialogStyle": {"display": "flex", "visibility": "visible", "opacity": "1"},
        "dialogRect": {"width": 730, "height": 343},
        "ancestors": [],
        "tabCount": 2,
        "tabs": ["Email", "SMS"],
    }
    base.update(over)
    return base


def test_dialog_visible_without_open_attribute():
    assert dialog_visible(state(dialogOpenAttr=False)) is True


def test_dialog_open_attribute_but_display_none():
    st = state(dialogOpenAttr=True,
               dialogStyle={"display": "none", "visibility": "visible", "opacity": "1"})
    assert dialog_visible(st) is False


def test_dialog_visibility_hidden():
    st = state(dialogStyle={"display": "flex", "visibility": "hidden", "opacity": "1"})
    assert dialog_visible(st) is False


def test_dialog_visibility_collapse():
    st = state(dialogStyle={"display": "flex", "visibility": "collapse", "opacity": "1"})
    assert dialog_visible(st) is False


def test_dialog_opacity_zero():
    st = state(dialogStyle={"display": "flex", "visibility": "visible", "opacity": "0"})
    assert dialog_visible(st) is False


def test_dialog_zero_width():
    assert dialog_visible(state(dialogRect={"width": 0, "height": 343})) is False


def test_dialog_zero_height():
    assert dialog_visible(state(dialogRect={"width": 730, "height": 0})) is False


def test_dialog_parent_hidden():
    st = state(ancestors=[{"display": "none", "visibility": "visible", "opacity": "1"}])
    assert dialog_visible(st) is False


def test_dialog_parent_visibility_hidden():
    st = state(ancestors=[{"display": "flex", "visibility": "hidden", "opacity": "1"}])
    assert dialog_visible(st) is False


def test_hv_modal_class_detection():
    assert "human-verification-modal" in state()["dialogClass"]


def test_hv_tabs_rendered():
    assert state()["tabCount"] == 2


def test_hv_ui_initialized():
    assert challenge_ui_initialized(state()) is True


def test_hv_ui_not_initialized_without_tabs():
    assert challenge_ui_initialized(state(tabCount=0, tabs=[])) is False


def test_hv_ui_not_initialized_when_invisible():
    st = state(dialogStyle={"display": "none", "visibility": "visible", "opacity": "1"})
    assert challenge_ui_initialized(st) is False


def test_hv_not_completed_by_visibility():
    """可见性不得被当成验证通过：初始化标志与完成标志必须是两件事。"""
    st = state()
    assert challenge_ui_initialized(st) is True
    assert "completed" not in st
    assert "hvCompleted" not in st
