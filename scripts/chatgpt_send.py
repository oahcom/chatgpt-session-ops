# -*- coding: utf-8 -*-
"""chatgpt_send.py — 统一发送/收割器（API 直连 + PoW，P0 通道）。

用法：
    python chatgpt_send.py <conv_id> <prompt_file>          # 投递到既有会话
    python chatgpt_send.py --new <prompt_file>              # 开全新会话（默认按身份轮转均分）
    python chatgpt_send.py --tail <conv_id>                 # 只收割末条 assistant 回复
    python chatgpt_send.py --account acct2 --tail <conv_id> # 显式选择账号
    python chatgpt_send.py --account acct1 --list       # 查询单账号会话
    python chatgpt_send.py --list-all                  # 聚合本机已配置账号可见会话
    python chatgpt_send.py --rotation                  # 查看轮转状态（游标/计数/不可用身份）
    python chatgpt_send.py --rotation-reset            # 重置游标与计数（不清不可用标记）
    python chatgpt_send.py --rotation-enable  nologin  # 清除不可用标记，下次重新尝试该身份
    python chatgpt_send.py --rotation-disable nologin  # 手动把身份移出轮转
    python chatgpt_send.py --owner <conv_id>           # 查会话归属账号
    python chatgpt_send.py --anon-list                 # 匿名会话 id 对照表（留存记录）
    python chatgpt_send.py --anon-read <conv_id>       # 按 id 读取匿名会话全文（API，标签页丢了也能读）
    python chatgpt_send.py --anon-send <conv_id> <prompt_file>  # 按 id 启用匿名通道续聊
    python chatgpt_send.py --anon-devices              # 本地留存了哪些设备身份（oai-did）
    python chatgpt_send.py --anon-relink <conv_id> <device>     # 手工修正会话->设备归属
    python chatgpt_send.py --anon-restore <device>     # 把留存设备身份回灌当前浏览器
    python chatgpt_send.py --proxy-show                # 当前出口代理来自哪里
    python chatgpt_send.py --proxy-set <url>           # 写入 proxy.json（http/https/socks5/socks5h）
    python chatgpt_send.py --proxy-check [--browser]   # 量出口 IP：API 侧 + 浏览器侧是否一致
    python chatgpt_send.py --extract <src> --between <起> <止> [--out <p>]  # 取唯一交付块（字节级）
    python chatgpt_send.py --extract <src> --fence <n> [--out <p>]         # 取第 n 个围栏块
    python chatgpt_send.py --verify-copy <a> <b>       # 回灌前比对两份文件是否字节一致
    python chatgpt_send.py --check-name "<名称>" [--argv "<命令串>"]  # B10/B11 命名与权限
    python chatgpt_send.py --check-schedule "FREQ=HOURLY;INTERVAL=1"  # B1 排程口径
    python chatgpt_send.py --check-oneshot 2026-11-19T16:00:00+08:00  # B4/B5 一次性窗口

交付正文托管（A1/A2/A4 的机器判定，纯本地不触网）：
    定位器必须显式给：--between 两行标记（strip 后整行相等，各恰好 1 处）或 --fence 第 n 块。
    脚本不猜「最长块」、不做 Unicode 规范化、不转 CRLF；只允许裁掉首尾空行。
    输出 JSON 带 src_sha256 / sha256 / offset_start / offset_end，正文另存为独立文件。
    标记不唯一、顺序反、块为空、文件缺失 → exit 2，禁止投递。
    回灌前跑 --verify-copy：两份不一致 → exit 2（A4），禁止用摘要替代原文。

调度口径校验（B1/B10/B11 的机器判定，纯本地）：
    --check-name 把名称按空白分词（全角空格等价）：只读恰好 2 段且以「（只读）」结尾，
    写入恰好 3 段；动作词必须与词表整段相等（不做包含/前缀匹配）；只读名称的 argv
    必须全部落在白名单内。任一条不成立 → exit 2。
    --check-schedule 只接受 FREQ=HOURLY 且 INTERVAL=1；天维度、INTERVAL≠1 一律 exit 2。
    这两个开关就是 automation 创建/巡检前的闸口，别再用正则自己判。

出口代理（统一）：
    API、匿名浏览器、自启动浏览器共用同一个出口，解析顺序
    CHATGPT_PROXY > proxy.json > 系统 https_proxy/http_proxy > 本机端口探测(7890/7897/10809/1080/8888)。
    解析不到就拒绝联网（fail-closed），只有 CHATGPT_ALLOW_DIRECT=1 才放行直连。
    127.0.0.1/localhost 永不过代理：CDP 与本地闸口(CHATGPT_BASE) 必须直连。

匿名通道（nologin）可用性（实测 2026-11-19）：
    能建会话、能多轮对话（同一标签页内连续发送，session id 不变）、能按 id 经 API 读全文。
    限制：匿名会话只在**创建它的标签页**内可写，新开标签页访问 /uc/<id> 会被重定向到首页；
          不进任何会话历史（会话列表恒 0 条），只与设备身份 oai-did 绑定；
          服务端可能回收，故 skill 侧用 anon-sessions.json 留存 id 对照表。
    设备身份：每次投递都把浏览器 cookie jar（含 HttpOnly）按 oai-did 存到
          accounts/nologin/devices/<did>.{txt,json}；读旧会话时按登记设备取回身份，
          对不上就依次试其他留存设备并自动修正归属（--anon-devices 可查）。

建会话身份优先级（--new 默认行为）：
    第一优先：acct1 / acct2 均分轮转（游标只在两者之间推进）。
    兜底：仅当两个登录态都 429（本次命中或处于 429 冷却）时，才启用 nologin 匿名通道。
    不兜底的情形：401（认证失效，应重新提取凭据）、403（PoW/风控，短冷却后自愈）。
    为什么匿名只做兜底：匿名会话不入任何会话列表（list 恒为空），与设备身份(oai-did)绑定，
    换设备即 404 conversation_inaccessible，随时可能被服务端回收——不能承担需要留存的任务。
    失败分级：401 封 6h；403 补 cookie + 重跑 PoW 重试一次，仍失败冷却 15min；
    429 冷却 180s；匿名浏览器通道不可用冷却 30min。成功后把 conv_id -> 账号
    登记到 conv-accounts.json，后续投递/收割未显式指定 --account 时按记录路由。
    环境变量：CHATGPT_ANON_FALLBACK=0 彻底禁用匿名兜底。

--list-all 输出：
    conversations  扁平列表（每条标注 account）
    by_account     按账号分组，便于归档
    summary        各账号会话计数
    quota          各账号最近一次响应的限速观测头（只读）
    errors         不可用账号 / 接口错误

账号凭据放在 accounts/<acct1|acct2>/{cookies.json,access_token.txt}。
--list-all 只聚合各身份获准查看的会话；429 时停止扫描。

前置：acct1/default 认证文件在本脚本目录；acct2 在 accounts/acct2/ 下：
    cookies.json      由 extract_auth.py 从 Edge CDP 提取
    access_token.txt  同上（accessToken）
    pow_solver.py     PoW 解算器
    nologin           匿名态：不加载认证文件，走浏览器通道（AnonBrowser）
                      —— 建/发必须过 Cloudflare Turnstile，纯 API POST 恒 403；
                      故 UI 建会话（https://chatgpt.com/uc/<id>）+ API GET 收割。

协议要点（血泪条款，勿删）：
    - POST /backend-api/conversation 必须带 conversation_id，否则静默落入新会话；
      --new 模式故意不带，用于开 Owner 新会话。
    - 403 "Unusual activity" = PoW 缺失，不是风控；必须带
      OpenAI-Sentinel-Chat-Requirements-Token + OpenAI-Sentinel-Proof-Token。
    - 失败先定性（classify_failure）：代理/网络问题修代理（repair_proxy），上游限速按返回体
      resets_in/retry_after/Retry-After 的真实时限退避（_retry_after，缺省才默认），风控补 cookie+PoW。
      可用 conv-api/<id8>.json 缓存 tail 兜底 parent_message_id。
    - POST 200 即消息已提交；SSE 流只用于收割，需读到 EOF（勿提前关连接）。
    - 413 conversation_too_large → 用 continue_full.py 走「开始新聊天」摘要续接。
"""
import base64
import contextlib
import copy
import gzip
import hashlib
import http.client
import inspect
import ipaddress
import json
import os
import re
import shlex
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pow_solver import solve  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.4341.60")
CTX = ssl.create_default_context()
# CHATGPT_BASE：API 根。默认直连；指向闸口时走 asip/chatgpt_l7.py
#   （http://127.0.0.1:17892，429 适配：账号层退避短路 + transport 换 ok）。
API_BASE = os.environ.get("CHATGPT_BASE", "https://chatgpt.com").rstrip("/")

# ---------- 建会话身份轮转 ----------
# 优先登录态：acct1 / acct2 均分轮转；nologin（匿名）只作兜底——
# 实测匿名会话不进任何会话历史（list 返回 0 条），随时可能被服务端回收，
# 仅按 conv_id 在短期内可读，因此只在两个登录态都 429 时启用。
ROTATION_ORDER = ("acct1", "acct2", "nologin")
ROTATION_PRIMARY = ("acct1", "acct2")
ROTATION_FALLBACK = ("nologin",)
ROTATION_FILE = os.path.join(BASE, "account-rotation.json")
CONV_MAP_FILE = os.path.join(BASE, "conv-accounts.json")
ANON_SESSIONS_FILE = os.path.join(BASE, "anon-sessions.json")
UNAVAILABLE_TTL = 6 * 3600  # 不可用标记有效期：过期后容许再试一次（凭据可能已刷新）
COOLDOWN_429 = 180          # 429 只是临时限速，短冷却后重新纳入轮转
COOLDOWN_403 = 900          # 403 多为 PoW/风控或隐性限速，15 分钟后允许重试


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 外部 id 校验 ----------
# conv_id / 设备号都会拼进文件名（replies/、conv-api/、anon-sessions/、devices/）。
# 不校验就等于把 `../../x` 当文件名用：`--anon-read ../../evil` 会在工作目录外落盘。
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def safe_id(value, what="id"):
    """校验外部传入的会话 id / 设备号；不合法直接抛错，不留给下游拼接。"""
    v = (value or "").strip()
    if not _ID_RE.match(v):
        raise ValueError("%s 非法：%r（只允许字母数字与 _-，长度 1-80）" % (what, value))
    return v


def id8(conv_id, fallback="new"):
    """取 8 位短前缀做文件名；非法 id 退化为哈希，绝不拼出路径分隔符。"""
    v = (conv_id or "").strip()
    if not v:
        return fallback
    if _ID_RE.match(v):
        return v[:8]
    return hashlib.sha1(v.encode("utf-8", "replace")).hexdigest()[:8]


def cli_id(value):
    """CLI 入口校验：打印人话并返回 None，调用方据此 return 2。"""
    try:
        return safe_id(value, "conv_id")
    except ValueError as e:
        print("ERROR: %s" % e, flush=True)
        print("提示：conv_id 形如 11111111-2222-4333-8444-555555555555，只含字母数字与 _-", flush=True)
        return None


def _harden_perms(path):
    """凭据/状态文件在 POSIX 上收成 0600；Windows 无此语义，静默跳过。"""
    if os.name != "posix":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _atomic_write(path, text):
    """tmp → fsync → replace。状态 json 与匿名归档都只有这一份副本，写半截就全丢。

    tmp 名必须**唯一**（pid + 线程）：固定 `.tmp` 时，同进程两个线程会写同一个临时文件，
    Windows 上 os.replace 直接 WinError 32（另一个程序正在使用此文件），线程被打死、
    锁留在原地，后面的人等 8s 超时后「跳过加锁」继续写——实测 4 线程 ×20 次只落 60 条。
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # 目录 fsync：rename 只保证「目录项变更」可见，不保证断电后仍在（POSIX 语义）
        try:
            dfd = os.open(parent or ".", os.O_RDONLY)
        except OSError:
            dfd = None
        if dfd is not None:
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _harden_perms(path)


def _atomic_write_bytes(path, data):
    """二进制原子写。用于交付正文字节级落盘——不能经过文本层，否则 CRLF 会被改写。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # 目录 fsync：rename 只保证「目录项变更」可见，不保证断电后仍在（POSIX 语义）
        try:
            dfd = os.open(parent or ".", os.O_RDONLY)
        except OSError:
            dfd = None
        if dfd is not None:
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _harden_perms(path)


# ---------- 交付正文托管（A1/A2/A4 的机器判定） ----------
# 这三条的旧状态是「只在文档里写口径」，等于人判。人判在无人值守链路上必然漂移：
# 只要有人图省事用「最长代码块」或「首尾 strip 后重排」，正文就换了一个字节而没人知道。
# 所以这里把三件事做成可执行的：按显式定位器取唯一块、字节级落盘、回灌前比对 SHA-256。
class CustodyError(Exception):
    """提取/校验不成立。调用方必须 exit 2，禁止把半成品当正文投递。"""


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _lines_with_offsets(raw):
    """按 \\n 切行并保留每行起始字节偏移。只认 \\n，避免 splitlines 把 \\x0b 等也当换行。"""
    out, pos = [], 0
    while pos < len(raw):
        nl = raw.find(b"\n", pos)
        end = len(raw) if nl < 0 else nl + 1
        out.append((pos, raw[pos:end]))
        pos = end
    return out


def _trim_blank_lines(blob):
    """只裁掉首尾「整行只有空白」的行（A1 唯一允许的改动），其余字节一个不动。

    注意保留最后一行自身的换行：那是行终止符，不是多余空白。
    """
    rows = _lines_with_offsets(blob)
    keep = [i for i, (_, ln) in enumerate(rows) if ln.strip()]
    if not keep:
        return b""
    start = rows[keep[0]][0]
    last_off, last_ln = rows[keep[-1]]
    return blob[start:last_off + len(last_ln)]


def extract_deliverable(src, begin=None, end=None, fence=None, out=None):
    """取出唯一交付块并以字节级落盘。

    定位器必须由调用方显式指定，脚本不做猜测：
      - ``between``：两行标记（strip 后整行相等），各必须恰好出现 1 次；
      - ``fence``：第 n 个围栏块（``` 或 ~~~，1 起）。
    这也正是 A2 的要求——按约定定位，不按「最长」定位。
    """
    if not os.path.isfile(src):
        raise CustodyError("源文件不存在：%s" % src)
    raw = open(src, "rb").read()
    if not raw:
        raise CustodyError("源文件为空：%s" % src)
    lines = _lines_with_offsets(raw)
    locator = ""
    if begin is not None and end is not None:
        b, e = begin.encode("utf-8"), end.encode("utf-8")
        bi = [i for i, (_, ln) in enumerate(lines) if ln.strip() == b]
        ei = [i for i, (_, ln) in enumerate(lines) if ln.strip() == e]
        if len(bi) != 1 or len(ei) != 1:
            raise CustodyError("标记不唯一：begin=%d 处、end=%d 处（各须恰好 1 处）"
                               % (len(bi), len(ei)))
        if bi[0] >= ei[0]:
            raise CustodyError("标记顺序错：begin 在第 %d 行，end 在第 %d 行" % (bi[0] + 1, ei[0] + 1))
        start = lines[bi[0]][0] + len(lines[bi[0]][1])
        stop = lines[ei[0]][0]
        locator = "between:%s..%s" % (begin, end)
    elif fence is not None:
        try:
            n = int(fence)
        except (TypeError, ValueError):
            raise CustodyError("--fence 需要正整数：%r" % (fence,))
        if n < 1:
            raise CustodyError("--fence 从 1 开始：%r" % (fence,))
        spans, opened = [], None
        for off, ln in lines:
            s = ln.lstrip()
            if s.startswith(b"```") or s.startswith(b"~~~"):
                if opened is None:
                    opened = off + len(ln)
                else:
                    spans.append((opened, off))
                    opened = None
        if len(spans) < n:
            raise CustodyError("围栏块不足：要求第 %d 个，实际 %d 个" % (n, len(spans)))
        start, stop = spans[n - 1]
        locator = "fence:%d" % n
    else:
        raise CustodyError("必须显式指定定位器：--between <起> <止> 或 --fence <n>")
    body = _trim_blank_lines(raw[start:stop])
    if not body.strip():
        raise CustodyError("交付块为空（定位器 %s 命中空块）" % locator)
    out = out or (src + ".body")
    _atomic_write_bytes(out, body)
    return {"result": "ok", "locator": locator, "src": os.path.abspath(src),
            "src_sha256": _sha256_bytes(raw), "src_bytes": len(raw),
            "body": os.path.abspath(out), "bytes": len(body),
            "offset_start": start, "offset_end": stop,
            "sha256": _sha256_bytes(body)}


def verify_copy(src, dst):
    """回灌前比对：两份文件必须字节一致（A1/A4）。不一致即禁止回灌。"""
    for p in (src, dst):
        if not os.path.isfile(p):
            raise CustodyError("文件不存在：%s" % p)
    a, b = _sha256_file(src), _sha256_file(dst)
    return {"result": "ok" if a == b else "mismatch", "src": os.path.abspath(src),
            "dst": os.path.abspath(dst), "src_sha256": a, "dst_sha256": b,
            "src_bytes": os.path.getsize(src), "dst_bytes": os.path.getsize(dst),
            "equal": a == b}


# ---------- 调度口径校验（B1/B10/B11 的机器判定） ----------
# 这三条原先只有「文档口径 + 人眼正则」，而人眼正则恰好是审查反复抓到的漏洞：
# `^.+ .+ .+$` 连四段都放行；`startswith("--rotation")` 会把 --rotation-reset 一起放行。
# 这里把词表与段数规则写成代码，自动化在创建/巡检前跑一次即可判定。
READONLY_SUFFIX = "（只读）"
READONLY_VERBS = ("查看", "列出", "收割", "校验", "盘点", "巡检", "比对", "读取")
WRITE_VERBS = ("投递", "新建", "写入", "切换", "重置", "删除", "归档", "修复",
               "设置", "处置", "执行", "补采")
READONLY_ARGV = ("--list", "--tail", "--owner", "--anon-list", "--anon-devices",
                 "--proxy-show", "--rotation", "--list-all")


class ScheduleError(Exception):
    """命名/排程口径不成立。调用方必须 exit 2，禁止启用该 automation。"""


def split_name(name):
    """按空白分词，全角空格与半角一视同仁（B10 的段数判定依据）。"""
    return [p for p in str(name or "").replace("\u3000", " ").split() if p]


def check_automation_name(name, argv=None):
    """B10/B11：只读恰好 2 段且带「（只读）」后缀；写入恰好 3 段；动作词整段相等。"""
    segs = split_name(name)
    if len(segs) == 2 and segs[1].endswith(READONLY_SUFFIX):
        kind = "readonly"
        verb = segs[1][:-len(READONLY_SUFFIX)]
        if verb not in READONLY_VERBS:
            raise ScheduleError("只读名称的动作词不在读词表：%r（允许：%s）"
                                % (verb, "、".join(READONLY_VERBS)))
    elif len(segs) == 3:
        kind = "write"
        verb = segs[2]
        if verb not in WRITE_VERBS:
            raise ScheduleError("写入名称的动作词不在写词表：%r（允许：%s）"
                                % (verb, "、".join(WRITE_VERBS)))
    elif len(segs) == 2:
        raise ScheduleError("2 段但第 2 段缺少「%s」后缀：%r（只读要后缀，写入要 3 段）"
                            % (READONLY_SUFFIX, segs[1]))
    else:
        raise ScheduleError("段数 %d 不合法：只读恰好 2 段、写入恰好 3 段（对象名里不许有空格）"
                            % len(segs))
    cmds = [c for c in (argv or []) if c]
    if kind == "readonly":
        bad = [c for c in cmds if c not in READONLY_ARGV]
        if bad:
            raise ScheduleError("只读名称出现白名单外命令：%s（白名单：%s）"
                                % (", ".join(bad), " ".join(READONLY_ARGV)))
    return {"result": "ok", "name": name, "segments": segs, "kind": kind,
            "verb": verb, "argv": cmds}


