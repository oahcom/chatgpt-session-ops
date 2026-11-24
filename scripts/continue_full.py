# -*- coding: utf-8 -*-
"""continue_full.py — 413 满员续接器（P1 通道：CDP + 真实鼠标事件）。

当 chatgpt_send.py 返回 413 conversation_too_large 时使用：
    python continue_full.py <old_conv_id> <prompt_file>

流程：
    1. 打开 chatgpt.com/c/<old_conv_id>，填入提示词尝试点发送
    2. 若弹出「长度上限」横幅 → 真实鼠标点「开始新聊天 / 在新会话中继续」
    3. 摘要续接产生新会话后重发提示词
    4. 新旧 conv_id 映射追加写入 newchat-map.json

依赖：browser-automation-cdp 技能的 cdp.py（Edge 调试端口 9222）。

出口：本脚本不新建浏览器，网络出口由**你启动的那个浏览器**决定。若要用代理，
启动时带上 `--proxy <url>`（start_chrome.py 支持），或先让系统代理生效；
`chatgpt_send.py --proxy-check --browser` 可验证浏览器侧出口是否与 API 一致。
"""
import json
import os
import re
import sys
import time

_CDP_DIR = os.environ.get("CHATGPT_CDP_DIR") or os.path.join(
    os.path.expanduser("~"), ".workbuddy", "skills", "browser-automation-cdp", "scripts")
sys.path.insert(0, _CDP_DIR)
from cdp import CDPClient  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
MAP = os.path.join(BASE, "newchat-map.json")

# R373-01：P1 通道也必须进同一套会话日志。这里复用 chatgpt_send 的日志实现，
# 不另行造一套——两套实现必然漂移，漂移之后"全量留存"就不成立了。
import importlib.util as _ilu

_send_spec = _ilu.spec_from_file_location(
    "chatgpt_send_for_log", os.path.join(BASE, "chatgpt_send.py"))
_cs = _ilu.module_from_spec(_send_spec)
_send_spec.loader.exec_module(_cs)


class _LogCtx:
    """把 api 该有的两个属性（account / _last_reply_text）凑出来，供日志层使用。"""

    def __init__(self, account="cdp"):
        self.account = account
        self._last_reply_text = None


JS_REPLY_SNAPSHOT = """(function(){
  var nodes = document.querySelectorAll('[data-message-author-role="assistant"]');
  var last = nodes.length ? (nodes[nodes.length - 1].innerText || '') : '';
  // JS 只负责**采集原始信号**，判定放 Python（judge_error）——写在 JS 里没法单测，
  // 而 hasError 直接决定 captured/partial，必须有可验证的实现。
  var alertText = [...document.querySelectorAll('[role="alert"], [data-testid*="error" i]')]
      .map(function(e){ return (e.innerText || '').trim(); })
      .filter(function(t){ return t && t.length < 300; })
      .join(' ');
  return JSON.stringify({
    count: nodes.length,
    last: last.trim(),
    streaming: [...document.querySelectorAll('button')].some(function(b){
      return /停止生成|stop/i.test(b.getAttribute('aria-label') || '');
    }),
    alert_text: alertText
  });
})()"""


ERROR_PATTERNS = ("Something went wrong", "出现错误", "网络错误", "消息发送失败")


def judge_error(own, alert_text):
    """判定"页面是否明确报错"。返回 (has_error, structural, text)。

    规则（R376-01）：
      - **结构性错误独立判定，绝不受正文长度影响**：页面级 alert 里出现错误词即算。
        旧版用 `own.length < 200` 同时管住了结构性错误，长正文会屏蔽真实错误。
      - 正文内错误词只作辅助：仅当**整条正文就是一句错误提示**时成立；
        长正文里提到"网络错误"（在讨论它）不算。
    """
    alert_text = (alert_text or "").strip()
    structural = any(p in alert_text for p in ERROR_PATTERNS)
    own = (own or "").strip()
    text_err = False
    if own and len(own) < 200:
        # R377-01：必须拿**整条正文**匹配，不能只看第一行。
        # 反例：own = "Something went wrong\nPlease try again later."
        # 只看第一行会误判成错误提示——它带了额外说明，不是纯错误。
        # 规范化：合并空白 + 去掉首尾标点，再与错误提示整体相等。
        core = " ".join(own.split()).strip().rstrip(".。!！?？ ")
        text_err = core in ERROR_PATTERNS
    return (structural or text_err), structural, text_err

