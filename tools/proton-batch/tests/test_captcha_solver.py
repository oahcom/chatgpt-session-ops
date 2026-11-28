# -*- coding: utf-8 -*-
"""拼图求解器的离线回归：轮廓评分与模板定位。"""
from __future__ import annotations

import numpy as np
import cv2

from proton_batch.captcha_solver import find_holes, locate_piece, piece_template


def _shape() -> np.ndarray:
    """构造一个带「顶部凸起 / 左侧内凹 / 右侧外凸」的 110x110 形状。"""
    m = np.zeros((110, 110), np.uint8)
    cv2.rectangle(m, (0, 14), (92, 109), 255, -1)      # 主体
    cv2.rectangle(m, (42, 0), (68, 15), 255, -1)       # 顶部凸起 → top < 0
    cv2.rectangle(m, (0, 42), (14, 68), 0, -1)         # 左侧内凹 → lm > 0
    cv2.rectangle(m, (92, 40), (109, 70), 255, -1)     # 右侧外凸 → rm > 0
    return m


def _canvas() -> np.ndarray:
    img = np.full((875, 809, 3), 250, np.uint8)
    img[0:109, :] = 255                                # 顶部白条
    shape = _shape()
    # 真缺口
    img[400:510, 300:410][shape > 0] = 40
    # 镜像干扰项
    img[600:710, 550:660][cv2.flip(shape, 1) > 0] = 40
    # 滑块预览（贴在白条上）
    patch = np.random.RandomState(0).randint(60, 200, (100, 130, 3), dtype=np.uint8)
    img[4:104, 8:138] = patch
    return img


def test_find_holes_prefers_the_matching_shape(tmp_path):
    path = tmp_path / "cap.png"
    cv2.imwrite(str(path), _canvas())
    holes = find_holes(str(path))
    assert len(holes) == 2, holes
    real, decoy = holes
    assert real["center"] == [355, 455]
    assert real["score"] > decoy["score"] + 8
    assert real["lm"] > 0 and real["rm"] > 0 and real["top"] < 0


def test_locate_piece_finds_moved_slider(tmp_path):
    img = _canvas()
    tpl, mask = piece_template(img)
    assert tpl is not None

    home, score = locate_piece(img, tpl, mask, skip_home=False)
    assert score > 0.99
    assert abs(home[0] - 73) <= 2 and abs(home[1] - 54) <= 2

    moved = img.copy()
    moved[4:104, 8:138] = 255
    h, w = tpl.shape[:2]
    moved[300:300 + h, 400:400 + w] = tpl
    found, score2 = locate_piece(moved, tpl, mask)
    assert score2 > 0.99
    assert abs(found[0] - (400 + w // 2)) <= 2
    assert abs(found[1] - (300 + h // 2)) <= 2
