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
    lines = [l for l in prompt.splitlines() if not l.strip().startswith("#")]
    prompt = "\n".join(lines).strip()
    if not prompt:
        print("ERROR: 提示词没有正文（只有注释行）：%s" % prompt_file, flush=True)
        return 2

    port = int(os.environ.get("CHATGPT_CDP_PORT") or 9222)
    c = CDPClient(host="127.0.0.1", port=port, timeout=30)
    try:
        c.ensure_page(url_contains="chatgpt.com")
        # 注意：navigate 的参数名是 timeout，不是 wait（写错直接 TypeError）
        c.navigate("https://chatgpt.com/c/" + old_id, timeout=30)
    except Exception as e:
        print("ERROR: CDP 端口 %d 不可用或页面打不开：%s" % (port, e), flush=True)
        print("提示：先 python start_chrome.py --port %d，并确认已登录" % port, flush=True)
        return 2
    time.sleep(6)

    st = state(c)
    print("state:", st, flush=True)
    if not st:
        print("ERROR: 读不到页面状态（页面白屏或 JS 异常），不继续盲点", flush=True)
        return 2

    # 尝试直接发送（可能已可发）
    print("fill:", fill_prompt(c, prompt), flush=True)
    time.sleep(1)
    print("send:", c.evaluate(JS_SEND).get("value"), flush=True)
    time.sleep(4)
    st = state(c)
    print("state2:", st, flush=True)

    if not (st.get("ctxLimit") or st.get("hasFup")):
        print("已发送或无需续接，退出")
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

    # 新会话重发
    print("fill2:", fill_prompt(c, prompt), flush=True)
    time.sleep(1)
    print("send2:", c.evaluate(JS_SEND).get("value"), flush=True)
    time.sleep(4)
    print("done ->", state(c), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