def reply_snapshot(c):
    """取 assistant 消息的**当前快照**（条数 + 最后一条正文 + 是否仍在生成）。

    R374-01：只拿"最后一条 assistant 正文"是不够的——上一轮的回复会留在 DOM 里，
    本轮若发送失败，最后一条仍是旧回复，直接取就会把它记成本轮回复（归属错误）。
    所以必须先在发送前取基线条数，发送后只认**新增**的那条。
    """
    try:
        raw = c.evaluate(JS_REPLY_SNAPSHOT).get("value")
        d = json.loads(raw or "{}")
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    last = d.get("last") or ""
    has_err, structural, text_err = judge_error(last, d.get("alert_text"))
    return {
        "count": int(d.get("count") or 0),
        "last": last,
        "streaming": bool(d.get("streaming")),
        "hasError": has_err,
        "structuralError": structural,
        "textError": text_err,
    }


def reply_after(c, baseline, settle_tries=8, settle_gap=1.5, stable_need=2):
    """拿本轮新增的回复，并判定 captured / partial / empty / unavailable。

    R374-01 的判据：
      - 归属：条数必须比基线**多**，否则无法证明属于本轮 → unavailable
      - 完整：仍在流式生成 → partial
      - 空：确认属于本轮且已完成、但正文为空 → empty

    R375-01 的追加要求：**streaming=False 单次采样不足以证明完成**。
    新消息节点可能刚创建、正文还没开始写，或流式状态有短暂间隙。因此要求
    连续 stable_need 次采样"正文完全一致 且 streaming 均为 false"才算稳定；
    超时仍不稳定 → partial，绝不升级为 captured。
    另外 hasError（页面明确报错）不得当成正常 captured。
    """
    if baseline is None:
        return None, "unavailable"
    snap = None
    stable_run = 0
    prev_text = None
    text = ""          # R376-02：全空档（始终没有新消息）时也不能未绑定后访问
    # 预算按"轮数"控制（不是墙钟），否则 settle_gap=0 的测试会立刻过期，
    # 真实运行时也不会因为单次 evaluate 慢就把本该拿到的回复判成 partial。
    for _ in range(max(1, int(settle_tries))):
        snap = reply_snapshot(c)
        if snap is None:
            return None, "unavailable"
        if snap["count"] <= baseline["count"]:
            stable_run = 0             # P1-4：跨过空档，稳定序列必须清零
            prev_text = None
            time.sleep(settle_gap)     # 还没出现新消息，继续等
            continue
        text = snap["last"] or ""
        if snap["streaming"]:
            stable_run = 0             # 还在生成，稳定序列中断
        elif prev_text is None or text != prev_text:
            stable_run = 1             # 首次观测，或正文变了 → 从本次重新计
        else:
            stable_run += 1            # 与上次一致且已停 → 稳定序列 +1
        prev_text = text
        if stable_run >= stable_need:
            break
        time.sleep(settle_gap)

    if snap is None:
        return None, "unavailable"
    if snap["count"] <= baseline["count"]:
        return None, "unavailable"        # 没新增 = 本轮无可归属回复，旧回复不得顶替

    text = snap["last"]
    stable = (stable_run >= stable_need) and not snap["streaming"]

    if snap["hasError"]:
        # 页面明确报错：内容即使拿到了也只是残缺，绝不能报 captured。
        # 没有正文 → unavailable（不是 partial：partial 意味着"拿到了部分"）。
        return (text or None), ("partial" if text else "unavailable")
    if not text:
        return None, ("empty" if stable else "unavailable")
    return (text, "captured") if stable else (text, "partial")