def check_schedule(rrule):
    """B1：周期任务固定 1 小时。天维度、INTERVAL≠1（含用 0 表示高频）一律拒绝。"""
    raw = str(rrule or "").strip()
    if not raw:
        raise ScheduleError("rrule 为空")
    norm = raw.upper().replace(" ", "")
    parts = {}
    for chunk in norm.split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k] = v
    freq = parts.get("FREQ")
    if freq != "HOURLY":
        raise ScheduleError("FREQ 必须 HOURLY（周期固定 1 小时），收到 %r" % (freq or "缺失"))
    iv = parts.get("INTERVAL")
    if iv is not None:
        try:
            n = int(iv)
        except ValueError:
            raise ScheduleError("INTERVAL 不是整数：%r" % iv)
        if n != 1:
            raise ScheduleError("INTERVAL 必须为 1：%d 会变成 %s 小时一次，"
                                "高频也不能用 INTERVAL<1 表示" % (n, n))
    return {"result": "ok", "rrule": raw, "freq": "HOURLY", "interval": 1}


def check_oneshot(scheduled_at, now=None):
    """B4/B5：一次性任务必须带时区、必须落在未来 1 小时内。

    事故：盘点出 4 个早已过点却仍 ACTIVE 的 one-shot，没人负责归档。
    所以「建之前」就把已过去与过远的两头堵住，而不是等事后巡检。
    """
    raw = str(scheduled_at or "").strip()
    if not raw:
        raise ScheduleError("scheduledAt 为空")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ScheduleError("scheduledAt 不是 ISO 8601：%r" % raw)
    if dt.tzinfo is None:
        raise ScheduleError("scheduledAt 缺时区偏移（必须形如 2026-11-19T16:00:00+08:00）：%r" % raw)
    base = now or datetime.now(dt.tzinfo)
    delta = (dt - base).total_seconds()
    if delta <= 0:
        raise ScheduleError("scheduledAt 已过去（%s）：不得建成 ACTIVE，先按 B5 归档" % raw)
    if delta > 3600:
        raise ScheduleError("scheduledAt 距今 %.0f 分钟 > 60 分钟：改走每小时条件巡检（B2）"
                            % (delta / 60.0))
    return {"result": "ok", "scheduled_at": raw, "seconds_from_now": int(delta),
            "lifetime_ok": True}


def _size_of(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _read_ephemeral(path):
    """读瞬时状态文件（槽位/租约），返回 (info, readable)，**绝不改文件**。

    为什么不用 `_load_json`：它会把「解析失败的非空文件」隔离改名。槽位与租约是
    「先 O_EXCL 占名、再写内容」的，读的人完全可能读到半截 JSON；一旦被隔离，
    文件名就空出来了，第二个进程立刻能占同一个槽位——实测表现就是同一个槽位
    发给两个进程（并发超发）。瞬时文件的正确姿态是：读不懂就当它还在，不回收。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return {}, False
    if size == 0:
        return {}, False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    return data, True


def reply_digest(raw_body):
    """A12：把一条 assistant 消息的 parts 统计出来，用于核对「GET 全文是否完整」。

    返回 {message_id, parts, chars, sha256}；没有可用消息返回 None。
    为什么要有它：流被截断时 SSE 可能是半截，GET 回来的 mapping 也可能缺 part，
    只看「长度变长了」会把缺 part 的回复当完整——必须按 part 数与拼接 SHA-256 对账。
    """
    try:
        data = json.loads(raw_body)
    except Exception:
        return None
    best = None
    for node_id, node in (data.get("mapping") or {}).items():
        msg = (node or {}).get("message") or {}
        if (msg.get("author") or {}).get("role") != "assistant":
            continue
        parts = [p for p in ((msg.get("content") or {}).get("parts") or []) if isinstance(p, str)]
        if not parts or not "".join(parts).strip():
            continue
        key = (msg.get("create_time") or 0, msg.get("id") or node_id)
        if best is None or key > best[0]:
            best = (key, msg, parts)
    if best is None:
        return None
    _key, msg, parts = best
    joined = "".join(parts).encode("utf-8")
    return {"message_id": msg.get("id") or "",
            "parts": [len(p) for p in parts],
            "chars": sum(len(p) for p in parts),
            "sha256": hashlib.sha256(joined).hexdigest()}


def _load_json(path, default):
    """读状态 json。文件损坏时隔离为 .corrupt-<ts> 并告警，而不是静默返回空。

    静默返回空最危险的地方在于：下一次 _save_json 会把原来的内容永久覆盖掉
    （conv-accounts 最多 500 条归属记录会一次性清空）。
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, json.JSONDecodeError) as e:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size > 0:
            bad = "%s.corrupt-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
            try:
                os.replace(path, bad)
                print("WARN: 状态文件损坏已隔离 %s -> %s（后续按空状态重建，旧内容在隔离文件里）"
                      % (os.path.basename(path), os.path.basename(bad)), flush=True)
                return copy.deepcopy(default)
            except OSError:
                pass
        print("WARN: 读取 %s 失败：%s" % (os.path.basename(path), e), flush=True)
        return copy.deepcopy(default)


# ---------- 状态文件跨进程锁 ----------
# 三个 json 都是「读-改-写」，两个进程同时跑会互相覆盖（比如两个发送器同时推进游标，
# 就会把均分打成 2:0）。这里用 O_EXCL 锁文件做排他，带 stale 检测与线程级重入保护。
# 重入保护必须按线程记（早先按路径记一个集合，等于让同进程的第二个线程直接跳过加锁，
# 并发下照样丢更新）；跨进程仍然靠 O_EXCL 锁文件。
_LOCK_HELD = {}            # path -> 持有者线程 ident
LOCK_STALE_SEC = 60        # 锁文件超过这个年龄视为残留（进程被杀）
LOCK_TIMEOUT_SEC = 3.0     # 临界区都在毫秒级；等 3s 足够，等 8s 只会在拥塞时放大停顿


def _proc_alive(pid):
    """进程是否还在（只判存在，不判身份）。读不到一律按「不在」之外的保守侧处理。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == 259              # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _proc_start_time(pid):
    """进程启动时间（Windows 用 FILETIME 整数，POSIX 用 /proc 的 st_mtime）。

    读不到返回 None —— 调用方必须把 None 当「身份不可确认」，禁止据此回收锁。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return None
            try:
                c, e, k, u = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                          ctypes.byref(k), ctypes.byref(u)):
                    return None
                return (c.dwHighDateTime << 32) | c.dwLowDateTime
            finally:
                k32.CloseHandle(h)
        return os.stat("/proc/%d" % pid).st_mtime
    except Exception:
        return None


