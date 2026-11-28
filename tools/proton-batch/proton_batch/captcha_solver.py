# -*- coding: utf-8 -*-
"""Proton 拼图验证码求解（实测通过的算法，2026-11）。

页面结构：
    外层 iframe[title="Captcha"]
      └ iframe[src*="captcha/v1/assets"]
          └ canvas  (bitmap 809x875, CSS 显示约 370x400)

图片里有两个"缺口"：一个是真缺口（形状 = 左上角滑块），另一个是镜像干扰项。
两者外形极像，靠三条轮廓特征区分（在缺口 mask 上量）：

    lm  = 左边缘中段相对上下端的水平位移  → 真缺口  > 0（左侧内凹）
    rm  = 右边缘中段相对上下端的水平位移  → 真缺口  > 0（右侧外凸）
    top = 上边缘中段相对左右端的垂直位移  → 真缺口  < 0（顶部凸起）

实测：真缺口 6 分，干扰项 1~4 分。

拖动后必须闭环校验：再抓一帧、用帧差找出滑块落点，残差大于阈值就补一次拖动。
释放鼠标不会提交，点"下一步"才提交，而且验证码有约 60-90 秒时效。
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path

import cv2
import numpy as np

from .browser_driver import BrowseDriver, BrowserError

GRAB_JS = r"""
const deep = page.frameLocator('iframe[title="Captcha"]').frameLocator('iframe[src*="captcha/v1/assets"]');
const c = deep.locator('canvas').first();
await c.waitFor({ state: 'visible', timeout: 20000 });
let data = "";
for (let i = 0; i < 40; i++) {
  data = await c.evaluate(el => el.toDataURL('image/webp', 0.6));
  if (data.length > 22000) break;
  await page.waitForTimeout(300);
}
const cBox = await c.boundingBox();
const dim = await c.evaluate(el => [el.width, el.height]);
const oBox = await page.locator('iframe[title="Captcha"]').boundingBox();
return { data, cBox, dim, oBox };
"""

PRESS_POINT = (78, 74)   # 滑块 bbox 中心（canvas 坐标，形状固定）
TOL = 6                  # 允许残差（canvas 像素）

DRAG_JS = r"""
const x1 = %f, y1 = %f, x2 = %f, y2 = %f, steps = %d;
await browse.drag({ from: { x: x1, y: y1 }, to: { x: x2, y: y2 }, steps });
return "dragged";
"""


def find_holes(path: str | Path, th: int = 120) -> list[dict]:
    """返回按可信度降序排列的缺口列表。"""
    img = cv2.imread(str(path))
    if img is None:
        return []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    m = (gray < th).astype(np.uint8)
    m[0:150, :] = 0                                  # 去掉左上角滑块区
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    out: list[dict] = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 6000 or not (95 <= w <= 125) or not (95 <= h <= 125):
            continue
        sub = (labels[y:y + h, x:x + w] == i).astype(np.uint8)
        fill = sub.sum() / float(w * h)
        if fill < 0.55:
            continue
        left = np.full(h, np.nan)
        right = np.full(h, np.nan)
        top = np.full(w, np.nan)
        for yy in range(h):
            xs = np.where(sub[yy] > 0)[0]
            if len(xs):
                left[yy], right[yy] = xs.min(), xs.max()
        for xx in range(w):
            ys = np.where(sub[:, xx] > 0)[0]
            if len(ys):
                top[xx] = ys.min()
        band = slice(int(h * 0.35), int(h * 0.65))
        ends = np.r_[slice(int(h * 0.05), int(h * 0.22)), slice(int(h * 0.78), int(h * 0.95))]
        lm = float(np.nanmean(left[band]) - np.nanmean(left[ends]))
        rm = float(np.nanmean(right[band]) - np.nanmean(right[ends]))
        tband = slice(int(w * 0.35), int(w * 0.65))
        tends = np.r_[slice(int(w * 0.05), int(w * 0.22)), slice(int(w * 0.78), int(w * 0.95))]
        tm = float(np.nanmean(top[tband]) - np.nanmean(top[tends]))
        score = round(lm + rm - tm, 1)
        out.append({"bbox": [int(x), int(y), int(w), int(h)],
                    "center": [int(x + w // 2), int(y + h // 2)],
                    "lm": round(lm, 1), "rm": round(rm, 1), "top": round(tm, 1),
                    "fill": round(float(fill), 2), "score": score})
    out.sort(key=lambda c: -c["score"])
    return out


def moved_blob(a: np.ndarray, b: np.ndarray, skip=(0, 0, 200, 170)):
    """帧差找滑块落点，返回 (中心, bbox)；跳过左上角原位。"""
    diff = cv2.absdiff(a, b).max(axis=2)
    m = (diff > 40).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((13, 13), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    best = None
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 2500:
            continue
        cx, cy = x + w // 2, y + h // 2
        if skip[0] <= cx <= skip[2] and skip[1] <= cy <= skip[3]:
            continue
        if best is None or area > best[0]:
            best = (area, (int(cx), int(cy)), (int(x), int(y), int(w), int(h)))
    return best


TPL_BOX = (4, 0, 140, 108)     # 滑块预览所在区域（canvas 坐标）


def piece_template(img: np.ndarray):
    """取左上角滑块作为模板 + 非白 mask。"""
    x0, y0, x1, y1 = TPL_BOX
    tpl = img[y0:y1, x0:x1]
    mask = (tpl.min(axis=2) < 240).astype(np.uint8) * 255
    if mask.mean() < 5:
        return None, None
    return tpl, mask


def locate_piece(img: np.ndarray, tpl: np.ndarray, mask: np.ndarray,
                 skip_home: bool = True):
    """模板匹配定位滑块，返回 ((cx,cy), score)。比帧差稳得多（不受缺口重叠影响）。"""
    res = cv2.matchTemplate(img, tpl, cv2.TM_CCORR_NORMED, mask=mask)
    r = res.copy()
    if skip_home:
        r[0:150, 0:170] = -1
    _, score, _, loc = cv2.minMaxLoc(r)
    return (loc[0] + tpl.shape[1] // 2, loc[1] + tpl.shape[0] // 2), float(score)


class PuzzleSolver:
    def __init__(self, driver: BrowseDriver, logger: logging.Logger,
                 work_dir: str | Path):
        self.d = driver
        self.log = logger
        self.dir = Path(work_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 抓图

    def grab(self, tag: str) -> tuple[dict, np.ndarray]:
        res = self.d.execute(GRAB_JS, timeout=90)
        if not res or not res.get("data"):
            raise BrowserError("抓取验证码 canvas 失败")
        raw = res["data"].split(",", 1)[1]
        path = self.dir / f"captcha_{tag}.webp"
        path.write_bytes(base64.b64decode(raw))
        return res, cv2.imread(str(path))

    # ------------------------------------------------------------ 主流程

    def solve(self) -> dict:
        time.sleep(1.5)                      # 等弹窗动画结束，坐标才稳定
        res, img = self.grab("a")
        holes = find_holes(self.dir / "captcha_a.webp")
        if not holes:
            return {"ok": False, "reason": "no-hole"}
        if len(holes) > 1 and holes[0]["score"] - holes[1]["score"] < 8:
            self.log.warning("缺口区分度低: %s", [(h["score"], h["center"]) for h in holes])
        target = holes[0]["center"]
        self.log.info("缺口: %s", [(h["center"], h["score"]) for h in holes])

        sx = res["cBox"]["width"] / res["dim"][0]
        sy = res["cBox"]["height"] / res["dim"][1]
        ox = res["oBox"]["x"] + res["cBox"]["x"]
        oy = res["oBox"]["y"] + res["cBox"]["y"]

        def page(px: float, py: float) -> tuple[float, float]:
            return ox + px * sx, oy + py * sy

        def drag(fx, fy, tx, ty, steps=26):
            a, b = page(fx, fy), page(tx, ty)
            self.log.info("拖动 (%.0f,%.0f) -> (%.0f,%.0f)", a[0], a[1], b[0], b[1])
            out = self.d.drag(a[0], a[1], b[0], b[1], steps)
            self.log.debug("drag 返回: %s", out)

        blob = None
        img2 = img
        for attempt in range(3):
            drag(PRESS_POINT[0], PRESS_POINT[1], target[0], target[1])
            time.sleep(1.0)
            _, img2 = self.grab(f"b{attempt}")
            blob = moved_blob(img, img2)
            if blob:
                break
            self.log.warning("第 %d 次拖动后滑块未移动，重试", attempt + 1)
            time.sleep(1.5)

        if not blob:
            self.log.error("三次拖动都无效，坐标可能偏移")
            return {"ok": False, "reason": "drag-no-effect", "target": target, "holes": holes}

        # 用模板匹配精确定位，逐次补正
        tpl, mask = piece_template(img)
        for attempt in range(3):
            if tpl is None:
                break
            pc, score = locate_piece(img2, tpl, mask)
            dx, dy = target[0] - pc[0], target[1] - pc[1]
            self.log.info("滑块位置 %s (score=%.3f) 残差 (%d,%d)", pc, score, dx, dy)
            if score < 0.6:
                self.log.warning("匹配分过低，停止补正")
                break
            if abs(dx) <= 2 and abs(dy) <= 2:
                break
            drag(pc[0], pc[1], target[0], target[1], steps=10)
            time.sleep(0.9)
            _, img2 = self.grab(f"c{attempt}")
        return {"ok": True, "target": target, "holes": holes}