JS_STATE = """(function(){
  var body = document.body.innerText || '';
  var ta = document.querySelector('.ProseMirror[contenteditable=true]') || document.querySelector('#prompt-textarea') || document.querySelector('textarea');
  var composer = ta ? (ta.innerText || ta.value || '') : '';
  return JSON.stringify({
    url: location.href,
    composerLen: composer.length,
    ctxLimit: /上下文窗口已达上限|已达长度上限|长度上限/.test(body),
    hasFup: /开新会话|在新会话中继续|继续聊天|新会话中继续/.test(body),
    streaming: [...document.querySelectorAll('button')].some(function(b){return /停止生成|stop/i.test(b.getAttribute('aria-label')||'');})
  });
})()"""

JS_FOCUS = """(function(){
  var ta = document.querySelector('.ProseMirror[contenteditable=true]') || document.querySelector('#prompt-textarea') || document.querySelector('textarea');
  if (!ta) return 'NO_COMPOSER';
  ta.focus();
  return 'FOCUSED';
})()"""

JS_COMPOSER_LEN = """(function(){
  var ta = document.querySelector('.ProseMirror[contenteditable=true]') || document.querySelector('#prompt-textarea') || document.querySelector('textarea');
  return ta ? ((ta.innerText || ta.value || '').length) : -1;
})()"""

# 思考模式开关：输入框右侧有个文字为「思考」的按钮，aria-pressed 表示状态。
# 实测（2026-11-19）：未开时 aria-pressed="false"，点一次变 "true" 且 class 增加 accentSubtle-*
JS_THINKING = """(function(){
  var btns = document.querySelectorAll('button');
  for (var i = 0; i < btns.length; i++) {
    var t = (btns[i].innerText || '').trim();
    var a = (btns[i].getAttribute('aria-label') || '');
    if (t === '思考' || t === 'Thinking' || /^(思考|Thinking)$/.test(a)) {
      var r = btns[i].getBoundingClientRect();
      return JSON.stringify({found: true, pressed: btns[i].getAttribute('aria-pressed'),
                             x: r.x + r.width / 2, y: r.y + r.height / 2});
    }
  }
  return JSON.stringify({found: false});
})()"""

JS_SEND = """(function(){
  var btns = document.querySelectorAll('button');
  for (var i = 0; i < btns.length; i++) {
    var lbl = (btns[i].getAttribute('aria-label') || '') + '|' + (btns[i].getAttribute('data-testid') || '');
    if (/send|发送/i.test(lbl) && !btns[i].disabled && btns[i].getAttribute('aria-disabled') !== 'true') {
      btns[i].click(); return 'SENT_BTN';
    }
  }
  return 'NO_SEND';
})()"""

# 真实鼠标事件点击（js click 可能被 React 忽略）
JS_CLICK_FUP = """(function(){
  var all = [...document.querySelectorAll('button, a[role=button]')];
  var labels = ['开新会话', '在新会话中继续', '在新聊天中继续', '继续聊天', '新会话中继续', 'Continue in a new chat', 'Start a new chat'];
  var bs = all.filter(function(b){
    var t = (b.innerText||'').trim();
    return labels.some(function(l){ return t === l || t.indexOf(l) >= 0; });
  });
  if (!bs.length) return JSON.stringify({clicked:false, reason:'NO_BTN'});
  var r = bs[0].getBoundingClientRect();
  return JSON.stringify({clicked:true, label:(bs[0].innerText||'').trim().slice(0,25), x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)});
})()"""


def state(c):
    try:
        return json.loads(c.evaluate(JS_STATE).get("value") or "{}")
    except Exception:
        return {}


def real_click(c, x, y):
    for typ in ("mousePressed", "mouseReleased"):
        c.send("Input.dispatchMouseEvent", {
            "type": typ, "x": x, "y": y, "button": "left", "clickCount": 1,
        })
    time.sleep(2)