def _lock_holder_stale(lock):
    """锁能否回收：租约过期 + 原持有者确认不在（或 PID 已被复用）。

    判定顺序（A22）：
      1. 读不到锁内容（老格式/损坏）→ 退化为 mtime 判据，兼容旧行为；
      2. 租约未过期 → 不可回收；
      3. 原 PID 不存活 → 可回收；
      4. 存活但启动时间读不到 → **不可回收**（宁可等超时，也不误删活锁）；
      5. 存活但启动时间与记录不一致 → PID 被复用，可回收；
      6. 否则不可回收。
    """
    try:
        with open(lock, "r", encoding="utf-8") as f:
            info = json.load(f)
        if not isinstance(info, dict):
            info = {}
    except Exception:
        info = {}
    pid = info.get("pid")
    if not pid:
        try:
            return time.time() - os.path.getmtime(lock) > LOCK_STALE_SEC
        except OSError:
            return False
    try:
        created = float(info.get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0.0
    if time.time() - created <= LOCK_STALE_SEC:
        return False
    if not _proc_alive(pid):
        return True
    cur = _proc_start_time(pid)
    if cur is None:
        return False
    return info.get("process_start_time") not in (None, cur)


class LockTimeout(RuntimeError):
    """抢锁超时。strict 模式下必须抛出，绝不「跳过加锁继续跑」。"""


@contextlib.contextmanager
def _file_lock(path, timeout=LOCK_TIMEOUT_SEC, strict=False):
    me = threading.get_ident()
    if _LOCK_HELD.get(path) == me:  # 同线程重入：直接放行（避免自锁）
        yield
        return
    lock = path + ".lock"
    end = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            # 锁里写持有者身份：只靠 mtime 会在 PID 复用/长任务时误判（A22）
            try:
                os.write(fd, json.dumps({
                    "pid": os.getpid(),
                    "process_start_time": _proc_start_time(os.getpid()),
                    "created_at": time.time(),
                    "thread": me,
                    "path": os.path.basename(path),
                }).encode("utf-8"))
            except OSError:
                pass
            break
        except FileExistsError:
            try:
                if _lock_holder_stale(lock):
                    os.remove(lock)  # 干掉残留锁（已确认持有者不在）
                    continue
            except OSError:
                pass
            if time.time() >= end:
                if strict:
                    raise LockTimeout("锁等待超时（%s）" % os.path.basename(path))
                print("WARN: 状态文件锁等待超时，跳过加锁（%s）" % os.path.basename(path), flush=True)
                break
            time.sleep(0.1)
        except OSError as e:
            if strict:
                raise LockTimeout("加锁失败（%s）：%s" % (os.path.basename(path), e))
            print("WARN: 状态文件加锁失败（%s）：%s" % (os.path.basename(path), e), flush=True)
            break
    _LOCK_HELD[path] = me
    try:
        yield
    finally:
        _LOCK_HELD.pop(path, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.remove(lock)
            except OSError:
                pass


def _save_json(path, data):
    with _file_lock(path):
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


# ---------- 调度槽位与租约（B8 / B15 的落地实现）----------
SLOT_CAP = 3          # 调度器全局并发上限（日志实测 concurrency=3）
LEASE_SEC = 60        # 默认租约秒数（B15）
HEARTBEAT_SEC = 15    # 心跳间隔（B15）
RUN_MAX_SEC = 3600    # 单次运行上限，超时记 TIMEOUT 并释放租约（B15）


def _slots_dir(base=None):
    return os.path.join(base or BASE, "slots")


def _lease_reclaimable(info, now=None, expect_run_id=None):
    """这份租约能否回收（B15 判定口径，纯函数便于回归）。

    返回 (bool, reason)。reason 取值：
      empty                空/损坏/无 run_id → 可回收
      lease_valid          租约未到期，别人在跑
      run_id_mismatch      文件里不是目标实例（不回收也不接管，调用方须 exit 1）
      process_gone         原进程确实不在了
      pid_reused           PID 存活但启动时间不符（被系统复用）
      identity_unreadable  进程身份读不到 → 禁止回收
      alive                原进程仍在且租约过期（长任务未续期）
    """
    now = time.time() if now is None else now
    if not isinstance(info, dict) or not info.get("run_id"):
        return True, "empty"
    if expect_run_id is not None and info.get("run_id") != expect_run_id:
        return False, "run_id_mismatch"
    try:
        until = float(info.get("lease_until") or 0)
    except (TypeError, ValueError):
        until = 0.0
    if now < until:
        return False, "lease_valid"
    if not _proc_alive(info.get("pid")):
        return True, "process_gone"
    cur = _proc_start_time(info.get("pid"))
    if cur is None:
        return False, "identity_unreadable"
    if info.get("process_start_time") not in (None, cur):
        return True, "pid_reused"
    return False, "alive"


def _record_corruption(kind, path, why):
    """损坏登记（append-only JSONL）：所有发现瞬时文件损坏的入口都往这里写。

    为什么必须集中登记：槽位与租约「读不懂就不动」是安全的，但如果没人记，
    就会出现「任务永远排队、永远没人知道为什么」。登记里带 sha256，是为了让
    后续人工恢复（B15-R）能核对「批准的是不是同一个文件」。
    """
    try:
        digest = _sha256_file(path)
    except OSError:
        digest = "?"
    row = {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "kind": kind, "path": os.path.abspath(path), "why": why,
           "size": _size_of(path), "sha256": digest}
    ok = True
    try:
        with open(os.path.join(BASE, "corrupt-registry.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
    except OSError as e:
        ok = False
        print("WARN: 损坏登记写入失败（%s）：%s" % (os.path.basename(path), e), flush=True)
    print("WARN: %s 不可用（%s）：%s size=%d sha256=%s；按占用处理，见 corrupt-registry.jsonl"
          % (kind, why, os.path.basename(path), row["size"], digest[:16]), flush=True)
    return row if ok else None


def _log_corrupt_lease(path, why):
    return _record_corruption("lease", path, why)


def lease_acquire(path, task_id, run_id=None, lease_sec=LEASE_SEC, lock_timeout=LOCK_TIMEOUT_SEC):
    """领租约。成功返回 run_id；已有有效租约返回 None（调用方 exit 1，不得起第二个实例）。

    strict 锁：抢不到锁就按「租约冲突」处理（B15），绝不无锁进临界区。
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    now = time.time()
    try:
        with _file_lock(path, timeout=lock_timeout, strict=True):
            info, readable = _read_ephemeral(path)
            if not readable and _size_of(path) > 0:
                # 读不懂的租约宁可判「有人在跑」，也不能放第二个实例进来（B15 身份不可读禁回收）
                _log_corrupt_lease(path, "identity_unreadable")
                return None
            ok, why = _lease_reclaimable(info)
            if not ok:
                # 审计要能分辨「有人在跑」与「抢不到锁」：两者都拒绝启动，但原因不同
                print("WARN: 租约冲突（%s），不启动第二个实例：%s"
                      % (why, os.path.basename(path)), flush=True)
                return None
            _atomic_write(path, json.dumps({
                "task_id": task_id,
                "run_id": run_id,
                "pid": os.getpid(),
                "process_start_time": _proc_start_time(os.getpid()),
                "started_at": now,
                "heartbeat_at": now,
                "lease_until": now + lease_sec,
            }, ensure_ascii=False, indent=2))
    except LockTimeout:
        print("WARN: 租约锁等待超时 → 按 B15 判冲突（调用方 exit 1），不启动第二个实例", flush=True)
        return None
    return run_id


def lease_heartbeat(path, run_id, lease_sec=LEASE_SEC, lock_timeout=LOCK_TIMEOUT_SEC):
    """续期（B15：每 15s 一次）。返回 False 说明租约已不属于自己（或抢不到锁）。"""
    try:
        return _lease_heartbeat_locked(path, run_id, lease_sec, lock_timeout)
    except LockTimeout:
        print("WARN: 心跳抢不到租约锁，本次续期失败（按不属于自己处理）", flush=True)
        return False


def _lease_heartbeat_locked(path, run_id, lease_sec, lock_timeout):
    with _file_lock(path, timeout=lock_timeout, strict=True):
        info, _readable = _read_ephemeral(path)
        if info.get("run_id") != run_id:
            return False
        now = time.time()
        info["heartbeat_at"] = now
        info["lease_until"] = now + lease_sec
        _atomic_write(path, json.dumps(info, ensure_ascii=False, indent=2))
    return True


def lease_release(path, run_id, lock_timeout=LOCK_TIMEOUT_SEC):
    """主动释放：只有持有者能释放。抢不到锁 → 不释放（返回 False，让调用方记 orphan）。"""
    try:
        return _lease_release_locked(path, run_id, lock_timeout)
    except LockTimeout:
        print("WARN: 释放租约抢不到锁，保留租约（按 TIMEOUT_ORPHAN 处理）", flush=True)
        return False


def _lease_release_locked(path, run_id, lock_timeout):
    with _file_lock(path, timeout=lock_timeout, strict=True):
        info, _readable = _read_ephemeral(path)
        if info.get("run_id") != run_id:
            return False
        try:
            os.remove(path)
        except OSError:
            _atomic_write(path, "{}")
    return True


def lease_reclaim(path, expect_run_id=None, lock_timeout=LOCK_TIMEOUT_SEC):
    """按 B15 口径回收。返回 (是否回收, reason)。抢不到锁 → 不回收（lock_timeout）。"""
    try:
        with _file_lock(path, timeout=lock_timeout, strict=True):
            info, readable = _read_ephemeral(path)
            if not readable and _size_of(path) > 0:
                # 身份读不懂 → 不回收（B15）。旧版会把它当空租约直接删掉，等于放第二个实例进来。
                _log_corrupt_lease(path, "identity_unreadable")
                return False, "identity_unreadable"
            ok, why = _lease_reclaimable(info, expect_run_id=expect_run_id)
            if ok:
                try:
                    os.remove(path)
                except OSError:
                    _atomic_write(path, "{}")
            return ok, why
    except LockTimeout:
        return False, "lock_timeout"


def lease_force_release(path, approval_path):
    """B15-R：损坏租约的人工恢复闸口（R1–R8）。

    租约读不懂就永不自动回收 → 可能永久占位。解法不是加 TTL（年龄不等于死亡），
    而是留一条**需要人明确批准、且留痕**的通道。批准文件字段：
        {"sha256": "<损坏租约的哈希>", "approved_at": "<ISO8601>",
         "approver": "<谁批的>", "reason": "<为什么批>"}
    返回 (ok, reason)。reason ∈ absent / no_approval / approval_incomplete /
    sha256_mismatch / process_alive / quarantine_failed / quarantined。
    """
    if not os.path.exists(path):
        return True, "absent"
    # R2：整段恢复在同一把 strict 锁内完成，避免「校验时是 A、改名时已被替换成 B」的 TOCTOU
    try:
        with _file_lock(path, strict=True):
            return _force_release_locked(path, approval_path)
    except LockTimeout:
        return False, "lock_timeout"


def _force_release_locked(path, approval_path):
    try:
        with open(approval_path, encoding="utf-8") as f:
            approval = json.load(f)
    except (OSError, ValueError):
        return False, "no_approval"
    if not isinstance(approval, dict):
        return False, "no_approval"
    want = str(approval.get("sha256") or "").strip().lower()
    # R6：批准必须可追溯到人、时间、原因，缺一项就是「口头放行」
    if not want or not approval.get("approved_at") or not approval.get("approver")             or not approval.get("reason"):
        return False, "approval_incomplete"
    try:
        got = _sha256_file(path)
    except OSError as e:
        return False, "unreadable:%s" % e
    if got != want:
        return False, "sha256_mismatch"
    # R1：先把证据登记下来（原哈希/大小/发现时间），再谈恢复。
    # 登记失败必须阻断——「恢复成功但没有审计记录」比不恢复更危险。
    if _record_corruption("lease-recovery", path, "approved_recovery") is None:
        return False, "audit_registration_failed"
    # R3/R4：能读出 pid 且进程还活着 → 不许恢复（文件坏了不代表进程死了）
    info, readable = _read_ephemeral(path)
    if readable:
        pid = info.get("pid")
        if pid and _proc_alive(pid):
            return False, "process_alive"
    else:
        # 身份读不出来 ≠ 原实例已死。必须由批准人给出独立确认依据（例如进程表快照、
        # 子进程检查、B7 状态核对结果），否则拒绝——不能让「批准」替「证据」。
        if not str(approval.get("identity_evidence") or "").strip():
            return False, "identity_unconfirmed"
    bad = "%s.corrupt-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    try:
        os.replace(path, bad)      # R7：隔离归档，不覆盖
    except OSError as e:
        return False, "quarantine_failed:%s" % e
    # R8：隔离后确认原路径确实空出来了，别把别人的新租约当成自己的战果
    if os.path.exists(path):
        return False, "quarantine_failed:path_still_present"
    print("WARN: 损坏租约已按批准隔离 %s -> %s（原字节保留，可复查）"
          % (os.path.basename(path), os.path.basename(bad)), flush=True)
    return True, "quarantined"


def _call_body(body, cancel):
    """按签名决定传不传 cancel（零参 body 照旧可用）。"""
    try:
        params = [p for p in inspect.signature(body).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                  and p.default is p.empty]
        if len(params) >= 1:
            return body(cancel)
    except (TypeError, ValueError):
        pass
    return body()


def lease_run(path, task_id, body, run_id=None, lease_sec=LEASE_SEC,
              heartbeat_sec=HEARTBEAT_SEC, max_sec=RUN_MAX_SEC, grace_sec=5):
    """领租约 → 后台按 heartbeat_sec 续期 → 执行 body → 释放。

    返回 (value, status)，status ∈ DONE / TIMEOUT / TIMEOUT_ORPHAN / LEASE_BUSY。

    超时语义（B15）：
      - 先把 cancel 置位，给执行体 grace_sec 收尾；执行体确实停了才释放租约，记 TIMEOUT。
      - 执行体不响应取消（仍在跑）→ 记 TIMEOUT_ORPHAN 并**保留租约**：
        这时释放租约等于允许第二个实例起来，正好造成双跑——比留下僵尸实例危险得多。
    body 可选地接受一个参数（threading.Event），在中途检查它并尽快返回。
    """
    rid = lease_acquire(path, task_id, run_id=run_id, lease_sec=lease_sec)
    if not rid:
        return None, "LEASE_BUSY"
    stop = threading.Event()

    def _beat():
        while not stop.wait(heartbeat_sec):
            try:
                lease_heartbeat(path, rid, lease_sec)
            except Exception:
                pass

    th = threading.Thread(target=_beat, daemon=True)
    th.start()
    box = {}
    cancel = threading.Event()

    def _wrap():
        try:
            box["value"] = _call_body(body, cancel)
        except BaseException as e:      # noqa: BLE001 - 原样回抛给调用方
            box["error"] = e

    runner = threading.Thread(target=_wrap, daemon=True)
    runner.start()
    runner.join(max_sec)
    timed_out = runner.is_alive()
    if timed_out:
        cancel.set()
        runner.join(grace_sec)
    stop.set()
    if runner.is_alive():
        return None, "TIMEOUT_ORPHAN"   # 租约故意不释放
    try:
        lease_release(path, rid)        # 执行体已停：释放租约
    except Exception:
        pass
    if timed_out:
        return None, "TIMEOUT"
    if "error" in box:
        raise box["error"]
    return box.get("value"), "DONE"


def acquire_slot(task_id, run_id=None, cap=SLOT_CAP, base=None, lease_sec=LEASE_SEC,
                 lock_timeout=LOCK_TIMEOUT_SEC):
    """B8：原子领取全局并发槽位。

    读当前 RUNNING 数 + 占槽在同一次跨进程加锁内完成，所以两个任务不可能
    同时读到 2 再一起启动。返回 (slot_path, run_id)；满员返回 (None, run_id)，
    调用方必须记 QUEUED，不得当已执行。
    """
    d = _slots_dir(base)
    os.makedirs(d, exist_ok=True)
    run_id = run_id or uuid.uuid4().hex[:12]
    try:
        # strict=True：抢不到锁就**不启动**（fail-closed）。旧版超时后跳过加锁继续跑，
        # 6 进程抢槽实测出现 6 个槽位全被占——「WARN 不等于阻断」在并发上限上是致命的。
        with _file_lock(os.path.join(d, ".slots"), timeout=lock_timeout, strict=True):
            # 先回收死槽（进程没了 / PID 复用），再占位
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".slot"):
                    continue
                sp = os.path.join(d, fn)
                info, readable = _read_ephemeral(sp)
                if not readable:
                    # 空文件（size=0）才是「占名未写」的中间态：超 5s 仍空 → 创建者已死，可回收。
                    # 非空但读不懂 = 身份不明 → **永不自动回收**（B15 身份不可读禁回收），
                    # 旧版按 mtime 超过 5s 就删，等于用「文件年龄」冒充「进程死亡」。
                    if _size_of(sp) > 0:
                        _record_corruption("slot", sp, "identity_unreadable")
                        continue
                    try:
                        age = time.time() - os.path.getmtime(sp)
                    except OSError:
                        age = 0.0
                    if age <= 5:
                        continue
                    ok = True
                else:
                    ok, why = _lease_reclaimable(info)
                    if ok and why == "empty":
                        try:
                            age = time.time() - os.path.getmtime(sp)
                        except OSError:
                            age = 0.0
                        ok = age > 5
                if ok:
                    try:
                        os.remove(sp)
                    except OSError:
                        pass
            for i in range(1, int(cap) + 1):
                sp = os.path.join(d, "slot-%d.slot" % i)
                try:
                    fd = os.open(sp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    continue
                now = time.time()
                try:
                    os.write(fd, json.dumps({
                        "task_id": task_id,
                        "run_id": run_id,
                        "pid": os.getpid(),
                        "process_start_time": _proc_start_time(os.getpid()),
                        "started_at": now,
                        "heartbeat_at": now,
                        "lease_until": now + lease_sec,
                    }, ensure_ascii=False, indent=2).encode("utf-8"))
                finally:
                    os.close(fd)
                return sp, run_id
    except LockTimeout as e:
        print("WARN: 抢不到槽位锁，本轮按 QUEUED 处理（不启动）：%s" % e, flush=True)
        return None, run_id
    return None, run_id


def release_slot(slot_path, run_id=None):
    """释放槽位（只有持有者能释放）。"""
    if not slot_path or not os.path.exists(slot_path):
        return False
    if run_id is not None:
        info, _readable = _read_ephemeral(slot_path)
        if info.get("run_id") not in (None, run_id):
            return False
    try:
        os.remove(slot_path)
        return True
    except OSError:
        return False


def _update_json(path, mutate, default):
    """锁内读-改-写，杜绝并发丢更新。mutate(data) 就地修改，返回 False 表示无需落盘。"""
    with _file_lock(path):
        data = _load_json(path, default)
        if mutate(data) is False:
            return data
        _save_json(path, data)
        return data


def _normalize_rotation(st):
    order = [a for a in st.get("order") or ROTATION_ORDER if a in ROTATION_ORDER]
    for a in ROTATION_ORDER:  # 保证三身份齐全且顺序稳定
        if a not in order:
            order.append(a)
    st["order"] = order
    # 游标只在登录态身份之间推进（匿名是兜底，不参与均分）
    st["cursor"] = int(st.get("cursor") or 0) % len(ROTATION_PRIMARY)
    st.setdefault("created", {a: 0 for a in order})
    st.setdefault("failures", {a: 0 for a in order})
    st.setdefault("unavailable", {})
    st.setdefault("fallback_uses", 0)
    for a in order:
        st["created"].setdefault(a, 0)
        st["failures"].setdefault(a, 0)
    return st


def load_rotation():
    return _normalize_rotation(_load_json(ROTATION_FILE, {}))


def mutate_rotation(fn):
    """锁内读-改-写轮转状态；fn(st) 就地修改。并发建会话时计数/游标不会互相覆盖。"""
    out = {}

    def mutate(st):
        _normalize_rotation(st)
        fn(st)
        st["updated"] = _now()
        out.update(st)

    _update_json(ROTATION_FILE, mutate, {})
    return _normalize_rotation(out)


def save_rotation(st):
    st["updated"] = _now()
    _save_json(ROTATION_FILE, st)


def mark_unavailable(st, account, reason, ttl=None):
    st.setdefault("unavailable", {})[account] = {
        "reason": reason, "at": _now(), "epoch": time.time(),
        "ttl": UNAVAILABLE_TTL if ttl is None else ttl,
    }


def is_unavailable(st, account):
    info = (st.get("unavailable") or {}).get(account)
    if not info:
        return False
    ttl = float(info.get("ttl") or UNAVAILABLE_TTL)
    return time.time() - float(info.get("epoch") or 0) <= ttl


def purge_expired(st):
    """清掉过期的不可用标记（is_unavailable 只读，不再隐式改状态）。"""
    changed = False
    for a in list(st.get("unavailable") or {}):
        info = st["unavailable"][a] or {}
        ttl = float(info.get("ttl") or UNAVAILABLE_TTL)
        if time.time() - float(info.get("epoch") or 0) > ttl:
            st["unavailable"].pop(a, None)
            changed = True
    return changed


def rotation_plan(st):
    """登录取证身份（acct1/acct2）从游标起顺次尝试；不可用的排到末尾（避免死锁）。
    匿名态不进入该计划——它只在两者都 429 时由 anon_fallback_ready() 决定是否兜底。"""
    n = len(ROTATION_PRIMARY)
    seq = [ROTATION_PRIMARY[(st["cursor"] + i) % n] for i in range(n)]
    return [a for a in seq if not is_unavailable(st, a)] + [a for a in seq if is_unavailable(st, a)]


def anon_fallback_block_reason(st, outcomes):
    """兜底放行条件：两个登录态**全部**因 429（本次命中或 429 冷却中）失败。

    outcomes: {account: 状态码或 "SKIP:<原因>"}
    返回 None 表示放行；否则返回一句人话说明为什么没放行。
    """
    if os.environ.get("CHATGPT_ANON_FALLBACK", "1") == "0":
        return "CHATGPT_ANON_FALLBACK=0（匿名兜底被环境变量禁用）"
    if not outcomes:
        return "没有任何登录态尝试结果，不启用兜底"
    if not all("429" in str(v) for v in outcomes.values()):
        return "登录态并非全部 429（401=凭据失效、403=风控，都不该用匿名顶）"
    info = (st.get("unavailable") or {}).get("nologin") or {}
    if "manually disabled" in (info.get("reason") or ""):
        return "nologin 已被手动禁用（--rotation-enable nologin 解封）"
    if is_unavailable(st, "nologin"):
        ttl = float(info.get("ttl") or UNAVAILABLE_TTL)
        left = int(ttl - (time.time() - float(info.get("epoch") or 0)))
        return "匿名通道冷却中（%s），约 %d 秒后可重试" % (info.get("reason") or "unknown", max(left, 0))
    return None


def anon_fallback_ready(st, outcomes):
    return anon_fallback_block_reason(st, outcomes) is None


def record_conv(conv_id, account, prompt_file):
    if not conv_id:
        return
    if not _ID_RE.match(conv_id):  # 外部传入的 id 先过校验再进状态表，避免污染/穿越
        print("WARN: 拒绝登记非法 conv_id：%r" % (conv_id,), flush=True)
        return

    def mutate(data):
        data[conv_id] = {"account": account, "prompt": prompt_file, "ts": _now()}
        if len(data) > 500:  # 只保留最近 500 条
            for k in sorted(data, key=lambda x: (data[x] or {}).get("ts") or "")[:len(data) - 500]:
                data.pop(k, None)

    _update_json(CONV_MAP_FILE, mutate, {})


def conv_owner(conv_id):
    info = _load_json(CONV_MAP_FILE, {}).get(conv_id)
    return (info or {}).get("account")


# ---------- 匿名会话 id 对照表（服务端不给历史，skill 侧自己留存） ----------
def load_anon_sessions():
    data = _load_json(ANON_SESSIONS_FILE, {})
    data.setdefault("sessions", {})
    return data


def save_anon_sessions(data):
    data["updated"] = _now()
    _save_json(ANON_SESSIONS_FILE, data)


def record_anon_session(conv_id, tab=None, prompt="", reply="", device=""):
    """登记/更新匿名会话：conv_id -> 标签页、prompt、最后回复、设备号、可读状态。"""
    if not conv_id:
        return None
    if not _ID_RE.match(conv_id):
        print("WARN: 拒绝登记非法匿名会话 id：%r" % (conv_id,), flush=True)
        return None
    if device and not _ID_RE.match(device):
        device = ""  # 设备号也会拼进 devices/<did>.txt，非法值直接丢弃
    out = {}

    def mutate(data):
        data.setdefault("sessions", {})
        s = data["sessions"].setdefault(conv_id, {"created": _now()})
        s.setdefault("created", _now())
        s["updated"] = _now()
        if tab:
            s["tab"] = tab
        if prompt:
            s["prompt"] = os.path.basename(prompt)
        if reply:
            s["reply_excerpt"] = reply.strip().replace("\n", " ")[:200]
            s["replied"] = _now()
        if device:
            s["device"] = device
        s["status"] = "live" if tab else s.get("status", "archived")
        out.update(s)

    _update_json(ANON_SESSIONS_FILE, mutate, {"sessions": {}})
    return out


def anon_session(conv_id):
    return (load_anon_sessions().get("sessions") or {}).get(conv_id)


def mark_anon_archived(conv_id, reason=""):
    def mutate(data):
        data.setdefault("sessions", {})
        s = (data.get("sessions") or {}).get(conv_id)
        if not s:
            return False
        s["status"] = "archived"
        s["archived_reason"] = reason
        s["updated"] = _now()

    _update_json(ANON_SESSIONS_FILE, mutate, {"sessions": {}})


# ---------- 匿名设备身份留存 ----------
# 匿名会话只认 oai-did：浏览器换 profile 后设备号变了，新 cookie 读旧会话会 404。
# 因此把设备身份完整落在本地（都在 accounts/nologin/ 下，已 gitignore）：
#   device-cookie.txt        最新一份 Cookie 头（兼容旧版用法）
#   devices/<did>.txt        按设备号分档的 Cookie 头
#   devices/<did>.json       完整 cookie jar（含 HttpOnly / 过期时间），可回灌浏览器
# 读旧会话时按 anon-sessions.json 记录的 device 取回身份；记录缺失或对不上时依次试
# 其他留存设备（新→旧），命中即自动修正归属 —— 这是跨浏览器重启存活的关键。
AUTH_COOKIE_RE = re.compile(
    r"(?i)(session[-_]?token|authjs|next-auth|access[-_]?token|refresh[-_]?token)")
DEVICE_TRY_MAX = 8          # 一次读取最多试几个设备快照：别把 429 配额烧在遍历上


def anon_device_dir():
    return os.path.join(BASE, "accounts", "nologin", "devices")


def _read_text(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def parse_cookie_header(cookie):
    """Cookie 头 → {name: value}；坏片段直接丢弃，不抛错。"""
    out = {}
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


# ---------- 统一出口代理 ----------
# 所有出网动作共用同一个出口：API（urllib）、匿名浏览器（Chrome --proxy-server）、
# 自启动的浏览器进程。只给 API 配代理等于「请求走隧道、浏览器裸连」——
# 设备身份与 cookie 会从直连那条路露出去，所以这里做成单点解析 + 缺失即拒。
#
# 解析优先级：CHATGPT_PROXY > proxy.json > 系统 https_proxy/http_proxy/all_proxy > 本机端口探测
# 兜底：一条都解析不到时拒绝联网（除非显式 CHATGPT_ALLOW_DIRECT=1）。
PROXY_FILE = os.path.join(BASE, "proxy.json")
PROXY_AUTODETECT_PORTS = (7890, 7897, 10809, 1080, 8888)
# 本机地址永不过代理：CDP / 本地闸口(CHATGPT_BASE) 一旦被塞进代理，代理挂了就全链路瘫痪
PROXY_BYPASS_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
_PROXY_CACHE = {}


def normalize_proxy(url):
    """补 scheme 并校验形态；空串表示「不配代理」。"""
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "http://" + u           # 只给 host:port 时按 http 代理处理
    scheme = u.split("://", 1)[0].lower()
    if scheme not in PROXY_SCHEMES:
        raise ValueError("不支持的代理类型 %r（只支持 http/https/socks5/socks5h）" % scheme)
    if not urllib.parse.urlsplit(u).hostname:
        raise ValueError("代理地址缺少主机名：%r" % url)
    return u


def load_proxy_config():
    """proxy.json 只放出口配置；损坏时按默认处理，不让它挡住发送。"""
    cfg = _load_json(PROXY_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}
    return cfg


def _tcp_alive(host, port, timeout=0.4):
    try:
        with socket.create_connection((host, int(port)), timeout):
            return True
    except (OSError, ValueError):
        return False


def _proxy_alive(url, host="chatgpt.com", port=443, timeout=6):
    """真连一次：CONNECT / SOCKS 隧道 + TLS 握手。

    只测端口通不通会踩坑：本机常见「端口还在听、上游节点已死」的代理，
    CONNECT 返回 200 后立刻断流，选错就等于整条链路全废（实测 7890 死 / 7897 活）。
    """
    try:
        u = normalize_proxy(url)
        scheme = u.split("://", 1)[0].lower()
        if scheme.startswith("socks"):
            raw = _socks5_open(_parse_socks_proxy(u), host, port, timeout)
        else:
            hp = urllib.parse.urlsplit(u)
            raw = socket.create_connection((hp.hostname, hp.port or 8080), timeout)
            raw.settimeout(timeout)
            lines = ["CONNECT %s:%d HTTP/1.1" % (host, port), "Host: %s:%d" % (host, port)]
            if hp.username:
                cred = "%s:%s" % (urllib.parse.unquote(hp.username),
                                  urllib.parse.unquote(hp.password or ""))
                lines.append("Proxy-Authorization: Basic "
                             + base64.b64encode(cred.encode("utf-8")).decode("ascii"))
            raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin1"))
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = raw.recv(4096)
                if not chunk:
                    raise OSError("代理在 CONNECT 阶段直接断开")
                head += chunk
            status = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
            if " 200" not in status:
                raise OSError("代理拒绝 CONNECT：%s" % status)
        t = CTX.wrap_socket(raw, server_hostname=host)
        t.close()
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def resolve_proxy_info(refresh=False):
    """返回 {url, source}；url 为空表示没有可用出口。结果进程内缓存。"""
    if _PROXY_CACHE and not refresh:
        return _PROXY_CACHE
    cfg = load_proxy_config()
    url, source = "", ""
    for key in ("CHATGPT_PROXY", "CHATGPT_PROXY_URL"):
        if os.environ.get(key):
            try:
                url, source = normalize_proxy(os.environ[key]), "env:" + key
            except ValueError as e:
                print("%s 配置非法：%s" % (key, e), file=sys.stderr, flush=True)
                raise SystemExit(3)
            break
    if not url and cfg.get("url"):
        url, source = normalize_proxy(cfg["url"]), "proxy.json"
    if not url:
        for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
                    "all_proxy", "ALL_PROXY"):
            v = os.environ.get(key)
            if v and "://" in v:
                try:
                    url, source = normalize_proxy(v), "env:" + key
                except ValueError:
                    continue
                break
    if not url and cfg.get("autodetect", True):
        ports = cfg.get("autodetect_ports") or PROXY_AUTODETECT_PORTS
        probe = cfg.get("autodetect_probe", True)
        for p in ports:
            try:
                p = int(p)
            except (TypeError, ValueError):
                continue
            if not _tcp_alive("127.0.0.1", p):
                continue
            cand = "http://127.0.0.1:%d" % p
            if probe:
                # 端口开着不代表能用：必须真握手一次（死节点会 CONNECT 200 后断流）
                ok, why = _proxy_alive(cand, timeout=cfg.get("autodetect_timeout", 5))
                if not ok:
                    # 走 stderr：这条提示会出现在每次解析里，不能弄脏 --proxy-show 的 JSON
                    print("跳过本机代理 %d（探测失败：%s）" % (p, why),
                          file=sys.stderr, flush=True)
                    continue
            url, source = cand, "autodetect:%d" % p
            break
    _PROXY_CACHE.update({"url": url, "source": source, "config": cfg})
    return _PROXY_CACHE


def resolve_proxy(refresh=False):
    return resolve_proxy_info(refresh)["url"]


def allow_direct():
    """显式放行直连的口子：只用于本机调试，默认关闭。"""
    if os.environ.get("CHATGPT_ALLOW_DIRECT") == "1":
        return True
    return bool(load_proxy_config().get("allow_direct"))


def require_proxy(refresh=False):
    """取出口；取不到就拒绝联网（fail-closed），并给出三条可用配法。"""
    info = resolve_proxy_info(refresh)
    if info["url"]:
        return info["url"]
    if allow_direct():
        print("WARN: 没有可用代理，CHATGPT_ALLOW_DIRECT=1 放行直连（出口 IP 会暴露，仅限调试）",
              flush=True)
        return ""
    print("未配置出口代理，拒绝直连（fail-closed）。任选一种配法：\n"
          "  1) 环境变量：CHATGPT_PROXY=http://127.0.0.1:7890\n"
          "  2) 写入配置：python chatgpt_send.py --proxy-set socks5h://user:pass@1.2.3.4:1080\n"
          "  3) 临时放行：CHATGPT_ALLOW_DIRECT=1（真实 IP 会暴露，仅调试用）\n"
          "配完用 python chatgpt_send.py --proxy-check 验证出口。",
          file=sys.stderr, flush=True)
    # 固定 3：编排层要能区分「参数错(2)」「未满足联网条件(3)」「业务失败(1)」
    raise SystemExit(3)


def save_proxy_config(url):
    """把出口写进 proxy.json（含凭据，落盘即 600）。"""
    cfg = load_proxy_config()
    if url:
        cfg["url"] = normalize_proxy(url)
    else:
        cfg.pop("url", None)
    _atomic_write(PROXY_FILE, json.dumps(cfg, ensure_ascii=False, indent=2))
    try:
        if os.name == "posix":
            os.chmod(PROXY_FILE, 0o600)
    except OSError:
        pass
    _PROXY_CACHE.clear()
    return cfg


def _parse_socks_proxy(url):
    u = urllib.parse.urlsplit(normalize_proxy(url))
    return {
        "host": u.hostname or "127.0.0.1",
        "port": u.port or 1080,
        "user": urllib.parse.unquote(u.username or ""),
        "password": urllib.parse.unquote(u.password or ""),
        # socks5h = 由代理解析域名（远端 DNS），跨境场景必需；socks5 = 本地解析
        "rdns": u.scheme.lower() == "socks5h",
    }


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("SOCKS5 连接被对端关闭")
        buf += chunk
    return buf


def _socks5_open(cfg, host, port, timeout=None):
    """建立到 host:port 的 SOCKS5 隧道（RFC1928 + 用户名密码认证）。

    urllib 的 ProxyHandler 只认 http 代理：`socks5h://` 塞进去会直接
    「unknown url type」。所以 socks 走自定义连接类，而不是靠 ProxyHandler。
    """
    if host in PROXY_BYPASS_HOSTS:
        return socket.create_connection((host, int(port)), timeout)   # 本机直连
    s = socket.create_connection((cfg["host"], int(cfg["port"])), timeout)
    try:
        if timeout:
            s.settimeout(timeout)
        if cfg["user"]:
            s.sendall(b"\x05\x02\x00\x02")
        else:
            s.sendall(b"\x05\x01\x00")
        ver, method = _recv_exact(s, 2)
        if ver != 5:
            raise OSError("SOCKS5 握手失败：版本字节 %d" % ver)
        if method == 2:
            if not cfg["user"]:
                raise OSError("SOCKS5 代理要求用户名密码，但地址里没带")
            u = cfg["user"].encode("utf-8")
            p = cfg["password"].encode("utf-8")
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            _v, status = _recv_exact(s, 2)
            if status != 0:
                raise OSError("SOCKS5 认证失败（status=%d）" % status)
        elif method != 0:
            raise OSError("SOCKS5 没有可用的认证方式（method=%d）" % method)
        if cfg["rdns"]:
            addr, atyp = host.encode("utf-8"), 3
        else:
            try:
                addr, atyp = socket.inet_aton(host), 1
            except OSError:
                addr, atyp = host.encode("utf-8"), 3
        head = bytes([atyp]) if atyp == 1 else bytes([atyp, len(addr)]) if atyp == 3 else b""
        s.sendall(b"\x05\x01\x00" + head + addr + struct.pack(">H", int(port)))
        rep = _recv_exact(s, 4)
        if rep[1] != 0:
            raise OSError("SOCKS5 CONNECT 被拒（code=%d，4=主机不可达 5=连接被拒）" % rep[1])
        if rep[3] == 1:
            _recv_exact(s, 4)
        elif rep[3] == 3:
            _recv_exact(s, _recv_exact(s, 1)[0])
        elif rep[3] == 4:
            _recv_exact(s, 16)
        _recv_exact(s, 2)
        return s
    except Exception:
        try:
            s.close()
        except OSError:
            pass
        raise


def _socks_conn_classes(cfg):
    """按目标主机生成 http/https 连接类：connect() 先打 SOCKS 隧道再交回上层。"""
    class _HTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = _socks5_open(cfg, self.host, self.port, self.timeout)

    class _HTTPS(http.client.HTTPSConnection):
        def connect(self):
            raw = _socks5_open(cfg, self.host, self.port, self.timeout)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)

    return _HTTP, _HTTPS


class _BypassProxyHandler(urllib.request.ProxyHandler):
    """http 代理：本机地址（CDP / 本地闸口）不代理，其余全走代理。"""

    def proxy_open(self, req, proxy, type):
        host = urllib.parse.urlsplit(req.full_url).hostname or ""
        if host in PROXY_BYPASS_HOSTS:
            return None
        return super().proxy_open(req, proxy, type)


class _SocksHandler(urllib.request.HTTPSHandler):
    """socks 代理：http 与 https 都走自定义连接类。"""

    def __init__(self, proxy_url, context=None):
        super().__init__(context=context)
        self._cfg = _parse_socks_proxy(proxy_url)

    def http_open(self, req):
        cls = _socks_conn_classes(self._cfg)[0]
        return self.do_open(cls, req)

    def https_open(self, req):
        cls = _socks_conn_classes(self._cfg)[1]
        return self.do_open(cls, req, context=self._context)


def build_opener_for(proxy, context=None, extra=()):
    """按出口构造 opener；proxy 为空时显式禁用环境代理，保证行为可预期。

    extra 用于附加 CookieProcessor 之类的 handler —— 别处再 build 一个
    裸 opener 就等于绕开出口（warm_cookies 曾这么干过）。
    """
    ctx = context or CTX
    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    handlers.extend(extra)
    url = normalize_proxy(proxy)
    if not url:
        handlers.insert(0, urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)
    if url.split("://", 1)[0].lower().startswith("socks"):
        handlers.insert(0, _SocksHandler(url, context=ctx))
    else:
        handlers.insert(0, _BypassProxyHandler({"http": url, "https": url}))
    return urllib.request.build_opener(*handlers)


IP_ECHO_ENDPOINTS = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://ipinfo.io/ip")
_IP_RE = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F]*:[0-9a-fA-F:.]*)$")


def _http_get_text(url, proxy, timeout=10):
    opener = build_opener_for(proxy)
    rq = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/plain"})
    with opener.open(rq, timeout=timeout) as resp:
        return resp.status, resp.read(4096).decode("utf-8", "replace").strip()


def _classify_webrtc(ips, proxy_ip=None):
    """ICE 候选 → 泄漏结论（A23 判定口径，纯函数便于回归）。"""
    ips = [str(x) for x in (ips or [])]
    pub = [ip for ip in ips if _is_public_ip(ip)]
    if not ips:
        status = "clean"                 # 被策略屏蔽，或只拿到 mDNS 名
    elif not pub:
        status = "private_only"
    elif not proxy_ip:
        status = "public_unknown"
    elif proxy_ip in pub:
        status = "via_proxy"
    else:
        status = "leaked"
    out = {"status": status, "ips": ips, "public_ips": pub}
    if status == "leaked":
        out["error"] = "WebRTC 泄漏：公网候选 %s 与代理出口 %s 不一致" % (pub, proxy_ip)
    elif status == "public_unknown":
        out["error"] = "拿到公网候选 %s，但没有代理出口 IP 可比对" % (pub,)
    return out


def _is_public_ip(ip):
    """是否公网 IP（WebRTC 泄漏只关心公网地址）。"""
    try:
        a = ipaddress.ip_address(str(ip).strip("[]"))
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local
                or a.is_multicast or a.is_reserved or a.is_unspecified)


JS_WEBRTC = r"""
new Promise((resolve) => {
  const out = {status: "ok", ips: []};
  const done = () => resolve(JSON.stringify(out));
  if (!window.RTCPeerConnection) { out.status = "unsupported"; return done(); }
  const ips = new Set();
  let pc;
  try {
    pc = new RTCPeerConnection({iceServers: [{urls: "stun:stun.l.google.com:19302"}]});
  } catch (e) { out.status = "error"; out.error = String(e); return done(); }
  try { pc.createDataChannel("probe"); } catch (e) {}
  pc.onicecandidate = (e) => {
    const c = e.candidate && e.candidate.candidate;
    if (!c) return;
    const m = /([0-9]{1,3}(?:\.[0-9]{1,3}){3}|[0-9a-f]{1,4}(?::[0-9a-f]{0,4}){2,7})/i.exec(c);
    if (m) ips.add(m[1]);
  };
  pc.createOffer().then(o => pc.setLocalDescription(o)).catch(() => {});
  setTimeout(() => { try { pc.close(); } catch (e) {} out.ips = Array.from(ips); done(); }, 5000);
})
"""


SITE_PROBE_URL = "https://chatgpt.com/"


def probe_site(proxy=None, timeout=25):
    """A26：不只量 IP，要验**目标站本身**。量 IP 通但站点回 403 是真实事故形态。

    判定：
      ok           200
      challenge    403 + Cloudflare 挑战页（浏览器能过，说明出口本身没被封）
      blocked_vpn  403 + "Unable to load site ... turn it off"：出口 IP 被按 VPN 封
      forbidden    403 但非已知页型（附正文前 160 字，别猜）
      http_<code>  其他状态码
      unreachable  连不上（隧道断/超时/TLS 失败）
    """
    url = resolve_proxy() if proxy is None else normalize_proxy(proxy)
    out = {"url": SITE_PROBE_URL, "status": None, "verdict": "", "note": "", "ok": False}
    try:
        opener = build_opener_for(url)
        rq = urllib.request.Request(SITE_PROBE_URL, headers={
            "User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
        try:
            with opener.open(rq, timeout=timeout) as resp:
                st = resp.status
                body = resp.read(65536).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            st = e.code
            try:
                body = e.read(65536).decode("utf-8", "replace")
            except Exception:
                body = ""
    except Exception as e:
        out["verdict"] = "unreachable"
        out["note"] = "%s: %s" % (type(e).__name__, e)
        return out

    out["status"] = st
    low = body.lower()
    if st == 200:
        out["verdict"] = "ok"
        out["ok"] = True
    elif st == 403 and "unable to load site" in low:
        out["verdict"] = "blocked_vpn"
        out["note"] = "出口 IP 被按 VPN 封；换节点，不要在 cookie/PoW 上白折腾"
    elif st == 403 and any(k in low for k in ("just a moment", "challenge-platform",
                                              "cf_chl", "enable javascript", "cf-mitigated")):
        out["verdict"] = "challenge"
        out["ok"] = True
        out["note"] = "CF 挑战页：浏览器侧可过，API 直连仍可能 403"
    elif st == 403:
        out["verdict"] = "forbidden"
    else:
        out["verdict"] = "http_%d" % st
    if not out["note"] and not out["ok"]:
        vis = re.sub(r"(?s)<(script|style).*?</\1>", " ", body)
        out["note"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", vis)).strip()[:160]
    return out


def check_egress(proxy=None, timeout=10):
    """量出口：代理侧 IP vs 直连侧 IP。两者相同即说明代理没生效。"""
    url = resolve_proxy() if proxy is None else normalize_proxy(proxy)
    out = {"proxy": url, "proxy_ip": None, "direct_ip": None, "same": None,
           "verdict": "", "errors": []}
    if url:
        for ep in IP_ECHO_ENDPOINTS:
            try:
                st, txt = _http_get_text(ep, url, timeout)
                if st == 200 and _IP_RE.match(txt):
                    out["proxy_ip"] = txt
                    break
                out["errors"].append("proxy %s -> HTTP %s" % (ep, st))
            except Exception as e:
                out["errors"].append("proxy %s: %s" % (ep, e))
    # 直连对照：超时要短，避免国内环境白等
    for ep in IP_ECHO_ENDPOINTS[:2]:
        try:
            st, txt = _http_get_text(ep, "", min(timeout, 6))
            if st == 200 and _IP_RE.match(txt):
                out["direct_ip"] = txt
                break
        except Exception as e:
            out["errors"].append("direct %s: %s" % (ep, e))
    if not url:
        out["verdict"] = "未配置代理：当前所有动作直连"
    elif not out["proxy_ip"]:
        out["verdict"] = "代理不可用：请求发不出去（检查地址/认证/服务是否在跑）"
    elif out["direct_ip"] and out["direct_ip"] == out["proxy_ip"]:
        out["same"] = True
        out["verdict"] = "代理未生效：代理出口与直连出口是同一个 IP"
    else:
        out["same"] = False
        out["verdict"] = "代理生效"
    return out


def cookie_header(jar):
    """[{'name','value',...}] → "k=v; k2=v2"（保持顺序，同名只留第一个）。"""
    pairs, seen = [], set()
    for ck in jar or []:
        if not isinstance(ck, dict):
            continue
        k = (ck.get("name") or "").strip()
        if not k or k in seen:
            continue
        seen.add(k)
        pairs.append("%s=%s" % (k, ck.get("value", "")))
    return "; ".join(pairs)


def device_id_from_cookie(cookie):
    return parse_cookie_header(cookie).get("oai-did", "")


def _device_txt(did):
    return os.path.join(anon_device_dir(), did + ".txt")


def _device_jar(did):
    return os.path.join(anon_device_dir(), did + ".json")


def _sanitize_jar(jar):
    """剔除登录态凭据类 cookie，只留设备身份与 CF 系列。

    浏览器若恰好处于登录态（用户手动开过这个 profile），把整包 jar 写盘
    等于把 session token 存进 nologin 目录。宁可不留，也不落凭据。
    """
    out, dropped = [], []
    for ck in jar or []:
        if not isinstance(ck, dict) or not ck.get("name"):
            continue
        name = ck["name"]
        if AUTH_COOKIE_RE.search(name):
            dropped.append(name)
            continue
        out.append({k: ck[k] for k in
                    ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
                    if ck.get(k) is not None})
    if dropped:
        print("WARN: 设备快照丢弃登录态 cookie（%s）：匿名通道不需要，且不该落盘"
              % ", ".join(sorted(set(dropped))), flush=True)
    return out


def save_device_cookie(cookie):
    """把设备身份落盘（Cookie 头字符串或 cookie dict 列表都接受）。

    返回设备号（oai-did）；拿不到合法设备号就不落分档快照，避免拼出非法文件名。
    """
    if not cookie:
        return ""
    if isinstance(cookie, str):
        jar = _sanitize_jar([{"name": k, "value": v, "domain": ".chatgpt.com", "path": "/"}
                             for k, v in parse_cookie_header(cookie).items()])
        header = cookie_header(jar)
    else:
        jar = _sanitize_jar(cookie)
        header = cookie_header(jar)
    did = device_id_from_cookie(header)
    if not _ID_RE.match(did or ""):
        return ""
    try:
        os.makedirs(anon_device_dir(), exist_ok=True)
        _atomic_write(os.path.join(BASE, "accounts", "nologin", "device-cookie.txt"), header)
        if jar:
            _atomic_write(_device_jar(did), json.dumps(jar, ensure_ascii=False, indent=2))
        _atomic_write(_device_txt(did), header)
    except OSError as e:
        print("WARN: 设备快照落盘失败：%s" % e, flush=True)
    return did


def load_device_jar(did):
    """完整 jar；只有 .txt 的老快照按 Cookie 头重建（会缺 HttpOnly，但身份可用）。"""
    if not did or not _ID_RE.match(did):
        return []
    try:
        with open(_device_jar(did), encoding="utf-8") as f:
            jar = json.load(f)
        if isinstance(jar, list) and jar:
            return jar
    except (OSError, json.JSONDecodeError):
        pass
    return [{"name": k, "value": v, "domain": ".chatgpt.com", "path": "/"}
            for k, v in parse_cookie_header(_read_text(_device_txt(did))).items()]


def load_device_cookie(did):
    """取设备 Cookie 头：优先 .txt；只有 jar 时从 jar 重建，老快照同样读得回。"""
    if not did or not _ID_RE.match(did):
        return ""
    txt = _read_text(_device_txt(did)).strip()
    if txt:
        return txt
    return cookie_header(load_device_jar(did))


def list_device_ids(limit=50):
    """已留存设备号，按快照时间倒序（新→旧）。"""
    try:
        names = os.listdir(anon_device_dir())
    except OSError:
        return []
    stamps = {}
    for n in names:
        base, ext = os.path.splitext(n)
        if ext not in (".txt", ".json") or not _ID_RE.match(base):
            continue
        try:
            mt = os.path.getmtime(os.path.join(anon_device_dir(), n))
        except OSError:
            continue
        stamps[base] = max(stamps.get(base, 0), mt)
    ordered = sorted(stamps.items(), key=lambda kv: kv[1], reverse=True)
    return [d for d, _ in ordered[:max(1, limit)]]


def device_summary():
    """--anon-devices 用：留存了哪些设备身份、各自绑了几条会话。"""
    sessions = (load_anon_sessions().get("sessions") or {})
    counts = {}
    for s in sessions.values():
        d = (s or {}).get("device")
        if d:
            counts[d] = counts.get(d, 0) + 1
    current = device_id_from_cookie(
        _read_text(os.path.join(BASE, "accounts", "nologin", "device-cookie.txt")))
    rows = []
    for did in list_device_ids(limit=200):
        jar = load_device_jar(did)
        full = os.path.exists(_device_jar(did))
        txt = load_device_cookie(did)
        try:
            p = _device_txt(did) if os.path.exists(_device_txt(did)) else _device_jar(did)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p)))
        except OSError:
            stamp = ""
        rows.append({
            "device": did,
            "cookies": len(jar) or len(parse_cookie_header(txt)),
            "full_jar": full,
            "sessions": counts.get(did, 0),
            "current": did == current,
            "snapshot_at": stamp,
        })
    return {"count": len(rows), "devices": rows,
            "note": ("current=最新快照（新浏览器会拿它当默认身份）；"
                     "full_jar=false 表示该快照由 Cookie 头重建，缺 HttpOnly 项")}


class ChatGPTAPI:
    def __init__(self, base_dir=None, account="default"):
        # CHATGPT_AUTH_DIR selects credential/cache root; account data stays isolated.
        root = base_dir or os.environ.get("CHATGPT_AUTH_DIR") or BASE
        if account not in ("default", "acct1", "acct2", "nologin"):
            raise ValueError("account must be default, acct1, acct2, or nologin")
        b = root if account in ("default", "acct1") else os.path.join(root, "accounts", account)
        self.account = account
        self.base_dir = b
        self.is_anon = account == "nologin"  # 匿名态：走 /backend-anon/，POST 需浏览器过 Turnstile
        self.anon = None
        if account == "nologin":
            self.cookies, self.token, self.cookie = [], "", ""
        else:
            ck_path = os.path.join(b, "cookies.json")
            tk_path = os.path.join(b, "access_token.txt")
            with open(ck_path, encoding="utf-8") as f:
                self.cookies = json.load(f)
            with open(tk_path, encoding="utf-8") as f:
                self.token = f.read().strip()
            if not isinstance(self.cookies, list):
                raise ValueError("%s 格式不对：应为 cookie 数组" % ck_path)
            self.cookie = "; ".join(
                "%s=%s" % (ck["name"], ck["value"])
                for ck in self.cookies
                if isinstance(ck, dict) and ("chatgpt.com" in (ck.get("domain") or "")
                                             or "openai.com" in (ck.get("domain") or ""))
            )
        # 出口：全部网络动作统一从 CHATGPT_PROXY / proxy.json 取，取不到就拒绝联网。
        # 支持 http/https/socks5/socks5h；本机地址（CDP、本地闸口）自动绕过。
        info = resolve_proxy_info()
        self.proxy = require_proxy()
        self.proxy_source = info.get("source", "")
        self._opener = build_opener_for(self.proxy, CTX)
        self.last_headers = {}
        self.last_body = ""
        self.last_status = 0
        self.quota = {}  # 观测用：最近一次响应的限速相关头

    # ---------- 匿名态设备身份 ----------
    def device_cookie_path(self):
        return os.path.join(BASE, "accounts", "nologin", "device-cookie.txt")

    def ensure_device_cookie(self, refresh=False):
        """匿名态的 cookie 来源：优先当前浏览器，其次上次落盘的 device-cookie.txt。"""
        if self.cookie and not refresh:
            return self.cookie
        saved = "" if refresh else _read_text(self.device_cookie_path()).strip()
        if saved and not refresh:
            self.cookie = saved
            return self.cookie
        try:
            br = self.anon or AnonBrowser()
            self.anon = br
            jar = br.cookie_jar()
            live = cookie_header(jar)
        except Exception:
            live, jar = "", []
        self.cookie = live or saved
        if live:
            save_device_cookie(jar or live)
        return self.cookie

    def _device_id(self):
        for part in (self.cookie or "").split(";"):
            part = part.strip()
            if part.startswith("oai-did="):
                return part.split("=", 1)[1]
        return ""

    def warm_cookies(self):
        """补齐 Cloudflare 侧 cookie（__cf_bm/_cfuvid/__cflb）。

        凭据只存了 session-token 的身份（如 acct2 提取时只拿到 2 个 cookie），GET 一切正常，
        但 POST /conversation 会被判 403 "Unusual activity"——缺的正是首页下发的那几个 CF cookie。
        做法：用现有 cookie 访问首页接收 Set-Cookie，合并后回写 cookies.json。
        """
        if self.is_anon:
            return False
        try:
            import http.cookiejar
            jar = http.cookiejar.CookieJar()
            # 必须复用统一出口：这里自己 build 一个裸 opener 就会直连 chatgpt.com
            opener = build_opener_for(self.proxy, CTX,
                                      extra=[urllib.request.HTTPCookieProcessor(jar)])
            for ck in self.cookies:
                jar.set_cookie(http.cookiejar.Cookie(
                    0, ck["name"], ck["value"], None, False,
                    ck.get("domain") or ".chatgpt.com", False, False, "/", True,
                    False, None, False, None, None, {}))
            # CF cookie 只在站点根下发；API_BASE 可能指向本地闸口（127.0.0.1:xxxx），
            # 对闸口发请求拿不到 __cf_bm，所以这里固定打真实站点。
            host = API_BASE.split("//", 1)[-1].split("/", 1)[0]
            warm_url = (API_BASE + "/") if "chatgpt.com" in host else "https://chatgpt.com/"
            with opener.open(urllib.request.Request(warm_url, headers={"User-Agent": UA}), timeout=45) as r:
                r.read()
        except Exception as e:
            print("warm_cookies failed: %s" % e, flush=True)
            return False
        known = {c["name"]: c for c in self.cookies}
        changed = []
        for ck in jar:
            if ck.name not in known:
                self.cookies.append({"name": ck.name, "value": ck.value,
                                     "domain": ck.domain or ".chatgpt.com"})
                changed.append(ck.name)
            elif ck.value != known[ck.name].get("value"):
                known[ck.name]["value"] = ck.value
                changed.append(ck.name + "(更新)")
        if not changed:
            return False
        try:
            _save_json(os.path.join(self.base_dir, "cookies.json"), self.cookies)
        except OSError:
            pass
        self.cookie = "; ".join(
            "%s=%s" % (c["name"], c["value"])
            for c in self.cookies
            if "chatgpt.com" in (c.get("domain") or "") or "openai.com" in (c.get("domain") or "")
        )
        print("warm_cookies: 补充 %s" % ", ".join(changed), flush=True)
        return True

    @staticmethod
    def _quota_from_headers(headers):
        """摘出限速相关响应头，仅作观测，不做调度。"""
        out = {}
        for k, v in headers.items():
            if "ratelimit" in k or k in ("retry-after", "x-request-id"):
                out[k] = v
        return out

    def _retry_after(self, default, body=None):
        """限速退避时长（用户裁定 2026-11-19）：按返回体/头里的真实时限，不搞固定秒数。

        取值顺序：① 响应体限速字段（resets_in / retry_after / resets_in_seconds /
        rate_limit.reset 等）→ ② Retry-After 头 → ③ 都没有才用 default 并注明。
        服务端声明值夹在 [5, 3600]；仅 default 兜底沿用 [5, 180] 旧口径。
        """
        raw = body if body is not None else getattr(self, "last_body", None)
        if raw:
            try:
                d = json.loads(raw)
            except Exception:
                d = None
            val = self._rate_seconds_from(d)
            if val is not None:
                return int(max(5, min(val, 3600)))
        raw = (self.last_headers or {}).get("retry-after")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            print("限速响应未提供时限，按默认 %ss 退避" % default, flush=True)
            return int(max(5, min(float(default), 180)))
        return int(max(5, min(val, 3600)))

    @staticmethod
    def _rate_seconds_from(d):
        """从响应体挖限速重置秒数；挖不到返回 None。绝对 epoch 也换算成秒。"""
        if not isinstance(d, dict):
            return None
        # 常见形态：{"detail":{"rate_limit":{"resets_in":N}}} / {"resets_in":N} / {"retry_after":N}
        cands = [d]
        for key in ("detail", "error", "rate_limit", "rateLimit"):
            v = d.get(key)
            if isinstance(v, dict):
                cands.append(v)
                for k2 in ("rate_limit", "rateLimit", "reset"):
                    v2 = v.get(k2)
                    if isinstance(v2, dict):
                        cands.append(v2)
        for c in cands:
            for key in ("resets_in", "resets_in_seconds", "retry_after", "retry_after_seconds"):
                v = c.get(key)
                if isinstance(v, (int, float)) and v >= 0:
                    return float(v)
            for key in ("reset", "resets_at", "retry_at"):
                v = c.get(key)
                if isinstance(v, (int, float)):
                    # >1e9 视为 epoch
                    if v > 1e9:
                        return max(0.0, v - time.time())
                    if v >= 0:
                        return float(v)
        return None

    @staticmethod
    def classify_failure(status, body):
        """失败定性（用户裁定 2026-11-19）：先分清代理/网络问题还是上游限速/风控。

        返回 'proxy' | 'ratelimit' | 'risk' | 'other'。
        - proxy：连接层失败（SSL EOF、重置、超时、status 0）→ 修代理，不计时死等。
        - ratelimit：429 或响应体带限速字段 → 按返回体时限退避。
        - risk：403 Unusual activity 等风控 → 补 cookie/PoW 流程。
        """
        if status == 0:
            return "proxy"
        b = body or ""
        if b.startswith("EXC"):
            return "proxy"
        if status == 429:
            return "ratelimit"
        if status == 403 and "Unusual activity" in b:
            return "risk"
        if status == 403 and b.lstrip().startswith("<html"):
            return "risk"  # Cloudflare 质询页 = 出口/指纹被拦，但归属风控处置链
        return "other"

    def repair_proxy(self):
        """代理问题跟进修复：重新探测出口并重建 opener。返回新出口 url（可能为空）。"""
        print("定性=代理/网络问题 —— 重新探测出口并重建连接（不盲等）", flush=True)
        try:
            info = resolve_proxy_info(refresh=True)
            url = info.get("url") or ""
        except SystemExit:
            url = ""
        self.proxy = url
        if url:
            self._opener = build_opener_for(url, CTX)
            print("代理已切换/恢复：%s（%s）" % (url, info.get("source")), flush=True)
        else:
            print("代理探测仍无可用出口，需人工跟进代理工具（--proxy-check 诊断）", flush=True)
        return url

    @staticmethod
    def _decompress(data, enc):
        """按 Content-Encoding 解压；不认识的编码原样返回，绝不让解压失败吞掉响应。"""
        enc = (enc or "").lower()
        if not data or not enc:
            return data
        try:
            if enc == "gzip":
                return gzip.decompress(data)
            if enc == "deflate":
                try:
                    return zlib.decompress(data)
                except zlib.error:
                    return zlib.decompress(data, -zlib.MAX_WBITS)
        except Exception:
            pass
        return data

    def req(self, method, url, body=None, extra_headers=None, timeout=120):
        if self.is_anon:
            if not self.cookie:
                self.ensure_device_cookie()
            url = url.replace("/backend-api/", "/backend-anon/")
        self.last_truncated = False
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        rq = urllib.request.Request(url, data=raw, method=method)
        rq.add_header("User-Agent", UA)
        rq.add_header("Accept", "application/json, text/event-stream")
        rq.add_header("Accept-Encoding", "gzip, deflate")
        if self.cookie:
            rq.add_header("Cookie", self.cookie)
        if self.token:
            rq.add_header("Authorization", "Bearer " + self.token)
        if self.is_anon:
            dev = self._device_id()
            rq.add_header("OAI-Device-Id", dev or str(uuid.uuid4()))
            rq.add_header("OAI-Language", "zh-CN")
        rq.add_header("Origin", "https://chatgpt.com")
        rq.add_header("Referer", "https://chatgpt.com/")
        if raw is not None:
            rq.add_header("Content-Type", "application/json")
        for k, v in (extra_headers or {}).items():
            rq.add_header(k, v)
        try:
            with self._opener.open(rq, timeout=timeout) as resp:
                data = resp.read()  # 读到 EOF，勿提前 close
                data = self._decompress(data, resp.headers.get("Content-Encoding"))
                self.last_headers = {k.lower(): v for k, v in resp.headers.items()}
                self.quota = self._quota_from_headers(self.last_headers)
                text = data.decode("utf-8", "replace")
                self.last_body = text
                self.last_status = resp.status
                return resp.status, text
        except urllib.error.HTTPError as e:
            rraw = e.read()
            rraw = self._decompress(rraw, (e.headers or {}).get("Content-Encoding"))
            self.last_headers = {k.lower(): v for k, v in (e.headers or {}).items()}
            self.quota = self._quota_from_headers(self.last_headers)
            text = rraw.decode("utf-8", "replace")[:300]
            self.last_body = text
            self.last_status = e.code
            return e.code, text
        except http.client.IncompleteRead as e:
            # 流被截断（实测：思考模式单条 SSE 可达 23MB，隧道中途断流）。
            # 关键：已收到的字节里通常已包含 conversation_id 与大部分回复，
            # 整包当失败会导致「换身份重投 → 同一 prompt 建出两个会话」。
            partial = getattr(e, "partial", b"") or b""
            self.last_truncated = True
            self.last_headers = {}
            self.quota = {}
            self.last_body = ""
            self.last_status = 200
            print("WARN: 响应流被截断（IncompleteRead，已收 %d 字节），按已收内容解析，稍后用 GET 补全文"
                  % len(partial), flush=True)
            return 200, partial.decode("utf-8", "replace")
        except Exception as e:
            self.last_headers = {}
            self.quota = {}
            self.last_body = ""
            self.last_status = 0
            return 0, "EXC " + str(e)

    # ---------- tail ----------
    @staticmethod
    def tail_from_mapping(raw_body):
        try:
            data = json.loads(raw_body)
        except Exception:
            return None
        mapping = data.get("mapping") or {}
        best_id, best_t = None, 0
        for nid, node in mapping.items():
            m = node.get("message")
            if not m:
                continue
            t = m.get("create_time") or 0
            if t > best_t:
                best_t, best_id = t, nid
        return best_id

    def cached_tail(self, conv_id):
        p = os.path.join(self.base_dir, "conv-api", id8(conv_id) + ".json")
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return self.tail_from_mapping(f.read())
        except Exception:
            return None

    def live_tail(self, conv_id, tries=2):
        for attempt in range(tries):
            st, body = self.req("GET", API_BASE + "/backend-api/conversation/" + conv_id, timeout=90)
            if st == 200:
                return self.tail_from_mapping(body), body
            kind = self.classify_failure(st, body)
            print("GET %s -> %s (try %d, 定性=%s)" % (id8(conv_id), st, attempt + 1, kind), flush=True)
            if attempt + 1 < tries:
                if kind == "proxy":
                    self.repair_proxy()   # 修代理，不盲等
                    time.sleep(3)
                elif kind == "ratelimit":
                    time.sleep(self._retry_after(90, body))  # 按返回体真实时限
                else:
                    time.sleep(self._retry_after(20, body))
        return None, None

    def list_conversations(self, page_size=28, max_pages=100):
        """Fetch visible conversation metadata for this authenticated identity."""
        assert 1 <= page_size <= 100 and 1 <= max_pages <= 1000
        from urllib.parse import urlencode

        conversations, seen = [], set()
        for page in range(max_pages):
            query = urlencode({"offset": page * page_size, "limit": page_size, "sort": "updated"})
            status, body = self.req("GET", API_BASE + "/backend-api/conversations?" + query, timeout=90)
            if status != 200:
                return conversations, status, body
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return conversations, status, "invalid JSON in conversation list"
            items = payload if isinstance(payload, list) else payload.get("items", payload.get("conversations", []))
            if not isinstance(items, list):
                return conversations, status, "unrecognized conversation list response"
            fresh = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                cid = item.get("id") or item.get("conversation_id")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                conversations.append({
                    "account": self.account,
                    "id": cid,
                    "title": item.get("title") or "",
                    "updated": item.get("update_time") or item.get("updated_at") or item.get("create_time"),
                })
                fresh += 1
            if not items or fresh == 0 or len(items) < page_size:
                return conversations, status, ""
        return conversations, 200, "page limit reached; result may be incomplete"

    # ---------- PoW ----------
    def requirements(self):
        st, body = self.req("POST", API_BASE + "/backend-api/sentinel/chat-requirements",
                            {"p": None, "generator": "chat_requirements_cache_key", "proofofwork": None})
        if st != 200:
            return None, None
        try:
            d = json.loads(body)
        except json.JSONDecodeError:
            print("WARN: chat-requirements 返回非 JSON：%r" % (body[:200],), flush=True)
            return None, None
        p = d.get("proofofwork") or {}
        return d.get("token"), solve(p.get("seed", ""), p.get("difficulty", "0"))

    # ---------- send ----------
    def send(self, conv_id, text, parent_message_id, req_token, proof, timeout=420, model=None):
        body = {
            "action": "next",
            "messages": [{"id": str(uuid.uuid4()), "author": {"role": "user"},
                          "content": {"content_type": "text", "parts": [text]},
                          "metadata": {}}],
            "parent_message_id": parent_message_id,
            "model": model or "auto",
            "timezone_offset_min": 480,
            "suggestions": [],
            "history_and_training_disabled": False,
            "conversation_mode_kind": "primary_assistant",
            "force_paragen": False,
            "force_rate_limit": False,
            "reset_rate_limits": False,
            "websocket_request_id": str(uuid.uuid4()),
        }
        if conv_id:  # 空 = 开新会话（Owner 模式）；非空必须带上，否则静默落新会话
            body["conversation_id"] = conv_id
        return self.req("POST", API_BASE + "/backend-api/conversation", body, {
            "OpenAI-Sentinel-Chat-Requirements-Token": req_token,
            "OpenAI-Sentinel-Proof-Token": proof,
        }, timeout=timeout)

    # ---------- 思考模式 ----------
    @staticmethod
    def resolve_thinking_model(api, cache={}):
        """开思考模式时该用哪个 model slug。

        实测（2026-11-19，/backend-api/models）：同一标题有两个 slug，
        带 `-t-` 的 max_tokens=262144，不带的是 34834 —— `-t-` 就是 thinking 变体，
        也正是浏览器里「思考」按钮切换的那个。默认会话用 `auto`，不思考。

        优先级：CHATGPT_MODEL 显式指定 > 默认模型的 `-t-` 同名变体 > 任意大窗口 `-t-` 模型。
        """
        key = api.account
        if key in cache:
            return cache[key]
        chosen = os.environ.get("CHATGPT_MODEL") or ""
        if not chosen:
            st, body = api.req("GET", API_BASE + "/backend-api/models")
            if st != 200:
                print("WARN: 取不到模型清单（st=%s），思考模式回退 auto" % st, flush=True)
                return cache.setdefault(key, "auto")
            try:
                d = json.loads(body)
            except Exception:
                return cache.setdefault(key, "auto")
            models = d.get("models") or []
            default = d.get("default_model_slug") or "auto"
            by_slug = {m.get("slug"): m for m in models}
            if "-t-" in default:
                chosen = default
            else:
                want_title = (by_slug.get(default) or {}).get("title")
                for m in models:
                    slug = m.get("slug") or ""
                    if "-t-" in slug and want_title and m.get("title") == want_title:
                        chosen = slug
                        break
                if not chosen:
                    for m in models:
                        slug = m.get("slug") or ""
                        if "-t-" in slug and (m.get("max_tokens") or 0) > 100000:
                            chosen = slug
                            break
        chosen = chosen or "auto"
        cache[key] = chosen
        print("thinking model: %s（默认 %s）" % (chosen, "auto"), flush=True)
        return chosen

    # ---------- harvest ----------
    @staticmethod
    def extract_reply_sse(raw_text):
        last = ""
        for line in raw_text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            msg = obj.get("message") or {}
            parts = (msg.get("content") or {}).get("parts") or []
            txt = "".join(p for p in parts if isinstance(p, str))
            if (msg.get("author") or {}).get("role") == "assistant" and txt.strip():
                last = txt
        return last

    @staticmethod
    def harvest_mapping(raw_body):
        try:
            data = json.loads(raw_body)
        except Exception:
            return ""
        msgs = []
        for node in (data.get("mapping") or {}).values():
            m = node.get("message")
            if not m:
                continue
            role = (m.get("author") or {}).get("role")
            text = "".join(p for p in ((m.get("content") or {}).get("parts") or []) if isinstance(p, str)).strip()
            if role in ("user", "assistant") and text:
                msgs.append((m.get("create_time") or 0, role, text))
        msgs.sort(key=lambda x: x[0])
        for _, role, text in reversed(msgs):
            if role == "assistant":
                return text
        return ""

    @staticmethod
    def harvest_transcript(raw_body):
        """按时间顺序返回整段对话 [(role, text), ...]，用于匿名会话留存。"""
        try:
            data = json.loads(raw_body)
        except Exception:
            return []
        rows = []
        for node in (data.get("mapping") or {}).values():
            m = node.get("message")
            if not m:
                continue
            role = (m.get("author") or {}).get("role")
            text = "".join(p for p in ((m.get("content") or {}).get("parts") or [])
                           if isinstance(p, str)).strip()
            if role in ("user", "assistant") and text:
                rows.append((m.get("create_time") or 0, role, text))
        rows.sort(key=lambda x: x[0])
        return [(role, text) for _, role, text in rows]

    def save_reply(self, conv_id, pfile, text):
        """落盘回复全文。默认不截断（旧版静默只留末尾 2 万字，等于丢结论）。

        需要控制体积时设 CHATGPT_REPLY_MAX_CHARS（>0 才生效，保留开头并在头部注明）。
        """
        if not text:
            return None
        short = id8(conv_id)
        body, note = text, ""
        try:
            limit = int(os.environ.get("CHATGPT_REPLY_MAX_CHARS") or 0)
        except ValueError:
            limit = 0  # 变量写错不该让整次收割白跑
        if limit > 0 and len(text) > limit:
            body = text[:limit]
            note = "\n> 已按 CHATGPT_REPLY_MAX_CHARS=%d 截断，原文 %d 字符\n" % (limit, len(text))
        rel = "replies/reply-%s-%s.md" % (short, datetime.now().strftime("%H%M%S"))
        path = os.path.join(self.base_dir, rel)
        seq = 1
        while os.path.exists(path):  # 同一秒重复收割，别互相覆盖
            rel = "replies/reply-%s-%s-%d.md" % (short, datetime.now().strftime("%H%M%S"), seq)
            path = os.path.join(self.base_dir, rel)
            seq += 1
        _atomic_write(path, "# reply for %s (prompt %s)\n%s\n%s\n" % (short, pfile, note, body))
        print("saved reply -> %s (%d chars)" % (rel, len(text)), flush=True)
        return path


class AnonBrowser:
    """匿名（无登录）会话的浏览器通道。

    匿名态建会话必须过 Cloudflare Turnstile：纯 API 的
    POST /backend-anon/conversation 恒返回 403 "Unusual activity"，
    而真实浏览器里 UI 可正常建会话（落点 https://chatgpt.com/uc/<id>）。
    因此：建/发走 UI，收割走 API（backend-anon GET 带页面 cookie 可用）。

    依赖：browser-automation-cdp 的 cdp.py，且浏览器须以调试端口启动：
        python start_chrome.py --temp --port 9333
    """

    COMPOSER = "#mobile-composer-prompt, #prompt-textarea"

    def __init__(self, port=None, cdp_dir=None):
        self.port = int(port or os.environ.get("CHATGPT_ANON_CDP_PORT") or 9333)
        self.cdp_dir = cdp_dir or os.environ.get("CHATGPT_CDP_DIR") or os.path.join(
            os.path.expanduser("~"), ".workbuddy", "skills", "browser-automation-cdp", "scripts")
        self._client = None
        self.last_cid = None   # 最近一次成功发送落到的会话 id（供异常时登记，避免孤儿会话）
        self.last_tab = None
        # 浏览器也必须走同一个出口：API 走代理、页面裸连等于把设备身份从直连暴露出去
        self.proxy = require_proxy()

    # ---------- 连接 ----------
    def _autostart(self):
        """CDP 未就绪时自动拉起临时 profile 的有头浏览器（CHATGPT_ANON_AUTOSTART=0 可关）。"""
        if os.environ.get("CHATGPT_ANON_AUTOSTART", "1") == "0":
            return False
        script = os.path.join(self.cdp_dir, "start_chrome.py")
        if not os.path.exists(script):
            return False
        # 每个端口独立 profile：`--temp` 的目录是固定的，多端口并存时第二实例会被
        # Edge/Chrome 的单实例机制吞掉（实测：CDP 永不就绪）。
        prof = os.path.join(tempfile.gettempdir(), "cdp-anon-%d" % self.port)
        cmd = [sys.executable, script, "--profile", prof, "--port", str(self.port),
               # A23：WebRTC 走 UDP，--proxy-server 管不住。开关名是
               # `--webrtc-ip-handling-policy`（实测 2026-11-19：Edge 157 认这个；
               # 带 `force-` 前缀的旧名字被忽略，仍会漏出公网 IPv4/IPv6）。
               "--chrome-arg=--webrtc-ip-handling-policy=disable_non_proxied_udp",
               "--chrome-arg=--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
        if self.proxy:
            cmd += ["--proxy", self.proxy]   # 与 API 同一出口，宁可起不来也不要裸连
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("autostart 浏览器失败：%s" % e, flush=True)
            return False
        end = time.time() + 30
        from cdp import CDPClient as _C
        while time.time() < end:
            time.sleep(2)
            try:
                if _C(port=self.port, timeout=5).is_alive():
                    return True
            except Exception:
                continue
        return False

    def _cdp(self):
        """导入 cdp.CDPClient（顺手把技能目录塞进 sys.path）。

        别在方法里直接 `from cdp import ...`：egress_ip 曾因此拿不到模块，
        报 "No module named 'cdp'" 而不是真正去连浏览器。
        """
        if self.cdp_dir not in sys.path:
            sys.path.insert(0, self.cdp_dir)
        try:
            from cdp import CDPClient
        except Exception as e:
            raise RuntimeError("CDP 客户端不可用（%s）：%s" % (self.cdp_dir, e))
        return CDPClient

    def client(self):
        if self._client is None:
            CDPClient = self._cdp()
            cl = CDPClient(port=self.port, timeout=90)
            if not cl.is_alive() and self._autostart():
                cl = CDPClient(port=self.port, timeout=90)
            if not cl.is_alive():
                raise RuntimeError(
                    "CDP 端口 %d 未就绪：先运行 python start_chrome.py --temp --port %d" % (self.port, self.port))
            try:
                cl.ensure_page(url_contains="chatgpt.com")
            except Exception:
                cl.ensure_page()  # 刚启动的实例只有 about:blank
                cl.navigate("https://chatgpt.com/", timeout=60)
            # 未显示的窗口会让页面 visibilityState=hidden，应用会挂起发送
            cl.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
            self._client = cl
        return self._client

    def _js(self, expr):
        r = self.client().evaluate(expr)
        return r.get("value") if isinstance(r, dict) else r

    # ---------- 设备身份（oai-did）----------
    def cookie_jar(self):
        """浏览器侧完整 cookie（含 HttpOnly）。

        document.cookie 读不到 HttpOnly，而匿名会话的设备身份与 CF 系列 cookie
        恰恰可能落在这些上面 —— 只存 JS 可见的那部分，换 profile 后就会 404。
        故优先走 CDP Storage.getCookies，失败才退化为 document.cookie。
        """
        try:
            if self.cdp_dir not in sys.path:
                sys.path.insert(0, self.cdp_dir)
            from cdp import CDPClient
            b = CDPClient(port=self.port, timeout=20)
            try:
                b.connect_browser()
                res = b.send("Storage.getCookies", {})
            finally:
                try:
                    b.close()
                except Exception:
                    pass
            jar = []
            for ck in (res or {}).get("cookies") or []:
                domain = (ck.get("domain") or "").lstrip(".").lower()
                if "chatgpt.com" not in domain and "openai.com" not in domain:
                    continue
                jar.append({k: ck[k] for k in
                            ("name", "value", "domain", "path", "expires", "httpOnly",
                             "secure", "sameSite") if ck.get(k) is not None})
            if jar:
                return jar
        except Exception:
            pass
        return [{"name": k, "value": v}
                for k, v in parse_cookie_header(self._js("document.cookie") or "").items()]

    def cookie(self):
        jar = self.cookie_jar()
        return cookie_header(jar) or (self._js("document.cookie") or "")

    def identity(self):
        """把当前浏览器的设备身份落盘，返回设备号。

        建会话流程一启动就调用：哪怕后面发送失败，设备号也已留存，
        不会出现「会话建出来了但没人知道它属于哪台设备」的死档。
        """
        return save_device_cookie(self.cookie_jar())

    def restore_identity(self, did):
        """把留存设备身份回灌到当前浏览器（换 profile 后找回同一个 oai-did）。

        只回灌 cookie，不负责找回标签页 —— 匿名会话的可写通道仍然是原标签页。
        """
        jar = load_device_jar(did)
        if not jar:
            return 0
        cl = self.client()
        cookies = []
        for ck in jar:
            item = {"name": ck.get("name"), "value": ck.get("value"),
                    "domain": ck.get("domain") or ".chatgpt.com",
                    "path": ck.get("path") or "/"}
            if ck.get("expires"):
                item["expires"] = ck["expires"]
            if ck.get("httpOnly"):
                item["httpOnly"] = True
            if ck.get("secure"):
                item["secure"] = True
            if ck.get("sameSite"):
                item["sameSite"] = ck["sameSite"]
            if item["name"]:
                cookies.append(item)
        if not cookies:
            return 0
        try:
            cl.send("Network.enable", {})
        except Exception:
            pass
        cl.send("Network.setCookies", {"cookies": cookies})
        return len(cookies)

    # ---------- 出口校验 ----------
    def egress_ip(self, timeout=25):
        """开一次性标签页问出口 IP：验证浏览器这一侧真的走了代理。

        不导航当前页：当前页可能是正在续聊的 /uc/<id>，导航它等于毁掉可写通道。
        """
        CDPClient = self._cdp()
        self.client()          # 没起就按统一出口自动拉起（--proxy 由 _autostart 带上）
        tid, res = "", None
        try:
            b = CDPClient(port=self.port, timeout=timeout)
            try:
                b.connect_browser()
                res = b.send("Target.createTarget",
                             {"url": "https://api.ipify.org/?format=text"})
            finally:
                try:
                    b.close()
                except Exception:
                    pass
            tid = (res or {}).get("targetId") or ""
        except Exception as e:
            raise RuntimeError("无法新建校验标签页：%s" % e)
        if not tid:
            raise RuntimeError("无法新建校验标签页：%r" % (res,))
        ip = ""
        try:
            cl = CDPClient(port=self.port, timeout=timeout)
            cl.connect_page(tid)
            end = time.time() + timeout
            while time.time() < end:
                time.sleep(1.5)
                got = cl.evaluate("document.body ? document.body.innerText.trim() : ''")
                txt = (got or {}).get("value") if isinstance(got, dict) else got
                if txt and _IP_RE.match(txt):
                    ip = txt
                    break
            try:
                cl.close()
            except Exception:
                pass
        finally:
            try:
                b2 = CDPClient(port=self.port, timeout=15)
                b2.connect_browser()
                b2.send("Target.closeTarget", {"targetId": tid})
                b2.close()
            except Exception:
                pass
        return ip

    def webrtc_probe(self, proxy_ip=None, timeout=25):
        """A23：WebRTC 走 UDP 直连，`--proxy-server` 管不住它 —— 唯一的隐蔽泄漏面。

        在一次性标签页里跑真实 ICE，返回
          status: clean（没拿到任何候选/IP）/ private_only / via_proxy /
                  leaked（拿到公网 IP 且不是代理出口）/ public_unknown / unverified
        `unverified` 必须当作「没验成」，不得报告全链路出口通过。
        """
        CDPClient = self._cdp()
        self.client()
        tid = ""
        try:
            b = CDPClient(port=self.port, timeout=timeout)
            try:
                b.connect_browser()
                res = b.send("Target.createTarget", {"url": "about:blank"})
            finally:
                try:
                    b.close()
                except Exception:
                    pass
            tid = (res or {}).get("targetId") or ""
        except Exception as e:
            return {"status": "unverified", "error": "无法新建探测标签页：%s" % e, "ips": []}
        if not tid:
            return {"status": "unverified", "error": "无 targetId：%r" % (res,), "ips": []}
        try:
            cl = CDPClient(port=self.port, timeout=timeout)
            cl.connect_page(tid)
            r = cl.send("Runtime.evaluate", {"expression": JS_WEBRTC,
                                             "awaitPromise": True,
                                             "returnByValue": True,
                                             "userGesture": True},
                        timeout=timeout + 10)
            val = ((r or {}).get("result") or {}).get("value") or ""
            data = json.loads(val) if val else {}
            if data.get("status") != "ok":
                return {"status": "unverified",
                        "error": data.get("error") or data.get("status") or "无返回值", "ips": []}
            ips = [str(x) for x in (data.get("ips") or [])]
        except Exception as e:
            return {"status": "unverified",
                    "error": "%s: %s" % (type(e).__name__, e), "ips": []}
        finally:
            try:
                b2 = CDPClient(port=self.port, timeout=15)
                b2.connect_browser()
                b2.send("Target.closeTarget", {"targetId": tid})
                b2.close()
            except Exception:
                pass
        return _classify_webrtc(ips, proxy_ip)

    # ---------- 页面动作 ----------
    def _wait(self, expr, timeout=30, interval=0.6):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self._js(expr):
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def _clear_composer(self):
        self._js("""(() => {
            const t = document.querySelector('%s');
            if (!t) return false;
            t.focus();
            const d = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
            if (d && t.tagName === 'TEXTAREA') {
                d.set.call(t, '');
                t.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward'}));
            } else {
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
            }
            return true;
        })()""" % self.COMPOSER)

    def _send_button(self):
        return self._js("""(() => {
            const bs = [...document.querySelectorAll('button')].filter(b =>
                /发送消息|Send message/i.test(b.getAttribute('aria-label') || '') ||
                b.getAttribute('data-testid') === 'send-button');
            for (const b of bs) {
                const r = b.getBoundingClientRect();
                if (r.width > 10 && r.height > 10 && b.getAttribute('aria-disabled') !== 'true') {
                    return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
                }
            }
            return null;
        })()""")

    def _idle(self):
        """停止生成按钮消失 + 文本长度连续 3 次不变。

        预算默认 900 秒，可用 CHATGPT_ANON_IDLE_SEC 覆盖（长回复可调大）。
        """
        stable, last = 0, -1
        try:
            budget = int(os.environ.get("CHATGPT_ANON_IDLE_SEC") or 900)
        except ValueError:
            budget = 900
        end = time.time() + max(budget, 30)
        while time.time() < end:
            state = self._js("""(() => ({
                generating: [...document.querySelectorAll('button')].some(b =>
                    /停止生成|Stop generating|stop streaming/i.test(b.getAttribute('aria-label') || '')),
                len: (document.body.innerText || '').length
            }))()""")
            if not state:
                return False
            if not state.get("generating") and state.get("len") == last:
                stable += 1
                if stable >= 3:
                    return True
            else:
                stable = 0
            last = state.get("len")
            time.sleep(3)
        return False

    def _conv_id(self):
        u = self.client().page_url() or ""
        for key in ("/uc/", "/c/"):
            if key in u:
                cid = u.split(key, 1)[1].split("?")[0].split("/")[0]
                if len(cid) >= 8:
                    return cid
        return None

    # ---------- 标签页定位（匿名会话只在原标签页内可写） ----------
    def probe_tabs(self):
        """只读探测标签页，不自动拉起浏览器（供 --anon-list 使用）。"""
        cl = None
        try:
            if self.cdp_dir not in sys.path:
                sys.path.insert(0, self.cdp_dir)
            from cdp import CDPClient
            cl = CDPClient(port=self.port, timeout=10)
            if not cl.is_alive():
                return []
            return [t for t in cl.targets() if t.get("type") == "page"]
        except Exception:
            return []
        finally:
            if cl is not None:
                try:
                    cl.close()
                except Exception:
                    pass


    def find_tab(self, conv_id):
        """按 URL（/uc/<id>）找仍然存活的匿名会话标签页。

        只探测不自动拉起浏览器：浏览器都没起，那条会话的标签页必然也没了，
        去 autostart 一个空浏览器只会白等 30 秒并让调用方误判。
        """
        for t in self.probe_tabs():
            if "/uc/" + conv_id in (t.get("url") or ""):
                return t
        return None

    def alive(self):
        """CDP 端口是否可连（不触发自动拉起）。"""
        try:
            if self.cdp_dir not in sys.path:
                sys.path.insert(0, self.cdp_dir)
            from cdp import CDPClient
            return CDPClient(port=self.port, timeout=8).is_alive()
        except Exception:
            return False

    def _attach(self, target_id):
        from cdp import CDPClient
        if self._client is not None:
            try:
                self._client.close()  # 换页时关掉旧 ws，避免长跑进程堆积连接
            except Exception:
                pass
        cl = CDPClient(port=self.port, timeout=90)
        cl.connect_page(target_id)
        cl.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        self._client = cl
        return cl

    def _new_tab(self, url="https://chatgpt.com/"):
        """另开一个标签页并附上去。

        必须另开而不是导航现有页：匿名会话只在**创建它的标签页**内可写，
        把已有 /uc/<id> 页导航到首页等于永久毁掉那条会话的可写通道。
        """
        from cdp import CDPClient
        b = CDPClient(port=self.port, timeout=90)
        try:
            b.connect_browser()
            res = b.send("Target.createTarget", {"url": url})
        finally:
            b.close()
        tid = (res or {}).get("targetId")
        if not tid:
            raise RuntimeError("新建标签页失败：%r" % (res,))
        time.sleep(1.0)
        return self._attach(tid)

    def send(self, text, conv_id=None):
        """匿名会话发消息：返回 (conv_id, reply_text, tab_target_id)。

        关键实测结论：匿名会话只在**创建它的那个标签页**内可写；
        重新导航/新标签页打开 /uc/<id> 会被重定向回首页。
        因此有 conv_id 时只复用存活标签页，不重新导航。
        """
        if conv_id:
            tab = self.find_tab(conv_id)
            if not tab:
                if not self.alive():
                    # 浏览器没起 ≠ 会话丢失：别顺手把对照表标成 archived
                    raise RuntimeError(
                        "CDP 端口 %d 未运行，无法确认匿名会话 %s 的标签页：先启动浏览器"
                        "（python scripts/start_chrome.py --temp --port %d）再试；"
                        "若浏览器确实重启过，该标签页已丢失，会话仍可用 `--anon-read %s` 读取。"
                        % (self.port, conv_id[:8], self.port, conv_id))
                mark_anon_archived(conv_id, "标签页已不存在（匿名会话无法重开，直接访问 /uc/ 会重定向）")
                raise RuntimeError(
                    "匿名会话 %s 的标签页已不存在，无法续聊：匿名会话只在创建它的标签页内可写，"
                    "重新打开 /uc/<id> 会被重定向到首页。可用 `--anon-read %s` 从 API 读取全文，"
                    "或改用登录态账号重建该会话。" % (conv_id[:8], conv_id))
            cl = self._attach(tab["id"])
        else:
            cl = self.client()
            cur = cl.page_url() or ""
            if "/uc/" in cur or "/c/" in cur:
                # 当前页正挂着一条匿名会话：导航走就永久失去它的可写标签页 → 另开新页
                print("anon: 当前页是既有匿名会话，另开标签页建新会话", flush=True)
                cl = self._new_tab("https://chatgpt.com/")
            else:
                cl.navigate("https://chatgpt.com/", timeout=60)
        if not self._wait("!!document.querySelector('%s')" % self.COMPOSER, timeout=45):
            raise RuntimeError("匿名页输入框未出现（可能是 Cloudflare 质询，重启浏览器实例一次）")
        self._clear_composer()
        cl.evaluate("(() => { const t = document.querySelector('%s'); t && t.focus(); return true; })()" % self.COMPOSER)
        cl.send("Input.insertText", {"text": text})
        btn = None
        for _ in range(30):
            btn = self._send_button()
            if btn:
                break
            time.sleep(0.6)
        if not btn:
            raise RuntimeError("发送按钮不可用（输入可能未进 React 状态）")
        for kind in ("mousePressed", "mouseReleased"):
            cl.send("Input.dispatchMouseEvent",
                    {"type": kind, "x": btn["x"], "y": btn["y"], "button": "left", "clickCount": 1})
        cid = None
        end = time.time() + 120
        while time.time() < end:
            cid = self._conv_id()
            if cid:
                break
            time.sleep(1.5)
        if not cid:
            raise RuntimeError("发送后未拿到匿名会话 ID（/uc/<id>）")
        if not _ID_RE.match(cid):
            raise RuntimeError("发送后拿到的会话 ID 不合规（%r），可能是页面异常" % (cid,))
        # 先记下来：万一后面等待/收割异常，上层还能把这条会话登记进对照表（避免孤儿会话）
        self.last_cid, self.last_tab = cid, (cl.target_id or "")
        if conv_id and cid != conv_id:
            raise RuntimeError("匿名会话续接失败：目标 %s 实际落入 %s（请改用登录态账号重建）"
                               % (conv_id[:8], cid[:8]))
        if not self._idle():
            print("WARN: %s 秒内未确认生成结束，按当前 DOM 内容收割（回复可能不完整，可用 --anon-read 补读）"
                  % os.environ.get("CHATGPT_ANON_IDLE_SEC", "900"), flush=True)
        reply = self._js("""(() => {
            const nodes = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
            const last = nodes[nodes.length - 1];
            return last ? (last.innerText || '').trim() : '';
        })()""") or ""
        return cid, reply, (cl.target_id or "")


def anon_archive_path(conv_id):
    """归档文件路径：默认 id8，若该前缀已被别的会话占用则加后缀，避免互相覆盖。

    文件名只由 id8() 产出的安全短 id 拼成，非法 conv_id 会退化为哈希，
    因此 `--anon-read ../../x` 之类不会在工作目录外写出文件。
    """
    short = id8(conv_id, "unknown")
    base = os.path.join(BASE, "anon-sessions", short)
    path = base + ".md"
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                head = f.readline()
        except OSError:
            head = ""
        if conv_id not in head:
            path = base + "-" + short[-4:] + ".md"
    return path


def _read_device_order(recorded, devices=None, limit=DEVICE_TRY_MAX):
    """读匿名会话时该按什么顺序试设备身份：先是登记的那台，再是其余留存快照（新→旧）。"""
    order = []
    if recorded and _ID_RE.match(recorded):
        order.append(recorded)
    for did in (list_device_ids(limit=limit * 2) if devices is None else devices):
        if did and _ID_RE.match(did) and did not in order:
            order.append(did)
        if len(order) >= limit:
            break
    return order


def read_anon(api, conv_id, save=True, devices=None):
    """按 id 读取匿名会话全文（API 侧仍可读，即便标签页已丢失）。

    设备身份对不上会 404 conversation_inaccessible。记录里的设备若已失效（换了
    profile、快照被清），依次试其他留存设备；命中就自动把归属修好，省得人工比对。
    """
    conv_id = safe_id(conv_id, "conv_id")
    info = anon_session(conv_id) or {}
    recorded = info.get("device") or ""
    order = _read_device_order(recorded, devices)
    st, body, used, tried = 0, "", "", []
    for did in order:
        snap = load_device_cookie(did)
        if not snap:
            continue
        api.cookie = snap  # 用会话所属设备的身份读；浏览器换 profile 后新 cookie 会 404
        tried.append(did)
        st, body = api.req("GET", API_BASE + "/backend-api/conversation/" + conv_id, timeout=90)
        if st == 200:
            used = did
            break
        if st in (0, 429) or st >= 500:
            break  # 通道问题：换设备也一样，别把余下快照全试一遍
        print("anon-read: 设备 %s 读不到（HTTP %s），换下一个留存设备"
              % (did[:8], st), flush=True)
    if st != 200 and not tried:
        # 一份快照都没有（首次在新机器上读旧会话）：退回当前浏览器/最新快照再试
        try:
            api.cookie = ""
            api.ensure_device_cookie()
        except Exception:
            pass
        st, body = api.req("GET", API_BASE + "/backend-api/conversation/" + conv_id, timeout=90)
        used = api._device_id()
    if st != 200:
        return st, "", []
    if used and used != recorded:
        print("anon-read: 设备归属已修正 %s -> %s（原记录缺失或设备已换）"
              % ((recorded or "-")[:8], used[:8]), flush=True)
    transcript = api.harvest_transcript(body)
    last = ""
    for role, text in reversed(transcript):
        if role == "assistant":
            last = text
            break
    if save and transcript:
        # 全文留存在本地：服务端不给历史，这是唯一归档手段。
        # 先写 tmp 再 replace —— 崩在半路也不能留个截断的归档（那是唯一副本）。
        path = anon_archive_path(conv_id)
        chunks = ["# 匿名会话 %s（%s）\n\n" % (conv_id, _now())]
        for role, text in transcript:
            chunks.append("## %s\n\n%s\n\n" % (role, text))
        _atomic_write(path, "".join(chunks))
        record_anon_session(conv_id, reply=last, device=used or api._device_id())
    return st, last, transcript


def probe_anon(api, conv_id, tries=2):
    """探测匿名会话是否还可访问：区分「会话真失效」与「限速/网络抖动」。

    只有 404（conversation_inaccessible）和 403 才说明会话不可用；
    429/0/5xx 是通道问题，重试后再报「暂时读不到」，绝不误判成会话丢失。
    """
    st = 0
    for i in range(tries):
        st, _ = api.req("GET", API_BASE + "/backend-api/conversation/" + conv_id, timeout=60)
        if st == 200:
            return 200, None
        if st in (403, 404):
            return st, "会话不可访问"
        if st in (0, 429, 500, 502, 503, 504) and i + 1 < tries:
            time.sleep(5)
            continue
        break
    return st, "通道异常"


def send_anon(api, conv_id, prompt_file, text):
    """匿名态投递：浏览器发送 + API 收割（backend-anon GET 已验证可用）。"""
    if conv_id:
        conv_id = safe_id(conv_id, "conv_id")
    br = api.anon or AnonBrowser()
    api.anon = br
    # 先留存浏览器当前的设备身份：后面无论发送成功还是中途炸掉，
    # 这条会话的 oai-did 都已经在本地，不会变成「建出来了但读不回来」的死档。
    did = ""
    try:
        did = br.identity()
    except Exception as e:
        print("WARN: 设备身份留存失败（%s），继续投递" % e, flush=True)
    if conv_id:
        # 先确认会话仍可访问：匿名会话绑定设备身份，失效后无法续接（404 conversation_inaccessible）
        snap = load_device_cookie((anon_session(conv_id) or {}).get("device") or "")
        if snap:
            api.cookie = snap
        probe, kind = probe_anon(api, conv_id)
        if probe != 200:
            if kind == "会话不可访问":
                raise RuntimeError(
                    "匿名会话 %s 不可访问（HTTP %s）：匿名会话与设备身份绑定、不入历史，"
                    "换设备或清 profile 后即失效；可用 `--anon-read %s` 按留存设备快照重试，"
                    "或改用登录态账号重建该会话" % (conv_id[:8], probe, conv_id))
            raise RuntimeError(
                "匿名会话 %s 暂时读不到（HTTP %s：限速或网络抖动），**不代表会话已丢失**；"
                "等几分钟重试，或用 --anon-read 直接读取" % (conv_id[:8], probe))
    try:
        cid, dom_reply, tab = br.send(text, conv_id)
    except Exception:
        # 发送流程中途异常时会话可能已经建成：登记下来，别让它变成查不到的孤儿会话。
        # 设备号必须取浏览器侧的（api.cookie 此时可能为空，取它等于把归属丢了）。
        if getattr(br, "last_cid", None):
            try:
                did = save_device_cookie(br.cookie_jar()) or did
            except Exception:
                pass
            record_anon_session(br.last_cid, tab=getattr(br, "last_tab", None),
                                prompt=prompt_file, device=did or api._device_id())
            print("WARN: 匿名会话 %s 已建立但发送流程异常，已登记到对照表（status=live 需确认，device=%s）"
                  % (br.last_cid[:8], (did or "-")[:8]), flush=True)
        raise
    try:
        jar = br.cookie_jar()
        api.cookie = cookie_header(jar) or api.cookie
        did = save_device_cookie(jar) or did
    except Exception:
        pass
    if not api.cookie:
        api.cookie = load_device_cookie(did)
    reply = ""
    st, body = api.req("GET", API_BASE + "/backend-api/conversation/" + cid, timeout=90)
    if st == 200:
        reply = api.harvest_mapping(body)
    if not reply:
        reply = dom_reply
    path = api.save_reply(cid, os.path.basename(prompt_file), reply)
    record_anon_session(cid, tab=tab, prompt=prompt_file, reply=reply,
                        device=did or api._device_id())
    print("ANON_SEND %s -> %s replyLen=%d tab=%s device=%s"
          % (cid[:8], st, len(reply or ""), (tab or "-")[:8], (did or "-")[:8]), flush=True)
    return (200 if reply else st or 0), path, cid


def send_prompt(api, conv_id, prompt_file, new_chat=False, thinking=None):
    """完整投递流程：tail → requirements+PoW → POST → 收割 → 落盘。

    返回 (status, reply_path, real_conv_id)；real_conv_id 用于登记会话归属。
    thinking=True 时用思考模型（`-t-` 变体）；None = 读 CHATGPT_THINKING 环境变量。
    """
    if conv_id:
        conv_id = safe_id(conv_id, "conv_id")
    try:
        with open(prompt_file, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise RuntimeError("读不到提示词文件 %s：%s" % (prompt_file, e))
    if not text.strip():
        raise RuntimeError("提示词文件为空：%s" % prompt_file)
    lines = [l for l in text.splitlines() if not l.strip().startswith("#")]
    text = "\n".join(lines).strip()
    if not text:
        raise RuntimeError("提示词没有正文（只有注释行）：%s" % prompt_file)
    pfile = os.path.basename(prompt_file)

    if api.is_anon:
        # 匿名态 POST 恒 403（Turnstile），必须走浏览器通道
        return send_anon(api, conv_id, prompt_file, text)

    if new_chat:
        tail = None  # 新会话无 parent
    else:
        tail, raw = api.live_tail(conv_id, tries=2)
        if not tail:
            tail = api.cached_tail(conv_id)
            print("using cached tail:", tail, flush=True)
        if not tail:
            print("FAIL: no parent_message_id (GET 被限速且无缓存 tail)", flush=True)
            return 0, None, None

    if thinking is None:
        thinking = os.environ.get("CHATGPT_THINKING", "") not in ("", "0", "false", "False")
    model = None
    if thinking:
        model = api.resolve_thinking_model(api)
        # A27 硬要求：显式要思考就必须真开了思考。解析不出 `-t-` 一律拒绝投递（exit 1），
        # 不允许静默回落 auto —— 否则「审查会话」会以不思考的身份跑完还被当正常。
        if not model or "-t-" not in str(model):
            if os.environ.get("CHATGPT_ALLOW_NON_THINKING", "") in ("1", "true", "True"):
                print("WARN: 思考模型解析失败（%r），按 CHATGPT_ALLOW_NON_THINKING=1 降级为 auto"
                      % (model,), flush=True)
                model = None
            else:
                print("FAIL: 要求思考模式但解析不出 -t- 模型（model=%r）。"
                      "先确认 /backend-api/models 可达；确实要降级请显式设 "
                      "CHATGPT_ALLOW_NON_THINKING=1" % (model,), flush=True)
                raise SystemExit(1)

    st, resp = 0, ""
    for attempt in (1, 2):
        req_token, proof = api.requirements()
        if not req_token:
            if api.account == "nologin":
                # 匿名态拿不到 requirements（服务端 401），不做 60s 重试，直接让轮转换身份
                print("FAIL: nologin 无法获取 requirements（服务端要求登录）", flush=True)
                return 401, None, None
            # 失败定性（用户裁定 2026-11-19）：代理问题修代理；上游限速按返回体时限退避
            kind = api.classify_failure(getattr(api, "last_status", 0),
                                        getattr(api, "last_body", ""))
            print("requirements 失败，定性=%s" % kind, flush=True)
            if kind == "proxy":
                api.repair_proxy()
                time.sleep(3)
            else:
                time.sleep(api._retry_after(60, getattr(api, "last_body", "")))
            req_token, proof = api.requirements()
            if not req_token:
                print("FAIL: no requirements token", flush=True)
                return 0, None, None

        t0 = time.time()
        # A16：思考模式单条实测可达 19 分钟，7 分钟会误杀；分档超时按是否思考给
        send_timeout = 1800 if (thinking and model) else 420
        st, resp = api.send(conv_id, text, tail, req_token, proof, model=model,
                            timeout=send_timeout)
        print("SEND %s -> %s respLen=%d in %.0fs" % (pfile, st, len(resp), time.time() - t0), flush=True)
        if st == 403 and "Unusual activity" in (resp or "") and attempt == 1:
            print("403 Unusual activity —— 补 Cloudflare cookie 并重跑 requirements + PoW 重试一次", flush=True)
            api.warm_cookies()
            time.sleep(3)
            continue
        break

    if st == 413 or "conversation_too_large" in resp:
        print("413 conversation_too_large —— 用 continue_full.py 走「开始新聊天」摘要续接", flush=True)
        return 413, None, None
    if st != 200:
        print("resp: %s" % resp[:200], flush=True)
        return st, None, None

    # 校验 conversation_id：防静默落新会话事故
    # 三级兜底：SSE 逐行解析 → 整包 JSON → 精确正则（流被截断时仍能捞出 id）
    parsed = None
    for line in resp.splitlines():
        if line.startswith("data:"):
            try:
                obj = json.loads(line[5:].strip())
                cid = obj.get("conversation_id")
                if cid:
                    parsed = cid
                    break
            except Exception:
                pass
    if not parsed:
        try:
            parsed = json.loads(resp).get("conversation_id")
        except Exception:
            pass
    if not parsed:
        m = re.search(r'"conversation_id"\s*:\s*"([0-9a-fA-F-]{36})"', resp or "")
        if m:
            parsed = m.group(1)
            print("conversation_id 由正则兜底解析得到：%s" % parsed, flush=True)
    # A11 硬要求：解析不出唯一且合规的 id 就判失败，禁止「WARN 一下接着走」或换身份重投。
    # 旧版在 id 不合规时回落到传入的 conv_id 继续落盘，等于把「没认回会话」粉饰成成功。
    if not parsed:
        print("FAIL: 200 但解析不出 conversation_id（无法证明落点会话）；按 A11 判失败，不重投",
              flush=True)
        return 1, None, None
    if not _ID_RE.match(parsed):
        print("FAIL: 解析出的 conversation_id 不合规（%r）；按 A11 判失败，不重投、不落盘"
              % (parsed,), flush=True)
        return 1, None, None
    real_conv = parsed
    if conv_id and real_conv != conv_id:
        print("WARN: 落入了不同会话! target=%s real=%s" % (conv_id, real_conv), flush=True)

    reply = api.extract_reply_sse(resp) or api.harvest_mapping(resp)
    if getattr(api, "last_truncated", False) and (conv_id or real_conv):
        # 流被截断：SSE 里的回复可能只到一半。用 GET 拉 mapping 取全文，
        # 避免把半截内容当定稿落盘（归档是唯一副本）。
        full = ""
        gst, gbody = api.req("GET", API_BASE + "/backend-api/conversation/" + (conv_id or real_conv),
                             timeout=120)
        if gst == 200:
            full = api.harvest_mapping(gbody)
            digest = reply_digest(gbody)
            if digest:
                # A12：按 part 统计对账，不只看「长度变长了」
                print("GET 全文核对：message=%s parts=%s chars=%d sha256=%s"
                      % ((digest["message_id"] or "?")[:12], digest["parts"],
                         digest["chars"], digest["sha256"][:12]), flush=True)
                if digest["chars"] != len(full):
                    print("WARN: parts 合计 %d 字符 ≠ 拼接 %d 字符（可能缺 part），按不完整处理"
                          % (digest["chars"], len(full)), flush=True)
        if len(full) > len(reply or ""):
            print("补全文：SSE %d 字符 → GET %d 字符" % (len(reply or ""), len(full)), flush=True)
            reply = full
        else:
            print("WARN: 流被截断且 GET 未取到更完整内容（GET st=%s，现有 %d 字符）"
                  % (gst, len(reply or "")), flush=True)
    path = api.save_reply(conv_id or real_conv, pfile, reply)
    return st, path, (conv_id or real_conv)


def _attempt_create(st, name, prompt_file, attempts, thinking=None):
    """用指定身份建会话并记录结果；返回 (status, conv_id)。"""
    try:
        api = ChatGPTAPI(account=name)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        def init_fail(s):
            mark_unavailable(s, name, "credentials unavailable: %s" % e)
            s["failures"][name] = s.get("failures", {}).get(name, 0) + 1

        st.update(mutate_rotation(init_fail))
        attempts.append({"account": name, "status": "INIT_FAIL", "detail": str(e)})
        print("rotate: %s 凭据不可用，跳过（%s）" % (name, e), flush=True)
        return "INIT_FAIL", None
    try:
        status, _path, conv = send_prompt(api, None, prompt_file, new_chat=True, thinking=thinking)
    except Exception as e:
        status, conv = 0, None
        print("rotate: %s 通道异常（%s）" % (name, e), flush=True)
    attempts.append({"account": name, "status": status, "conv_id": conv})
    if status == 200:
        # conv 可能为 None（POST 成功但 SSE 未解析出 id）；不能当失败去试下一个身份，
        # 否则会重复建会话、把一条 prompt 花两次配额。
        return status, conv
    reason, ttl = None, None
    if status == 401:
        reason = "HTTP 401（认证失效，需重新提取凭据）"
    elif status == 403:
        # PoW/风控或隐性限速，可自愈：短冷却，勿长期封禁
        reason, ttl = "HTTP 403（PoW/风控，冷却后重试）", COOLDOWN_403
    elif status == 429:
        reason, ttl = "HTTP 429 限速冷却", COOLDOWN_429
    elif status == 0 and name == "nologin":
        reason, ttl = "匿名浏览器通道不可用（CDP/Turnstile）", 1800

    def failed(s):
        s["failures"][name] = s.get("failures", {}).get(name, 0) + 1
        if reason:
            mark_unavailable(s, name, reason, ttl)

    st.update(mutate_rotation(failed))
    print("rotate: %s 建会话失败(st=%s)，切下一身份" % (name, status), flush=True)
    return status, None


def _mark_success(st, name, conv, prompt_file, attempts):
    def mutate(s):
        s["created"][name] = s.get("created", {}).get(name, 0) + 1
        if name in ROTATION_PRIMARY:
            s["cursor"] = (ROTATION_PRIMARY.index(name) + 1) % len(ROTATION_PRIMARY)
        else:
            # 兜底不推进游标：登录态恢复后仍按原均分顺序轮转，否则会白跳过一个账号的轮次
            s["fallback_uses"] = s.get("fallback_uses", 0) + 1
        s["unavailable"].pop(name, None)  # 成功即解封（可能之前是 403/429 冷却）
        s["last"] = {"account": name, "conv_id": conv, "prompt": prompt_file, "ts": _now()}

    st.update(mutate_rotation(mutate))  # 锁内重读再改，避免并发丢计数
    record_conv(conv, name, prompt_file)
    print("NEW_CONV %s account=%s" % (conv, name), flush=True)
    print(json.dumps({"result": "ok", "conv_id": conv, "account": name,
                      "attempts": attempts}, ensure_ascii=False), flush=True)


def create_chat_rotating(prompt_file, thinking=None):
    """登录态优先建会话：acct1/acct2 均分轮转；仅当两者都 429 才用匿名兜底。

    匿名会话不进任何账号历史（会话列表恒为空），只按 conv_id 短期可读，
    因此只作为限速时的应急产能，不承担需要留存/续接的任务。
    """
    st = load_rotation()
    if purge_expired(st):
        # 锁内重读再清：解封动作不能把并发进程刚写进去的计数/游标覆盖回旧值
        st = mutate_rotation(purge_expired)
    outcomes, attempts, skipped = {}, [], {}
    for name in ROTATION_PRIMARY:
        if is_unavailable(st, name):
            reason = (st["unavailable"].get(name) or {}).get("reason", "unavailable")
            skipped[name] = reason
            outcomes[name] = "SKIP:" + reason
    plan = [a for a in rotation_plan(st) if a not in skipped]
    print("rotation plan: %s (cursor=%s)%s" % (
        " -> ".join(plan) or "(无可用登录态)", st["cursor"],
        "；跳过 " + json.dumps(skipped, ensure_ascii=False) if skipped else ""), flush=True)

    for name in plan:
        status, conv = _attempt_create(st, name, prompt_file, attempts, thinking)
        outcomes[name] = status
        if conv:
            _mark_success(st, name, conv, prompt_file, attempts)
            return 0
        if status == 200:
            print("WARN: %s 建会话返回 200 但未解析出 conv_id（消息可能已送达），"
                  "为免重复建会话停止轮转" % name, flush=True)
            print(json.dumps({"result": "ok_no_conv_id", "account": name,
                              "attempts": attempts}, ensure_ascii=False), flush=True)
            return 0

    # ---- 兜底：两个登录态都 429（本次命中或 429 冷却中）才允许用匿名 ----
    block = anon_fallback_block_reason(st, outcomes)
    if block is None:
        print("两个登录态均 429 —— 启用匿名兜底（不入历史，仅短期可读，勿用于长任务）", flush=True)
        status, conv = _attempt_create(st, "nologin", prompt_file, attempts, thinking)
        if conv:
            _mark_success(st, "nologin", conv, prompt_file, attempts)
            print("WARN: 该会话为匿名会话，不出现在任何账号的会话列表，勿用于需留存的长任务", flush=True)
            return 0
        if status == 200:
            print("WARN: 匿名会话已建立但未解析出 conv_id，停止重试以免重复建会话", flush=True)
            print(json.dumps({"result": "ok_no_conv_id", "account": "nologin",
                              "attempts": attempts}, ensure_ascii=False), flush=True)
            return 0
        outcomes["nologin"] = status
    else:
        print("匿名兜底未放行：%s" % block, flush=True)

    print(json.dumps({"result": "failed", "outcomes": outcomes, "attempts": attempts}, ensure_ascii=False), flush=True)
    return 1


def rotation_summary():
    st = load_rotation()
    created = st.get("created", {})
    prim = {a: created.get(a, 0) for a in ROTATION_PRIMARY}
    prim_total = sum(prim.values()) or 1
    return {
        "policy": "acct1/acct2 均分轮转；nologin 仅在两者都 429 时兜底（其会话不入历史）",
        "primary": list(ROTATION_PRIMARY),
        "fallback": list(ROTATION_FALLBACK),
        "cursor": st["cursor"],
        "next": ROTATION_PRIMARY[st["cursor"]],
        "created": created,
        # share 只在登录态之间算，兜底的匿名次数单列，避免把 1/3 当成「没均分好」
        "share": {k: round(v / float(prim_total), 2) for k, v in prim.items()},
        "fallback_uses": st.get("fallback_uses", 0),
        "failures": st.get("failures", {}),
        "unavailable": st.get("unavailable", {}),
        "unavailable_ttl_sec": UNAVAILABLE_TTL,
        "last": st.get("last"),
        "updated": st.get("updated"),
    }


def main():
    args = sys.argv[1:]
    thinking_flag = "--thinking" in args
    if thinking_flag:
        args = [a for a in args if a != "--thinking"]
    account, explicit_account = "default", False
    if args and args[0] in ("--help", "-h"):
        print(__doc__)
        return 0
    # --account 先摘出来：否则 `--account acct1 --owner/--anon-xxx` 会被当成位置参数
    if len(args) >= 2 and args[0] == "--account":
        account, explicit_account = args[1], True
        args = args[2:]
        if not args:
            print(__doc__)
            return 1
    # ---- 交付正文托管（纯本地，不触网；A1/A2/A4）----
    if args and args[0] == "--extract":
        rest, begin, end, fence, out, positional = args[1:], None, None, None, None, []
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--between":
                if i + 2 >= len(rest):
                    print(json.dumps({"result": "error", "error": "--between 需要两个标记行"},
                                     ensure_ascii=False))
                    return 2
                begin, end = rest[i + 1], rest[i + 2]
                i += 3
            elif a == "--fence":
                if i + 1 >= len(rest):
                    print(json.dumps({"result": "error", "error": "--fence 需要一个序号"},
                                     ensure_ascii=False))
                    return 2
                fence = rest[i + 1]
                i += 2
            elif a == "--out":
                if i + 1 >= len(rest):
                    print(json.dumps({"result": "error", "error": "--out 需要一个路径"},
                                     ensure_ascii=False))
                    return 2
                out = rest[i + 1]
                i += 2
            else:
                positional.append(a)
                i += 1
        if len(positional) != 1:
            print(json.dumps({"result": "error",
                              "error": "--extract 需要且只需要一个源文件（收到 %d 个）" % len(positional)},
                             ensure_ascii=False))
            return 2
        try:
            print(json.dumps(extract_deliverable(positional[0], begin=begin, end=end,
                                                 fence=fence, out=out),
                             ensure_ascii=False, indent=2))
            return 0
        except CustodyError as e:
            print(json.dumps({"result": "error", "error": str(e)}, ensure_ascii=False))
            return 2
        except OSError as e:
            print(json.dumps({"result": "error", "error": "io: %s" % e}, ensure_ascii=False))
            return 2
    if args and args[0] == "--verify-copy":
        if len(args) != 3:
            print(json.dumps({"result": "error", "error": "--verify-copy 需要两个文件路径"},
                             ensure_ascii=False))
            return 2
        try:
            res = verify_copy(args[1], args[2])
        except CustodyError as e:
            print(json.dumps({"result": "error", "error": str(e)}, ensure_ascii=False))
            return 2
        print(json.dumps(res, ensure_ascii=False, indent=2))
        # 不一致即禁止回灌（A4）：退 2 而不是 1，这是「参数/前置不成立」不是业务失败
        return 0 if res["equal"] else 2
    # ---- 调度口径校验（纯本地；B1/B10/B11）----
    if args and args[0] == "--check-name":
        if len(args) < 2:
            print(json.dumps({"result": "error", "error": "--check-name 需要 automation 名称"},
                             ensure_ascii=False))
            return 2
        name, rest = args[1], args[2:]
        argv = None
        if "--argv" in rest:
            i = rest.index("--argv")
            if i + 1 >= len(rest):
                print(json.dumps({"result": "error", "error": "--argv 需要命令串"},
                                 ensure_ascii=False))
                return 2
            argv = shlex.split(rest[i + 1])
        try:
            print(json.dumps(check_automation_name(name, argv), ensure_ascii=False, indent=2))
            return 0
        except ScheduleError as e:
            print(json.dumps({"result": "error", "error": str(e)}, ensure_ascii=False))
            return 2
    if args and args[0] == "--check-schedule":
        if len(args) < 2:
            print(json.dumps({"result": "error", "error": "--check-schedule 需要 rrule"},
                             ensure_ascii=False))
            return 2
        try:
            print(json.dumps(check_schedule(args[1]), ensure_ascii=False, indent=2))
            return 0
        except ScheduleError as e:
            print(json.dumps({"result": "error", "error": str(e)}, ensure_ascii=False))
            return 2
    if args and args[0] == "--check-oneshot":
        if len(args) < 2:
            print(json.dumps({"result": "error", "error": "--check-oneshot 需要 scheduledAt"},
                             ensure_ascii=False))
            return 2
        try:
            print(json.dumps(check_oneshot(args[1]), ensure_ascii=False, indent=2))
            return 0
        except ScheduleError as e:
            print(json.dumps({"result": "error", "error": str(e)}, ensure_ascii=False))
            return 2
    # ---- 出口代理（API / 浏览器 / 自启动共用同一个出口）----

    if args and args[0] == "--proxy-url":
        # 给 shell 用：只输出 URL（解析不到就空行 + exit 1），便于 git 等外部动作对齐出口
        url = resolve_proxy()
        print(url)
        return 0 if url else 1
    if args and args[0] in ("--proxy", "--proxy-show"):
        info = resolve_proxy_info(refresh=True)
        print(json.dumps({"proxy": info["url"] or None,
                          "source": info["source"] or None,
                          "allow_direct": allow_direct(),
                          "bypass": list(PROXY_BYPASS_HOSTS),
                          "config_file": PROXY_FILE,
                          "candidates": list(PROXY_AUTODETECT_PORTS)},
                         ensure_ascii=False, indent=2))
        return 0 if info["url"] else 1
    if len(args) == 2 and args[0] == "--proxy-set":
        try:
            url = normalize_proxy(args[1])
        except ValueError as e:
            print(json.dumps({"result": "error", "detail": str(e)}, ensure_ascii=False))
            return 2
        save_proxy_config(url)
        print(json.dumps({"result": "ok", "proxy": url, "config_file": PROXY_FILE,
                          "next": "用 --proxy-check 验证出口；浏览器需重启才会换代理"},
                         ensure_ascii=False))
        return 0
    if args and args[0] == "--proxy-clear":
        save_proxy_config("")
        print(json.dumps({"result": "ok", "proxy": None, "config_file": PROXY_FILE},
                         ensure_ascii=False))
        return 0
    if args and args[0] == "--proxy-check":
        info = resolve_proxy_info(refresh=True)
        res = check_egress(info["url"])
        res["source"] = info["source"] or None
        # A26：量到 IP 不等于能用；必须再验一次目标站
        res["site"] = probe_site(info["url"])
        if "--browser" in args[1:] and info["url"]:
            # 浏览器侧出口：单独开一次性标签页量，避免污染正在续聊的 /uc/<id>
            br = AnonBrowser()
            try:
                res["browser_ip"] = br.egress_ip()
            except Exception as e:
                res["browser_ip"] = None
                res["browser_error"] = str(e)
            # A23：WebRTC 走 UDP，--proxy-server 管不住；必须单独验
            try:
                res["webrtc"] = br.webrtc_probe(proxy_ip=res.get("proxy_ip"))
            except Exception as e:
                res["webrtc"] = {"status": "unverified", "error": str(e), "ips": []}
            if res.get("browser_ip") and res.get("proxy_ip"):
                res["browser_match"] = res["browser_ip"] == res["proxy_ip"]
        site = res.get("site") or {}
        ok = bool(res.get("proxy_ip")) and not res.get("same") and bool(site.get("ok"))
        if not site.get("ok"):
            msg = ("目标站不可用（%s）：%s"
                   % (site.get("verdict"), site.get("note") or ""))[:240]
            prev = res.get("verdict") or ""
            res["verdict"] = (prev + "；" + msg) if prev and prev != "代理生效" else msg
        reasons = []
        if res.get("verdict") and res["verdict"] != "代理生效":
            reasons.append(res["verdict"])
        if "--browser" in args[1:] and res.get("browser_ip") is None:
            ok = False      # 要求验浏览器却没验成，不能算通过
            reasons.append("要求验浏览器但未取到 browser_ip")
        if res.get("browser_match") is False:
            ok = False
            reasons.append("浏览器出口与 API 不一致：浏览器没走代理，重启浏览器并带 --proxy")
        wt = (res.get("webrtc") or {}).get("status")
        if wt == "leaked":
            ok = False
            reasons.append((res["webrtc"].get("error") or "WebRTC 泄漏"))
        elif wt in ("unverified", "public_unknown"):
            ok = False
            reasons.append("WebRTC 出口未验证（%s）：不能声称全链路走代理" % wt)
        res["full_chain_verified"] = bool(ok)
        if reasons:
            res["verdict"] = "；".join(reasons)[:400]
        print(json.dumps(res, ensure_ascii=False, indent=2))
        # 出口不满足 → 3（未满足联网条件），不是业务失败（A24 语义）
        return 0 if ok else 3
    # ---- 轮转管理（纯本地状态，不触网）----
    if args and args[0] in ("--rotation", "--rotation-status"):
        print(json.dumps(rotation_summary(), ensure_ascii=False, indent=2))
        return 0
    if len(args) == 2 and args[0] in ("--rotation-enable", "--rotation-disable"):
        name = args[1]
        if name not in ROTATION_ORDER:
            print("usage: %s acct1|acct2|nologin" % args[0], flush=True)
            return 2
        enable = args[0] == "--rotation-enable"

        def mutate(s):
            if enable:
                s["unavailable"].pop(name, None)
                s["failures"][name] = 0
            else:
                mark_unavailable(s, name, "manually disabled")

        mutate_rotation(mutate)
        print(json.dumps(rotation_summary(), ensure_ascii=False, indent=2))
        return 0
    if args and args[0] == "--rotation-reset":
        def reset(s):
            s["cursor"] = 0
            s["fallback_uses"] = 0
            s["created"] = {a: 0 for a in s["order"]}
            s["failures"] = {a: 0 for a in s["order"]}
            s.pop("last", None)

        mutate_rotation(reset)
        print(json.dumps(rotation_summary(), ensure_ascii=False, indent=2))
        return 0
    if len(args) == 2 and args[0] == "--owner":
        cid = cli_id(args[1])
        if cid is None:
            return 2
        owner = conv_owner(cid)
        print(json.dumps({"conv_id": cid, "account": owner, "known": bool(owner)}, ensure_ascii=False))
        return 0 if owner else 1
    # ---- 匿名会话 id 对照表 ----
    if args and args[0] == "--anon-list":
        data = load_anon_sessions()
        live_ids = set()
        try:
            live_ids = {t.get("id") for t in AnonBrowser().probe_tabs()}
        except Exception:
            pass
        rows = []
        for cid, s in sorted((data.get("sessions") or {}).items(),
                             key=lambda kv: kv[1].get("updated") or "", reverse=True):
            if live_ids:
                alive = s.get("tab") in live_ids
                status = "live" if alive else "archived"
            else:
                alive = None
                status = "unknown（CDP 未就绪，无法确认标签页）" if s.get("status") == "live" else (s.get("status") or "archived")
            rows.append({
                "conv_id": cid,
                "status": status,
                "tab_alive": alive,
                "device": s.get("device"),
                "device_known": bool(load_device_cookie(s.get("device") or "")),
                "prompt": s.get("prompt"),
                "reply_excerpt": s.get("reply_excerpt"),
                "created": s.get("created"),
                "updated": s.get("updated"),
            })
        print(json.dumps({"count": len(rows), "sessions": rows,
                          "note": ("live=标签页仍在，可直接续聊；archived=只能用 --anon-read 读全文；"
                                   "device_known=false 说明没有该设备的本地快照，读取会 404"),
                          }, ensure_ascii=False, indent=2))
        return 0
    if args and args[0] == "--anon-devices":
        print(json.dumps(device_summary(), ensure_ascii=False, indent=2))
        return 0
    if len(args) == 3 and args[0] == "--anon-relink":
        cid = cli_id(args[1])
        if cid is None:
            return 2
        try:
            did = safe_id(args[2], "device")
        except ValueError as e:
            print("ERROR: %s" % e, flush=True)
            return 2
        if not load_device_cookie(did):
            print(json.dumps({"conv_id": cid, "device": did, "result": "error",
                              "detail": "本地没有该设备快照，无法建立归属"}, ensure_ascii=False))
            return 1
        record_anon_session(cid, device=did)
        print(json.dumps({"conv_id": cid, "device": did, "relinked": True}, ensure_ascii=False))
        return 0
    if len(args) == 2 and args[0] == "--anon-restore":
        try:
            did = safe_id(args[1], "device")
        except ValueError as e:
            print("ERROR: %s" % e, flush=True)
            return 2
        try:
            n = AnonBrowser().restore_identity(did)
        except Exception as e:
            print(json.dumps({"device": did, "result": "error", "detail": str(e)}, ensure_ascii=False))
            return 1
        print(json.dumps({"device": did, "restored_cookies": n,
                          "note": ("已把留存身份回灌当前浏览器；刷新页面后生效。"
                                   "注意别在仍存活的 /uc/ 标签页上刷新——那会毁掉该会话的可写通道")},
                         ensure_ascii=False))
        return 0 if n else 1
    if len(args) == 2 and args[0] == "--anon-read":
        cid = cli_id(args[1])
        if cid is None:
            return 2
        api = ChatGPTAPI(account="nologin")
        try:
            status, last, transcript = read_anon(api, cid)
        except Exception as e:
            print(json.dumps({"conv_id": cid, "result": "error", "detail": str(e)},
                             ensure_ascii=False))
            return 1
        out = {"conv_id": cid, "status": status,
               "device": api._device_id() or None,
               "turns": len(transcript), "last_reply": last,
               "transcript": [{"role": r, "text": t} for r, t in transcript],
               "saved": "anon-sessions/" + os.path.basename(anon_archive_path(cid))
               if transcript else None}
        if status in (403, 404):
            out["hint"] = ("会话与设备身份绑定：已按登记设备与所有本地快照依次尝试仍读不到。"
                           "用 --anon-devices 看留存了哪些设备，确认目标设备快照是否还在；"
                           "快照丢失则无法再读回这条会话")
        elif status == 429:
            out["hint"] = "限速（通道问题，不代表会话丢失），等几分钟重试"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if status == 200 else 1
    if len(args) == 3 and args[0] == "--anon-send":
        cid = cli_id(args[1])
        if cid is None:
            return 2
        api = ChatGPTAPI(account="nologin")
        try:
            status, _p, conv = send_prompt(api, cid, args[2],
                                              thinking=True if thinking_flag else None)
        except Exception as e:
            print(json.dumps({"conv_id": cid, "result": "error", "detail": str(e)},
                             ensure_ascii=False))
            return 1
        print(json.dumps({"conv_id": conv, "status": status}, ensure_ascii=False))
        return 0 if status == 200 else 1
    # ---- 会话清单 ----
    if args and args[0] == "--list-all":
        combined = []
        errors = []
        quota = {}
        for name in ("acct1", "acct2", "nologin"):
            try:
                api = ChatGPTAPI(account=name)
            except (OSError, json.JSONDecodeError) as e:
                if name != "nologin":
                    errors.append("%s: credentials unavailable (%s)" % (name, e))
                    continue
                raise
            items, status, error = api.list_conversations()
            combined.extend(items)
            quota[name] = api.quota
            if status != 200 or error:
                errors.append("%s: HTTP %s %s" % (name, status, error))
                if status == 429:
                    break
        by_account = {}
        for c in combined:
            by_account.setdefault(c["account"], []).append(c)
        summary = {k: len(v) for k, v in by_account.items()}
        print(json.dumps({
            "conversations": combined,
            "by_account": by_account,
            "summary": summary,
            "quota": quota,
            "errors": errors,
        }, ensure_ascii=False, indent=2))
        return 1 if errors else 0
    if not args:
        print(__doc__)
        return 1

    def build(name):
        try:
            return ChatGPTAPI(account=name)
        except (ValueError, OSError, json.JSONDecodeError) as e:
            print("FAIL: account auth unavailable: %s" % e, flush=True)
            return None

    # ---- 开新会话 ----
    if args[0] == "--new":
        if len(args) != 2:
            print("usage: [--account acct1|acct2|nologin|auto] [--thinking] --new <prompt_file>", flush=True)
            return 2
        if explicit_account and account != "auto":
            api = build(account)
            if api is None:
                return 2
            if account == "nologin":
                print("nologin：走匿名浏览器通道（需 CDP 端口 %s 的未登录浏览器）"
                      % os.environ.get("CHATGPT_ANON_CDP_PORT", "9333"), flush=True)
            try:
                status, _path, conv = send_prompt(api, None, args[1], new_chat=True,
                                                  thinking=True if thinking_flag else None)
            except Exception as e:
                print(json.dumps({"result": "error", "detail": str(e)}, ensure_ascii=False))
                return 1
            if status == 200 and conv:
                record_conv(conv, account, args[1])
                print("NEW_CONV %s account=%s" % (conv, account), flush=True)
            return 0 if status == 200 else 1
        # 默认：acct1/acct2 均分轮转，双 429 才匿名兜底
        return create_chat_rotating(args[1], thinking=True if thinking_flag else None)

    # ---- 既有会话：未显式指定身份时按登记归属路由 ----
    # 先做参数校验，别在缺参数时索引越界（曾导致 `--tail` 空参数直接 IndexError）
    if args[0] == "--list" and len(args) != 1:
        print("usage: [--account acct1|acct2|nologin] --list", flush=True)
        return 2
    if args[0] == "--tail" and len(args) != 2:
        print("usage: [--account acct1|acct2|nologin] --tail <conv_id>", flush=True)
        return 2
    raw_id = args[1] if args[0] == "--tail" else (args[0] if not args[0].startswith("--") else "")
    conv_id = cli_id(raw_id) if raw_id else ""
    if raw_id and conv_id is None:
        return 2
    owner = conv_owner(conv_id) if conv_id else None
    if not explicit_account and owner and owner != account:
        print("route: 会话 %s 登记归属 %s，自动切换身份" % (conv_id[:8], owner), flush=True)
        account = owner
    elif explicit_account and owner and owner != account:
        print("WARN: 会话 %s 登记归属 %s，当前显式使用 %s（可能 404/403）"
              % (conv_id[:8], owner, account), flush=True)
    api = build(account)
    if api is None:
        return 2
    if args[0] == "--list":
        items, status, error = api.list_conversations()
        print(json.dumps({"account": account, "conversations": items, "status": status, "error": error, "quota": api.quota}, ensure_ascii=False, indent=2))
        return 0 if status == 200 and not error else 1
    if args[0] == "--tail":
        tail, raw = api.live_tail(conv_id, tries=2)
        print("tail:", tail)
        if raw:
            print(api.harvest_mapping(raw)[-4000:])
        if not raw:
            print("FAIL: 未取到会话内容（限速或会话不可访问）", flush=True)
            return 1
        return 0
    if len(args) != 2:
        print("usage: [--account acct1|acct2|nologin] <conv_id> <prompt_file>", flush=True)
        return 2
    try:
        status, _path, _conv = send_prompt(api, conv_id, args[1],
                                             thinking=True if thinking_flag else None)
    except Exception as e:
        print(json.dumps({"conv_id": conv_id, "result": "error", "detail": str(e)}, ensure_ascii=False))
        return 1
    if status == 200:
        record_conv(conv_id, account, args[1])
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
