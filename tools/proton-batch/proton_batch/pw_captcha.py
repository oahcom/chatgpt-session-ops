# -*- coding: utf-8 -*-
"""Proton 拼图验证码：Playwright 版求解器（复用 captcha_solver 的 cv2 算法）。"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path

import cv2

from proton_batch.captcha_solver import (find_holes, locate_piece, moved_blob,
                                         piece_template)

PRESS_POINT = (78, 74)
CAPTCHA_OUTER = 'iframe[title="Captcha"]'
CAPTCHA_INNER = 'iframe[src*="captcha/v1/assets"]'


class PwCaptcha:
    def __init__(self, page, logger: logging.Logger, work_dir: str | Path):
        self.pg = page
        self.log = logger
        self.dir = Path(work_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.inner = page.frame_locator(CAPTCHA_OUTER).frame_locator(CAPTCHA_INNER)
        self.canvas = self.inner.locator("canvas").first

    def grab(self, tag: str):
        data = ""
        for _ in range(40):
            data = self.canvas.evaluate("el => el.toDataURL('image/webp', 0.6)")
            if len(data) > 22000:
                break
            time.sleep(0.3)
        raw = data.split(",", 1)[1]
        path = self.dir / f"pw_captcha_{tag}.webp"
        path.write_bytes(base64.b64decode(raw))
        box = self.canvas.bounding_box()
        dim = self.canvas.evaluate("el => [el.width, el.height]")
        return {"box": box, "dim": dim}, cv2.imread(str(path))

    def solve(self) -> dict:
        time.sleep(1.5)
        res, img = self.grab("a")
        holes = find_holes(self.dir / "pw_captcha_a.webp")
        if not holes:
            return {"ok": False, "reason": "no-hole"}
        target = holes[0]["center"]
        self.log.info("缺口候选: %s", [(h["center"], h["score"]) for h in holes])

        box, dim = res["box"], res["dim"]
        if not box:
            return {"ok": False, "reason": "no-box"}
        sx = box["width"] / dim[0]
        sy = box["height"] / dim[1]

        def page_xy(px, py):
            return box["x"] + px * sx, box["y"] + py * sy

        def drag(fx, fy, tx, ty, steps=26):
            a = page_xy(fx, fy)
            b = page_xy(tx, ty)
            self.log.info("拖动 (%.0f,%.0f) -> (%.0f,%.0f)", a[0], a[1], b[0], b[1])
            self.pg.mouse.move(a[0], a[1])
            self.pg.mouse.down()
            for i in range(1, steps + 1):
                self.pg.mouse.move(a[0] + (b[0] - a[0]) * i / steps,
                                   a[1] + (b[1] - a[1]) * i / steps)
                time.sleep(0.012)
            self.pg.mouse.up()

        blob = None
        img2 = img
        for attempt in range(3):
            drag(PRESS_POINT[0], PRESS_POINT[1], target[0], target[1])
            time.sleep(1.0)
            _, img2 = self.grab(f"b{attempt}")
            blob = moved_blob(img, img2)
            if blob:
                break
            self.log.warning("第 %d 次拖动无效", attempt + 1)
        if not blob:
            return {"ok": False, "reason": "drag-no-effect", "target": target}

        tpl, mask = piece_template(img)
        for attempt in range(3):
            if tpl is None:
                break
            pc, score = locate_piece(img2, tpl, mask)
            dx, dy = target[0] - pc[0], target[1] - pc[1]
            self.log.info("滑块 %s score=%.3f 残差 (%d,%d)", pc, score, dx, dy)
            if score < 0.6:
                break
            if abs(dx) <= 2 and abs(dy) <= 2:
                break
            drag(pc[0], pc[1], target[0], target[1], steps=10)
            time.sleep(0.9)
            _, img2 = self.grab(f"c{attempt}")
        return {"ok": True, "target": target}

    def submit(self) -> bool:
        """点弹窗里的下一步/验证按钮。"""
        for scope in (self.pg, self.pg.frame_locator(CAPTCHA_OUTER)):
            for name in ("Next", "下一步", "Verify", "Confirm", "Continue"):
                try:
                    loc = scope.get_by_role("button", name=name, exact=False)
                    if loc.count() and loc.first.is_visible():
                        loc.first.click(timeout=5000)
                        self.log.info("点击 %s 提交验证码", name)
                        return True
                except Exception:  # noqa: BLE001
                    continue
        for sel in ('button:has-text("Next")', 'button:has-text("Verify")'):
            try:
                self.pg.locator(sel).first.click(timeout=4000)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False