def enable_thinking(c):
    """把「思考」按钮点到开启（幂等）。返回 ALREADY / ENABLED / NO_TOGGLE / ERROR。

    用户明示（2026-11-19）：启动 ChatGPT 时必须开思考模式。不开的话回复更浅、
    更「防御性」，评审类任务质量明显下降。CHATGPT_NO_THINKING=1 可显式关闭。
    """
    if os.environ.get("CHATGPT_NO_THINKING", "") not in ("", "0", "false"):
        return "SKIPPED"
    try:
        info = json.loads(c.evaluate(JS_THINKING).get("value") or "{}")
    except Exception as e:
        return "ERROR:%s" % e
    if not info.get("found"):
        return "NO_TOGGLE"      # 匿名态/旧版 UI 没有这个按钮，不算失败
    if info.get("pressed") == "true":
        return "ALREADY"
    real_click(c, info["x"], info["y"])
    try:
        again = json.loads(c.evaluate(JS_THINKING).get("value") or "{}")
    except Exception:
        return "ENABLED?"       # 点了但读不回状态
    return "ENABLED" if again.get("pressed") == "true" else "FAILED_TO_TOGGLE"


def fill_prompt(c, text):
    """填入提示词：先聚焦，再用 CDP Input.insertText（原生 setter/execCommand 常被 React 忽略）。

    旧实现用字符串替换把提示词塞进 JS —— 那会把函数签名里的同名字符串
    一起换掉，直接产出语法错误的表达式，页面侧必然失败。
    """
    if c.evaluate(JS_FOCUS).get("value") == "NO_COMPOSER":
        return "NO_COMPOSER"
    c.send("Input.insertText", {"text": text})
    time.sleep(0.8)
    return "LEN:%s" % (c.evaluate(JS_COMPOSER_LEN).get("value"),)


def conv_id_from_url(url):
    m = re.search(r"/c/([0-9a-f-]{36})", url or "")
    return m.group(1) if m else None


def main():
    if len(sys.argv) < 3:
        print("usage: python continue_full.py <old_conv_id> <prompt_file>", flush=True)
        return 2
    old_id, prompt_file = sys.argv[1], sys.argv[2]
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$", old_id):
        print("ERROR: old_conv_id 非法：%r（只允许字母数字与 _-）" % (old_id,), flush=True)
        return 2
    try:
        with open(prompt_file, encoding="utf-8") as f:
            prompt = f.read()
    except OSError as e:
        print("ERROR: 读不到提示词文件 %s：%s" % (prompt_file, e), flush=True)
        return 2
    # 与 chatgpt_send 同口径：**不再按 `#` 删行**。旧版把 Rust 属性
    # `#[derive]`/`#[test]`/`#[cfg]` 和 markdown 标题一起当注释删掉，正文被静默截断
    # （2026-11-21 r1d/r1e 实证）。P1 当时漏改，等于同一条 bug 在第二条通道上复现。
    # R377-03：NUL / 控制字符 / 无可见正文必须拒绝——两条通道同一口径，
    # 否则损坏文件从 P1 溜进去，等于校验白做。
    try:
        _cs.validate_prompt_text(prompt, prompt_file)
    except RuntimeError as e:
        print("ERROR: %s" % e, flush=True)
        return 2
    prompt = prompt.strip()
    if not prompt:
        print("ERROR: 提示词没有正文：%s" % prompt_file, flush=True)
        return 2

    # R373-02/07：发送前先固化本轮提示词；留不住就不发送。
    logctx = _LogCtx()
    try:
        turn_id, begin_path = _cs.log_begin(logctx, old_id, prompt_file, prompt)
    except Exception as e:
        print("FAIL: 提示词无法留存（%s）——按纪律不发送" % e, flush=True)
        return 2

    def _finish(status, conv_id, reply_state, error="", reply=None):
        """每条退出路径都必须留下终态，不允许"发了但不知道结果"。"""
        logctx._last_reply_text = reply
        logctx.reply_source = "dom"     # R374-01：P1 只有 DOM 证据，标明来源
        try:
            _cs.log_end(logctx, turn_id, begin_path, old_id, conv_id,
                        status, None, reply_state, error=error)
        except Exception as le:
            print("FAIL: 回复留存失败（%s）——本轮只有 begin，读取时显示未完成" % le,
                  flush=True)

    port = int(os.environ.get("CHATGPT_CDP_PORT") or 9222)
    c = CDPClient(host="127.0.0.1", port=port, timeout=30)
    try:
        c.ensure_page(url_contains="chatgpt.com")
        # 注意：navigate 的参数名是 timeout，不是 wait（写错直接 TypeError）
        c.navigate("https://chatgpt.com/c/" + old_id, timeout=30)
    except Exception as e:
        print("ERROR: CDP 端口 %d 不可用或页面打不开：%s" % (port, e), flush=True)
        print("提示：先 python start_chrome.py --port %d，并确认已登录" % port, flush=True)
        _finish(None, old_id, "unavailable", error="cdp_unavailable: %s" % e)
        return 2
    time.sleep(6)

    st = state(c)
    print("state:", st, flush=True)
    if not st:
        print("ERROR: 读不到页面状态（页面白屏或 JS 异常），不继续盲点", flush=True)
        _finish(None, old_id, "unavailable", error="page_state_unreadable")
        return 2

    # 先开思考模式（幂等；关闭后才填词，避免切换模型把输入框里的内容吃掉）
    print("thinking:", enable_thinking(c), flush=True)

    # R374-01：发送**之前**取 DOM 基线（条数），发送后只认新增的那条。
    # 不取基线就只剩"最后一条 assistant"，会把上一轮的回复误记成本轮。
    baseline = reply_snapshot(c)
    print("reply baseline:", baseline, flush=True)

    # 尝试直接发送（可能已可发）
    print("fill:", fill_prompt(c, prompt), flush=True)
    time.sleep(1)
    print("send:", c.evaluate(JS_SEND).get("value"), flush=True)
    time.sleep(4)
    st = state(c)
    print("state2:", st, flush=True)

    if not (st.get("ctxLimit") or st.get("hasFup")):
        print("已发送或无需续接，退出")
        rp, rstate = reply_after(c, baseline)
        cur = conv_id_from_url(state(c).get("url", "")) or old_id
        _finish(200, cur, rstate, reply=rp)
        return 0

    # 点「开始新聊天」续接（真实鼠标）
    r = c.evaluate(JS_CLICK_FUP).get("value")
    print("fup btn:", r, flush=True)
    try:
        d = json.loads(r)
        if d.get("clicked"):
            real_click(c, d["x"], d["y"])
    except Exception:
        pass
    time.sleep(10)

    new_id = conv_id_from_url(state(c).get("url", ""))
    print("new conv:", new_id, flush=True)
    if not new_id:
        print("FAIL: 未拿到新会话 id")
        _rp, _rst = reply_after(c, baseline)
        _finish(None, None, "unavailable", error="new_conv_id_unresolved", reply=_rp)
        return 2

    # 记录映射（原子写：满员续接的映射丢了就只能靠人工回忆旧 id）
    m = {}
    if os.path.exists(MAP):
        try:
            with open(MAP, encoding="utf-8") as f:
                m = json.load(f)
            if not isinstance(m, dict):
                m = {}
        except (OSError, json.JSONDecodeError) as e:
            print("WARN: newchat-map.json 读不出来（%s），按空表重建" % e, flush=True)
            m = {}
    m[old_id] = new_id
    tmp = MAP + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, MAP)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        print("WARN: newchat-map.json 落盘失败（%s）：续接已发生，请手工记下 %s -> %s"
              % (e, old_id, new_id), flush=True)

    # 新会话重发：切到新会话后条数已变，必须重取基线，否则归属判定失效
    baseline = reply_snapshot(c)
    print("reply baseline2:", baseline, flush=True)
    print("thinking2:", enable_thinking(c), flush=True)
    print("fill2:", fill_prompt(c, prompt), flush=True)
    time.sleep(1)
    print("send2:", c.evaluate(JS_SEND).get("value"), flush=True)
    time.sleep(4)
    print("done ->", state(c), flush=True)
    # 续接轮记在**新**会话下；旧会话只留 begin（它确实没收到这条）
    rp, rstate = reply_after(c, baseline)
    _finish(200, new_id, rstate, reply=rp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
