# -*- coding: utf-8 -*-
"""chatgpt_send.py — 统一发送/收割器（API 直连 + PoW，P0 通道）。

用法：
    python chatgpt_send.py <conv_id> <prompt_file>          # 投递到既有会话
    python chatgpt_send.py [--timeout SECONDS] <conv_id> <prompt_file>  # 显式覆盖客户端超时（否则按 --thinking 分档）
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
    python chatgpt_send.py --log-list                  # 会话日志：有留存记录的会话一览
    python chatgpt_send.py --log-show <conv_id>        # 会话日志：读某会话的全部交互（全文）
    python chatgpt_send.py --log-search <词> [--conv <id>] [--limit N] [--json]  # 会话日志：检索
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
    python chatgpt_send.py --check-send-log <log> [--prompt <pfile>]  # 投递日志：SENT/TRUNCATED/IN_FLIGHT/NOT_SENT

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
    第一优先：acct1 / acct2 / acct3 / acct4 / acct5 / acct6 均分轮转（游标只在六个登录态之间推进）。
    兜底：仅当全部登录态都 429（本次命中或处于 429 冷却）时，才启用 nologin 匿名通道。
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

账号凭据放在 accounts/<acct1|acct2|acct3|acct4|acct5|acct6>/{cookies.json,access_token.txt}。
--list-all 只聚合各身份获准查看的会话；429 时停止扫描。

前置：acct1/default 认证文件在本脚本目录；acct2/acct3/acct4/acct5/acct6 分别在 accounts/acct2/、accounts/acct3/、accounts/acct4/、accounts/acct5/、accounts/acct6/ 下：
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
    - HTTP 413 Payload Too Large → 本次发送终止；不轮换账号、不自动重试；检查实际请求体及服务端错误响应。

投递超时（--timeout，优先级：命令行 > CHATGPT_SEND_TIMEOUT > --thinking 分档）：
    作用于发送/收割这条 HTTP 读写路径的 socket 超时，不改变 --thinking 选择模型的语义。
    **只有显式配置（--timeout 或 CHATGPT_SEND_TIMEOUT）才覆盖分档**；两者都没有时按
    --thinking 分档（非思考 420s / 思考 1800s）。CLI 不再无条件写 600——那会把
    思考模式的 1800s 分档在唯一可被操作者触达的路径上打死（DEF-ops-04）。
    超时一律打印可机读标记，便于事后 grep 统计：
      TIMEOUT_PARTIAL after Ns, M bytes buffered, saved to <path>   # 已收内容落盘（<auth>/timeouts/）
      TIMEOUT_EMPTY after Ns                                       # 零字节
    IncompleteRead（流被截断）同路：PARTIAL after Ns, M bytes buffered, saved to <path>，同样落盘。

送达凭证（截断与完整必须分得开）：
    完整响应      → `SEND <pfile> -> 200 respLen=<n> in <s>s`，exit 0（格式与旧版一致）
    截断/超时     → `SEND_TRUNCATED pfile=<pfile> st=<st> bytes=<n>`，exit 3
    截断路径仍走既有链路（GET 补全文 + 归档，不丢字节），但**绝不打 `SEND ... -> 200`**：
    那行是唯一送达凭证，被截断响应冒充就等于门禁假绿（DEF-ops-01）。

投递日志判定（NOT_SENT 的机读版）：
    `--check-send-log <log> [--prompt <prompt_file>]` 按 CB-B8 判一次投递是否「没发出去」。
    三合取（无 SEND + <200 字节 + mtime>5 分钟）之前必须**先看进程死活**：在飞投递的静默期
    （tail GET + 最长 3600s 退避）同时满足三条，按三合取判死就会重复投递（DEF-ops-02）。
    verdict：SENT / TRUNCATED / IN_FLIGHT / NOT_SENT；exit 0/3/0/1。
"""
import base64
import contextlib
import copy
import csv
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
import unicodedata
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

# DECISION-156：Cookie 持久化边界。只有这些域（及其子域）有资格被写盘。
# 请求头过滤只看「能不能发出去」，这里管「能不能存下来」——两道独立的关。
_OPENAI_COOKIE_DOMAINS = ("chatgpt.com", "openai.com")
# CHATGPT_BASE：API 根。默认直连；指向闸口时走 asip/chatgpt_l7.py
#   （http://127.0.0.1:17892，429 适配：账号层退避短路 + transport 换 ok）。
API_BASE = os.environ.get("CHATGPT_BASE", "https://chatgpt.com").rstrip("/")

# ---------- 投递超时（可配置） ----------
# 实测依据（162 次投递）：19 次卡在 ~1700s（16 次落在 1769–1773s，正是 1800s 客户端超时），
# in-flight 累计 9.33h = 整段墙钟 18.12h 的 24.7%，其中 17 次最终 FAIL「回复完整性未证明」。
# 正常投递中位数 182.5s，P90 完全由这个超时决定而非真实分布——所以它必须可配。
# 注意：CLI 未显式给 --timeout / CHATGPT_SEND_TIMEOUT 时**不再**把它写进 SEND_TIMEOUT
# （否则 --thinking 的 1800s 分档在 CLI 上会被 600s 无条件覆盖，DEF-ops-04）。
# 保留常量是为了文档与外部导入兼容；实际分档见 _send_prompt_impl。
DEFAULT_SEND_TIMEOUT = 600
# None = 未覆盖（沿用 --thinking 的 420/1800 分档）；int = 覆盖值。
# 只有 CLI（main）或环境变量会把它变成 int，库调用方直接调 send_prompt 仍保持旧行为。
SEND_TIMEOUT = None


def _parse_timeout(raw):
    """把一次超时配置收敛成正整数秒；不可用返回 None（由调用方决定回退到哪个默认）。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        v = int(s, 10)
    except (TypeError, ValueError):
        print("WARN: 超时值不是整数：%r —— 按未指定处理" % (raw,), flush=True)
        return None
    if v <= 0:
        print("WARN: 超时必须 > 0（收到 %r）—— 按未指定处理" % (raw,), flush=True)
        return None
    return v


def _extract_timeout_flag(argv):
    """从 argv 里摘出 `--timeout N` / `--timeout=N`，返回 (原始值或 None, 剩余 argv)。

    为什么要先摘出来：这个脚本没有 argparse，`<conv_id> <prompt_file>` 的位置校验
    是 `len(args) != 2`——`--timeout 600` 留在 args 里会被当成第三个位置参数，
    所有既有调用方式（不带 --timeout）必须继续可用，带 --timeout 也不能变位置语义。
    """
    out, value, seen = [], None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--timeout" or a.startswith("--timeout="):
            if seen:
                raise ValueError("--timeout 重复指定")
            if a == "--timeout":
                if i + 1 >= len(argv):
                    raise ValueError("--timeout 需要秒数（形如 --timeout 600）")
                value = argv[i + 1]
                i += 2
            else:
                value = a.split("=", 1)[1]
                i += 1
            seen = True
            continue
        out.append(a)
        i += 1
    return value, out


SEND_TIMEOUT = _parse_timeout(os.environ.get("CHATGPT_SEND_TIMEOUT"))
SEND_TIMEOUT_SOURCE = "env" if SEND_TIMEOUT is not None else ""


def _cap_timeout(default):
    """辅助请求（tail/GET 全文）的超时上限：只允许 --timeout 收紧，绝不放宽。

    为什么不直接全用 --timeout：这些 GET 本来是 90/120s 的短请求，给 600s 只会让
    卡死的 GET 更慢；给 --timeout 60 时它们必须跟着一起变短，否则探针仍会拖满 90s。
    """
    if SEND_TIMEOUT is None:
        return default
    return min(default, SEND_TIMEOUT)


# ---------- 送达判定（CB-B8 三合取的机读版） ----------
# 旧判据（无 SEND + <200 字节 + mtime>5 分钟）在**在飞**投递上三个条件全部成立：
#   <conv_id> 路径发送前先 live_tail（两次 GET，各 ≤90s），失败后 _retry_after 最长退避
#   3600s；这段静默期日志一个字节都不写，进程却在推进。按三合取判死 → 重投 →
#   同一提示词两条回复、两个工作项。所以判死之前必须先看「进程是否还在」。
NOT_SENT_MAX_BYTES = 200        # 旧判据：文件小于 200 字节
NOT_SENT_MIN_AGE_SEC = 300      # 旧判据：mtime 超过 5 分钟
INFLIGHT_BACKOFF_WINDOW = 3600  # api._retry_after 的退避上限＝最长零输出窗口

# 完整投递的唯一送达凭证。SEND_TRUNCATED 不是它：截断只证明「发出去了一部分」。
SEND_OK_RE = re.compile(r"^SEND .+ -> 200\b")
SEND_TRUNCATED_RE = re.compile(r"^SEND_TRUNCATED\b")


def read_log_text(path):
    """按 BOM 读日志：UTF-8（含 BOM）默认，UTF-16LE/BE 也认。

    为什么不能写死 utf-8：本机 PowerShell 重定向产出 UTF-16LE+BOM，按 utf-8 硬读时
    每个 ASCII 字节前都有 0x00，`SEND` 这类判据全部漏匹配（CB-B10 / DEF-ops-08）。
    """
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xff\xfe\x00\x00") or raw.startswith(b"\xff\xfe"):
        enc = "utf-16"
    elif raw.startswith(b"\xfe\xff"):
        enc = "utf-16-be"
    elif raw.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8-sig"
    else:
        enc = "utf-8"
    return raw.decode(enc, "replace")


def _iter_process_cmdlines():
    """枚举 (pid, 命令行) 的尽力而为实现；返回 None 表示**枚举能力不可用**。

    None ≠ 没有进程：读不到就按「可能还活着」处理（fail-closed），否则一次权限/工具
    缺失就会把在飞投递判死并触发重投。顺序：/proc（精确、零依赖）→ psutil → wmic。
    """
    if os.path.isdir("/proc"):
        rows = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            rows.append((int(entry),
                         raw.replace(b"\x00", b" ").decode("utf-8", "replace")))
        return rows
    try:
        import psutil
    except Exception:
        psutil = None
    if psutil is not None:
        rows = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                info = proc.info
                cmd = info.get("cmdline") or []
                rows.append((info.get("pid"), " ".join(str(c) for c in cmd)))
            except Exception:
                continue
        return rows
    if os.name == "nt":
        try:
            done = subprocess.run(
                ["wmic", "process", "get", "ProcessId,CommandLine", "/format:csv"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        rows = []
        for row in csv.reader(done.stdout.decode("utf-8", "replace").splitlines()):
            if len(row) >= 3 and row[-1].strip().isdigit():
                rows.append((int(row[-1]), ",".join(row[1:-1])))
        return rows
    return None


def live_senders(prompt_file):
    """命令行里提到该 pfile 的存活 pid 列表；None = 枚举不可用（当作「可能活着」）。"""
    if not prompt_file:
        return []
    target = os.path.basename(str(prompt_file))
    rows = _iter_process_cmdlines()
    if rows is None:
        return None
    me = os.getpid()
    out = []
    for pid, cmdline in rows:
        # 必须排除自己：判定器常以 `--check-send-log <log> --prompt <pfile>` 被调用，
        # 自己的命令行里就含着 pfile——不排除的话每次都「找到存活进程」，
        # NOT_SENT 永远判不出来（恒 IN_FLIGHT 的假红变假绿）。
        if pid == me:
            continue
        if target and target in (cmdline or ""):
            out.append(pid)
    return out


def judge_send_log(log_path, prompt_file=None, now=None, live_pids=None):
    """判定一次投递日志：SENT / TRUNCATED / IN_FLIGHT / NOT_SENT。

    顺序是有意为之，每一步都在堵一个已实测的假绿/假红：
      ① `SEND ... -> 200` → SENT（唯一送达凭证；`SEND_TRUNCATED` 不匹配这个正则）
      ② `SEND_TRUNCATED` → TRUNCATED（已发但完整性未证明，不能算 SENT，也不该重投）
      ③ 三合取不成立（有 SEND/够大/够新）→ IN_FLIGHT（还可能成功）
      ④ 三合取成立 → **先查进程死活**：
         - 同 pfile 的进程还在 → IN_FLIGHT（静默期，最长 3600s）
         - 枚举不到进程时按「可能活着」→ IN_FLIGHT（fail-closed）
         - mtime 仍在 3600s 退避窗口内且首行是 `timeout:` → IN_FLIGHT
         - 以上都不成立 → NOT_SENT（真的死了，可以重投）
    `live_pids` 是给测试/调用方注入「已知存活 pid」的口子；None = 现场枚举。
    """
    if not os.path.isfile(log_path):
        raise FileNotFoundError(log_path)
    text = read_log_text(log_path)
    lines = text.splitlines()
    size = os.path.getsize(log_path)
    mtime = os.path.getmtime(log_path)
    base_now = time.time() if now is None else float(now)
    age = max(0.0, base_now - mtime)

    has_send = any(SEND_OK_RE.match(ln.strip()) for ln in lines)
    has_truncated = any(SEND_TRUNCATED_RE.match(ln.strip()) for ln in lines)
    first_line = ""
    for ln in lines:
        if ln.strip():
            first_line = ln.strip()
            break
    window_open = age <= INFLIGHT_BACKOFF_WINDOW and first_line.startswith("timeout:")

    pids = live_senders(prompt_file) if live_pids is None else list(live_pids)
    alive_unknown = pids is None
    alive = alive_unknown or bool(pids)

    verdict, reasons = "IN_FLIGHT", []
    if has_send:
        verdict, reasons = "SENT", ["send_line"]
    elif has_truncated:
        verdict, reasons = "TRUNCATED", ["send_truncated_line"]
    elif size >= NOT_SENT_MAX_BYTES:
        reasons = ["log_has_bytes"]
    elif age <= NOT_SENT_MIN_AGE_SEC:
        reasons = ["log_is_fresh"]
    elif alive:
        reasons = ["sender_process_alive" if not alive_unknown else "liveness_unknown"]
    elif window_open:
        reasons = ["inside_backoff_window"]
    else:
        verdict, reasons = "NOT_SENT", ["no_send_line", "log_empty", "log_stale",
                                        "no_sender_process"]

    return {
        "verdict": verdict,
        "sent": verdict == "SENT",
        "truncated": verdict == "TRUNCATED",
        "not_sent": verdict == "NOT_SENT",
        "in_flight": verdict == "IN_FLIGHT",
        "bytes": size,
        "age_sec": int(age),
        "first_line": first_line,
        "live_pids": [] if pids is None else pids,
        "liveness_unknown": alive_unknown,
        "reasons": reasons,
        "log": os.path.abspath(log_path),
        "prompt_file": os.path.abspath(prompt_file) if prompt_file else None,
    }


def _send_exit_code(status, conv=None):
    """投递类命令的退出码：截断优先于 200（SEND_TRUNCATED 绝不能以 0 收场）。

    只判 conv 上的 send_truncated 标记，不判完整性：GET 之后 reply_complete 可能变 True，
    但「发送时被截断」是既成事实，送达凭证已经缺失，exit 0 会让门禁假绿。
    """
    if getattr(conv, "send_truncated", False):
        return 3
    return 0 if status == 200 else 1

# ---------- 建会话身份轮转 ----------
# 优先登录态：acct1 / acct2 / acct3 / acct4 / acct5 / acct6 均分轮转；nologin（匿名）只作兜底——
# 实测匿名会话不进任何会话历史（list 返回 0 条），随时可能被服务端回收，
# 仅按 conv_id 在短期内可读，因此只在全部登录态都 429 时启用。
ROTATION_ORDER = ("acct1", "acct2", "acct3", "acct4", "acct5", "acct6", "nologin")
ROTATION_PRIMARY = ("acct1", "acct2", "acct3", "acct4", "acct5", "acct6")
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
    """校验外部传入的会话 id / 设备号；不合法直接抛错，不留给下游拼接。

    F-37：契约是「不合法 → ValueError」，不是 AttributeError——非 str
    （int/float/bool/list/…）一律走同一条拒绝路径，不得借 `.strip` 炸出别样异常。
    """
    v = value.strip() if isinstance(value, str) else ""
    if not _ID_RE.match(v):
        raise ValueError("%s 非法：%r（只允许字母数字与 _-，长度 1-80）" % (what, value))
    return v


def id8(conv_id, fallback="new"):
    """取 8 位短前缀做文件名；非法 id 退化为哈希，绝不拼出路径分隔符。

    F-37：永不抛异常——非 str/bytes 先经 _as_text 收敛（没有可解析内容 → fallback）。
    """
    v = _as_text(conv_id).strip()
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


def _replace_with_bounded_retry(tmp, path):
    """
    Replace path atomically, retrying transient Windows sharing/access
    failures with a strictly bounded delay budget.

    Only WinError 5 and 32 are retryable. Lock acquisition and lock
    timeout behavior are deliberately outside this function.
    """
    retry_delays = (0.05, 0.10, 0.20, 0.40, 0.80, 1.00)
    max_attempts = len(retry_delays) + 1

    for attempt in range(max_attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            winerror = getattr(exc, "winerror", None)
            retryable = winerror in (5, 32)

            if not retryable or attempt >= len(retry_delays):
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                except OSError as cleanup_error:
                    raise OSError(
                        "atomic replace failed and temporary-file cleanup "
                        "also failed: tmp=%r path=%r cleanup_error=%r"
                        % (tmp, path, cleanup_error)
                    ) from exc
                raise

            time.sleep(retry_delays[attempt])
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                raise OSError(
                    "atomic replace failed and temporary-file cleanup "
                    "also failed: tmp=%r path=%r cleanup_error=%r"
                    % (tmp, path, cleanup_error)
                )
            raise


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
        _replace_with_bounded_retry(tmp, path)
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


def _atomic_create_exclusive(path, text):
    """原子独占创建并写入：文件已存在则抛 FileExistsError，**绝不覆盖**。

    F-21：`_atomic_write` 用 `os.replace`，语义是**覆盖**；靠「先检查不存在再写」
    找唯一名是 TOCTOU——两个线程/进程可以同时通过检查、同时写同一个路径，
    后写的覆盖先写的证据。隔离档案是异常证据的**唯一副本**，覆盖即永久丢失。
    这里用内核级 `O_CREAT|O_EXCL` 把「占名 + 判重」合成一个原子操作。
    写入失败时文件已占名（空壳），调用方应换名重试；已存在则抛 FileExistsError。
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.remove(path)
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
        _replace_with_bounded_retry(tmp, path)
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
    # F-37：路径参数先过类型守卫——os.path.isfile(None/3.5) 抛 TypeError，
    # os.path.isfile(True) 更是把 True 当文件描述符 1（F-31b 同款陷阱）。
    # 契约是 CustodyError，不是 TypeError。
    if not isinstance(src, (str, bytes, os.PathLike)):
        raise CustodyError("源文件路径非法：%r" % (src,))
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
                 "--proxy-show", "--rotation", "--list-all",
                 "--log-search", "--log-list", "--log-show")


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


_RRULE_PART_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)=([^;\r\n]+)$")
_RRULE_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_RRULE_ALLOWED = ("FREQ", "INTERVAL")


def check_schedule(rrule):
    """B1：周期任务固定 1 小时。天维度、INTERVAL≠1（含用 0 表示高频）一律拒绝。

    F-14（第二十七轮审查口径）——三条硬约束，缺一条就等于「校验语义 ≠ 下发语义」：

    1. **重复 rule part 一律拒绝**（RFC 5545 §3.3.10：rule part 不得重复）。
       旧版用字典解析，同名 key 取最后一个：`FREQ=DAILY;FREQ=HOURLY` 校验时读到
       HOURLY 判通过，下发时却发原始串——调度器若取第一个就变成 DAILY，
       「周期固定 1 小时」被确定性绕过。
    2. **下发规范化 RRULE**：只用**已验证字段**重新构造（`FREQ=HOURLY;INTERVAL=1`），
       绝不再把用户原始字符串直接下发——原始串就是重复属性与内部空白的入口。
    3. **枚举值大小写不敏感**（RFC 5545 §2：enumerated property values are
       case-insensitive）：`freq=hourly` 合法，接受后规范化为 `HOURLY`。
       大小写本身不是 RFC 违规，不能拿它当拒绝理由。

    另外两条防御：拒绝内部空白（旧版 `.replace(" ", "")` 会把 `FREQ=HO URLY`
    悄悄修补成合法输入，等于替用户改输入）；拒绝 FREQ/INTERVAL 之外的 rule part
    （`BYxxx`/`UNTIL`/`COUNT` 会改变触发语义，不得静默忽略后照样下发）。
    """
    # F-22（第三十轮审查）：必须在 .strip() **之前**检查 CR/LF——
    # 旧版先 strip() 再查 \s，`FREQ=HOURLY\n` / `\nFREQ=HOURLY` 的换行被
    # strip 吃掉，绕过「换行一律畸形」的契约。预处理不能替输入消除违规字符。
    if "\r" in str(rrule or "") or "\n" in str(rrule or ""):
        raise ScheduleError("rrule 含换行（CR/LF 一律畸形，不参与首尾 strip 清理）")
    raw = str(rrule or "").strip()
    if not raw:
        raise ScheduleError("rrule 为空")
    if re.search(r"\s", raw):
        raise ScheduleError("rrule 含空白字符（内部空格会让 'FREQ=HO URLY' 被悄悄"
                            "修补成合法输入，等于替用户改了输入）：%r" % raw)
    parts = {}
    for chunk in raw.split(";"):
        if not chunk:
            raise ScheduleError("rrule 含空 rule part（分号多写）：%r" % raw)
        m = _RRULE_PART_RE.match(chunk)
        if not m:
            raise ScheduleError("rrule 的 rule part 必须是 NAME=VALUE：%r" % chunk)
        name, value = m.group(1).upper(), m.group(2)
        if name in parts:
            raise ScheduleError("rrule 含重复 rule part %s（RFC 5545 §3.3.10 禁止重复；"
                                "不同解析器会取到不同的那一个）：%r" % (name, raw))
        parts[name] = value
    unsupported = [k for k in parts if k not in _RRULE_ALLOWED]
    freq = parts.get("FREQ")
    if freq is None:
        raise ScheduleError("rrule 缺 FREQ")
    if freq.upper() != "HOURLY":
        raise ScheduleError("FREQ 必须 HOURLY（周期固定 1 小时），收到 %r" % freq)
    iv = parts.get("INTERVAL")
    if iv is None:
        interval = 1                      # RFC 5545：INTERVAL 缺省即 1
    else:
        if len(iv) > 32:
            raise ScheduleError("INTERVAL 过长（%d 位）：%r" % (len(iv), iv[:32]))
        if not _RRULE_INT_RE.match(iv):
            raise ScheduleError("INTERVAL 不是 RFC 十进制整数：%r" % iv)
        # **不调用 int()**：Python 3.11+ 对超长十进制串会抛 ValueError
        # （位数上限 4300），从 check_schedule 里逃逸出去就是异常逃逸
        # （与 F-07 同型）。这里直接比数字串，零转换、零溢出。
        body = iv[1:] if iv[:1] in ("+", "-") else iv
        norm_digits = body.lstrip("0") or "0"
        if iv.startswith("-") or norm_digits != "1":
            raise ScheduleError("INTERVAL 必须为 1：%r（高频也不能用 INTERVAL<1 表示）"
                                % iv)
        interval = 1
    if unsupported:
        raise ScheduleError("rrule 含未支持的 rule part %s：B1 只允许 FREQ 与 INTERVAL"
                            "（BYxxx/UNTIL/COUNT 会改变触发语义，不得静默忽略）"
                            % ",".join(sorted(unsupported)))
    # 下发用规范化串：由**已验证字段**重新构造，不含用户原始串里的任何杂质。
    # 契约：**唯一可执行字段是 `rrule`**；`original_input_for_audit_only`
    # 只留档审计，任何下游都不得拿它去排程。
    canonical = "FREQ=HOURLY;INTERVAL=%d" % interval
    return {"result": "ok", "rrule": canonical,
            "original_input_for_audit_only": raw,
            "executable_field": "rrule",
            "freq": "HOURLY", "interval": interval}


def check_oneshot(scheduled_at, now=None):
    """B4/B5：一次性任务必须带时区、必须落在未来 1 小时内。

    事故：盘点出 4 个早已过点却仍 ACTIVE 的 one-shot，没人负责归档。
    所以「建之前」就把已过去与过远的两头堵住，而不是等事后巡检。

    F-29（第三十五轮）——与 B1 的 F-14 同一条契约「**校验语义必须等于下发语义**」：
    `fromisoformat` 接受空格分隔、紧凑 `20261119T181819+0000`、无秒等多种写法，
    旧版把这些**原始串**回显成 `scheduled_at` 让下游拿去排程——同一个时刻有多种
    字符串表示，下游按字符串比较/透传就会错。改为：回显**规范化后的 ISO 串**
    （`dt.isoformat()`），原始输入只留在 `original_input_for_audit_only` 供审计，
    与 `check_schedule` 的字段命名一致。
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
    # F-28（第三十五轮）：`now` 是库 API 参数，naive datetime 或非 datetime 会让
    # `dt - base` 抛 TypeError 逃逸——与 F-07（超长 int 让 int() 抛 ValueError）
    # 同型：调用方只 catch ScheduleError，异常一逃逸就等于「校验器自己崩了」。
    # 校验器对任何输入都只允许两种结局：返回结果，或抛 ScheduleError。
    base = now if now is not None else datetime.now(dt.tzinfo)
    if not isinstance(base, datetime):
        raise ScheduleError("now 必须是 datetime（收到 %s）：不得让 TypeError 逃逸"
                            % type(base).__name__)
    if base.tzinfo is None or base.utcoffset() is None:
        raise ScheduleError("now 必须带时区偏移（naive datetime 无法与 scheduledAt 比较）：%r"
                            % (base,))
    delta = (dt - base).total_seconds()
    if delta <= 0:
        raise ScheduleError("scheduledAt 已过去（%s）：不得建成 ACTIVE，先按 B5 归档" % raw)
    if delta > 3600:
        raise ScheduleError("scheduledAt 距今 %.0f 分钟 > 60 分钟：改走每小时条件巡检（B2）"
                            % (delta / 60.0))
    return {"result": "ok", "scheduled_at": dt.isoformat(),
            "original_input_for_audit_only": raw,
            "executable_field": "scheduled_at",
            "seconds_from_now": int(delta), "lifetime_ok": True}


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


def _as_dict(obj):
    """把任意值收敛成 dict；不是 dict 一律返回空 dict（F-12）。

    为什么需要：解析网络响应时，「JSON 解析成功」不等于「形状符合预期」。
    服务端返回 `null`、数组、或字段类型变了，下一行 `.get()` 就是 AttributeError。
    这些解析器本来都写了 `except Exception: return None` 来优雅降级「解析失败」，
    但对「解析成功、形状不符」漏了守卫——同一份不可信输入两种待遇。
    统一用本函数收敛，形状不符就当「没有这一层」，绝不抛异常。
    """
    return obj if isinstance(obj, dict) else {}


def _as_seq(obj):
    """把任意值收敛成 list；不是 list/tuple 一律返回空 list（F-12）。"""
    return list(obj) if isinstance(obj, (list, tuple)) else []


def _as_float(value, default=0.0):
    """把不可信状态文件里的数值字段收敛成 float，**永不抛异常**（F-30）。

    为什么需要：`float()` 对超大整数抛的是 `OverflowError`（ArithmeticError
    子类），而历史上六处调用点只写了 `except (TypeError, ValueError)`——
    `{"created_at": 10**400}` 这类损坏字段会让异常直接逃逸出 `_file_lock`，
    调用方拿到的不是 LockTimeout 而是 OverflowError。更糟的是它出现在**锁判定**
    路径上：判定器一崩，互斥就没了确定性结局。

    与 `_as_dict`/`_as_seq` 同一原则：状态文件是不可信输入，任何字段损坏都应
    降级成「没有这个值」，而不是让异常穿过临界区。
    """
    if isinstance(value, bool) or value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if out != out or out in (float("inf"), float("-inf")):
        return default
    return out


def _as_text(value):
    """把不可信响应体裁成 str；不是可文本化类型就返回空串（F-31）。

    为什么需要：同一份不可信输入不能两种待遇——`reply_digest`/
    `harvest_mapping_checked` 走 `json.loads` 的 `except Exception`，任意类型
    都降级成 None；而 `_scan_sse`/`_sse_conv_ids` 直接 `(raw or "").splitlines()`，
    收到 bytes 抛 `TypeError: startswith first arg must be bytes`、收到 int 抛
    `AttributeError`。当前 HTTP 层统一 `.decode("utf-8","replace")` 后再传入，
    所以不可达；但只要哪天上层改流式/二进制读取，异常就会从解析器穿出去。

    bytes/bytearray 解码（HTTP 体本就是字节）；其余非 str 一律空串——
    「没有可解析内容」是保守结论（不完整），不是猜测。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", "replace")
        except Exception:
            return ""
    return ""


def _as_int(value, default=0):
    """把不可信状态文件里的整数字段收敛成 int，**永不抛异常**（F-34）。

    F-07 口径：绝不借 `int()` 把非法折叠成合法——`int(True) == 1`、
    `int(3.5) == 3`，会让「不是下标/计数的东西」悄悄变成下标/计数。
    只认两种形态：
      - int 且不是 bool（True 不是 1）；
      - 纯十进制数字串（"42" 可以，" 42"/"4.2"/"0x2a" 不行；超长串在
        3.11+ 会让 int() 抛 ValueError，一并降级成 default）。
    float/bool/None/其余类型一律 default——「没有这个值」是保守结论。
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value and all(c in "0123456789" for c in value):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return default
    return default


def _as_ctime(value):
    """规范化 create_time，返回 (ok, number)。

    ok=False 表示这个时间值**不可用**——记录无效，不得靠它参与排序，
    否则「顺序」就是编出来的。
    缺失（None）返回 (True, 0.0)：记录本身没坏，只是没有时间戳，视为最旧，
    内容照收，不影响「取最新」的选择。

    F-12c：数值必须**有限**（拒 NaN / ±inf），且**不得在 float64 精确整数
    范围外转 float**——那会静默丢精度，把一个 10^18 的时间戳变成一个
    「相近但不同」的数；精度不可信时宁可判不可用，也不猜。
    """
    if value is None:
        return True, 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, 0.0
    if isinstance(value, int):
        if value > 9007199254740991 or value < -9007199254740991:
            return False, 0.0        # 大整数：float() 会丢精度 → 判不可用
        return True, float(value)
    if value != value:
        return False, 0.0            # NaN：非有限数参与排序没有意义
    if value == float("inf") or value == float("-inf"):
        return False, 0.0            # ±inf
    # F-12c（第三十轮审查）：float 分支此前只查有限性，整数值型 float
    # 超 ±2^53−1（如 9007199254740992.0、1e20）照样放行——与文档声明的
    # 「不得在精度范围外转 float」矛盾：这个 float 本身就不能精确表示整数，
    # 排序结果不可信。非整数型 float（如 1.5）保留，有限数足以排序。
    if abs(value) <= 9007199254740991 and value == int(value):
        return True, float(value)     # 安全范围内的整数值型 float：精确
    if abs(value) > 9007199254740991 and value == int(value):
        return False, 0.0            # 大整数值型 float：精度不可信，拒
    return True, float(value)        # 非整数型 float（如 1.5）：有限数足以排序


def _part_is_structured(part):
    """part 是不是「合法但不可文本化」的结构化 part（如多模态图片对象）。

    ChatGPT 的 `content.parts` 不保证每项都是字符串：图片/音频/视频会以
    `{"content_type": ..., ...}` 的对象形式出现。这类 part **不是畸形**——
    把它当畸形会把正常的多模态用户消息误报成「assistant 回复残缺」（F-19）；
    但它也确实**不是文本**，直接 join 会让内容凭空消失（F-12a）。
    所以必须把「合法但非文本」和「非法」分开认，不能一刀切。
    """
    return isinstance(part, dict) and isinstance(part.get("content_type"), str)


def _split_parts(parts):
    """把 parts 拆成 (texts, structured, malformed)。

    - `texts`：字符串 part 的列表（唯一能拼成文本的部分）
    - `structured`：结构化 part 的个数（合法但不可文本化）
    - `malformed`：True = 含**非法** part（既不是字符串，也不是可识别的结构化 part）
    """
    texts = []
    structured = 0
    malformed = False
    for p in parts:
        if isinstance(p, str):
            texts.append(p)
        elif _part_is_structured(p):
            structured += 1
        else:
            malformed = True
    return texts, structured, malformed


def _assistant_text(texts):
    """把 assistant 的文本 parts 拼成**唯一可比对口径**：拼接后 strip。

    落盘文本（`_scan_mapping` → `harvest_mapping_checked`）与完整性对账
    （`reply_digest`）必须对同一份 body 得到**同一个字符串**，否则 F-24 的
    「digest.sha256 == sha256(full)」比的是两个不同口径的拼接：合法完整回复
    只要首尾带空白（LLM 输出极常见），digest 算未 strip、full 是 strip，
    两者永不相等 → 被误标「完整性未证明」，门禁失去区分度（F-26）。

    收敛在**一个函数**里，杜绝两处各自拼接再次漂移。
    """
    return "".join(texts).strip()


def _scan_sse(raw_text):
    """扫描 SSE 文本，返回 (text, complete, detail)。

    降级姿态（第二十六轮审查口径）：坏事件只跳过、继续处理后续事件，
    但**必须被记录**——否则「部分流」会被当成完整回复落盘。

    complete = 没出现畸形事件 且 流以 [DONE] 正常收尾。
    detail 说明差在哪（坏事件数 / 是否见到 [DONE]）。

    F-15：显式 `message: null` 与「根本没有 message 字段」必须分开——前者是
    畸形事件（计入坏事件），后者是合法的非消息事件。旧版 `msg is None` 两者
    一视同仁，畸形事件能混进「complete=True」里。
    F-16：`[DONE]` 是**解析终点**，其后的数据一律不采信。
    F-19：结构化（非文本）part 合法但不可文本化——按角色区分：非 assistant
    角色属正常多模态内容，不算坏事件；assistant 角色的结构化 part 意味着
    「抽出来的文本只是投影」，按不完整处理。
    """
    last = ""
    bad = 0
    saw_done = False
    for line in _as_text(raw_text).splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            saw_done = True
            break                      # F-16：[DONE] 是终点，之后的事件不采信
        if not payload:
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            bad += 1
            continue
        if not isinstance(obj, dict):
            bad += 1
            continue
        if "message" not in obj:
            continue                   # 事件里没有 message（如 conversation_id）——正常
        msg = obj["message"]
        if not isinstance(msg, dict):
            bad += 1                   # F-15：显式 message: null / 非 dict → 畸形
            continue
        # F-12b：带 message 却没有合法 author 的事件是**畸形**——必须计入坏事件。
        # 旧版把 role 取成 None 就静默跳过，坏事件不进计数，complete 照样报 True，
        # 于是一段残缺流被当成完整回复落盘。
        author = msg.get("author")
        if not isinstance(author, dict):
            bad += 1
            continue
        role = author.get("role")
        content = msg.get("content")
        if content is None:
            parts = []
        elif isinstance(content, dict):
            parts = content.get("parts")
            if parts is None:
                parts = []
            elif not isinstance(parts, list):
                bad += 1
                continue
        else:
            bad += 1
            continue
        texts, structured, malformed = _split_parts(parts)
        if malformed:
            bad += 1                   # F-12a：非法 part → 该段内容会凭空消失
            continue
        if structured and role == "assistant":
            bad += 1                   # F-19：assistant 回复含非文本内容 → 文本是投影
            continue
        txt = "".join(texts)
        if role == "assistant" and txt.strip():
            last = txt
    detail = "坏事件 %d 个、%s" % (bad, "见到 [DONE]" if saw_done else "未见 [DONE]")
    return last, (bad == 0 and saw_done), detail


def _sse_conv_ids(raw_text):
    """按出现顺序取出 SSE 里的 conversation_id。

    返回 (first, uniq)：first = **首个**能解析出 id 的 data 事件里的 id；
    uniq = 全部出现过的 id（去重保序）。

    F-13：旧版解析 conversation_id 是「一路往后找，找到就用」，还会在整包
    JSON、正文正则里继续捞——等于「响应里出现过什么 id 就用哪个」。一旦后面
    出现另一个 id，落点判定就建立在一个无法确认归属的 id 上。
    现在只认**首个**事件的 id；一旦发现多个不同 id 就失败关闭。

    F-16：`[DONE]` 是**解析终点**——其后的数据一律不采信。否则一个
    「先 [DONE]、后补一个别的 id」的响应会把结束标记之后的 id 拿来做落点证明。
    """
    first = None
    uniq = []
    for line in _as_text(raw_text).splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break                       # F-16：结束标记之后的事件不参与身份判断
        if not payload:
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue                        # 坏事件跳过，不拿它当 id 来源
        if not isinstance(obj, dict):
            continue
        cid = obj.get("conversation_id")
        if not isinstance(cid, str) or not cid.strip():
            continue
        cid = cid.strip()
        if first is None:
            first = cid
        if cid not in uniq:
            uniq.append(cid)
    return first, uniq


def _scan_mapping(raw_body):
    """扫描会话 mapping，返回 (rows, complete)。

    rows = [(ctime, role, text), ...] 按时间升序。complete=False 表示 body 里有
    畸形节点或不可用的时间戳——采到的是「尽力而为」，**不得当作完整 mapping**
    （第二十六轮审查口径：跳过坏 node ≠ 证明 mapping 完整）。
    """
    try:
        data = json.loads(raw_body)
    except Exception:
        return [], False
    if not isinstance(data, dict):
        return [], False
    mapping = data.get("mapping")
    if not isinstance(mapping, dict):
        return [], False
    complete = True
    rows = []
    for node in mapping.values():
        if not isinstance(node, dict):
            complete = False
            continue
        m = node.get("message")
        if m is None:
            continue                     # 非消息节点（根节点等）——正常
        if not isinstance(m, dict):
            complete = False
            continue
        author = m.get("author")
        if not isinstance(author, dict):
            complete = False
            continue
        content = m.get("content")
        if content is None:
            parts = []
        elif isinstance(content, dict):
            parts = content.get("parts")
            if parts is None:
                # 新会话（gpt-6）assistant 消息用 content_type=code，正文在 text 字段，
                # 没有 parts 列表 → 旧逻辑恒采到空正文（误判为「内容未生成」）。
                ct = content.get("content_type")
                if ct in ("code", "text") and isinstance(content.get("text"), str):
                    parts = [content["text"]]
                else:
                    parts = []
            elif not isinstance(parts, list):
                complete = False
                continue
        else:
            complete = False
            continue
        texts, structured, malformed = _split_parts(parts)
        if malformed:
            complete = False       # F-12a：非法 part → 内容会凭空消失，跳过该节点
            continue
        role = author.get("role")
        if structured and role == "assistant":
            complete = False       # F-19：assistant 回复含非文本内容 → 文本是投影，不是全文
            continue
        ok, ct = _as_ctime(m.get("create_time"))
        if not ok:
            complete = False             # 时间戳不可用 → 顺序不可信
        text = _assistant_text(texts)
        if role in ("user", "assistant") and text:
            rows.append((ct, role, text))
    rows.sort(key=lambda x: x[0])
    return rows, complete


def reply_digest(raw_body):
    """A12：把一条 assistant 消息的 parts 统计出来，用于核对「GET 全文是否完整」。

    返回 {message_id, parts, chars, sha256}；**核不了就返回 None**。
    为什么要有它：流被截断时 SSE 可能是半截，GET 回来的 mapping 也可能缺 part，
    只看「长度变长了」会把缺 part 的回复当完整——必须按 part 数与拼接 SHA-256 对账。

    降级姿态（第二十六轮审查口径）：这是**完整性证明器**，不是内容提取器。
    只要 body 里有一处畸形（mapping/node/message/author/content/parts 形状不对、
    或时间戳不可用），就**整体判定不可用 → None**；绝不能跳过坏 node 后拿剩余
    part 算一个「看起来正常」的摘要——那正好把缺失的 part 藏进一个可信的外观里。
    """
    try:
        data = json.loads(raw_body)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None            # F-12：解析成功但不是 dict → 核不了
    mapping = data.get("mapping")
    if not isinstance(mapping, dict):
        return None
    best = None
    # order = mapping 插入序号，作为 ct 相同时的 tiebreaker。
    for order, (node_id, node) in enumerate(mapping.items()):
        if not isinstance(node, dict):
            return None        # 有一个坏 node → 整体不可用（不得跳过）
        msg = node.get("message")
        if msg is None:
            continue            # 非消息节点（根节点等）——正常，不影响完整性
        if not isinstance(msg, dict):
            return None
        author = msg.get("author")
        if not isinstance(author, dict):
            return None
        content = msg.get("content")
        if content is None:
            parts = []
        elif isinstance(content, dict):
            parts = content.get("parts")
            if parts is None:
                parts = []
            elif not isinstance(parts, list):
                return None
        else:
            return None
        ok, ct = _as_ctime(msg.get("create_time"))
        if not ok:
            return None        # 时间戳不可用 → 顺序不可信，核不了
        # D-01（第31轮审查）：**所有角色**先做 part 合法性检查——文档承诺
        # 「body 里有一处畸形就整体 None」，user 节点也不能例外。旧版把
        # `_split_parts` 放在 assistant 判断之后，user 的非法 part 被 continue
        # 跳过，摘要仍对一份含畸形节点的 mapping 出具可信外观。
        # 注意：用户**合法结构化 part**（多模态）不算畸形，照常放行（F-19）。
        texts, structured, malformed = _split_parts(parts)
        if malformed:
            return None    # 任何节点的非法 part → 摘要不再对应真实内容
        if author.get("role") != "assistant":
            continue        # 非 assistant 节点不参与对账（含用户多模态消息，F-19）
        if structured:
            return None    # F-19：assistant 消息含非文本内容 → 核不了全文
        if not texts or not _assistant_text(texts):
            continue
        # F-27（第34轮终检自检）：选「最新 assistant」的口径必须与
        # harvest_mapping_checked 完全一致。后者经 _scan_mapping 做
        # `rows.sort(key=ct)`（**稳定排序**）再 reversed 取第一个 assistant，
        # 即：ct 最大者；**ct 相同则取 mapping 插入序最后的那个**。
        # 旧版这里用 (ct, str(id)) 当 key，ct 相同时按 id 字典序选——与
        # 插入序无关。多条 assistant 共用 ct（如都缺 create_time→归 0）时，
        # digest 选中 id 字典序最大者、harvest 选中插入序最后者，算出两个
        # 不同 sha → 「digest.sha == sha256(full)」永不成立 → 合法完整回复
        # 被误标「完整性未证明」（与 F-26 同为 F-24 门禁的口径分裂）。
        # 改用 (ct, order)：ct 最大、ct 同取插入序最后，与稳定排序对齐。
        key = (ct, order)
        if best is None or key > best[0]:
            best = (key, msg, texts)
    if best is None:
        return None
    _key, msg, texts = best
    # F-26：对账口径必须与 harvest_mapping_checked 落盘的 full 完全一致——
    # 两者都走 _assistant_text（拼接 + strip）。旧版 digest 算未 strip、full
    # 是 strip，合法回复首尾带空白时 sha/chars 永不相等，完整回复被误标不完整。
    text = _assistant_text(texts)
    return {"message_id": msg.get("id") or "",
            "parts": [len(p) for p in texts],
            "chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def _load_json(path, default):
    """读状态 json。文件损坏时隔离为 .corrupt-<ts> 并告警，而不是静默返回空。

    静默返回空最危险的地方在于：下一次 _save_json 会把原来的内容永久覆盖掉
    （conv-accounts 最多 500 条归属记录会一次性清空）。
    「损坏」含两种（F-34）：语法坏（JSONDecodeError）与**顶层类型坏**（合法
    JSON 但不是预期形状，如手改成 `[]`/`"x"`）。两者同等隔离重建——否则
    同一份不可信输入两种待遇：conv_owner 对非 dict 有守卫，record_conv 没有。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if type(data) is not type(default):
            raise ValueError("顶层类型不符：期望 %s，实际 %s"
                             % (type(default).__name__, type(data).__name__))
        return data
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, ValueError) as e:   # JSONDecodeError 是 ValueError 子类
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


# ---------- 状态文件锁：线程级互斥 + 跨进程文件锁 ----------
# 三个 json 都是「读-改-写」，并发不加锁会互相覆盖（比如两个发送器同时推进游标，
# 就会把均分打成 2:0）。
# 两层保护：
#   1. 同进程多线程 → threading.Lock（零 I/O，不超时不死锁）；
#   2. 跨进程 → O_EXCL 锁文件（带 stale 检测）。
# 只靠文件锁在 Windows 上高竞争下极慢（O_EXCL + fsync + os.remove 每次都碰磁盘），
# 4 线程 × 20 次读-改-写能把 strict 3s 超时全部吃满。
# 重入保护按线程记（_LOCK_HELD）；线程锁天然可重入由 _LOCK_HELD 短路。
_LOCK_HELD = {}            # path -> 持有者线程 ident（重入短路）
_THREAD_LOCKS = {}         # path -> threading.Lock（同进程互斥）
_THREAD_LOCKS_GUARD = threading.Lock()
LOCK_STALE_SEC = 60        # 锁文件超过这个年龄视为残留（进程被杀）
LOCK_TIMEOUT_SEC = 3.0     # 临界区都在毫秒级；等 3s 足够，等 8s 只会在拥塞时放大停顿


def _thread_lock_for(path):
    """取（或建）path 对应的线程锁。建表操作本身用全局锁保护。"""
    with _THREAD_LOCKS_GUARD:
        lk = _THREAD_LOCKS.get(path)
        if lk is None:
            lk = threading.Lock()
            _THREAD_LOCKS[path] = lk
        return lk


PROC_DEAD = "DEAD"        # 确认进程不存在
PROC_ALIVE = "ALIVE"      # 确认进程存活
PROC_UNKNOWN = "UNKNOWN"  # 无权限 / 查询失败 / 身份无法确认


def _win_k32():
    """带显式原型声明的 kernel32 句柄（F-06）。

    ctypes 默认 restype 是 c_int（32 位），而 Windows HANDLE 是指针宽度——
    64 位下 OpenProcess 返回的句柄会被截断，后续 GetExitCodeProcess /
    CloseHandle / GetProcessTimes 全部拿到坏句柄：查询失败 → 被折叠成
    「进程不存在」→ 活锁被误删。必须显式声明 HANDLE/BOOL/DWORD 原型。
    """
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE,
                                    ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME)]
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _win_open_error_to_state(err):
    """OpenProcess 失败原因分桶（F-03）：明确不存在 → DEAD，其余 → UNKNOWN。

    ERROR_INVALID_PARAMETER(87) / ERROR_NOT_FOUND(1168) = 进程不存在；
    ERROR_ACCESS_DENIED(5) 等 = 进程可能存在但查不了——那是 UNKNOWN，不是死亡。
    """
    if err in (87, 1168):
        return PROC_DEAD
    return PROC_UNKNOWN


def _parse_pid(pid):
    """严格解析 pid（F-07）：只认「正整数语义」的输入，其余一律 None。

    为什么不能用 int()：int(999999.9) == 999999、int(True) == 1——把非法身份
    折叠成合法 pid 再去查死活，等于替伪造数据开了一条回收通道（租约里写
    999999.9，查的是 999999 死不死，两回事）。收敛统一走 _as_int（F-34）：
      - int 且不是 bool（True 不是 pid 1）；
      - 纯十进制数字串（"4242" 可以，" 4242"/"42.2"/"0x10" 不行）。
    float、bool、None、垃圾串一律 None → 调用方走 UNKNOWN。
    """
    v = _as_int(pid, None)
    return v if (v is not None and v > 0) else None


def _proc_state(pid):
    """进程存活三态判定（F-03）：DEAD / ALIVE / UNKNOWN。

    为什么三态：旧版 `_proc_alive` 只返回 bool，把「进程不存在」与「无权限查询、
    系统调用失败」折叠成同一个 False。`_lock_holder_stale` 一看 False 就判死回收，
    于是「查不到」被当成「死了」——活锁会被误删，跨进程互斥失效（第十六轮审查 F-03）。
    现在只有确认 DEAD 才允许回收；UNKNOWN 一律按「可能还活着」保守处理。

    返回值语义：
      DEAD    —— 系统明确说进程不存在（ESRCH / ERROR_INVALID_PARAMETER 等）；
      ALIVE   —— 系统确认进程存活（后续还要比对启动时间防 PID 复用）；
      UNKNOWN —— 无权限（EPERM / ERROR_ACCESS_DENIED）、查询失败、pid 非法
                 （F-07：bool/float/垃圾串不得借 int() 折叠成合法 pid）。
    """
    pid = _parse_pid(pid)
    if pid is None:
        return PROC_UNKNOWN   # 身份不可解析 ≠ 死亡
    try:
        if os.name == "nt":
            import ctypes
            k32 = _win_k32()                      # F-06：显式 HANDLE 原型，防 64 位截断
            h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return _win_open_error_to_state(ctypes.get_last_error())
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return PROC_UNKNOWN               # 查询失败 ≠ 死亡
                return PROC_ALIVE if code.value == 259 else PROC_DEAD   # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        import errno
        try:
            os.kill(pid, 0)
            return PROC_ALIVE
        except OSError as e:
            # ESRCH=明确不存在；EPERM=存在但无权限信号（POSIX 语义），其余同 UNKNOWN
            return PROC_DEAD if e.errno == errno.ESRCH else PROC_UNKNOWN
    except Exception:
        return PROC_UNKNOWN   # 意外异常同样不能当死亡证据


def _proc_start_time(pid):
    """进程启动时间（Windows 用 FILETIME 整数，POSIX 用 /proc 的 st_mtime）。

    读不到返回 None —— 调用方必须把 None 当「身份不可确认」，禁止据此回收锁。
    """
    pid = _parse_pid(pid)   # F-07：同款严格解析，不给 int() 折叠留通道
    if pid is None:
        return None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = _win_k32()                      # F-06：同款显式原型
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


def _parse_start_time(value):
    """验证存储的 process_start_time 合法性（F-10）。

    返回 (valid, normalized_value)。
    None       → (True, None)   旧格式兼容，视为未记录启动时间。
    bool/str/非 int|float/NaN/±inf/负数 → (False, None)  字段损坏——不得当
                  pid_reused 证据，调用方应按 identity_unreadable 处理（禁止回收）。
    非负 int / 有限非负 float → (True, value)  原值返回。**不转 float**——
                  Windows FILETIME 是 17 位大整数，float() 丢精度会把同一个值
                  判成不等（误报 pid_reused）。
    """
    if value is None:
        return True, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, None
    if isinstance(value, float):
        # 拒 NaN / ±inf / 负数——都不是合法的进程启动时间戳
        if value != value or value in (float("inf"), float("-inf")) or value < 0:
            return False, None
    elif value < 0:
        return False, None
    return True, value


def _write_all(fd, data):
    """完整写入，处理短写（F-09）。返回写入字节数；失败抛 OSError。"""
    written = 0
    while written < len(data):
        n = os.write(fd, data[written:])
        if n <= 0:
            raise OSError("write returned %d" % n)
        written += n
    return written


def _rename_no_overwrite(src, dst):
    """目标不覆盖的归档移动（R7 不覆盖，F-11）——不是原子重命名。

    Windows: os.rename 天然拒绝覆盖已存在目标（单步原子）。
    POSIX:   os.link + os.remove 两步——硬链接成功后、删除源前崩溃会留下两个
             路径指向同一 inode（不丢数据、不覆盖归档，但不是单步原子）。
    目标已存在时抛 FileExistsError。src/dst 必须同盘（归档名派生自原路径，恒满足）。
    """
    if os.name == "nt":
        os.rename(src, dst)
    else:
        os.link(src, dst)
        os.remove(src)


def _lock_holder_stale(lock):
    """锁能否回收：租约过期 + 原持有者确认不在（或 PID 已被复用）。

    判定顺序（A22）：
      0. 读取失败（OSError）→ **不可回收**（F-08：权限/瞬时读错误 ≠ 旧格式）；
         非空但 JSON 解析失败 / 顶层不是字典 → **不可回收**（F-08：损坏内容
         不得借 mtime 兼容路径逃生）；
         空文件/纯空白 → 老格式锁的字面形态（旧实现 O_EXCL 建锁后不写身份），
         走 mtime 判据（LOCK_STALE_SEC 内视为正在写身份的新锁）。**注意**：
         mtime 兼容是历史格式的退化恢复机制，不具备三态身份验证的同等保证
         （第十九轮审查口径：不得把两者等同描述）；
      0'. 内容为 `{"released": true}`（持有者释放时删不掉锁文件，主动改写的
         终态，见 `_release_lock_file`）→ **可回收**，与年龄/存活无关；
      1. 成功解析的字典里没有 pid **字段**（真·老格式）→ 退化为 mtime 判据；
         pid 字段**存在但假值/非法**（0/None/空串/float/bool/垃圾串）→ 身份损坏，
         按 UNKNOWN 处理，**不回收**（F-04/F-07：假值 pid 不得借 mtime 逃生）；
      2. 原 PID 确认不存在（DEAD）→ **可回收，与年龄无关**——LOCK_TIMEOUT_SEC(3)
         比 LOCK_STALE_SEC(60) 小一个数量级，按年龄先卡的话，崩在 60 秒内留下的
         锁任何调用方都等不到可回收的那一刻（等于永久死锁）；
      3. UNKNOWN（无权限/查询失败）→ **不可回收**（F-03：查不到 ≠ 死了）；
      4. 存活但启动时间读不到 → **不可回收**（宁可等超时，也不误删活锁）；
      5. 存活但启动时间与记录不一致 → PID 被复用，可回收；
      6. 否则不可回收。

    F-31b：`lock` 必须是路径字符串。`open(True)` 在 Python 里等价于 `open(1)`
    ——**把 stdout 当文件读**（fuzz 时实测会挂住读终端）；`open(10**400)` 抛
    `OverflowError`（既不是 OSError 也不是 ValueError）。本函数的契约是路径，
    入口先验类型，不合契约一律「不可回收」（保守，绝不动锁）。
    """
    if not isinstance(lock, (str, bytes, os.PathLike)):
        return False
    try:
        with open(lock, "r", encoding="utf-8") as f:
            raw = f.read()
    except (OSError, ValueError, OverflowError):
        # F-08：读不出内容（权限/瞬时错误/非法 UTF-8 均属读取失败），保守不回收。
        # UnicodeDecodeError 是 ValueError 子类，必须在这里拦住，否则异常会穿过
        # _try_reclaim_stale 炸掉 _file_lock，而不是走「不可回收」路径。
        return False
    if not raw.strip():
        # 老格式锁的字面形态：空文件（旧实现建锁后不写身份）。超龄才算残留。
        try:
            return time.time() - os.path.getmtime(lock) > LOCK_STALE_SEC
        except OSError:
            return False
    try:
        info = json.loads(raw)
    except ValueError:
        return False            # F-08：非空损坏内容 = 新格式损坏，保守不回收
    if not isinstance(info, dict):
        return False            # F-08：非字典内容保守不回收
    if info.get("released"):
        # 持有者已放弃锁但删不掉文件（Windows 共享冲突），见 _release_lock_file。
        # 与年龄、pid 无关：立刻允许回收，否则残留的活 pid 会造成永久死锁。
        return True
    if "pid" not in info:
        # 字典但无 pid 字段（老格式）→ mtime 判据
        try:
            return time.time() - os.path.getmtime(lock) > LOCK_STALE_SEC
        except OSError:
            return False
    pid = info.get("pid")
    # 死活先于年龄：确认 DEAD 的持有者不会因为锁只创建了几秒就「还年轻」而
    # 不可回收（LOCK_TIMEOUT_SEC 3s < LOCK_STALE_SEC 60s，先看年龄 = 崩在窗口内
    # 的锁永远等不到）。年龄已不参与判定：UNKNOWN 本来就禁止回收，ALIVE 只看
    # 启动时间是否一致。
    # F-03：只有确认 DEAD 才可回收。UNKNOWN（无权限查询/系统调用失败）绝不当死亡
    # ——「查不到」不是「死了」，UNKNOWN 回收活锁 = 跨进程互斥失效。
    state = _proc_state(pid)
    if state == PROC_DEAD:
        return True
    if state == PROC_UNKNOWN:
        return False
    cur = _proc_start_time(pid)
    if cur is None:
        return False
    # F-10：存储的启动时间必须先验格式——损坏值（"corrupted"/bool/dict/NaN）
    # 不得当 pid_reused 证据，否则存活进程的锁会被误回收。
    stored_ok, stored = _parse_start_time(info.get("process_start_time"))
    if not stored_ok:
        return False
    return stored is not None and stored != cur


class LockTimeout(RuntimeError):
    """抢锁超时。strict 模式下必须抛出，绝不「跳过加锁继续跑」。"""


def _kernel_lock(fd, timeout):
    """内核文件锁（Windows: msvcrt.locking / POSIX: fcntl.flock）。

    为什么需要它（F-02）：stale 锁回收的「判定」与「删除」若不互斥，两个进程
    同时判定可回收 → 先删后建的新锁会被迟到者按旧判定删掉，跨进程互斥失效。
    回收互斥不能再用 O_EXCL 锁文件（其自身的 stale 回收就是同一个坑，递归无解），
    而内核锁在持有进程死亡时自动释放——天然没有 stale 问题。
    """
    end = time.time() + timeout
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.time() >= end:
                return False
            time.sleep(0.01)


def _kernel_unlock(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _try_reclaim_stale(lock):
    """回收确认已死的残留锁。判定与删除必须同处内核锁守卫内（F-02）。

    守卫文件（lock + '.reclaim'）**永不删除**：它是内核锁的载体，删了重建会
    让两个进程各锁一个 inode，互斥悄悄失效。空文件常驻无害。
    拿不到守卫（或平台不支持内核锁）→ 不回收，宁可等超时也不冒险删活锁。
    """
    if not isinstance(lock, (str, bytes, os.PathLike)):
        return False            # F-31b：契约是路径，非路径一律不回收
    guard = lock + ".reclaim"
    try:
        fd = os.open(guard, os.O_CREAT | os.O_RDWR)
    except OSError:
        return False
    try:
        if not _kernel_lock(fd, timeout=min(LOCK_TIMEOUT_SEC, 1.0)):
            return False
        try:
            # 守卫内重新判定：迟到的回收者此刻看到的是「先删后建」的新锁（活锁），
            # 判定为不可回收 —— 这正是 F-02 的修复点，绝不能按守卫外的旧判定删。
            if not _lock_holder_stale(lock):
                return False
            try:
                os.remove(lock)
            except OSError:
                return False
            return True
        finally:
            _kernel_unlock(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _release_lock_file(lock):
    """释放时删除锁文件；删不掉（Windows 共享冲突）就改写成「已释放」终态。

    为什么不能只 `except OSError: pass`：Windows 上只要还有任何未关闭的句柄
    （`_lock_holder_stale` 每次抢锁失败都会 open(lock) 读身份），os.remove 就抛
    WinError 32（errno=EACCES）。fd 已关闭，没有别的删除手段；静默放弃会把一把
    「活 pid + 匹配启动时间」的锁永久留在盘上——判定永远为活，之后每个抢锁者
    都要等满 timeout 抛 LockTimeout（跨进程永久死锁，不是等 60 秒就好）。

    重试只覆盖「等待者恰好在这几毫秒里读完」的窗口；仍然删不掉就写入
    `{"released": true}`（读共享允许写），让任何抢锁者立即回收。写完后**不再
    碰这个路径**：此刻别的抢锁者可能已删旧建新，再删就会删掉别人的新锁。
    """
    for attempt in range(4):
        if attempt:
            time.sleep(0.01)          # 首次直删；冲突时最多再等 30ms
        try:
            os.remove(lock)
            return
        except OSError:
            pass
    marked = False
    if os.path.exists(lock):
        try:
            with open(lock, "w", encoding="utf-8") as f:
                f.write('{"released": true}')
            marked = True
        except OSError:
            pass
    if not marked and os.path.exists(lock):
        # 连标记都写不进去（只读/无权限/被独占）：锁会卡死直到有人手工清掉，
        # 必须让这件事可见，不能又是静默 pass。
        print("WARN: 锁文件无法删除也无法标记已释放（%s）" % os.path.basename(lock),
              flush=True)


@contextlib.contextmanager
def _file_lock(path, timeout=LOCK_TIMEOUT_SEC, strict=True):
    """默认 strict=True（fail-closed）：超时抛 LockTimeout，绝不「跳过加锁继续跑」。

    strict=False 仅限测试基建的宽松探测路径；业务代码误用请在评审里拦下——
    宽松模式超时后无锁进临界区，等价于裸奔（事故库 A22 / F-02 反复证明过）。
    """
    me = threading.get_ident()
    if _LOCK_HELD.get(path) == me:  # 同线程重入：直接放行（避免自锁）
        yield
        return
    # 第一层：同进程线程互斥（零 I/O，O(1)，不会因 Windows fsync 排队）
    tlock = _thread_lock_for(path)
    if not tlock.acquire(timeout=timeout):
        if strict:
            raise LockTimeout("线程锁等待超时（%s）" % os.path.basename(path))
        print("WARN: 线程锁等待超时，跳过加锁（%s）" % os.path.basename(path), flush=True)
        yield
        return
    # 第二层：跨进程文件锁（O_EXCL）
    lock = path + ".lock"
    end = time.time() + timeout
    fd = None
    try:
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                # 锁里写持有者身份：只靠 mtime 会在 PID 复用/长任务时误判（A22）
                # F-09：身份必须完整写入才算加锁成功。短写/OSError 都不得进临界区
                # ——空锁会走 mtime 兼容被其他进程回收，等于没锁。
                payload = json.dumps({
                    "pid": os.getpid(),
                    "process_start_time": _proc_start_time(os.getpid()),
                    "created_at": time.time(),
                    "thread": me,
                    "path": os.path.basename(path),
                }).encode("utf-8")
                try:
                    _write_all(fd, payload)
                except OSError as e:
                    # 清理半成品锁，绝不把空锁/残锁留给 mtime 兼容路径
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    fd = None
                    try:
                        os.remove(lock)
                    except OSError:
                        pass
                    raise LockTimeout("锁身份写入失败（%s）：%s" % (os.path.basename(path), e))
                break
            except FileExistsError:
                # F-02：判定与删除必须同处内核锁守卫内。旧代码「_lock_holder_stale()
                # 判定 → os.remove()」两步间，另一进程可能已删旧建新；迟到者会把
                # **新锁**按旧判定删掉，跨进程互斥失效。
                if _try_reclaim_stale(lock):
                    continue
                if time.time() >= end:
                    if strict:
                        raise LockTimeout("文件锁等待超时（%s）" % os.path.basename(path))
                    print("WARN: 状态文件锁等待超时，跳过加锁（%s）" % os.path.basename(path), flush=True)
                    break
                time.sleep(0.01)   # 10ms 轮询：高频读-改-写时 100ms 会错过释放窗口
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
                _release_lock_file(lock)
    finally:
        tlock.release()


def _save_json(path, data, timeout=LOCK_TIMEOUT_SEC):
    """锁内原子写。抢不到锁绝不无锁写（strict=True，对齐 B8/B15 fail-closed 口径）。

    超时抛 LockTimeout——调用方必须把 LockTimeout 当「本次写入未落盘」处理。
    不做重试：写入是单步操作，调用方有 _update_json 的重试兜底；独立调用方自行决定重试。
    """
    with _file_lock(path, timeout=timeout, strict=True):
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
    until = _as_float(info.get("lease_until"), 0.0)   # F-30：OverflowError 不得穿出
    if now < until:
        return False, "lease_valid"
    # F-03：只有确认 DEAD 才算 process_gone；UNKNOWN（查不到）禁止回收
    state = _proc_state(info.get("pid"))
    if state == PROC_UNKNOWN:
        return False, "identity_unreadable"
    if state == PROC_DEAD:
        return True, "process_gone"
    cur = _proc_start_time(info.get("pid"))
    if cur is None:
        return False, "identity_unreadable"
    # F-10：存储的 process_start_time 先验格式。损坏值（bool/str/dict/NaN）
    # 不得当 pid_reused 证据——那等于把「身份不可读」错误升级成「允许回收」。
    stored_ok, stored = _parse_start_time(info.get("process_start_time"))
    if not stored_ok:
        return False, "identity_unreadable"
    if stored is not None and stored != cur:
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
    except OSError as e:
        # F-32：写租约失败 → 没拿到租约。返回 None 让调用方按 LEASE_BUSY 退出，
        # 绝不带着「以为领到了」的状态继续跑（那会绕过 B15 的单实例保证）。
        _diag("写租约失败，不启动（按冲突处理）", path, e)
        return None
    return run_id


def lease_heartbeat(path, run_id, lease_sec=LEASE_SEC, lock_timeout=LOCK_TIMEOUT_SEC):
    """续期（B15：每 15s 一次）。返回 False 说明租约已不属于自己（或抢不到锁）。

    F-32：文档承诺「返回 bool」，所以**任何**失败都必须落成 False——
    旧版只 catch LockTimeout，`_atomic_write` 的 OSError（磁盘满/权限/只读盘）
    会直接穿出去。心跳线程里若没包 try 就是线程静默死亡（租约悄悄过期）；
    包了 try 也只是把异常吞掉，调用方仍拿不到「本次续期失败」这个信息。
    """
    try:
        return _lease_heartbeat_locked(path, run_id, lease_sec, lock_timeout)
    except LockTimeout:
        print("WARN: 心跳抢不到租约锁，本次续期失败（按不属于自己处理）", flush=True)
        return False
    except OSError as e:
        _diag("心跳写入失败，本次续期失败（租约可能因此过期）", path, e)
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
    """主动释放：只有持有者能释放。抢不到锁 → 不释放（返回 False，让调用方记 orphan）。

    F-32：同 `lease_heartbeat`——承诺返回 bool，OSError 不得逃逸。
    释放失败按「租约仍在」处理（调用方记 TIMEOUT_ORPHAN），这是保守方向：
    宁可留僵尸租约等人工闸口，也不谎报已释放。
    """
    try:
        return _lease_release_locked(path, run_id, lock_timeout)
    except LockTimeout:
        print("WARN: 释放租约抢不到锁，保留租约（按 TIMEOUT_ORPHAN 处理）", flush=True)
        return False
    except OSError as e:
        _diag("释放租约失败，保留租约（按 TIMEOUT_ORPHAN 处理）", path, e)
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
    """按 B15 口径回收。返回 (是否回收, reason)。抢不到锁 → 不回收（lock_timeout）。

    F-32：承诺返回 (bool, 稳定枚举)，OSError 不得逃逸——回收是**自动化巡检**
    的入口，异常穿出去会让巡检脚本崩掉整轮，而不是跳过这一个租约继续看下一个。
    写入失败时返回 (False, "write_failed")：明确「没回收成」，不是「回收了但没写」。
    """
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
    except OSError as e:
        _diag("回收租约失败，保留租约", path, e)
        return False, "write_failed"


FORCE_RELEASE_REASONS = (
    "absent", "no_approval", "approval_incomplete", "unreadable",
    "sha256_mismatch", "process_alive", "identity_unconfirmed",
    "audit_registration_failed", "quarantine_failed", "lock_timeout",
    "quarantined",
)


def _diag(what, path, detail):
    """诊断细节只进日志，绝不拼进返回值。

    为什么：reason 是给机器做等值比较的**稳定枚举**。一旦写成
    "quarantine_failed:WinError 32"，调用方既没法用 `== "quarantine_failed"` 分流，
    也把内部异常文本泄进了对外契约——换个 Python 版本错误消息变了，判定就跟着变。
    """
    print("WARN: %s（%s）：%s" % (what, os.path.basename(path), detail), flush=True)


def lease_force_release(path, approval_path):
    """B15-R：损坏租约的人工恢复闸口（R1–R8）。

    租约读不懂就永不自动回收 → 可能永久占位。解法不是加 TTL（年龄不等于死亡），
    而是留一条**需要人明确批准、且留痕**的通道。批准文件字段：
        {"sha256": "<损坏租约的哈希>", "approved_at": "<ISO8601>",
         "approver": "<谁批的>", "reason": "<为什么批>"}
    返回 (ok, reason)。reason 只能是 FORCE_RELEASE_REASONS 之一（稳定枚举，
    不含异常文本；细节走 _diag 落日志）：
        absent / no_approval / approval_incomplete / unreadable / sha256_mismatch /
        process_alive / identity_unconfirmed / audit_registration_failed /
        quarantine_failed / lock_timeout / quarantined
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
    if (not want or not approval.get("approved_at")
            or not approval.get("approver") or not approval.get("reason")):
        return False, "approval_incomplete"
    try:
        got = _sha256_file(path)
    except OSError as e:
        _diag("租约存在但读不出哈希，无法核对批准的是不是同一个文件", path, e)
        return False, "unreadable"
    if got != want:
        return False, "sha256_mismatch"
    # R3/R4：能读出 pid 且进程还活着 → 不许恢复（文件坏了不代表进程死了）。
    # F-03/F-05：三态判定对可读租约**无条件执行**——pid 缺失/假值同样落入
    # UNKNOWN（_proc_state 对 None/0/不可解析都返回 UNKNOWN），不得用 if pid
    # 隐式短路：那会让身份不完整的可读租约只凭 approval 就跳过证据要求。
    # UNKNOWN 不是死亡证据，走 identity_unconfirmed 同款口径（必须有独立确认依据），
    # 绝不因「查不到」放行。
    info, readable = _read_ephemeral(path)
    if readable:
        state = _proc_state(info.get("pid"))
        if state == PROC_ALIVE:
            return False, "process_alive"
        if state == PROC_UNKNOWN:
            if not str(approval.get("identity_evidence") or "").strip():
                return False, "identity_unconfirmed"
    else:
        # 身份读不出来 ≠ 原实例已死。必须由批准人给出独立确认依据（例如进程表快照、
        # 子进程检查、B7 状态核对结果），否则拒绝——不能让「批准」替「证据」。
        if not str(approval.get("identity_evidence") or "").strip():
            return False, "identity_unconfirmed"
    # R1：**前置检查全部通过后**才登记恢复事件。先登记后检查会在「被拒绝」时留下
    # 「已批准恢复」的假记录，审计上等于谎报一次恢复。
    if _record_corruption("lease-recovery", path, "approved_recovery") is None:
        return False, "audit_registration_failed"
    bad = "%s.corrupt-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    # F-11 / R7：隔离归档绝不覆盖已有归档。os.replace 会覆盖目标——旧归档
    # （同一秒二次恢复/预存同名文件）会被静默销毁。改用 _rename_no_overwrite，
    # 冲突时追加序号找唯一名。
    n = 0
    while True:
        try:
            _rename_no_overwrite(path, bad)
            break
        except FileExistsError:
            n += 1
            if n > 100:
                _diag("归档名冲突重试超限", path, bad)
                _record_corruption("lease-recovery-failed", path, "quarantine_failed")
                return False, "quarantine_failed"
            bad = "%s.corrupt-%s.%d" % (path, time.strftime("%Y%m%d-%H%M%S"), n)
        except OSError as e:
            _diag("租约隔离改名失败，原文件仍在", path, e)
            # 登记行已经写成「已批准恢复」，但隔离没成——必须补一条失败行，否则审计上
            # 只看到 approved_recovery、看不到恢复其实没落地，等于谎报一次已完成的恢复。
            _record_corruption("lease-recovery-failed", path, "quarantine_failed")
            return False, "quarantine_failed"
    # R8：隔离后确认原路径确实空出来了，别把别人的新租约当成自己的战果
    if os.path.exists(path):
        _diag("隔离改名返回成功但原路径仍存在", path, path)
        _record_corruption("lease-recovery-failed", path, "path_still_present")
        return False, "quarantine_failed"
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
    """释放槽位（只有持有者能释放）。F-37：非路径一律 False，不让 os.path 炸 TypeError。"""
    if not isinstance(slot_path, (str, bytes, os.PathLike)):
        return False
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


class RegistryShrinkRefused(RuntimeError):
    """注册表写入试图删除既有会话 ID（DECISION-143/144：删除阈值 0）。"""


class RegistryBaselineUnavailable(RuntimeError):
    """高水位基准缺失/损坏/不可写，跨进程保护失效，拒绝写入（DECISION-144 D2-C）。"""


def _baseline_path(path):
    return str(path) + ".highwater.json"


def _read_baseline(baseline_path):
    """返回 (ids_set, error)。error 非空即「存在但不可用」，属 D2-C。"""
    if not os.path.exists(baseline_path):
        return None, None                      # D2-A：不存在，交给调用方初始化
    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return None, "unreadable: %s" % exc
    if not isinstance(data, dict) or not isinstance(data.get("ids"), list):
        return None, "malformed: missing ids list"
    try:
        return set(safe_id(i, "baseline id") for i in data["ids"]), None
    except (TypeError, ValueError) as exc:
        return None, "malformed id: %s" % exc


def _write_baseline(baseline_path, ids):
    """原子落盘高水位。独立 tmp+replace，不占用「注册表每次 mutation 只写一次」的配额。"""
    tmp = baseline_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(
            {"ids": sorted(ids), "count": len(ids), "updated": _now()},
            f, ensure_ascii=False, indent=2,
        )
    os.replace(tmp, baseline_path)


def _probe_baseline_writable(baseline_path):
    """写前探针：确认基准可写。写不成就在动注册表之前失败（D2-C）。"""
    probe = baseline_path + ".probe"
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("{}")
        os.remove(probe)
    except OSError as exc:
        raise RegistryBaselineUnavailable(
            "baseline not writable (%s): %s" % (baseline_path, exc)
        )


def _registry_guard(path, before, after):
    """写前检查：既有 ID 一个都不许消失，且高水位基准必须可用（DECISION-144 D2）。

    单次写入保护（第一层）：before/after 的 ID 集合差。
    跨进程保护（第二层）：对照高水位基准，防止别的进程已经提前删掉。

    D2 四态：
      A 基准缺失 → 用当前注册表初始化（不初始化成空集），然后放行
      B 基准有效 → 正常比对
      C 基准损坏/不可写 → RegistryBaselineUnavailable，拒绝 mutation
      D 基准领先注册表（ids - after 非空）→ 拒绝，不自动降级；
        人工核验后用 CHATGPT_REGISTRY_HIGHWATER_RECONCILE=1 重建基准

    提交顺序：本函数只做检查与初始化，基准推进由 _registry_commit_baseline()
    在注册表真正落盘之后执行——保证基准永不领先于磁盘上的注册表。
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return
    if os.path.basename(str(path)) != "conv-accounts.json":
        return  # 只管注册表本体，轮转/匿名等状态文件不受此约束

    lost = set(before) - set(after)
    if lost:
        raise RegistryShrinkRefused(
            "registry write would delete %d existing conversation id(s): %s"
            % (len(lost), sorted(lost)[:5])
        )

    baseline_path = _baseline_path(path)
    known, error = _read_baseline(baseline_path)

    if error:
        reason = _reconcile_reason()
        if reason is None:
            raise RegistryBaselineUnavailable(
                "baseline %s %s; set CHATGPT_REGISTRY_HIGHWATER_RECONCILE=1 "
                "(and CHATGPT_REGISTRY_RECONCILE_REASON) after manual verification "
                "to rebuild" % (baseline_path, error)
            )
        _append_registry_audit(path, {
            "result": "attempted",
            "basis": reason,
            "cause": "baseline_unavailable: %s" % error,
            "baseline_before": {"path": baseline_path,
                                "sha256": _sha256_or_none(baseline_path),
                                "count": None},
            "registry": {"count": len(after),
                         "sha256": _sha256_or_none(path)},
            "lost_ids": [],
            "git_sha": _git_sha(),
        })
        print("WARN: 高水位基准损坏（%s），按人工核验要求重建" % error, flush=True)
        _write_baseline(baseline_path, set(after))
        return

    if known is None:                          # D2-A 首次初始化
        if not after:
            raise RegistryBaselineUnavailable(
                "refusing to initialize baseline from an empty registry: %s"
                % baseline_path
            )
        _probe_baseline_writable(baseline_path)
        _write_baseline(baseline_path, set(after))
        return

    vanished = known - set(after)              # D2-D 基准领先
    if vanished:
        reason = _reconcile_reason()
        if reason is None:
            raise RegistryShrinkRefused(
                "registry lost %d previously committed id(s) (cross-process): %s; "
                "set CHATGPT_REGISTRY_HIGHWATER_RECONCILE=1 "
                "(and CHATGPT_REGISTRY_RECONCILE_REASON) after manual "
                "verification to rebuild the baseline"
                % (len(vanished), sorted(vanished)[:5])
            )
        # 审计先落盘再重建：中途崩溃也能查出「谁在什么时候放弃了哪些 ID」。
        _append_registry_audit(path, {
            "result": "attempted",
            "basis": reason,
            "cause": "baseline_ahead",
            "baseline_before": {"path": baseline_path,
                                "sha256": _sha256_or_none(baseline_path),
                                "count": len(known)},
            "registry": {"count": len(after), "sha256": _sha256_or_none(path)},
            "lost_ids": sorted(vanished),
            "git_sha": _git_sha(),
        })
        print(
            "WARN: 人工核验放行，重建高水位基准（丢弃 %d 个历史 ID）: %s"
            % (len(vanished), sorted(vanished)[:5]), flush=True,
        )
        _write_baseline(baseline_path, set(after))
        return

    _probe_baseline_writable(baseline_path)


def _registry_commit_baseline(path, after):
    """注册表落盘成功后推进高水位。失败不回滚注册表（基准滞后是安全方向），
    但必须显式报错，不能静默。"""
    if not isinstance(after, dict):
        return
    if os.path.basename(str(path)) != "conv-accounts.json":
        return
    try:
        _write_baseline(_baseline_path(path), set(after))
    except OSError as exc:
        raise RegistryBaselineUnavailable(
            "registry committed but baseline update failed: %s" % exc
        )


def _audit_path(path):
    return str(path) + ".audit.jsonl"


def _sha256_or_none(path):
    """读不到返回 None——审计里如实写 null，不编造哈希。"""
    try:
        return _sha256_file(path)
    except OSError:
        return None


def _git_sha():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=os.path.dirname(os.path.abspath(__file__)),
            # text=True 会按本机代码页解码（中文 Windows = cp936）；显式 UTF-8
            # 才能与本仓库的编码约定一致，坏字节按 replace 保留而不是抛异常。
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _append_registry_audit(path, record):
    """DECISION-145 Q2：高水位重建属数据安全动作，必须持久化留痕，不能只靠 stdout。

    追加式 JSONL + fsync：崩溃也不会丢已写记录，且永不被注册表写入覆盖。
    落盘失败不吞——审计写不成就不该声称「已留痕」。
    """
    record = dict(record)
    record.setdefault("ts_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    record.setdefault("op", "HIGHWATER_RECONCILE")
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with open(_audit_path(path), "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


_RECONCILE_CONSUMED = []


def _reconcile_reason():
    """一次性人工授权：读得到就消费掉，同进程第二次调用返回 None。

    DECISION-145：不得成为永久宽松模式。env 只能解释为「这一次核验后重建」，
    所以同一进程内用一次即失效；跨进程由基线重建本身消解（重建后不再有差集）。
    """
    if os.environ.get("CHATGPT_REGISTRY_HIGHWATER_RECONCILE") != "1":
        return None
    if _RECONCILE_CONSUMED:
        return None
    _RECONCILE_CONSUMED.append(True)
    return (os.environ.get("CHATGPT_REGISTRY_RECONCILE_REASON") or "").strip() \
        or "unspecified (CHATGPT_REGISTRY_RECONCILE_REASON not set)"


def registry_audit(path=None):
    """DECISION-145 Q3：只读一致性审计。绝不修改注册表、绝不修改基准、
    绝不把审计结果自动转成恢复动作。
    """
    path = path or CONV_MAP_FILE
    baseline_path = _baseline_path(path)
    res = {
        "registry_path": os.path.abspath(path),
        "baseline_path": os.path.abspath(baseline_path),
        "registry_ids": 0,
        "baseline_ids": None,
        "baseline_error": None,
        "missing_from_registry": [],   # baseline - registry：基准领先
        "new_in_registry": [],         # registry - baseline：基准滞后（安全方向）
        "consistency": "UNKNOWN",
        "mutation": "NONE",
        "recovery": None,
        "registry_sha256": _sha256_or_none(path),
        "baseline_sha256": _sha256_or_none(baseline_path),
    }
    try:
        with open(path, "r", encoding="utf-8") as f:
            reg = json.load(f)
        if not isinstance(reg, dict):
            raise ValueError("registry is not a JSON object")
    except (OSError, ValueError) as exc:
        res["consistency"] = "REGISTRY_UNREADABLE"
        res["baseline_error"] = "registry: %s" % exc
        return res
    reg_ids = set(reg)
    res["registry_ids"] = len(reg_ids)

    known, error = _read_baseline(baseline_path)
    if error:
        res["consistency"] = "BASELINE_UNREADABLE"
        res["baseline_error"] = error
        return res
    if known is None:
        res["consistency"] = "BASELINE_MISSING"
        return res

    res["baseline_ids"] = len(known)
    missing = sorted(known - reg_ids)
    new = sorted(reg_ids - known)
    res["missing_from_registry"] = missing
    res["new_in_registry"] = new
    if missing:
        res["consistency"] = "BASELINE_AHEAD"
        res["recovery"] = "MANUAL_REVIEW_REQUIRED"
    elif new:
        res["consistency"] = "REGISTRY_AHEAD"
    else:
        res["consistency"] = "CONSISTENT"
    return res


def format_registry_audit(res):
    """人读格式（裁决要求含完整差集，不能只给数量）。"""
    L = ["Registry Audit", "",
         "Registry:           %s" % res["registry_path"],
         "Baseline:           %s" % res["baseline_path"],
         "Registry IDs:       %d" % res["registry_ids"],
         "Baseline IDs:       %s" % ("n/a" if res["baseline_ids"] is None
                                     else res["baseline_ids"]), ""]
    if res["baseline_error"]:
        L.append("Error:              %s" % res["baseline_error"])
        L.append("")
    L.append("Baseline - Registry (missing): %d" % len(res["missing_from_registry"]))
    for i in res["missing_from_registry"]:
        L.append("  - %s" % i)
    L.append("")
    L.append("Registry - Baseline (new):     %d" % len(res["new_in_registry"]))
    for i in res["new_in_registry"]:
        L.append("  + %s" % i)
    L += ["",
          "Consistency:        %s" % res["consistency"],
          "Mutation:           NONE"]
    if res["recovery"]:
        L.append("Recovery:           %s" % res["recovery"])
    return "\n".join(L)


def _update_json(path, mutate, default, timeout=LOCK_TIMEOUT_SEC, retries=3):
    """锁内读-改-写，杜绝并发丢更新。mutate(data) 就地修改，返回 False 表示无需落盘。

    抢不到锁时绝不无锁进临界区（strict=True，对齐 B8/B15 fail-closed 口径）：
    非 strict 超时后「跳过加锁继续跑」会让多个线程同时读-改-写，互相覆盖 = 丢更新。
    短暂重试后仍失败才抛 LockTimeout——调用方必须把 LockTimeout 当「本轮状态未更新」处理。
    mutate 被设计为幂等（每次对全新加载的 data 调用），重试安全。
    落盘直接用 _atomic_write（不再嵌套 _save_json 的 _file_lock）——临界区已持锁。
    """
    last = None
    for _ in range(retries):
        try:
            with _file_lock(path, timeout=timeout, strict=True):
                data = _load_json(path, default)
                before = dict(data) if isinstance(data, dict) else data
                if mutate(data) is False:
                    return data
                _registry_guard(path, before, data)
                _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
                _registry_commit_baseline(path, data)
                return data
        except LockTimeout as e:
            last = e
            time.sleep(0.05)
    raise last


def _normalize_rotation(st):
    """把轮转状态收敛成合法形状，**永不抛异常**（F-34）。

    状态文件是不可信输入：语法坏由 _load_json 隔离，但「合法 JSON、字段类型坏」
    （cursor="abc" / created="x" / order=5）会原样穿进来。旧版直接
    `int(st.get("cursor") or 0)`、`st["created"].setdefault(...)`——ValueError /
    OverflowError / TypeError / AttributeError 一路穿出 load_rotation，调用方
    拿到的不是「可用状态」而是崩溃。且 `int(3.5)==3`、`int(True)==1` 违反 F-07
    「不许把非法折叠成合法」。
    现在逐字段收敛：形状不符就当「没有这个值」，绝不抛异常。
    """
    if not isinstance(st, dict):
        st = {}
    order = [a for a in _as_seq(st.get("order")) if a in ROTATION_ORDER]
    for a in ROTATION_ORDER:  # 保证全部身份齐全且顺序稳定
        if a not in order:
            order.append(a)
    st["order"] = order
    # 游标只在登录态身份之间推进（匿名是兜底，不参与均分）；
    # _as_int 不折叠 bool/float，垃圾游标归零而不是猜一个下标
    st["cursor"] = _as_int(st.get("cursor"), 0) % len(ROTATION_PRIMARY)
    created, failures = _as_dict(st.get("created")), _as_dict(st.get("failures"))
    for a in order:
        created[a] = _as_int(created.get(a), 0)
        failures[a] = _as_int(failures.get(a), 0)
    st["created"], st["failures"] = created, failures
    # unavailable：每项必须是 dict，数值字段走 _as_float（is_unavailable/
    # purge_expired/mark_unavailable 都按这个形状读写）
    clean = {}
    for k, v in _as_dict(st.get("unavailable")).items():
        info = _as_dict(v)
        clean[k] = {
            "reason": _as_text(info.get("reason")),
            "at": _as_text(info.get("at")),
            "epoch": _as_float(info.get("epoch"), 0.0),
            "ttl": _as_float(info.get("ttl"), UNAVAILABLE_TTL),
        }
    st["unavailable"] = clean
    st["fallback_uses"] = _as_int(st.get("fallback_uses"), 0)
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

    try:
        _update_json(ROTATION_FILE, mutate, {})
    except LockTimeout as e:
        # 锁超时 = 本轮状态未更新；返回 out（空或部分），调用方拿到的就是「没改上」的信号
        print("WARN: 轮转状态更新失败（锁超时，本轮状态未变更）：%s" % e, flush=True)
    return _normalize_rotation(out)


def save_rotation(st):
    st["updated"] = _now()
    try:
        _save_json(ROTATION_FILE, st)
    except LockTimeout as e:
        print("WARN: 轮转状态保存失败（锁超时，本次写入未落盘）：%s" % e, flush=True)


def mark_unavailable(st, account, reason, ttl=None):
    unav = _as_dict(st.get("unavailable"))
    unav[account] = {
        "reason": _as_text(reason), "at": _now(), "epoch": time.time(),
        "ttl": UNAVAILABLE_TTL if ttl is None else _as_float(ttl, UNAVAILABLE_TTL),
    }
    st["unavailable"] = unav


def is_unavailable(st, account):
    info = _as_dict(_as_dict(st.get("unavailable")).get(account))
    if not info:
        return False
    ttl = _as_float(info.get("ttl"), UNAVAILABLE_TTL)          # F-30
    return time.time() - _as_float(info.get("epoch"), 0.0) <= ttl


def purge_expired(st):
    """清掉过期的不可用标记（is_unavailable 只读，不再隐式改状态）。"""
    changed = False
    unav = _as_dict(st.get("unavailable"))
    for a in list(unav):
        info = _as_dict(unav.get(a))
        ttl = _as_float(info.get("ttl"), UNAVAILABLE_TTL)      # F-30
        if time.time() - _as_float(info.get("epoch"), 0.0) > ttl:
            unav.pop(a, None)
            changed = True
    if changed:
        st["unavailable"] = unav
    return changed


def rotation_plan(st):
    """登录态身份（acct1/acct2/acct3/acct4/acct5/acct6）从游标起顺次尝试；不可用的排到末尾（避免死锁）。
    匿名态不进入该计划——它只在**全部登录态**都 429 时由 anon_fallback_ready() 决定是否兜底。"""
    st = _as_dict(st)
    cur = _as_int(st.get("cursor"), 0)                          # F-34
    n = len(ROTATION_PRIMARY)
    seq = [ROTATION_PRIMARY[(cur + i) % n] for i in range(n)]
    return [a for a in seq if not is_unavailable(st, a)] + [a for a in seq if is_unavailable(st, a)]


def anon_fallback_block_reason(st, outcomes):
    """兜底放行条件：全部登录态**都**因 429（本次命中或 429 冷却中）失败。

    outcomes: {account: 状态码或 "SKIP:<原因>"}
    返回 None 表示放行；否则返回一句人话说明为什么没放行。
    """
    if os.environ.get("CHATGPT_ANON_FALLBACK", "1") == "0":
        return "CHATGPT_ANON_FALLBACK=0（匿名兜底被环境变量禁用）"
    if not outcomes:
        return "没有任何登录态尝试结果，不启用兜底"
    if not all("429" in str(v) for v in outcomes.values()):
        return "登录态并非全部 429（401=凭据失效、403=风控，都不该用匿名顶）"
    info = _as_dict(_as_dict(st.get("unavailable")).get("nologin"))
    if "manually disabled" in _as_text(info.get("reason")):
        return "nologin 已被手动禁用（--rotation-enable nologin 解封）"
    if is_unavailable(st, "nologin"):
        ttl = _as_float(info.get("ttl"), UNAVAILABLE_TTL)      # F-30
        left = int(ttl - (time.time() - _as_float(info.get("epoch"), 0.0)))
        return "匿名通道冷却中（%s），约 %d 秒后可重试" % (info.get("reason") or "unknown", max(left, 0))
    return None


def anon_fallback_ready(st, outcomes):
    return anon_fallback_block_reason(st, outcomes) is None


def atomic_write_conv_map(data, lock_held=False):
    """
    Persist the conversation registry atomically.

    Normal callers use _save_json(), which acquires the cross-process lock.
    Migration may pass lock_held=True after acquiring _file_lock itself.

    Never perform an unlocked write.
    """
    if not isinstance(data, dict):
        raise ValueError("conversation registry must be a dictionary")

    # DECISION-144 D3：任何写注册表的入口都必须过单调性守卫。
    # 调用方若已持锁（lock_held=True），这里只读不写，不会二次加锁。
    if lock_held:
        _registry_guard(CONV_MAP_FILE, _load_json(CONV_MAP_FILE, {}), data)
        _atomic_write(CONV_MAP_FILE, json.dumps(data, ensure_ascii=False, indent=2))
        _registry_commit_baseline(CONV_MAP_FILE, data)
    else:
        _save_json(CONV_MAP_FILE, data)


def _read_conv_map_for_migration():
    """
    Read the registry while its cross-process lock is held.

    Unlike the general-purpose _load_json(), this function refuses to
    replace a malformed registry with an empty dictionary.

    A corrupt source is preserved for operator diagnosis.
    """
    try:
        with open(CONV_MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}

    if not isinstance(data, dict):
        raise ValueError("conversation registry root must be an object")

    return data


def _normalize_conv_registry_record(conv_id, raw):
    """
    Return (normalized_record, diagnostic).

    Invalid entries are preserved by the caller.
    Valid dictionary metadata is preserved without interpretation.
    """
    try:
        validated_id = safe_id(conv_id, "conversation id")
    except (TypeError, ValueError) as exc:
        return None, "invalid conversation id: %s" % exc

    if validated_id != conv_id:
        return None, "conversation id requires normalization"

    if isinstance(raw, str):
        if not validate_account_identity(raw):
            return None, "invalid legacy account identity"

        return {
            "account": raw,
            "prompt": None,
            "ts": None,
        }, None

    if not isinstance(raw, dict):
        return None, "record must be an object or legacy account string"

    account = raw.get("account")

    if not validate_account_identity(account):
        return None, "invalid or missing account identity"

    # Preserve every existing field, including note and free-form owner.
    # Missing ts is historical absence, not a reason to invent a timestamp.
    return dict(raw), None


def migrate_conv_accounts():
    """
    Convert valid legacy string records into canonical dictionary records.

    Safety:
    - Acquires the existing cross-process lock.
    - Reads the latest registry inside the lock.
    - Preserves malformed records and reports diagnostics.
    - Preserves all valid existing dictionary metadata.
    - Writes only when an actual conversion is required.
    - Uses atomic replacement.
    - Is idempotent.

    Returns a bounded summary dictionary.
    """
    summary = {
        "status": "ok",
        "total": 0,
        "migrated": 0,
        "unchanged": 0,
        "invalid": [],
        "changed": False,
    }

    with _file_lock(CONV_MAP_FILE, timeout=LOCK_TIMEOUT_SEC, strict=True):
        data = _read_conv_map_for_migration()
        summary["total"] = len(data)

        normalized = dict(data)
        changed = False

        for conv_id, raw in data.items():
            record, diagnostic = _normalize_conv_registry_record(
                conv_id, raw
            )

            if diagnostic is not None:
                summary["invalid"].append({
                    "conv_id": str(conv_id)[:80],
                    "reason": diagnostic,
                })
                summary["unchanged"] += 1
                continue

            if isinstance(raw, str):
                normalized[conv_id] = record
                summary["migrated"] += 1
                changed = True
            else:
                summary["unchanged"] += 1

        if changed:
            atomic_write_conv_map(normalized, lock_held=True)

        summary["changed"] = changed

    return summary


def record_conv(conv_id, account, prompt_file):
    """
    Register a successfully created/used conversation.

    Ownership cannot be silently transferred to a different account.
    Existing unrelated metadata is preserved.
    """
    if not conv_id:
        return

    try:
        conv_id = safe_id(conv_id, "conversation id")
    except (TypeError, ValueError) as exc:
        print(
            "WARN: 拒绝登记非法 conv_id：%r (%s)" % (conv_id, exc),
            flush=True,
        )
        return

    if not validate_account_identity(account):
        print(
            "WARN: 拒绝登记非法 account identity：%r" % (account,),
            flush=True,
        )
        return

    try:
        account_identity = normalize_account_identity(account)

        def mutate(data):
            if not isinstance(data, dict):
                raise ValueError("conversation registry must be a dictionary")

            exists = conv_id in data
            existing = data.get(conv_id)

            if exists:
                if isinstance(existing, str):
                    existing_account = existing
                    existing_metadata = {
                        "account": existing,
                    }
                elif isinstance(existing, dict):
                    existing_account = existing.get("account")
                    existing_metadata = dict(existing)
                else:
                    print(
                        "WARN: 会话归属记录格式非法，拒绝覆盖：%s"
                        % conv_id,
                        flush=True,
                    )
                    return False

                if not validate_account_identity(existing_account):
                    print(
                        "WARN: 会话归属记录身份非法，拒绝覆盖：%s"
                        % conv_id,
                        flush=True,
                    )
                    return False

                owner = existing_metadata.get("owner")
                if validate_account_identity(owner):
                    if (
                        normalize_account_identity(owner)
                        != normalize_account_identity(existing_account)
                    ):
                        print(
                            "WARN: 会话归属 owner/account 冲突，拒绝覆盖：%s"
                            % conv_id,
                            flush=True,
                        )
                        return False

                if (
                    normalize_account_identity(existing_account)
                    != account_identity
                ):
                    print(
                        "WARN: 会话归属冲突，拒绝覆盖：%s "
                        "(registered=%s, requested=%s)"
                        % (conv_id, existing_account, account),
                        flush=True,
                    )
                    return False

            else:
                existing_metadata = None

            if existing_metadata is None:
                record = {
                    "account": account,
                    "prompt": prompt_file,
                    "ts": _now(),
                }
            else:
                # Keep the historical spelling of account (e.g. default)
                # and preserve all unrelated metadata.
                record = existing_metadata
                record["prompt"] = prompt_file
                record["ts"] = _now()

            data[conv_id] = record

        _update_json(CONV_MAP_FILE, mutate, {})

    except LockTimeout as exc:
        print(
            "WARN: 会话归属登记失败（锁超时，本次未写入）：%s"
            % exc,
            flush=True,
        )


def register_conversations_batch(registrations):
    """
    Register a batch of conversations in one locked atomic replacement.

    The function:
    - Acquires the same strict cross-process registry lock as record_conv.
    - Reads the latest registry only after acquiring the lock.
    - Validates every candidate before making any change.
    - Preserves existing records when ownership already matches.
    - Rejects legacy strings and malformed/conflicting existing records.
    - Writes at most once through _atomic_write.
    - Re-reads and verifies the committed registry before releasing the lock.

    Returns the verified registered list.

    Raises on any validation, lock, read, write, or verification failure.
    No partial batch is intentionally committed.
    """
    if not isinstance(registrations, (list, tuple)):
        raise TypeError("registrations must be a list or tuple")

    normalized_candidates = []
    candidate_accounts = {}

    for registration in registrations:
        if not isinstance(registration, dict):
            raise ValueError("registration must be a dictionary")

        try:
            conv_id = safe_id(
                registration.get("id"),
                "conversation id",
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "invalid conversation id in batch: %r"
                % (registration.get("id"),)
            ) from exc

        account = registration.get("account")
        if not validate_account_identity(account):
            raise ValueError(
                "invalid account identity in batch: id=%s account=%r"
                % (conv_id, account)
            )

        canonical_account = normalize_account_identity(account)

        previous_account = candidate_accounts.get(conv_id)
        if previous_account is not None:
            if previous_account != canonical_account:
                raise ValueError(
                    "duplicate batch id has conflicting accounts: %s"
                    % conv_id
                )
            # Identical duplicate input is deduplicated.
            continue

        candidate_accounts[conv_id] = canonical_account
        normalized_candidates.append({
            "id": conv_id,
            "account": account,
            "canonical_account": canonical_account,
        })

    with _file_lock(
        CONV_MAP_FILE,
        timeout=LOCK_TIMEOUT_SEC,
        strict=True,
    ):
        registry = _read_conv_map_for_migration()
        registry_before = dict(registry) if isinstance(registry, dict) else registry

        if not isinstance(registry, dict):
            raise ValueError(
                "conversation registry must be a dictionary"
            )

        # Validate the complete candidate set before changing the registry.
        additions = []

        for candidate in normalized_candidates:
            conv_id = candidate["id"]
            requested_account = candidate["account"]
            requested_canonical = candidate["canonical_account"]

            if conv_id not in registry:
                additions.append({
                    "id": conv_id,
                    "account": requested_account,
                })
                continue

            existing = registry[conv_id]

            # Stage 2 policy: legacy bare strings are not canonical
            # ownership records. They must go through migrate_conv_accounts.
            if isinstance(existing, str):
                raise ValueError(
                    "legacy string registry record requires migration: %s"
                    % conv_id
                )

            if not isinstance(existing, dict):
                raise ValueError(
                    "registry record format is invalid: %s" % conv_id
                )

            existing_account = existing.get("account")
            if not validate_account_identity(existing_account):
                raise ValueError(
                    "registry record account is invalid: %s" % conv_id
                )

            owner = existing.get("owner")
            if validate_account_identity(owner):
                if (
                    normalize_account_identity(owner)
                    != normalize_account_identity(existing_account)
                ):
                    raise ValueError(
                        "registry owner/account conflict: %s" % conv_id
                    )

            existing_canonical = normalize_account_identity(
                existing_account
            )
            if existing_canonical != requested_canonical:
                raise ValueError(
                    "registry account conflict: id=%s registered=%s "
                    "requested=%s"
                    % (conv_id, existing_account, requested_account)
                )

        # No registry mutation occurs until every candidate has passed.
        for item in additions:
            registry[item["id"]] = {
                "account": item["account"],
                "prompt": None,
                "ts": _now(),
            }

        if additions:
            _registry_guard(CONV_MAP_FILE, registry_before, registry)
            _atomic_write(
                CONV_MAP_FILE,
                json.dumps(
                    registry,
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            _registry_commit_baseline(CONV_MAP_FILE, registry)

        # Verify the actual on-disk result while still holding the lock.
        final_registry = _read_conv_map_for_migration()

        if not isinstance(final_registry, dict):
            raise ValueError(
                "conversation registry is not a dictionary after write"
            )

        registered = []

        for candidate in normalized_candidates:
            conv_id = candidate["id"]
            expected_canonical = candidate["canonical_account"]
            record = final_registry.get(conv_id)

            if not isinstance(record, dict):
                raise RuntimeError(
                    "batch registration verification failed: %s"
                    % conv_id
                )

            actual_account = record.get("account")
            if not validate_account_identity(actual_account):
                raise RuntimeError(
                    "batch registration account verification failed: %s"
                    % conv_id
                )

            if (
                normalize_account_identity(actual_account)
                != expected_canonical
            ):
                raise RuntimeError(
                    "batch registration ownership verification failed: %s"
                    % conv_id
                )

            owner = record.get("owner")
            if validate_account_identity(owner):
                if (
                    normalize_account_identity(owner)
                    != normalize_account_identity(actual_account)
                ):
                    raise RuntimeError(
                        "batch registration owner verification failed: %s"
                        % conv_id
                    )

            registered.append({
                "id": conv_id,
                "account": actual_account,
            })

        # DECISION-151：`registered` 是「已验证归属的候选数」，不是「本次新增数」。
        # 传入已存在的候选时 registered 照常返回全部，但磁盘可能一字未写。
        # 幂等性判据必须是 added_count，不能用 registered 长度代替。
        _batch_last_added = len(additions)
        register_conversations_batch.last_added_count = _batch_last_added

        return registered


def restore_conv_map_minimal(candidate):
    """
    Restore missing conversation ownership records from an audited candidate.

    Safety properties:
      - Acquires the same strict registry lock used by normal writers.
      - Re-reads the live registry while holding that lock.
      - Never replaces or edits an existing registry entry.
      - Adds only valid, unambiguous candidate entries whose IDs are absent.
      - Converts validated legacy account strings to canonical records.
      - Performs at most one atomic write.
      - Reads the registry back under the lock and verifies the write.
      - Never prunes the registry.

    Returns a report. Conflicting candidate entries are reported and skipped.
    Raises on registry corruption/quarantine, invalid registry shape, lock
    timeout, write failure, or failed read-back verification.
    """
    if not isinstance(candidate, dict):
        raise ValueError("restore candidate must be a dictionary")

    path = CONV_MAP_FILE
    added = []
    preserved = []
    conflicts = []
    invalid_ids = 0

    def _canonical_candidate_record(raw):
        # Legacy strings are accepted only when the string itself is a
        # valid account identity. They are converted to the Stage 2 shape.
        if isinstance(raw, str):
            if not validate_account_identity(raw):
                return None, "invalid_legacy_account"
            return {
                "account": raw,
                "prompt": None,
                "ts": None,
            }, None

        if not isinstance(raw, dict):
            return None, "invalid_record_type"

        record = dict(raw)
        account = record.get("account")
        if not validate_account_identity(account):
            return None, "invalid_account"

        owner = record.get("owner")
        if validate_account_identity(owner):
            try:
                if (
                    normalize_account_identity(owner)
                    != normalize_account_identity(account)
                ):
                    return None, "owner_account_conflict"
            except (TypeError, ValueError):
                return None, "invalid_owner_identity"

        return record, None

    # All reads, comparisons, writes, and verification occur under the
    # exact same lock used by normal registry writers.
    with _file_lock(path, timeout=LOCK_TIMEOUT_SEC, strict=True):
        existed_before_read = os.path.exists(path)
        live = _load_json(path, {})

        # _load_json quarantines malformed JSON and returns the default.
        # Detect that transition and abort rather than treating corruption
        # as an empty registry and writing over the recovery state.
        if existed_before_read and not os.path.exists(path):
            raise RuntimeError(
                "registry was quarantined during restore; refusing to write"
            )

        if not isinstance(live, dict):
            raise RuntimeError(
                "live conversation registry is not a dictionary; refusing restore"
            )

        merged = dict(live)

        for raw_id, raw_record in candidate.items():
            try:
                conv_id = safe_id(raw_id, "conversation id")
            except (TypeError, ValueError):
                invalid_ids += 1
                conflicts.append({
                    "id": None,
                    "reason": "invalid_candidate_id",
                })
                continue

            candidate_record, reason = _canonical_candidate_record(raw_record)
            if reason:
                conflicts.append({
                    "id": conv_id,
                    "reason": reason,
                })
                continue

            # Existing live data always wins. Never replace an existing
            # entry with the older candidate snapshot.
            if conv_id in live:
                preserved.append(conv_id)

                live_record = live[conv_id]
                live_canonical, live_reason = _canonical_candidate_record(
                    live_record
                )

                if live_reason:
                    conflicts.append({
                        "id": conv_id,
                        "reason": "existing_live_record_" + live_reason,
                    })
                    continue

                try:
                    same_owner = (
                        normalize_account_identity(
                            live_canonical["account"]
                        )
                        == normalize_account_identity(
                            candidate_record["account"]
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    same_owner = False

                if not same_owner:
                    conflicts.append({
                        "id": conv_id,
                        "reason": "live_candidate_account_conflict",
                    })
                continue

            # Candidate record is absent from the live registry and has
            # passed ID, account, and owner validation.
            merged[conv_id] = candidate_record
            added.append(conv_id)

        # One atomic write only, and only if there is something to add.
        # Deliberately bypass record_conv() pruning: recovery must not
        # delete historical records to enforce the normal 500-entry cap.
        if added:
            _registry_guard(path, live, merged)
            _atomic_write(
                path,
                json.dumps(merged, ensure_ascii=False, indent=2),
            )
            _registry_commit_baseline(path, merged)

        # Read back under the same lock. Detect quarantine here as well.
        existed_before_verify = os.path.exists(path)
        verified = _load_json(path, {})

        if existed_before_verify and not os.path.exists(path):
            raise RuntimeError(
                "registry was quarantined during restore verification"
            )

        if not isinstance(verified, dict):
            raise RuntimeError(
                "registry read-back is not a dictionary"
            )

        if verified != merged:
            raise RuntimeError(
                "registry read-back mismatch; restore verification failed"
            )

        for conv_id in added:
            if conv_id not in verified:
                raise RuntimeError(
                    "restored conversation missing after read-back: %s"
                    % conv_id
                )

    return {
        "status": "ok",
        "added": added,
        "added_count": len(added),
        "preserved": preserved,
        "preserved_count": len(preserved),
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
        "invalid_ids": invalid_ids,
        "final_count": len(verified),
        "pruned": 0,
    }


_VALID_ACCOUNT_IDENTITIES = frozenset((
    "default",
    "acct1",
    "acct2",
    "acct3",
    "acct4",
    "acct5",
    "acct6",
    "nologin",
))


def normalize_account_identity(account):
    """
    Validate an account identity and return its comparison identity.

    "default" and "acct1" share the same credential root and therefore
    compare as the same identity. Other account identities remain distinct.
    """
    if not isinstance(account, str) or account not in _VALID_ACCOUNT_IDENTITIES:
        raise ValueError("invalid account identity")
    if account == "default":
        return "acct1"
    return account


def validate_account_identity(account):
    """Return True only for a configured account identity."""
    try:
        normalize_account_identity(account)
        return True
    except ValueError:
        return False


def lookup_conversation_owner(conv_id):
    """Resolve a conversation's stored account ownership.

    Returns:
        {"status": "known", "account": <account>} when ownership is valid.
        {"status": "unknown", "account": None} when no record exists.
        {"status": "invalid", "account": None} when the ID or record is invalid.
        {"status": "conflict", "account": None} when account assertions disagree.

    Legacy bare-string records are invalid until migrate_conv_accounts()
    converts them to canonical dictionary records. The optional "owner"
    field is free-form metadata unless it exactly names a valid account
    identity; in that case it is treated as a legacy account assertion.
    """
    try:
        conv_id = safe_id(conv_id, "conversation id")
    except (TypeError, ValueError):
        return {"status": "invalid", "account": None}

    try:
        with open(CONV_MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"status": "unknown", "account": None}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "invalid", "account": None}

    if not isinstance(data, dict):
        return {"status": "invalid", "account": None}

    if conv_id not in data:
        return {"status": "unknown", "account": None}

    record = data[conv_id]

    # Legacy bare-string records are not trusted for routing.
    # migrate_conv_accounts() is the only supported conversion path.
    if not isinstance(record, dict):
        return {"status": "invalid", "account": None}

    account = record.get("account")
    if not validate_account_identity(account):
        return {"status": "invalid", "account": None}

    # "owner" is normally free-form run/instance metadata.
    # For compatibility with the Stage 1 contract, recognize it as
    # an account assertion only when it exactly names a valid identity.
    owner = record.get("owner")
    if validate_account_identity(owner):
        try:
            if normalize_account_identity(owner) != normalize_account_identity(account):
                return {"status": "conflict", "account": None}
        except (TypeError, ValueError):
            return {"status": "invalid", "account": None}

    return {"status": "known", "account": account}


def resolve_conversation_account(conv_id, explicit_account=None):
    """
    Central conversation/account resolver.

    explicit_account=None means the caller did not explicitly select an
    account. A valid explicit account permits access to an unknown-owner
    conversation, but never overrides a known or contradictory registry entry.

    Return:
        {"status": ..., "account": ...}
    """
    if explicit_account is not None:
        if not validate_account_identity(explicit_account):
            return {"status": "invalid", "account": None}

    result = lookup_conversation_owner(conv_id)
    status = result["status"]

    if status == "known":
        registered_account = result["account"]

        if explicit_account is not None:
            if (
                normalize_account_identity(explicit_account)
                != normalize_account_identity(registered_account)
            ):
                return {"status": "conflict", "account": None}
            return {"status": "known", "account": explicit_account}

        return {"status": "known", "account": registered_account}

    if status == "unknown":
        if explicit_account is not None:
            return {"status": "unknown", "account": explicit_account}
        return {"status": "unknown", "account": None}

    return {"status": status, "account": None}


def conversation_owner_cli_result(raw_conv_id):
    """
    Produce the stable --owner JSON payload and exit status.

    Invalid input is never echoed. Unknown, invalid, and conflicting
    ownership all fail closed with exit status 2.
    """
    try:
        conv_id = safe_id(raw_conv_id, "conversation id")
    except (TypeError, ValueError):
        return {
            "conv_id": None,
            "account": None,
            "known": False,
            "status": "invalid",
        }, 2

    result = lookup_conversation_owner(conv_id)
    status = result["status"]

    if status == "known":
        account = result["account"]
        return {
            "conv_id": conv_id,
            "account": account,
            "known": True,
            "status": "known",
        }, 0

    return {
        "conv_id": conv_id,
        "account": None,
        "known": False,
        "status": status,
    }, 2


def conv_owner(conv_id):
    # F-12：_load_json 成功但文件内容不是 dict（手改成 [] 等）时不得直接 .get
    info = _as_dict(_as_dict(_load_json(CONV_MAP_FILE, {})).get(conv_id))
    return info.get("account")


# ---------- 匿名会话 id 对照表（服务端不给历史，skill 侧自己留存） ----------
def load_anon_sessions():
    """读匿名会话表并收敛形状（F-34）：sessions 必须是 dict[str, dict]。

    「合法 JSON、字段类型坏」（sessions="x"、条目="notadict"）不得让
    .setdefault/.get 变成 AttributeError——形状不符的条目就当没有这一层。
    """
    data = _as_dict(_load_json(ANON_SESSIONS_FILE, {}))
    sessions = {}
    for cid, s in _as_dict(data.get("sessions")).items():
        info = _as_dict(s)
        if info:
            sessions[cid] = info
    data["sessions"] = sessions
    return data


def save_anon_sessions(data):
    data["updated"] = _now()
    try:
        _save_json(ANON_SESSIONS_FILE, data)
    except LockTimeout as e:
        print("WARN: 匿名会话保存失败（锁超时，本次写入未落盘）：%s" % e, flush=True)


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
        sessions = _as_dict(data.get("sessions"))
        s = _as_dict(sessions.get(conv_id))
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
        sessions[conv_id] = s
        data["sessions"] = sessions
        out.update(s)

    try:
        _update_json(ANON_SESSIONS_FILE, mutate, {"sessions": {}})
    except LockTimeout as e:
        print("WARN: 匿名会话登记失败（锁超时，本次未写入）：%s" % e, flush=True)
    return out


def anon_session(conv_id):
    info = _as_dict(_as_dict(load_anon_sessions().get("sessions")).get(conv_id))
    return info or None


def mark_anon_archived(conv_id, reason=""):
    def mutate(data):
        sessions = _as_dict(data.get("sessions"))
        s = _as_dict(sessions.get(conv_id))
        if not s:
            return False
        s["status"] = "archived"
        s["archived_reason"] = reason
        s["updated"] = _now()
        sessions[conv_id] = s
        data["sessions"] = sessions

    try:
        _update_json(ANON_SESSIONS_FILE, mutate, {"sessions": {}})
    except LockTimeout as e:
        print("WARN: 匿名会话归档失败（锁超时，本次未写入）：%s" % e, flush=True)


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
    """Cookie 头 → {name: value}；坏片段直接丢弃，不抛错（F-37：非 str 经 _as_text 收敛）。"""
    out = {}
    for part in _as_text(cookie).split(";"):
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
    parts = urllib.parse.urlsplit(u)
    if not parts.hostname:
        raise ValueError("代理地址缺少主机名：%r" % url)
    # F-35：端口在这里就要验——否则坏端口（99999 / notaport）一路潜到
    # _parse_socks_proxy 的 u.port 才炸，错误出处变成 urllib 内部而非配置非法。
    try:
        parts.port
    except ValueError:
        raise ValueError("代理端口非法：%r（须为 0-65535 的整数）" % url)
    return u


def load_proxy_config():
    """proxy.json 只放出口配置；损坏时按默认处理，不让它挡住发送。"""
    cfg = _load_json(PROXY_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}
    return cfg


def _tcp_alive(host, port, timeout=0.4):
    """探测 TCP 端口通不通；任何不可信输入一律 False（F-35，永不抛异常）。"""
    p = _as_int(port, None)
    if p is None or not (0 < p < 65536):
        return False
    try:
        with socket.create_connection((host, p), timeout):
            return True
    except OSError:
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
            p = _as_int(p, None)   # F-35：int(inf) 的 OverflowError 不得穿出；True 不折叠成 1
            if p is None or not (0 < p < 65536):
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
    F-36：端口字段先收敛再用——垃圾端口抛 OSError（本函数的失败契约），
    绝不让 int()/struct.pack 的 TypeError/OverflowError 穿出。
    """
    dst_port = _as_int(port, None)
    if dst_port is None or not (0 < dst_port < 65536):
        raise OSError("目标端口非法：%r" % (port,))
    proxy_port = _as_int(_as_dict(cfg).get("port"), None)
    if proxy_port is None or not (0 < proxy_port < 65536):
        raise OSError("SOCKS 代理端口非法：%r" % (_as_dict(cfg).get("port"),))
    if host in PROXY_BYPASS_HOSTS:
        return socket.create_connection((host, dst_port), timeout)   # 本机直连
    s = socket.create_connection((_as_dict(cfg).get("host") or "127.0.0.1", proxy_port), timeout)
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
            pw = cfg["password"].encode("utf-8")
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(pw)]) + pw)
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
        s.sendall(b"\x05\x01\x00" + head + addr + struct.pack(">H", dst_port))
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
    """[{'name','value',...}] → "k=v; k2=v2"（保持顺序，同名只留第一个）。
    F-37：jar 不可迭代/不是 list 时按空处理（_as_seq），不抛 TypeError。"""
    pairs, seen = [], set()
    for ck in _as_seq(jar):
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
    """剔除登录态凭据类 cookie 与非 OpenAI 域 cookie，只留设备身份与 CF 系列。

    浏览器若恰好处于登录态（用户手动开过这个 profile），把整包 jar 写盘
    等于把 session token 存进 nologin 目录。宁可不留，也不落凭据。
    域判定同样在此 fail-closed：本函数是落盘信任边界，不能依赖调用方先过滤。
    """
    out, dropped, foreign = [], [], []
    for ck in jar or []:
        if not isinstance(ck, dict) or not ck.get("name"):
            continue
        name = ck["name"]
        if AUTH_COOKIE_RE.search(name):
            dropped.append(name)
            continue
        if not _cookie_domain_allowed(ck.get("domain")):
            foreign.append("%s@%s" % (name, ck.get("domain") or ""))
            continue
        out.append({k: ck[k] for k in
                    ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
                    if ck.get(k) is not None})
    if dropped:
        print("WARN: 设备快照丢弃登录态 cookie（%s）：匿名通道不需要，且不该落盘"
              % ", ".join(sorted(set(dropped))), flush=True)
    if foreign:
        print("WARN: 设备快照丢弃非 OpenAI 域 cookie（%s）：凭据只存自有域"
              % ", ".join(sorted(set(foreign))), flush=True)
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


# 思考模型解析：只缓存成功结果，失败必须允许后续重新解析。
_THINKING_MODEL_CACHE = {}
# 默认思考模型优先级：清单里存在该 slug 时优先选它（用户裁定 2026-11-26）。
# 只影响“自动选择”的排序；CHATGPT_MODEL 显式指定仍然最高优先。
# 环境变量 CHATGPT_PREFERRED_THINKING 可覆盖它；未设置时用下面的默认值。
_DEFAULT_PREFERRED_THINKING_SLUG = "gpt-6-t-mini"
_PREFERRED_THINKING_SLUG = _DEFAULT_PREFERRED_THINKING_SLUG
_PREFERRED_THINKING_ENV = "CHATGPT_PREFERRED_THINKING"
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _preferred_thinking_slug():
    """优先思考模型 slug：CHATGPT_PREFERRED_THINKING > 默认 gpt-6-t-mini。

    返回 (slug, source)，source 取 "env" 或 "default"。

    非法配置直接抛 ValueError，不悄悄退回默认值：配置写错却照常选出默认模型，
    等于让操作者以为“我设的优先模型生效了”——A27 要堵的正是这种静默回退。
    """
    raw = os.environ.get(_PREFERRED_THINKING_ENV)
    if raw is None:
        return _DEFAULT_PREFERRED_THINKING_SLUG, "default"
    slug = raw.strip()
    if not slug:
        raise ValueError(
            "%s 为空：删掉该变量走默认 %s，或给一个含 -t- 的 slug"
            % (_PREFERRED_THINKING_ENV, _DEFAULT_PREFERRED_THINKING_SLUG)
        )
    if not _SLUG_RE.match(slug) or "-t-" not in slug:
        raise ValueError(
            "%s=%r 不是思考模型 slug（必须含 -t-，只允许字母数字 . _ -）"
            % (_PREFERRED_THINKING_ENV, raw)
        )
    return slug, "env"

class ThinkingModelUnavailable(RuntimeError):
    """当前账号无法满足强制思考模式要求。

    reason_code:
        AUTH_INVALID
        MODEL_ENDPOINT_UNAVAILABLE
        THINKING_MODEL_UNAVAILABLE
    """

    VALID_REASON_CODES = frozenset((
        "AUTH_INVALID",
        "MODEL_ENDPOINT_UNAVAILABLE",
        "THINKING_MODEL_UNAVAILABLE",
    ))

    def __init__(self, reason_code, account, detail, model_slug=None):
        if reason_code not in self.VALID_REASON_CODES:
            raise ValueError("invalid thinking failure reason: %r" % reason_code)

        self.reason_code = reason_code
        self.account = account
        self.detail = str(detail)
        self.model_slug = model_slug

        super().__init__(
            "%s: account=%s: %s"
            % (reason_code, account, self.detail)
        )


def _cookie_domain_allowed(domain):
    """Cookie 持久化边界（DECISION-156）：只允许 OpenAI 自有域。

    精确匹配域名或其后缀，**不做字符串包含**——`notchatgpt.com`、
    `evil-openai.com`、`chatgpt.com.evil.io` 都必须被拒。
    这是「哪些凭据有资格被保存」的边界，与请求头过滤（「哪些能发出去」）是两道独立的关。
    """
    d = (domain or "").strip().lower().lstrip(".")
    if not d:
        return False
    for base in _OPENAI_COOKIE_DOMAINS:
        if d == base or d.endswith("." + base):
            return True
    return False

def _filter_openai_cookies(cookies):
    """只保留 OpenAI 域的 cookie，其余原样丢弃。保留字段不做重构。"""
    if not isinstance(cookies, list):
        return []
    return [c for c in cookies
            if isinstance(c, dict) and _cookie_domain_allowed(c.get("domain"))]

class ChatGPTAPI:
    def __init__(self, base_dir=None, account="default"):
        # CHATGPT_AUTH_DIR selects credential/cache root; account data stays isolated.
        root = base_dir or os.environ.get("CHATGPT_AUTH_DIR") or BASE
        if account not in _VALID_ACCOUNT_IDENTITIES:
            raise ValueError(
                "account must be one of %s"
                % ", ".join(sorted(_VALID_ACCOUNT_IDENTITIES))
            )
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
                if isinstance(ck, dict) and _cookie_domain_allowed(ck.get("domain"))
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
        self.last_truncated = False   # 上一次响应是否被截断（IncompleteRead / 超时）
        self.last_partial_path = None  # 截断/超时已收内容的落盘路径（无则 None）
        self.last_partial_bytes = 0    # 截断/超时实际收到的原始字节数（SEND_TRUNCATED 用）

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
        # DECISION-156：回写前先过持久化边界。混进来的非 OpenAI 域 cookie
        # （实测见过整包 Google 会话：accounts.google.com / .google.com /
        # .youtube.com / .google.co.jp / .google.com.hk）不得被反复落盘。
        # 内存与磁盘同步收敛，避免两者不一致。
        before = len(self.cookies)
        self.cookies = _filter_openai_cookies(self.cookies)
        dropped = before - len(self.cookies)
        try:
            _save_json(os.path.join(self.base_dir, "cookies.json"), self.cookies)
        except (OSError, LockTimeout):
            pass
        if dropped:
            print("warm_cookies: 丢弃 %d 条非 OpenAI 域 cookie（持久化边界）" % dropped,
                  flush=True)
        self.cookie = "; ".join(
            "%s=%s" % (c["name"], c["value"])
            for c in self.cookies
            if _cookie_domain_allowed(c.get("domain"))
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
        # F-30：`float("9"*400)` 得 inf，`float(None)` 得 TypeError——统一走
        # _as_float，非有限值/非数值都退回默认，绝不让 inf 参与退避计算。
        val = _as_float(raw, None)
        if val is None:
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
                # F-30：服务端响应体是不可信输入，超大整数会让 float(v) 抛
                # OverflowError。走 _as_float 降到 None 就当「没这个字段」。
                v = _as_float(v, None)
                if v is not None and v >= 0:
                    return v
            for key in ("reset", "resets_at", "retry_at"):
                v = c.get(key)
                v = _as_float(v, None)      # F-30：超大整数参与减法同样会 OverflowError
                if v is not None:
                    # >1e9 视为 epoch
                    if v > 1e9:
                        return max(0.0, v - time.time())
                    if v >= 0:
                        return v
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
        t0 = time.time()
        # 已收到的原始字节：超时/断流时这是唯一副本，必须边读边留（旧版 resp.read() 一次读完，
        # 中途超时异常里没有已读字节 → 已收内容全丢，19 次卡死全部零留存）。
        sink = []
        self.last_partial_path = None
        self.last_partial_bytes = 0
        try:
            with self._opener.open(rq, timeout=timeout) as resp:
                self._read_stream(resp, sink)   # 读到 EOF，勿提前 close
                data = self._decompress(b"".join(sink), resp.headers.get("Content-Encoding"))
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
            # 现在与超时同路：明确标记 + 落盘，字节不丢。
            # 注意：partial 已由 _read_stream() 在抛空前追加进 sink（它保证直接调用
            # 也不丢字节），此处不能再追加一次，否则 13 字节变 26 字节、正文重复。
            return self._partial_result(
                b"".join(sink), t0, "IncompleteRead: %s" % (e,), marker="PARTIAL")
        except (socket.timeout, TimeoutError) as e:
            return self._timeout_result(sink, t0, timeout, str(e) or type(e).__name__)
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
                return self._timeout_result(sink, t0, timeout, "urlopen: %s" % (reason,))
            self.last_headers = {}
            self.quota = {}
            self.last_body = ""
            self.last_status = 0
            return 0, "EXC " + str(e)
        except Exception as e:
            self.last_headers = {}
            self.quota = {}
            self.last_body = ""
            self.last_status = 0
            return 0, "EXC " + str(e)

    @staticmethod
    def _read_stream(resp, sink):
        """分块读到 EOF，边读边把字节追加进 sink。

        为什么不用 `resp.read()`：一次性读到底，中途 socket 超时抛出时异常对象里没有
        已读到的字节——已收内容全部白丢（xhttp 层也无法回捞）。分块读保证已经返回的
        chunk 都留在 sink 里，可以落盘保全。

        `IncompleteRead` 的处理：异常携带的 `e.partial`（本次没读完整的那一块）由本函数
        追加进 sink 后原样 re-raise，所以调用方（req()）直接拿 sink 落盘即可，**不得再
        追加一次**，否则同一段字节会出现两遍。

        注意：这里只覆盖 `read()` 阶段抛出的截断；若 `open()` / `__enter__` 阶段就抛
        `IncompleteRead`，此时 sink 还是空的，本函数无从保全。stdlib 的 chunked 解码错误
        都在 `read()` 内抛出，因此该分支现实不可达，无需额外处理。
        """
        while True:
            try:
                chunk = resp.read(16384)
            except http.client.IncompleteRead as e:
                part = getattr(e, "partial", b"") or b""
                if part:
                    sink.append(part)
                raise
            if not chunk:
                return
            sink.append(chunk)

    def _save_partial(self, data):
        """把已收到的原始字节落盘（不经过文本层，保持可再次解析）。"""
        d = os.path.join(self.base_dir, "timeouts")
        base = "partial-%s-%s-%d" % (self.account, datetime.now().strftime("%Y%m%d-%H%M%S"),
                                     os.getpid())
        for seq in range(0, 100):
            name = base + ("" if seq == 0 else "-%d" % seq) + ".txt"
            path = os.path.join(d, name)
            if os.path.exists(path):
                continue
            try:
                _atomic_write_bytes(path, data)
                return path
            except OSError as e:
                print("WARN: 部分内容落盘失败（%s）" % e, flush=True)
                return None
        return None

    def _timeout_result(self, sink, t0, budget, why):
        """超时统一收敛点：可机读标记 + 部分内容保全，绝不静默丢字节。

        标记格式固定（事后统计只认这两行）：
          TIMEOUT_PARTIAL after Ns, M bytes buffered, saved to <path>
          TIMEOUT_EMPTY after Ns
        有部分内容时按 200 返回，让既有链路照常解析/对账——最终仍会落到
        「FAIL: 回复完整性未证明」，标记只加在前面，判定口径不放宽。
        """
        elapsed = time.time() - t0
        self.last_truncated = True
        self.last_headers = {}
        self.quota = {}
        self.last_body = ""
        if not sink:
            self.last_status = 0
            print("TIMEOUT_EMPTY after %.0fs" % elapsed, flush=True)
            print("WARN: 超时边界 %ss（%s）——零字节返回，按连接层失败处理"
                  % (budget, why), flush=True)
            return 0, "TIMEOUT_EMPTY after %.0fs" % elapsed
        return self._partial_result(b"".join(sink), t0, why,
                                    marker="TIMEOUT_PARTIAL", budget=budget)

    def _partial_result(self, data, t0, why, marker="PARTIAL", budget=None):
        """部分内容的统一出口：落盘 + 打标记 + 按 200 交回既有解析链路。"""
        elapsed = time.time() - t0
        self.last_truncated = True
        self.last_headers = {}
        self.quota = {}
        path = self._save_partial(data) if data else None
        self.last_partial_path = path
        self.last_partial_bytes = len(data) if data else 0
        if path:
            print("%s after %.0fs, %d bytes buffered, saved to %s"
                  % (marker, elapsed, len(data), path), flush=True)
        else:
            print("%s after %.0fs, %d bytes buffered, saved to <FAILED>"
                  % (marker, elapsed, len(data)), flush=True)
        if budget is not None:
            print("WARN: 超时边界 %ss（%s）——按已收内容解析，完整性仍由后续对账判定"
                  % (budget, why), flush=True)
        else:
            print("WARN: 响应流被截断（%s）——按已收内容解析，稍后用 GET 补全文" % why,
                  flush=True)
        self.last_status = 200
        text = data.decode("utf-8", "replace")
        self.last_body = text
        return 200, text

    # ---------- tail ----------
    @staticmethod
    def tail_from_mapping(raw_body):
        """取最新消息的 node id（作 parent_message_id）。

        降级姿态（第二十六轮审查口径）：这是**尾部确认**，不是内容提取。
        mapping 有任何畸形（node/message 形状不对、时间戳不可用）就整体返回
        None——不能把「不完整 mapping 里最新的那条」当成**已确认的真实尾部**
        去续接对话，否则续接的父节点可能是错的。
        """
        try:
            data = json.loads(raw_body)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        mapping = data.get("mapping")
        if not isinstance(mapping, dict):
            return None
        best_id, best_t = None, 0.0
        for nid, node in mapping.items():
            if not isinstance(node, dict):
                return None
            m = node.get("message")
            if m is None:
                continue                     # 非消息节点——正常
            if not isinstance(m, dict):
                return None
            ok, t = _as_ctime(m.get("create_time"))
            if not ok:
                return None                 # 时间不可用 → 尾部不可信
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
            st, body = self.req("GET", API_BASE + "/backend-api/conversation/" + conv_id,
                                timeout=_cap_timeout(90))
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
            if isinstance(payload, list):
                items = payload
            elif isinstance(payload, dict):
                items = payload.get("items", payload.get("conversations", []))
            else:
                return conversations, status, "unrecognized conversation list response"
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
        if not isinstance(d, dict):
            # F-12：形状不对（null/数组）→ 拿不到 token 就按拿不到处理
            print("WARN: chat-requirements 返回的不是对象：%r" % (body[:200],), flush=True)
            return None, None
        p = _as_dict(d.get("proofofwork"))
        return d.get("token"), solve(p.get("seed", ""), p.get("difficulty", "0"))

    # ---------- send ----------
    def send(self, conv_id, text, parent_message_id, req_token, proof, timeout=420, model=None):
        body = {
            "action": "next",
            "messages": [{"id": str(uuid.uuid4()), "author": {"role": "user"},
                          "content": {"content_type": "text", "parts": [text]},
                          "metadata": {}}],
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
        if parent_message_id:  # 可选：GET 限速时省略，落点仍由 SSE 首事件唯一 id 校验
            body["parent_message_id"] = parent_message_id
        return self.req("POST", API_BASE + "/backend-api/conversation", body, {
            "OpenAI-Sentinel-Chat-Requirements-Token": req_token,
            "OpenAI-Sentinel-Proof-Token": proof,
        }, timeout=timeout)

    # ---------- 思考模式 ----------
    @staticmethod
    def resolve_thinking_model(api, cache=None):
        """解析强制思考模式所需的 model slug。

        成功：返回包含 -t- 的模型 slug。
        失败：抛 ThinkingModelUnavailable，携带明确原因分类。

        重要：
        - 401 不得折叠成 auto。
        - 非 200 不得折叠成 auto。
        - 畸形模型清单不得折叠成 auto。
        - 正常清单没有 -t- 变体不得折叠成 auto。
        - 只有成功解析的模型才进入 cache。
        """

        if cache is None:
            cache = _THINKING_MODEL_CACHE

        account = getattr(api, "account", "unknown")
        explicit = os.environ.get("CHATGPT_MODEL") or ""

        # 优先 slug 先解析：非法配置必须在任何请求之前就炸，不能等拿到清单才发现。
        preferred, preferred_source = _preferred_thinking_slug()

        # 显式模型配置与优先 slug 都参与 cache key，避免切换 CHATGPT_MODEL 或
        # 改 CHATGPT_PREFERRED_THINKING 后错误复用之前选定的模型。
        key = (account, explicit, preferred)

        if key in cache:
            return cache[key]

        def unavailable(reason_code, detail):
            raise ThinkingModelUnavailable(
                reason_code=reason_code,
                account=account,
                detail=detail,
            )

        # 用户显式指定模型时不请求模型清单，但仍然强制检查 -t-。
        if explicit:
            if "-t-" not in explicit:
                unavailable(
                    "THINKING_MODEL_UNAVAILABLE",
                    "CHATGPT_MODEL=%r 不是思考模型 slug" % explicit,
                )

            cache[key] = explicit
            print("thinking model: %s（显式指定）" % explicit, flush=True)
            return explicit

        # 请求模型清单。
        try:
            status, body = api.req(
                "GET",
                API_BASE + "/backend-api/models",
            )
        except Exception as e:
            unavailable(
                "MODEL_ENDPOINT_UNAVAILABLE",
                "请求 /backend-api/models 异常：%s: %s"
                % (type(e).__name__, e),
            )

        # 必须保留 HTTP 状态码，不能在这里退化成 auto。
        if status == 401:
            unavailable(
                "AUTH_INVALID",
                "/backend-api/models 返回 HTTP 401，认证失效",
            )

        if status != 200:
            unavailable(
                "MODEL_ENDPOINT_UNAVAILABLE",
                "/backend-api/models 返回 HTTP %s" % status,
            )

        # HTTP 200 但无法解析 JSON：端点响应不可用。
        try:
            data = json.loads(body)
        except Exception as e:
            unavailable(
                "MODEL_ENDPOINT_UNAVAILABLE",
                "模型清单 JSON 解析失败：%s" % e,
            )

        if not isinstance(data, dict):
            unavailable(
                "MODEL_ENDPOINT_UNAVAILABLE",
                "模型清单顶层不是 JSON object",
            )

        raw_models = data.get("models")

        if not isinstance(raw_models, list):
            unavailable(
                "MODEL_ENDPOINT_UNAVAILABLE",
                "模型清单 models 字段缺失或不是数组",
            )

        models = [_as_dict(item) for item in raw_models]

        default = data.get("default_model_slug")
        if not isinstance(default, str):
            default = "auto"

        by_slug = {
            model.get("slug"): model
            for model in models
            if isinstance(model.get("slug"), str)
        }

        chosen = ""

        # 优先级 0：清单里存在优先 slug 时默认选它。
        # 优先 slug 来自 CHATGPT_PREFERRED_THINKING，未设置时是默认值。
        if preferred in by_slug:
            chosen = preferred
            print(
                "thinking model: 优先命中 %s（source=%s）"
                % (preferred, preferred_source),
                flush=True,
            )
        else:
            # 不能静默：优先候选缺失必须留下可 grep 的诊断，否则日志看起来
            # 和“优先模型生效”一模一样。
            print(
                "thinking model: 优先候选 %s 不在清单里（source=%s），按后备顺序解析"
                % (preferred, preferred_source),
                flush=True,
            )

        # 优先级 1：默认模型本身就是思考模型。
        if not chosen and "-t-" in default:
            chosen = default

        elif not chosen:
            # 优先级 2：同标题的 -t- 变体。
            want_title = _as_dict(by_slug.get(default)).get("title")

            for model in models:
                slug = model.get("slug") or ""

                if (
                    "-t-" in slug
                    and want_title
                    and model.get("title") == want_title
                ):
                    chosen = slug
                    break

            # 优先级 3：任意大窗口思考模型。
            if not chosen:
                for model in models:
                    slug = model.get("slug") or ""
                    max_tokens = model.get("max_tokens")

                    if (
                        not isinstance(max_tokens, (int, float))
                        or isinstance(max_tokens, bool)
                    ):
                        continue

                    if "-t-" in slug and max_tokens > 100000:
                        chosen = slug
                        break

        # HTTP 200 + 合法清单，但没有任何可用思考模型。
        if not chosen:
            unavailable(
                "THINKING_MODEL_UNAVAILABLE",
                "模型清单正常，但没有可用的 -t- 思考模型",
            )

        # 只有成功结果可以缓存。
        cache[key] = chosen

        print(
            "thinking model: %s（默认 %s）" % (chosen, default),
            flush=True,
        )

        return chosen

    # ---------- harvest ----------
    @staticmethod
    def extract_reply_sse(raw_text):
        return _scan_sse(raw_text)[0]

    @staticmethod
    def extract_reply_sse_checked(raw_text):
        """返回 (text, complete, detail)。

        complete=False 表示「这是部分流」：出现过畸形事件，或流没有以 [DONE]
        收尾。坏事件只跳过、不影响后续事件，但**必须被记下来**——否则部分流
        会被当成完整回复落盘（第二十六轮审查口径）。
        """
        return _scan_sse(raw_text)

    @staticmethod
    def harvest_mapping(raw_body):
        return _scan_mapping(raw_body)[0]

    @staticmethod
    def harvest_mapping_checked(raw_body):
        """返回 (text, complete)。complete=False = mapping 有畸形，采到的是尽力而为。"""
        rows, complete = _scan_mapping(raw_body)
        for _, role, text in reversed(rows):
            if role == "assistant":
                return text, complete
        return "", complete

    @staticmethod
    def harvest_transcript(raw_body):
        """按时间顺序返回整段对话 [(role, text), ...]，用于匿名会话留存。"""
        rows, _complete = _scan_mapping(raw_body)
        return [(role, text) for _, role, text in rows]

    @staticmethod
    def harvest_transcript_checked(raw_body):
        """返回 (rows, complete)。complete=False = 留存不完整，必须能识别。"""
        rows, complete = _scan_mapping(raw_body)
        return [(role, text) for _, role, text in rows], complete

    def save_reply(self, conv_id, pfile, text):
        """落盘回复全文。默认不截断（旧版静默只留末尾 2 万字，等于丢结论）。

        需要控制体积时设 CHATGPT_REPLY_MAX_CHARS（>0 才生效，保留开头并在头部注明）。
        """
        self._last_reply_text = text or ""   # ""=明确空回复；None=从未捕获（R373-05）
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
        # F-36：端口是不可信配置（参数/环境变量）——int() 的 TypeError/ValueError/
        # OverflowError 不得穿出构造器。变量写错不该让匿名通道起不来（与
        # CHATGPT_REPLY_MAX_CHARS「变量写错不该让整次收割白跑」同口径：降级 + WARN）。
        raw = port or os.environ.get("CHATGPT_ANON_CDP_PORT") or 9333
        p = _as_int(raw, None)
        if p is None or not (0 < p < 65536):
            print("WARN: CDP 端口非法：%r（回退默认 9333）" % (raw,), flush=True)
            p = 9333
        self.port = p
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
                # DECISION-156：导入侧同样只收 OpenAI 自有域（精确匹配，不做包含）。
                if not _cookie_domain_allowed(ck.get("domain")):
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
    transcript, t_complete = api.harvest_transcript_checked(body)
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
        if not t_complete:
            # 留存不完整必须能被识别：mapping 有畸形节点/时间戳不可用，
            # 归档头部明确标注，不得让「尽力而为」的留存冒充完整对话
            # （第二十六轮审查口径）。
            chunks.append("> ⚠️ 留存不完整：服务端返回的 mapping 有畸形节点或不可用时间戳，"
                          "以下内容为尽力而为的抢救结果，不保证完整。\n\n")
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
        reply, acomplete = api.harvest_mapping_checked(body)
        if reply and not acomplete:
            print("WARN: 匿名会话 mapping 收割不完整——按尽力而为处理", flush=True)
    if not reply:
        reply = dom_reply
    path = api.save_reply(cid, os.path.basename(prompt_file), reply)
    record_anon_session(cid, tab=tab, prompt=prompt_file, reply=reply,
                        device=did or api._device_id())
    print("ANON_SEND %s -> %s replyLen=%d tab=%s device=%s"
          % (cid[:8], st, len(reply or ""), (tab or "-")[:8], (did or "-")[:8]), flush=True)
    return (200 if reply else st or 0), path, cid


class ConvRef(str):
    """会话 id 引用——str 子类：默认呈现为目标会话 id。

    F-13：落点不一致（投递 A、服务器落在 B）时，**必须同时携带两个 id**。
    只回一个就把「回复实际落在哪个会话」这条关键信息丢了——而这正是下游
    继续操作时最需要的东西；把回复悄悄归档到 A 名下则更糟：会话身份与
    回复归属分裂，下游会拿着错误的 conversation id 继续往下走。

    F-17：id **歧义**分支（响应里出现多个不同 id）同样不能把诊断信息丢掉——
    候选 id 列表放在 `candidate_ids` 里，失败结果要能让机器调用方定位问题。
    """

    def __new__(cls, conv_id, actual_conv_id=None, mismatch=False, reason="",
                candidate_ids=None, evidence_complete=None, reply_complete=None,
                send_truncated=None):
        obj = super().__new__(cls, conv_id or "")
        obj.target_conv_id = conv_id or ""
        obj.actual_conv_id = actual_conv_id or ""
        obj.mismatch = bool(mismatch)
        obj.reason = reason
        obj.candidate_ids = list(candidate_ids or [])
        # evidence_complete=None = 不适用（非落点不一致分支）；
        # False = 隔离三件事有失败，机器调用方必须据此判「证据不完整」。
        obj.evidence_complete = evidence_complete
        obj.reply_complete = reply_complete
        # 发送时响应是否被截断/超时（SEND_TRUNCATED）。只有完整响应才允许算送达凭证，
        # 所以这个标记必须一路带到 CLI 退出码（exit 3），不能被 st=200 吞掉。
        obj.send_truncated = send_truncated
        return obj


LANDING_MISMATCH_STATUS = 409
QUARANTINE_DIR = "quarantine"
PENDING_CLAIMS_FILE = "pending-claims.jsonl"


def _record_pending_claim(base, record):
    """落点不一致的**待认领登记**（F-13）。

    服务器实际落点 ≠ 系统已确认该会话归属：自动 `record_conv(actual)` 会把
    一次异常投递变成正式会话关系。所以另记一份异常登记——含 target、actual、
    时间、隔离档案路径与原因——**人工确认后**再正式登记归属。

    第三十轮审查 N-02：JSONL 追加必须**跨进程互斥**，否则两个进程同时 append
    会让记录交错/截断（`write` 不是原子的，尤其 >PIPE_BUF 或跨进程缓冲）。
    复用锁子系统的 `_file_lock`（与状态文件同一把锁语义，fail-closed）。
    """
    path = os.path.join(base, PENDING_CLAIMS_FILE)
    try:
        with _file_lock(path, strict=True):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
    except OSError as e:
        print("FAIL: 待认领登记写入失败（%s）——actual 会话可能无人认领" % e, flush=True)
        return False
    except LockTimeout as e:
        print("FAIL: 待认领登记加锁超时（%s）——actual 会话可能无人认领" % e, flush=True)
        return False
    return True


def _quarantine_reply(api, target_conv, actual_conv, reply, resp, status, pfile):
    """落点不一致的回复隔离归档：保全，但不归属。

    为什么必须隔离而不是直接归档到目标会话：回复实际落在 actual_conv，
    归档到 target_conv 会让下游把别的会话的回复当成本会话的回复继续操作
    （A10/F-13：会话身份与回复归属分裂）。隔离档案带完整证据——两个 id、
    HTTP 状态、原始响应全文与 sha256、回复全文与 sha256——供人工判定归属。

    隔离目录刻意放在 **`replies/` 之外**（与 `replies/` 同级）：下游按
    `replies/**` 递归扫描也够不到它——不靠文件名 glob 兜底（F-13 第 3 问）。

    返回 `(path, evidence_complete)`：
    - `evidence_complete=False` 表示三件事（完整响应、隔离档案、待认领登记）
      有任一失败——调用方**必须**把它当成「证据不完整」，不得报告为完整隔离保全
      （F-18/F-25：WARN 不是状态，机器调用方要能判定）。
    """
    base = getattr(api, "base_dir", BASE)
    try:
        api._last_reply_text = reply or ""     # 隔离件也要进会话日志，别留空白轮次
    except Exception:
        pass
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = "quarantine-reply-%s-%s-%s" % (id8(target_conv), id8(actual_conv), stamp)
    # F-21：用内核级 O_EXCL 独占创建占名，**不再**「先检查不存在再写」——
    # 那是 TOCTOU，两个进程能通过同一检查后互相覆盖隔离证据。
    # 独占创建失败（FileExistsError）就换下一个序号；绝不覆盖已存在的证据。
    resp_text = resp or ""
    resp_sha = hashlib.sha256(resp_text.encode("utf-8")).hexdigest()
    reply_text = reply or ""
    reserved = None
    for seq in range(1000):
        name = prefix if seq == 0 else "%s-%d" % (prefix, seq)
        candidate = os.path.join(base, QUARANTINE_DIR, name + ".md")
        try:
            _atomic_create_exclusive(candidate, "")   # 原子占名，内容随后写
            reserved = (name, candidate)
            break
        except FileExistsError:
            continue
        except OSError as e:
            print("FAIL: 隔离档案占名失败（%s）——回复可能丢失！" % e, flush=True)
            return None, False
    if reserved is None:
        print("FAIL: 隔离档案连续 1000 次占名冲突——回复可能丢失！", flush=True)
        return None, False
    name, path = reserved
    rel = os.path.join(QUARANTINE_DIR, name + ".md")
    resp_rel = os.path.join(QUARANTINE_DIR, name + ".resp.txt")
    resp_path = os.path.join(base, resp_rel)
    # 先落**完整**原始响应（F-18：800 字符的头部不足以保全证据，关键 id 与
    # 后续事件可能正好落在截断部分）。保全结果决定档案正文的真实性声明——
    # N-01：失败的保全操作绝不能在正式证据里继续声称成功。
    resp_ok = True
    resp_err = ""
    try:
        _atomic_create_exclusive(resp_path, resp_text)
    except OSError as e:
        resp_ok = False
        resp_err = "%s: %s" % (type(e).__name__, e)
        print("FAIL: 原始响应保全失败（%s）——隔离证据不完整" % e, flush=True)
    header = {
        "type": "landing_mismatch_quarantine",
        "target_conv_id": target_conv,
        "actual_conv_id": actual_conv,
        "reason": "投递目标会话与服务器返回的会话不一致；未静默改归属，回复隔离保全待人工判定",
        "http_status": status,
        "prompt_file": pfile,
        "quarantined_at": datetime.now().isoformat(timespec="seconds"),
        "reply_chars": len(reply_text),
        "reply_sha256": hashlib.sha256(reply_text.encode("utf-8")).hexdigest(),
        "resp_preserved": resp_ok,
        "resp_path": resp_rel.replace("\\", "/") if resp_ok else "",
        "resp_chars": len(resp_text) if resp_ok else 0,
        "resp_sha256": resp_sha if resp_ok else "",
        "resp_error": resp_err,
        "resp_head": resp_text[:800],
    }
    # N-01：声明随保全结果变化，三种状态明确区分（已保全 / 保全失败 / 未验证）
    if resp_ok:
        resp_line = "> **完整原始响应已保全**于 `%s`（%d 字符，sha256=%s）。\n" % (
            resp_rel.replace("\\", "/"), len(resp_text), resp_sha)
    else:
        resp_line = ("> ⚠️ **完整原始响应保全失败**（%s）——下方仅有前 800 字符速览，"
                     "不得当作完整证据；需人工重取。\n" % resp_err)
    body = (
        "# ⚠️ 隔离归档：落点不一致（landing_mismatch）\n\n"
        "> 这条回复**不是** `%s` 的正常会话回复——服务器实际把它落在 `%s`。\n"
        "> 本文件只用于保全与人工判定；下游不得把它当成 `%s` 的会话回复，"
        "也不得据此认为 `%s` 已经回完。\n"
        "%s\n"
        "```json\n%s\n```\n\n"
        "## 原始响应摘要（前 800 字符）\n\n```\n%s\n```\n\n"
        "## 回复全文（保全）\n\n%s\n"
    ) % (target_conv, actual_conv, target_conv, target_conv,
         resp_line, json.dumps(header, ensure_ascii=False, indent=2),
         resp_text[:800], reply_text)
    # N-02：占名后先用临时文件完整写入 + fsync，再 os.replace 发布到已占名路径
    # ——避免「占名成功但写到一半磁盘满」留下空壳/部分档案；失败则清除占名。
    try:
        _atomic_write(path, body)
    except OSError as e:
        print("FAIL: 隔离档案写入失败（%s）——回复可能丢失！" % e, flush=True)
        try:
            os.remove(path)
        except OSError:
            pass
        return None, False
    claimed = _record_pending_claim(base, {
        "target_conv_id": target_conv,
        "actual_conv_id": actual_conv,
        "quarantined_at": header["quarantined_at"],
        "quarantine_path": rel.replace("\\", "/"),
        "resp_path": resp_rel.replace("\\", "/") if resp_ok else "",
        "resp_preserved": resp_ok,
        "resp_error": resp_err,
        "resp_chars": len(resp_text) if resp_ok else 0,
        "resp_sha256": resp_sha if resp_ok else "",
        "reply_chars": len(reply_text),
        "reason": header["reason"],
        "claim_status": "pending_human_review",
    })
    evidence_complete = bool(resp_ok and claimed)
    if not evidence_complete:
        print("FAIL: 隔离证据不完整（响应保全=%s 待认领登记=%s）——"
              "不得报告为完整隔离保全" % (resp_ok, claimed), flush=True)
    return path, evidence_complete

# ---------- 会话日志（每条交互全文留存，按会话拆分，可检索） ----------
#
# 【全局不变量 · R381-03】begin 里的 prompt 是本轮提示词的**唯一**持久化副本。
# 任何删除 / 替换 / 迁移 / 覆盖 begin 文件的路径，都必须先证明 prompt 已在另一个
# 持久化位置完整落盘。审计范围：_migrate_pending、log_end、pending 清理、
# conversations 迁移、turn 去重、异常恢复与重命名。
# 违反它的直接后果见 docs/incidents/R381-prompt-loss-incident.md。
# 现存删除点只有一处：_migrate_pending 末尾——它前面已回读确认目标含 begin+end
# 且 payload/conv_id 一致。新增删除点前请先满足上面这条。
#
# 目的：任何一次经本插件发出的提示词与 ChatGPT 回复正文都要能在本地找回。
# prompts/ 会被覆盖或清理，replies/ 是扁平命名、按会话归集得靠猜。
#
# 两阶段落盘（R373-02）：
#   begin —— 发送**之前**写：本轮 turn_id + 提示词全文（此时失败则拒绝发送）
#   end   —— 发送**之后**写：回复全文 + reply_state + 状态
# 两行用 turn_id 关联，读取时合并成一"轮"。只有 begin 没有 end = 该轮未完成，
# 对读取方是可见状态，不会被当成"看起来成功"。追加写、不改写历史行。
#
# 目录含私有对话内容，与 replies/ 同等级：已由 .gitignore 排除，绝不入库。

def log_root():
    return os.path.join(BASE, "logs")

def log_dir():
    """已确认 conv_id 的会话日志。按 BASE 惰性解析——测试改 BASE 即自动隔离。"""
    return os.path.join(log_root(), "conversations")

def log_pending_dir():
    """新会话尚未拿到真实 conv_id 时的暂存区。取得真实 id 后整轮迁移过去。"""
    return os.path.join(log_root(), "pending")

def log_unassigned_dir():
    """身份不可用且无法归属的轮次。按原始标识分文件，绝不合并到同一个桶。"""
    return os.path.join(log_root(), "unassigned")

# R382-01：conv_id 必须有独立的严格判据（小写、带连字符的 UUID）。
# 不复用 _ID_RE——那个正则还管 device id / baseline id，放宽是为了它们；
# 而 conv_id 是会话身份，`"abc"` 这种"合法标签"不能成为可信身份。
_CONV_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

def _safe_conv_id(value):
    """规范化会话 id，三态返回（R381-02）：

    - ``""``  历史缺失（None 或空串）
    - ``str`` 合法会话 id
    - ``None`` 畸形身份（非空但不合法、或非字符串）

    旧版把"畸形"也归一成 ``""``，于是它与"历史缺失"完全不可区分——
    A 层会把它当历史空值放行，诊断信息丢失。审计这类问题必须能分辨
    "没有身份"和"身份是坏的"。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    if value == "":
        return ""
    return value if _CONV_ID_RE.fullmatch(value) else None

def _sanitize_segment(raw):
    """把任意原始标识压成可做文件名的片段（保留可辨识性，去掉路径字符）。"""
    txt = str(raw or "")
    keep = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in txt)[:48]
    digest = hashlib.sha256(txt.encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (keep or "empty", digest)

def conversation_log_path(conv_id):
    """合法 conv_id -> 日志文件。非法 id 一律抛错，由调用方走 unassigned 分文件。

    R373-03：旧版把所有非法 id 塞进 _unassigned.jsonl，等于把互不相干的会话
    合并成一个无法区分的桶，且丢失原始标识。现在非法即拒绝，绝不静默归并。
    """
    cid = _safe_conv_id(conv_id)
    if cid is None:
        raise ValueError("会话日志收到畸形 conv_id：%r" % (conv_id,))
    if not cid:
        raise ValueError("会话日志缺少 conv_id：%r" % (conv_id,))
    return os.path.join(log_dir(), cid + ".jsonl")

def pending_log_path(turn_id):
    return os.path.join(log_pending_dir(), _sanitize_segment(turn_id) + ".jsonl")

def unassigned_log_path(raw_id):
    return os.path.join(log_unassigned_dir(), _sanitize_segment(raw_id) + ".jsonl")

def _log_write(path, record):
    """追加一行并确保落盘；隔离历史上的不完整尾行（R388-A1）。

    JSONL 的一条记录必须以 ``\n`` 收尾。若上一次写入被截断（崩溃/短写残留），
    直接 append 会把新记录**拼进残行**，整行变成非法 JSON —— 该文件中从残行起
    的所有记录（含这条 begin 的 prompt）在读取视图里全部消失。
    所以写前先看最后一个字节：不是换行就先补一个，把旧残行隔离出去。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")

    with _file_lock(path, timeout=max(LOCK_TIMEOUT_SEC, 5.0)):
        with open(path, "a+b") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()

            if size:
                f.seek(-1, os.SEEK_END)
                last = f.read(1)
                if last != b"\n":
                    # 隔离旧的不完整尾行，绝不与新 JSON 拼接。
                    f.seek(0, os.SEEK_END)
                    f.write(b"\n")
                    print(
                        "WARN: JSONL 尾部缺少换行，已隔离旧残行：%s" % path,
                        file=sys.stderr,
                        flush=True,
                    )

            f.seek(0, os.SEEK_END)
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    return path


def _read_jsonl(path):
    """读 JSONL。**读失败返回 None**，与「文件不存在/确实是空」区分开（R376-04）。

    三种情况必须分开，否则调用方无法判断能否继续：
      - 文件不存在   → []      （正常：还没写过）
      - 文件存在且空 → []      （正常：写过但没内容）
      - 读不出来     → None    （异常：权限/编码/IO；不得当成"没有"）
    旧版一律返回 []，于是 pending 读失败被当空文件删掉（唯一副本丢失），
    target 读失败则 existing 被当成空字典，无法判断是否已有同键。
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return None
    out = []
    for lineno, raw in enumerate(lines, 1):
        terminated = raw.endswith("\n")
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            # R388-A1：末行未换行且解析失败 = 被截断的残行；其余 = 中间坏行。
            # 两者都不静默——损坏证据必须可见，否则"读不到"会被误当"没有"。
            kind = ("truncated tail"
                    if lineno == len(lines) and not terminated
                    else "corrupt line")
            print("WARN: JSONL %s: %s:%d" % (kind, path, lineno),
                  file=sys.stderr, flush=True)
            continue
        if isinstance(rec, dict):
            out.append(rec)
        else:
            print("WARN: JSONL non-object record: %s:%d" % (path, lineno),
                  file=sys.stderr, flush=True)
    return out

def _all_log_files():
    """三个桶的全部日志文件（枚举失败只告警，不炸）。"""
    files = []
    for d in (log_dir(), log_pending_dir(), log_unassigned_dir()):
        try:
            files += [os.path.join(d, n) for n in sorted(os.listdir(d))
                      if n.endswith(".jsonl")]
        except OSError as e:
            print("WARN: 无法枚举日志目录 %s: %s" % (d, e),
                  file=sys.stderr, flush=True)
            continue
    return files


def _merge_scalar(slot, key, value):
    """把 value 合并进 slot[key]，遵循 R389-02/R389-03 的身份规则。

    - 空值（""/None）不能覆盖已有有效值；
    - 两个不同有效值 → 打 WARN 标冲突，保留先到的，不静默采用最后一个。
    """
    old = slot.get(key)
    old_empty = old is None or old == ""
    new_empty = value is None or value == ""

    if new_empty and not old_empty:
        return                      # R389-02：空不能覆盖有效
    if old_empty and not new_empty:
        slot[key] = value
        return
    if old_empty and new_empty:
        slot.setdefault(key, value)
        return
    if old == value:
        return
    # R389-03：两个不同有效值
    if key == "conv_id":
        slot["_conv_id_conflict"] = True
        print("WARN: 同一轮出现不同 conv_id（%r vs %r）——保留先到者，冲突可见"
              % (old, value), file=sys.stderr, flush=True)
        return
    slot[key] = value               # 非身份字段：后到者优先（如 end 的 reply）


_TURN_PROMPT_KEYS = ("prompt", "prompt_chars", "prompt_sha256")
_TURN_REPLY_KEYS = ("reply", "reply_chars", "reply_sha256", "reply_state",
                    "reply_file", "status", "error")


def _aggregate_turns(files):
    """把若干日志文件按 turn_id 聚合；返回 (merged, order)。"""
    merged = {}
    order = []

    for file_index, fp in enumerate(files):
        rows = _read_jsonl(fp)
        if rows is None:
            print("WARN: 会话日志读不出来（%s）——本次跳过" % fp,
                  file=sys.stderr, flush=True)
            continue

        for row_index, rec in enumerate(rows):
            tid = str(rec.get("turn_id") or "")
            # 没有 turn_id 的历史记录不能意外合并。
            key = tid or "__legacy__%d_%d" % (file_index, row_index)

            if key not in merged:
                merged[key] = {"_file": fp, "_files": [fp]}
                order.append(key)
            else:
                srcs = merged[key]["_files"]
                if fp not in srcs:
                    srcs.append(fp)     # R389-04：记录全部来源

            slot = merged[key]
            phase = rec.get("phase") or "turn"

            if phase == "begin":
                for k, v in rec.items():
                    if k in _TURN_REPLY_KEYS or k in ("_file", "_files"):
                        continue
                    _merge_scalar(slot, k, v)

            elif phase == "end":
                for k, v in rec.items():
                    if k in _TURN_PROMPT_KEYS or k in ("_file", "_files"):
                        continue
                    _merge_scalar(slot, k, v)

            else:
                # 兼容历史单行格式。
                for k, v in rec.items():
                    if k in ("_file", "_files"):
                        continue
                    _merge_scalar(slot, k, v)
                slot["reply_state"] = _reply_state_of(
                    rec.get("status"), rec.get("reply"), None)

            slot.setdefault("reply_state", "unrecorded")
            if slot.get("status", "__missing__") == "__missing__":
                slot["status"] = None
            slot.setdefault("error", "")

    return merged, order


def iter_turns(conv_id=None):
    """按 turn_id 聚合后输出；可按 conv_id 或 turn_id 过滤（R389-01）。

    旧版先到先胜 + 只查单个桶，会让迁移中间态丢 prompt（pending 有 begin、
    conversations 有 end）。现在先跨全部文件聚合，再按入口 ID 过滤——
    两种入口都能找全同一轮的所有片段。
    """
    merged, order = _aggregate_turns(_all_log_files())

    if conv_id:
        cid = _safe_conv_id(conv_id)
        if cid:
            # UUID 入口：聚合后按 conv_id 匹配（pending 的 begin conv_id 为空，
            # 但合并后该轮已从 end 取得 cid，所以能命中）。
            keys = [k for k in order if merged[k].get("conv_id") == cid]
        else:
            # turn_id 入口：按 turn_id 精确匹配（含 conversations 里的同 turn）。
            keys = [k for k in order if k == str(conv_id)]
    else:
        keys = order

    for key in reversed(keys):
        rec = merged[key]
        rec.pop("phase", None)
        yield rec


def iter_log_records(conv_id=None):
    """兼容旧名：等价于 iter_turns（已跨文件聚合）。"""
    for rec in iter_turns(conv_id):
        yield rec


def log_begin(api, target_conv, prompt_file, prompt_text):
    """发送**之前**固化本轮提示词。返回 (turn_id, path)。

    调用方必须把抛出的异常当成"不许发送"——提示词没能落盘就发出去，
    等于用户要求留存的内容已被使用却无任何记录（R373-02 A）。
    """
    turn_id = "%s-%s" % (datetime.now().strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])
    ptext = prompt_text or ""
    rec = {
        "turn_id": turn_id,
        "phase": "begin",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "target_conv": str(target_conv or ""),
        "account": getattr(api, "account", "") or "",
        "prompt_file": os.path.basename(prompt_file or ""),
        "prompt": ptext,
        "prompt_chars": len(ptext),
        "prompt_sha256": hashlib.sha256(ptext.encode("utf-8")).hexdigest() if ptext else "",
        "reply": "",
        "reply_chars": 0,
        "reply_sha256": "",
        "reply_state": "unrecorded",
        "reply_file": "",
        "status": None,
        "error": "",
    }
    cid = _safe_conv_id(target_conv)
    if cid:
        rec["conv_id"] = cid
        path = _log_write(conversation_log_path(cid), rec)
    else:
        # 新会话此刻还不知道真实 id：先落 pending，拿到 id 后整轮迁移（R373-03）
        rec["conv_id"] = ""
        path = _log_write(pending_log_path(turn_id), rec)
    return turn_id, path

def _turn_payload(rec):
    """比较同键记录内容是否等价（忽略迁移时会改写的 conv_id）。"""
    return {k: v for k, v in rec.items() if k != "conv_id"}


def _migrate_pending(turn_id, begin_path, real_conv):
    """把 pending 的整轮复制进真实会话文件，**确认目标已完整**后才删 pending。

    R376-03 的根因是：靠写入顺序逼近事务性，只能移动崩溃窗口、不能消除它。
    这里改成「以目标文件的实际内容为准」：
      1) 复制 pending 的所有行进目标（(turn_id, phase) 幂等去重）
      2) 回读目标，确认该 turn_id 的 begin + end **都在**
      3) 两者都确认后，才删 pending
    任何一步失败或中途崩溃 → pending 仍在（它持有唯一完整副本）→ 下次重试补齐。
    读不出来一律中止且不删，绝不把"读不到"当成"没有"。
    """
    cid = _safe_conv_id(real_conv)
    if cid is None:
        print("FAIL: 迁移目标 conv_id 畸形（%r）——pending 保留" % (real_conv,), flush=True)
        return None
    if not begin_path:
        return None
    if not cid:
        print("FAIL: 迁移目标 conv_id 缺失——pending 保留", flush=True)
        return None
    if os.path.dirname(os.path.abspath(begin_path)) != os.path.abspath(log_pending_dir()):
        return None
    target = conversation_log_path(cid)

    source = _read_jsonl(begin_path)
    if source is None:
        print("FAIL: pending 读不出来（%s）——保留，不删" % begin_path, flush=True)
        return None
    if not source:
        print("FAIL: pending 为空（%s）——保留，不删" % begin_path, flush=True)
        return None

    target_rows = _read_jsonl(target)
    if target_rows is None:
        # R376-04：目标读不出来就无法判断是否已有同键，继续写可能造成重复，
        # 更可能让后面"回读确认"误判。直接中止，pending 保留。
        print("FAIL: 目标日志读不出来（%s）——迁初中止，pending 保留" % target, flush=True)
        return None

    existing = {}
    for r in target_rows:
        existing[(r.get("turn_id"), r.get("phase") or "turn")] = r

    for rec in source:
        key = (rec.get("turn_id"), rec.get("phase") or "turn")
        if key in existing:
            if _turn_payload(existing[key]) != _turn_payload(rec):
                print("FAIL: 迁移发现同键冲突 %r（内容不同）——保留 pending 待人工处理"
                      % (key,), flush=True)
                return None
            continue
        try:
            moved = dict(rec)
            moved["conv_id"] = cid
            _log_write(target, moved)
            existing[key] = moved          # 写成功才登记
        except Exception as e:
            print("FAIL: 迁移写入失败（%s）——pending 保留，可重试" % e, flush=True)
            return None

    # 回读确认：目标里该 turn_id 的 begin 与 end 都必须在
    verify_rows = _read_jsonl(target)
    if verify_rows is None:
        print("FAIL: 迁移后回读目标失败（%s）——pending 保留" % target, flush=True)
        return None
    # R377-02：不能只校验"键存在"，必须校验**内容一致**。
    # 反例：同键被静默写坏 → 键集合完整、missing 为空 → pending 被删，内容却已损坏。
    # A. 归属校验（R378-01）：目标里任何一条记录的 conv_id 都必须是本次迁移的会话。
    #    同时覆盖：同键挂到了别的会话、目标里混入别会话的记录。
    #    空 conv_id 的历史记录**允许存在**（不要求补齐），但它不是本次迁移的凭据。
    for r in verify_rows:
        rid = _safe_conv_id(r.get("conv_id"))
        if rid is None:
            print("FAIL: 目标记录 conv_id 畸形 %r——pending 保留"
                  % ((r.get("turn_id"), r.get("phase") or "turn"),), flush=True)
            return None
        if rid and rid != cid:
            print("FAIL: 目标记录 conv_id 与本次迁移不符 %r（期望 %s，实际 %s）——pending 保留"
                  % ((r.get("turn_id"), r.get("phase") or "turn"), cid, rid), flush=True)
            return None

    # C-1. 同键多条：彼此 payload 必须一致；且优先选"带本次 cid"的那条作为凭据
    #      （R379：不能因为空身份记录排在前面就选中它）。
    verified = {}
    for r in verify_rows:
        k = (r.get("turn_id"), r.get("phase") or "turn")
        rid = _safe_conv_id(r.get("conv_id"))
        if rid is None:
            print("FAIL: 目标记录 conv_id 畸形 %r——pending 保留"
                  % ((r.get("turn_id"), r.get("phase") or "turn"),), flush=True)
            return None
        if k in verified:
            prev = verified[k]
            if _turn_payload(prev) != _turn_payload(r):
                print("FAIL: 目标存在重复键且内容冲突 %r——pending 保留" % (k,), flush=True)
                return None
            # 已有记录身份不对（空或别人），但本条是本次 cid → 换成它
            if _safe_conv_id(prev.get("conv_id")) != cid and rid == cid:
                verified[k] = r
            continue
        verified[k] = r

    # B + C-2. 每条 source 都必须存在、内容一致，且**归属就是本次 cid**
    for rec in source:
        key = (rec.get("turn_id"), rec.get("phase") or "turn")
        actual = verified.get(key)
        if actual is None:
            print("FAIL: 迁移后缺行 %r——pending 保留，可重试" % (key,), flush=True)
            return None
        if _turn_payload(actual) != _turn_payload(rec):
            print("FAIL: 迁移后内容不一致 %r——pending 保留" % (key,), flush=True)
            return None
        # R379：内容相同还不够——必须能证明这条记录属于本次迁移。
        # 空 conv_id 是历史遗留，不得当作迁移成功的凭据。
        _aid = _safe_conv_id(actual.get("conv_id"))
        if _aid is None:
            print("FAIL: 迁移凭据身份畸形 %r——空/畸形/异会话记录不得冒充"
                  % (key,), flush=True)
            return None
        if _aid != cid:
            print("FAIL: 迁移凭据身份不明 %r（期望 %s，实际 %r）——空/异会话记录不得冒充"
                  % (key, cid, actual.get("conv_id")), flush=True)
            return None

    try:
        os.remove(begin_path)
    except OSError:
        pass          # 删不掉不算失败：读取端按 turn_id 去重合并
    return target


def log_end(api, turn_id, begin_path, target_conv, real_conv, status, reply_path,
            reply_state, error=""):
    """发送之后补写回复。失败会抛出——调用方必须显式报告"回复未留存"。

    R376-03：不再靠"写入顺序"逼近事务性（那只移动崩溃窗口，不消除它）。
    改为：pending 先持有**完整一轮**（begin + end），再整体迁移，
    由 `_migrate_pending` 回读确认目标已含两个 phase 之后才删 pending。
    这样任何一步崩溃，待迁移的那一轮都完整留在 pending 里，可重试。
    """
    reply = getattr(api, "_last_reply_text", None)
    reply = "" if reply is None else reply
    reply_file = ""
    if reply_path:
        try:
            reply_file = os.path.relpath(reply_path, BASE).replace("\\", "/")
        except ValueError:      # Windows 跨盘符
            reply_file = str(reply_path).replace("\\", "/")
    rec = {
        "turn_id": turn_id,
        "phase": "end",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "conv_id": _safe_conv_id(real_conv) or "",
        "reply": reply,
        "reply_chars": len(reply),
        "reply_sha256": hashlib.sha256(reply.encode("utf-8")).hexdigest() if reply else "",
        "reply_state": reply_state,
        "reply_source": getattr(api, "reply_source", "api"),
        "reply_file": reply_file,
        "status": status,
        "error": error or "",
    }
    # 归属优先级：服务器实际落点 > 投递目标。
    # 413/异常时 real_conv 为空，但 target_conv 是已知的——这一轮确实属于它，
    # 落到 unassigned 反而会把已知身份的轮次丢掉（R373-03 的"不得伪装"不等于"一律丢弃"）。
    _rid, _tid = _safe_conv_id(real_conv), _safe_conv_id(target_conv)
    if _rid is None or _tid is None:
        # 畸形身份不能当成"没有身份"往下走——那会把这一轮错误归到 unassigned。
        print("FAIL: conv_id 畸形（real=%r target=%r）——不归属，保留原有记录"
              % (real_conv, target_conv), flush=True)
        return None
    cid = _rid or _tid
    from_pending = bool(begin_path) and (
        os.path.dirname(os.path.abspath(begin_path)) == os.path.abspath(log_pending_dir()))
    if cid:
        rec["conv_id"] = cid
        target = conversation_log_path(cid)
        if from_pending:
            # 先让 pending 变成"完整一轮"，再整体迁移。
            # 顺序反了就会留下"目标有 begin、pending 没了、end 没写"的最坏形态。
            try:
                _log_write(begin_path, rec)
            except Exception as e:
                print("FAIL: end 写入 pending 失败（%s）——本轮回复未留存" % e, flush=True)
                return None
            moved = _migrate_pending(turn_id, begin_path, cid)
            if moved is None:
                print("FAIL: 迁移未完成——完整一轮仍在 pending（%s），可重试"
                      % begin_path, flush=True)
                return begin_path
            return moved
        _log_write(target, rec)      # 非 pending：直接追加，天然是一轮
        return target
    # 真实 id 与目标都不可用：按原始标识单独成文件（R373-03）。
    # 如果本轮 begin 已在 pending，必须保留唯一的 prompt 副本（R381-01）。
    if from_pending and begin_path:
        rec["conv_id"] = ""
        rec["raw_conv_id"] = str(target_conv or "")

        # begin 和 end 必须留在同一个 pending 文件中。
        # _log_write 失败时异常向上传播，原 begin 不受影响。
        _log_write(begin_path, rec)
        return begin_path

    target = unassigned_log_path(target_conv or turn_id)
    rec["conv_id"] = ""
    rec["raw_conv_id"] = str(target_conv or "")
    _log_write(target, rec)

    # 不允许在没有确认 prompt 已安全保存到其他位置时删除 begin。
    # 本分支不再删除 begin_path。
    return target


def search_logs(term, conv_id=None, limit=20):
    """在会话日志里按子串检索（大小写不敏感），返回命中记录（新 -> 旧）。"""
    needle = (term or "").lower()
    hits = []
    for rec in iter_turns(conv_id):
        blob = ((rec.get("prompt") or "") + "\n" + (rec.get("reply") or "")).lower()
        if needle in blob:
            hits.append(rec)
            if limit and len(hits) >= limit:
                break
    return hits

def _log_excerpt(text, term, width=80):
    """命中位置前后各取半个窗口做摘要（换行压成空格，便于单行显示）。"""
    text = text or ""
    i = text.lower().find((term or "").lower())
    if i < 0:
        return text[:width].replace("\n", " ")
    a = max(0, i - width // 2)
    b = min(len(text), i + len(term) + width // 2)
    return (("…" if a else "") + text[a:b].replace("\n", " ") +
            ("…" if b < len(text) else ""))


# R381-06：只拒绝会改变文本视觉顺序、可用于审计欺骗的 Bidi 控制符，
# 不整类拒绝 Cf——ZWJ/ZWNJ/ZWSP 等是正常排版需要（Owner R381 §4 口径）。
_BIDI_CONTROL_CODEPOINTS = frozenset(
    list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A)))


def _bidi_name(cp):
    return "U+%04X" % cp


def validate_prompt_text(text, prompt_file):
    """校验提示词正文。拒绝损坏/无实质内容，避免把垃圾当指令发出去（R377-03）。

    为什么必须显式拒绝：`"\x00".strip()` 仍等于 `"\x00"`，
    旧版 `if not text.strip()` 拦不住纯 NUL 文件——8141 个 NUL 会直接发送，
    还会在日志里留下"正常提示词"的 sha256，事后看不出是损坏。

    判据（Owner 口径）：拒绝任何 NUL；拒绝除 \r\n\t 外的 Cc 控制字符；
    并要求至少存在一个可见非空白字符。
    """
    if not text:
        raise RuntimeError("提示词文件为空：%s" % prompt_file)
    if "\x00" in text:
        raise RuntimeError("提示词文件含 NUL 字符，疑似损坏：%s" % prompt_file)
    for ch in text:
        if unicodedata.category(ch) == "Cc" and ch not in "\r\n\t":
            raise RuntimeError("提示词文件含非法控制字符：%s" % prompt_file)
    if not any(ch.isprintable() and not ch.isspace() for ch in text):
        raise RuntimeError("提示词文件不含可见正文：%s" % prompt_file)
    # R381-06：Bidi 控制符可改变文本视觉顺序（审计欺骗），逐个码点拒绝并报出位置。
    for i, ch in enumerate(text):
        cp = ord(ch)
        if cp in _BIDI_CONTROL_CODEPOINTS:
            raise RuntimeError(
                "提示词含双向控制字符 %s（位置 %d），疑似审计欺骗：%s"
                % (_bidi_name(cp), i, prompt_file))
    return text


def load_prompt_text(prompt_file):
    """读取、校验并规整提示词，返回**实际会被发送**的正文。

    R373-07：发送前由调用方取一次，之后整轮都用这一份不可变文本——
    绝不允许"发送后重新读文件"来补写日志（文件可能已被改写或删除）。
    R377-03：校验必须在写 begin **之前**完成；校验失败不产生 begin 记录。
    """
    try:
        with open(prompt_file, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise RuntimeError("读不到提示词文件 %s：%s" % (prompt_file, e))
    validate_prompt_text(text, prompt_file)
    text = text.strip()
    if not any(not l.strip().startswith("#") for l in text.splitlines()):
        raise RuntimeError("提示词没有正文（只有注释行）：%s" % prompt_file)
    return text



def _send_prompt_impl(
    api,
    conv_id,
    prompt_file,
    new_chat=False,
    thinking=None,
    text=None,
    _landing_store=None,
    _landing_work_item_id=None,
    _landing_attempt_id=None,
):
    """完整投递流程：tail → requirements+PoW → POST → 收割 → 落盘。

    返回 (status, reply_path, real_conv_id)；real_conv_id 用于登记会话归属。
    thinking=True 时用思考模型（`-t-` 变体）；None = 读 CHATGPT_THINKING 环境变量。
    超时取模块级 SEND_TIMEOUT（--timeout / CHATGPT_SEND_TIMEOUT），只改 socket 超时，
    不改模型选择。
    text 已给定时**不再读文件**——保证发送内容与日志留存内容是同一份。
    """
    if conv_id:
        conv_id = safe_id(conv_id, "conv_id")
    # Distinguish failures before POST (safe to rotate) from ambiguous
    # transport failures after a POST may have reached ChatGPT.
    api.last_send_started = False
    if text is None:
        text = load_prompt_text(prompt_file)
    # ponytail: 只做空正文判定，不再删行。旧版 `[l for l in ... if not
    # l.strip().startswith("#")]` 把 Rust 属性 `#[derive]`/`#[test]`/`#[cfg]`
    # 和 markdown 标题一起当注释删掉，投递的代码正文被静默截断（2026-11-21
    # r1d/r1e 实证：提示词 11 个 derive、26 个 test 到达会话时全部消失）。
    # 若将来确实要支持注释头，改成显式标记（如 `//prompt-comment:`）再删。
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
            # 2026-11-20：GET /backend-api/conversation/{id} 有独立配额桶
            # （x-oai-history-detail-rate-limited，retry-after 58s）。
            # 实测省略 parent_message_id 仍落原会话（test_no_parent.py LANDED），
            # 落点由 SSE 首事件唯一 conversation_id 证明，故降级而非失败。
            print("no parent_message_id —— 省略 parent 投递（落点由 SSE 校验）", flush=True)

    if thinking is None:
        thinking = os.environ.get(
            "CHATGPT_THINKING", ""
        ) not in ("", "0", "false", "False")

    model = None

    if thinking:
        # 解析失败时直接传播专用异常。
        # 不在 send_prompt 内降级 auto，也不调用 SystemExit。
        model = api.resolve_thinking_model(api)

        # A27 最终防线：
        # 即使解析函数被替换、mock 或错误实现，也不能让 auto 通过。
        if not model or "-t-" not in str(model):
            raise ThinkingModelUnavailable(
                reason_code="THINKING_MODEL_UNAVAILABLE",
                account=getattr(api, "account", "unknown"),
                detail="resolver 返回了无效思考模型：%r" % (model,),
                model_slug=model,
            )

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
        # --timeout / CHATGPT_SEND_TIMEOUT：在分档**之后**覆盖。
        # 分档逻辑本身一字不动（--thinking 的语义不变），只是给这条 socket 超时一个可配值。
        # 未显式配置时不打印 `timeout:` 行（分档值由 SEND 行的 `in Ns` 体现）——那行只是
        # 人工排障用的旁证，不得反过来成为「必须打印」的默认值来源。
        override = SEND_TIMEOUT
        if override is not None:
            send_timeout = override
            print("timeout: %ss (source=%s)"
                  % (send_timeout, SEND_TIMEOUT_SOURCE or "env"), flush=True)
        api.last_send_started = True
        st, resp = api.send(conv_id, text, tail, req_token, proof, model=model,
                            timeout=send_timeout)
        # 截断/超时被 _partial_result 折成 200 交回既有解析链路（不丢字节），但
        # `SEND ... -> 200` 是唯一送达凭证——截断响应绝不能打它，否则一次超时
        # 在日志、退出码、会话登记三个门禁上同时是绿的（DEF-ops-01）。
        send_truncated = bool(getattr(api, "last_truncated", False))
        if send_truncated:
            print("SEND_TRUNCATED pfile=%s st=%s bytes=%s"
                  % (pfile, st, getattr(api, "last_partial_bytes", 0)), flush=True)
        else:
            print("SEND %s -> %s respLen=%d in %.0fs"
                  % (pfile, st, len(resp), time.time() - t0), flush=True)
        if send_truncated or "TIMEOUT_" in (resp or "") or getattr(api, "last_partial_path", None):
            print("SEND_TIMEOUT_MARK pfile=%s budget=%ss st=%s partial_path=%s"
                  % (pfile, send_timeout, st,
                     getattr(api, "last_partial_path", None) or "-"), flush=True)
        if st == 403 and "Unusual activity" in (resp or "") and attempt == 1:
            print("403 Unusual activity —— 补 Cloudflare cookie 并重跑 requirements + PoW 重试一次", flush=True)
            api.warm_cookies()
            time.sleep(3)
            continue
        break

    if st == 413:
        print(
            "HTTP 413 Payload Too Large —— 本次发送终止；不轮换账号，不自动重试。"
            "请检查实际请求体及服务端错误响应。",
            flush=True,
        )
        return 413, None, None
    if st != 200:
        print("resp: %s" % resp[:200], flush=True)
        return st, None, None

    # 校验 conversation_id：防静默落新会话事故
    # F-13：只认**首个** data 事件里的 id，且必须**唯一**。
    # 旧版是「一路往后找，找到就用」，还会在整包 JSON、正文正则里继续捞——
    # 等于「响应里出现过什么 id 就用哪个」；一旦后面出现另一个 id，
    # 落点判定就建立在了一个无法确认归属的 id 上。
    first_cid, all_cids = _sse_conv_ids(resp)
    parsed = first_cid
    if not parsed:
        # F-13/F-16（第二十九轮审查）：**不再做整包 JSON / 正文正则兜底**。
        # 旧版在 SSE 解不出 id 时扫整包 JSON、再用正则在响应任意位置捞 id——
        # 这会捞到 `[DONE]` 之后的事件（审查方给出的反例：先 [DONE]，后写 B），
        # 把「响应里出现过什么 id」当成「服务器实际落在哪个会话」的证据。
        # 落点证明必须只信受信任的结构化来源；解不出就失败关闭。
        print("FAIL: 200 但 SSE 首个事件解析不出 conversation_id（无法证明落点会话）；"
              "按 A11 判失败，不重投、不做正文正则兜底", flush=True)
        return (1, None,
                ConvRef(conv_id, mismatch=True,
                        reason="conversation_id_unresolved: SSE 结构化事件里没有可用的 id"))
    # 唯一性：同一响应里出现两个不同 id → 无法证明落点，失败关闭
    if len(all_cids) > 1:
        print("FAIL: 同一响应里出现多个 conversation_id（%s）——无法确认落点，"
              "按 A11 判失败，不落盘" % ", ".join(all_cids), flush=True)
        # F-17：失败结果要带 target 与候选 id，不能把诊断信息丢掉
        return (1, None,
                ConvRef(conv_id, mismatch=True,
                        reason="ambiguous_conversation_id: 同一响应里出现多个 id",
                        candidate_ids=all_cids))
    # A11 硬要求：解析不出唯一且合规的 id 就判失败，禁止「WARN 一下接着走」或换身份重投。
    # 旧版在 id 不合规时回落到传入的 conv_id 继续落盘，等于把「没认回会话」粉饰成成功。
    if not parsed:
        print("FAIL: 200 但解析不出 conversation_id（无法证明落点会话）；按 A11 判失败，不重投",
              flush=True)
        return (1, None, ConvRef(conv_id, mismatch=True,
                                 reason="conversation_id_unresolved: 响应里没有可用的 id"))
    if not _ID_RE.match(parsed):
        print("FAIL: 解析出的 conversation_id 不合规（%r）；按 A11 判失败，不重投、不落盘"
              % (parsed,), flush=True)
        return (1, None, ConvRef(conv_id, mismatch=True,
                                 reason="conversation_id_invalid: %r" % (parsed,)))
    real_conv = parsed
    mismatch = bool(conv_id) and real_conv != conv_id

    reply, sse_complete, sse_detail = api.extract_reply_sse_checked(resp)
    map_complete = True
    if not reply:
        reply, map_complete = api.harvest_mapping_checked(resp)
        if reply and not map_complete:
            print("WARN: mapping 收割不完整——本次回复按「尽力而为」处理", flush=True)
    elif not sse_complete:
        # 部分流不得冒充完整回复：明确记下来，内容照收（第二十六轮审查口径）
        print("WARN: SSE 流不完整（%s）——本次回复按「部分流」处理" % sse_detail, flush=True)
    # F-23（第二十九轮审查）：完整性证明必须**真正参与放行**。
    # 旧版只在 `last_truncated`（IncompleteRead）时 GET 补全；SSE 没有 [DONE]
    # 但也没触发 IncompleteRead 时，部分流被直接正常归档 → fail-open。
    # 现在：只要 SSE/mapping 任一不完整就强制 GET 补全与完整性核验；
    # 仍无法证明完整时**不许冒充完整回复**——在归档文件头写明不完整声明。
    # 注意：不阻断归档（第二十六轮口径「解析失败不能阻断已获得内容的归档」），
    # 但归档件必须自带可机器识别的不完整标记。
    reply_complete = bool(sse_complete and map_complete and reply)
    completeness_note = ""
    if (not reply_complete or getattr(api, "last_truncated", False)) and real_conv:
        # GET 的目标必须是**服务器实际写入的那个会话**（real_conv），
        # 不能用 conv_id —— 落点不一致时按目标会话拉回来的，是别的会话的内容。
        full = ""
        digest_ok = False
        gst, gbody = api.req("GET", API_BASE + "/backend-api/conversation/" + real_conv,
                             timeout=_cap_timeout(120))
        if gst == 200:
            full, gcomplete = api.harvest_mapping_checked(gbody)
            if not gcomplete:
                print("WARN: GET 回来的 mapping 不完整——按尽力而为处理", flush=True)
            # A12：按 part 统计对账，不只看「长度变长了」。
            # 对账器是**辅助核验**，绝不能成为归档的前置硬依赖——它崩了/核不了，
            # 也必须继续 save_reply（否则唯一副本反而丢了）。
            digest = None
            try:
                digest = reply_digest(gbody)
            except Exception as e:
                print("WARN: 全文对账器异常（%s: %s）——跳过对账，继续归档"
                      % (type(e).__name__, e), flush=True)
            digest_ok = False
            if digest:
                print("GET 全文核对：message=%s parts=%s chars=%d sha256=%s"
                      % ((digest["message_id"] or "?")[:12], digest["parts"],
                         digest["chars"], digest["sha256"][:12]), flush=True)
                # F-24：digest 必须真正参与完整性判定——旧版只 print 一句
                # 「parts 合计 ≠ 拼接」，判定结果没接进任何放行条件。
                # 这里要求三个条件同时成立才算对账通过：
                #   ① GET mapping 自身完整（gcomplete）
                #   ② parts 合计 == 拼接长度（无缺 part）
                #   ③ 摘要的 sha256 与「按 parts 拼接出来的 full」逐字节一致
                #      （不是拿 digest 自己的 chars 和自己比，那是同源自证）
                digest_ok = bool(gcomplete and digest["chars"] == len(full) and
                                 digest["sha256"] == hashlib.sha256(
                                     full.encode("utf-8")).hexdigest())
                if not digest_ok:
                    print("WARN: GET 全文对账未通过（gcomplete=%s parts_chars=%d "
                          "joined_chars=%d）——本次回复按不完整处理"
                          % (gcomplete, digest["chars"], len(full)), flush=True)
            else:
                print("WARN: GET 全文无法对账（mapping 畸形）——按尽力而为处理，继续归档",
                      flush=True)
        if len(full) > len(reply or ""):
            print("补全文：SSE %d 字符 → GET %d 字符" % (len(reply or ""), len(full)), flush=True)
            reply = full
        else:
            print("WARN: GET 未取到更完整内容（GET st=%s，现有 %d 字符）"
                  % (gst, len(reply or "")), flush=True)
        # F-24：完整性判定真正接进放行：只有 GET 对账通过才能把内容标成「完整」。
        # 对账失败不阻断归档（保住唯一副本），但必须在归档件上留下机器可读的标记。
        if digest_ok and len(full) >= len(reply or ""):
            reply_complete = True
        else:
            reply_complete = False
    if not reply_complete and reply:
        completeness_note = (
            "⚠️ 回复完整性未证明：SSE 未正常以 [DONE] 收尾或 GET 全文对账未通过；"
            "以下内容为尽力而为收割，可能不完整，不得当作定稿。\n"
        )
        print("FAIL: 回复完整性未证明——归档件将明确标注，不冒充完整回复", flush=True)

    if mismatch:
        # A10/F-13：落点不一致 = 会话身份与回复归属分裂。
        # 旧版只 WARN 一句，随后把落在 B 的回复归档在 A 名下、还返回 A + 200
        # —— 下游据此认为「A 已经回完了」，拿着错误的 conversation id 继续操作。
        # 现在 fail-closed，四件事一起做：
        #   ① 业务结果判失败（不返回成功、不把回复归档成 A 的正常回复）
        #   ② 保全已收到的回复（写入独立隔离档案，带两个 id 与原始响应证据）
        #   ③ 不静默改归属（既不改成 B 蒙混过关，也不丢弃）
        #   ④ 失败返回携带两个 id（target 与 actual）
        print("FAIL: 落点不一致——投递 target=%s，服务器实际落在 actual=%s。"
              "按 A10/F-13 判失败，不静默改归属" % (conv_id, real_conv), flush=True)
        qpath, evidence_complete = _quarantine_reply(
            api, conv_id, real_conv, reply, resp, st, pfile)
        if qpath and evidence_complete:
            print("FAIL: 回复已完整隔离保全 -> %s（不得当成 %s 的正常会话回复）"
                  % (qpath, conv_id), flush=True)
        elif qpath:
            print("FAIL: 隔离档案已落盘但**证据不完整** -> %s；"
                  "原始响应/待认领登记可能缺失，需人工重取 actual=%s"
                  % (qpath, real_conv), flush=True)
        else:
            evidence_complete = False
            print("FAIL: 隔离归档未能落盘——回复可能已丢失，需人工重取 actual=%s"
                  % real_conv, flush=True)
        return (LANDING_MISMATCH_STATUS, qpath,
                ConvRef(conv_id, actual_conv_id=real_conv, mismatch=True,
                        reason="landing_mismatch: 投递目标与服务器实际落点不一致",
                        evidence_complete=evidence_complete,
                        send_truncated=send_truncated))

    # F-23：不完整的回复照归档（保住唯一副本），但归档文本自带醒目声明——
    # 下游看到这个标记就知道「这不是定稿」，不会把它当完整回复继续操作。
    archived = (completeness_note + reply) if completeness_note else reply
    path = api.save_reply(real_conv, pfile, archived)


    # D7: only the real successful send path may create consumable evidence.
    # The attempt identity is created before sending and passed unchanged
    # through transport retries. The event is bound to all three identities.
    if _landing_store is not None:
        try:
            from src.session_ops.authority import (
                validate_conversation_id,
                validate_work_item_id,
            )

            validate_work_item_id(_landing_work_item_id)
            validate_conversation_id(real_conv)

            if (
                not isinstance(_landing_attempt_id, str)
                or not _landing_attempt_id.startswith("att_")
            ):
                raise RuntimeError("trusted landing attempt_id is missing")

            landing_confirmed = (
                st == 200
                and not send_truncated
                and first_cid == real_conv
                and len(all_cids) == 1
                and all_cids[0] == real_conv
                and (not conv_id or conv_id == real_conv)
                and not mismatch
                and reply_complete is True
                and bool(path)
                and os.path.isfile(path)
            )

            if landing_confirmed:
                _landing_store._record_trusted_landing(
                    real_conv,
                    "confirmed",
                    True,
                    _landing_work_item_id,
                    _landing_attempt_id,
                )
        except Exception as exc:
            print(
                "WARN: trusted landing event was not recorded "
                "(%s); the sent message remains archived and unchanged"
                % exc,
                flush=True,
            )
    return st, path, ConvRef(real_conv, reply_complete=reply_complete,
                             send_truncated=send_truncated)


def _reply_state_of(status, reply, conv):
    """把"这轮到底有没有拿到回复"显式化（R373-05）。

    空字符串本身不能区分"没拿到"和"拿到了空回复"——那是两种完全不同的事实。
    """
    if status is None:
        return "unavailable"          # 未走到发送/异常中断
    if status == 200:
        if reply:
            complete = getattr(conv, "reply_complete", None)
            if complete is False:
                return "partial"      # 收割不完整，不得冒充完整
            return "captured"
        return "empty"                # 200 但正文为空：明确记"空"，不记"没有"
    if reply:
        # 非 200 但确实收到了正文（落点不一致被隔离）：内容是拿到了，只是不算正常回复
        return "quarantined"
    return "unavailable"              # 413/非 200 且无正文：本来就没有回复



def send_prompt_for_work_item(
    api,
    store,
    principal,
    work_item_id,
    conv_id,
    prompt_file,
    new_chat=False,
    thinking=None,
    attempt_id=None,
):
    """Send one WorkItem attempt, record its landing, then link it.

    A fresh attempt_id is allocated once per invocation, before sending.
    Retries inside _send_prompt_impl reuse that same attempt_id.
    A later independent invocation receives a different attempt_id.
    """
    import uuid

    from src.session_ops.authority import (
        Principal,
        SecureWorkItemStore,
        validate_work_item_id,
    )

    if not isinstance(store, SecureWorkItemStore):
        raise TypeError("store must be SecureWorkItemStore")
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a verified Principal")

    validate_work_item_id(work_item_id)
    if getattr(api, "is_anon", False):
        raise ValueError(
            "anonymous sends cannot establish trusted landing evidence"
        )

    if attempt_id is None:
        attempt_id = "att_" + str(uuid.uuid4())
    elif (
        not isinstance(attempt_id, str)
        or re.fullmatch(
            r"att_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            attempt_id,
        ) is None
    ):
        raise ValueError("attempt_id must be a valid att_UUIDv4")

    # Match main's R373-07 flow: read and validate once, before logging or sending.
    text = load_prompt_text(prompt_file)
    turn_id, begin_path = log_begin(api, conv_id, prompt_file, text)

    api._last_reply_text = None
    try:
        result = _send_prompt_impl(
            api,
            conv_id,
            prompt_file,
            new_chat,
            thinking,
            text=text,
            _landing_store=store,
            _landing_work_item_id=work_item_id,
            _landing_attempt_id=attempt_id,
        )
    except BaseException as exc:
        try:
            log_end(
                api,
                turn_id,
                begin_path,
                conv_id,
                None,
                None,
                None,
                "unavailable",
                error="%s: %s" % (type(exc).__name__, exc),
            )
        except Exception as log_exc:
            print(
                "FAIL: 异常轮次补写失败（%s）——该轮只有 begin，"
                "读取时会显示未完成" % log_exc,
                flush=True,
            )
        raise

    status, path, conv = (tuple(result) + (None, None, None))[:3]
    reply = getattr(api, "_last_reply_text", None)
    reply_state = _reply_state_of(status, reply, conv)

    try:
        log_end(
            api,
            turn_id,
            begin_path,
            conv_id,
            conv,
            status,
            path,
            reply_state,
        )
    except Exception as log_exc:
        print(
            "FAIL: 回复留存失败（%s）——本轮只有 begin（提示词已存），"
            "状态将显示 unrecorded；回复正文在 %s"
            % (log_exc, path or "未落盘"),
            flush=True,
        )

    if (
        status != 200
        or not isinstance(conv, ConvRef)
        or conv.mismatch
        or conv.reply_complete is not True
    ):
        return result, None, None

    try:
        link = store.link_session(
            principal,
            str(conv),
            work_item_id,
            attempt_id,
        )
        return result, link, None
    except Exception as exc:
        # link_session owns consume + link atomicity. A deterministic
        # active-link conflict is marked REJECTED only after its link
        # transaction has rolled back. Other DB failures remain retryable.
        return result, None, exc
def send_prompt(api, conv_id, prompt_file, new_chat=False, thinking=None):
    """完整投递流程（外层）：先固化提示词，再发送，最后补写回复。

    R373-02 的核心：**提示词必须在发送前落盘**。留不住就不发送——
    否则用户要求留存的内容已被使用却没有任何记录，且事后无法补救。

    回复落盘失败无法撤销已发生的交互，因此不阻断返回；但该轮只有 begin 没有 end，
    读取端（iter_turns）会把它显示成未完成（reply_state=unrecorded），不是成功。
    """
    # R373-07：一次读取、整轮复用，绝不在发送后重读文件补写日志
    text = load_prompt_text(prompt_file)

    turn_id, begin_path = log_begin(api, conv_id, prompt_file, text)   # 失败即抛：不许发送

    try:
        api._last_reply_text = None      # None=未捕获；""=明确空回复（R373-05）
        reply_state = "unavailable"
        result = _send_prompt_impl(api, conv_id, prompt_file, new_chat, thinking,
                                   text=text)
    except BaseException as e:
        # 抛异常也要补齐本轮：begin 已落盘，这里补 end 让状态可见
        try:
            log_end(api, turn_id, begin_path, conv_id, None, None, None,
                    "unavailable", error="%s: %s" % (type(e).__name__, e))
        except Exception as le:
            print("FAIL: 异常轮次补写失败（%s）——该轮只有 begin，读取时会显示未完成"
                  % le, flush=True)
        raise

    status, path, conv = (tuple(result) + (None, None, None))[:3]
    reply = getattr(api, "_last_reply_text", None)
    reply_state = _reply_state_of(status, reply, conv)
    try:
        log_end(api, turn_id, begin_path, conv_id, conv, status, path,
                reply_state)
    except Exception as le:
        # 回复已拿到但日志补写失败：明确报错，不静默。begin 行仍在，轮次可追溯。
        print("FAIL: 回复留存失败（%s）——本轮只有 begin（提示词已存），"
              "状态将显示 unrecorded；回复正文在 %s" % (le, path or "未落盘"),
              flush=True)
    return result


def _attempt_create(
    st, name, prompt_file, attempts, thinking=None, work_item_context=None
):
    """用指定身份建会话并记录结果；返回 (status, conv_id)。

    HTTP 413 是本次发送的终止信号，不是账号级故障，
    不增加失败计数，不设置账号冷却。
    """
    try:
        api = ChatGPTAPI(account=name)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        def init_fail(s):
            mark_unavailable(s, name, "credentials unavailable: %s" % e)
            s["failures"][name] = s.get("failures", {}).get(name, 0) + 1

        st.update(mutate_rotation(init_fail))
        attempts.append({
            "account": name,
            "status": "INIT_FAIL",
            "detail": str(e),
        })
        print("rotate: %s 凭据不可用，跳过（%s）" % (name, e), flush=True)
        return "INIT_FAIL", None

    try:
        if work_item_context is None:
            status, _path, conv = send_prompt(
                api,
                None,
                prompt_file,
                new_chat=True,
                thinking=thinking,
            )
        else:
            result, link, link_error = send_prompt_for_work_item(
                api,
                work_item_context["store"],
                work_item_context["principal"],
                work_item_context["work_item_id"],
                None,
                prompt_file,
                new_chat=True,
                thinking=thinking,
                attempt_id=work_item_context["attempt_id"],
            )
            status, _path, conv = (tuple(result) + (None, None, None))[:3]
            ambiguous_transport = (
                status in (408, 425)
                or (isinstance(status, int) and status >= 500)
                or (status == 0 and bool(getattr(api, "last_send_started", False)))
            )
            if ambiguous_transport:
                detail = "send outcome unknown after POST attempt (status=%s)" % status
                if work_item_context is not None:
                    work_item_context.update({
                        "status": status,
                        "api": api,
                        "link": None,
                        "link_error": detail,
                        "conversation_id": None,
                        "account": name,
                        "outcome_unknown": True,
                    })
                attempts.append({"account": name, "status": "OUTCOME_UNKNOWN", "reason": detail})
                return "OUTCOME_UNKNOWN", None
            if isinstance(conv, ConvRef) and conv.mismatch:
                # HTTP 200 was accepted but the returned conversation identity
                # is not trustworthy. Never rotate and resend this prompt.
                actual = getattr(conv, "actual_conv_id", "") or ""
                reason = getattr(conv, "reason", "") or "conversation identity mismatch"
                detail = "conversation outcome unknown: %s; actual=%s" % (reason, actual or "unknown")
                work_item_context.update({
                    "status": 200,
                    "api": api,
                    "link": None,
                    "link_error": detail,
                    "conversation_id": (getattr(conv, "target_conv_id", "") or str(conv) or None),
                    "account": name,
                    "outcome_unknown": True,
                })
                attempts.append({
                    "account": name,
                    "status": "OUTCOME_UNKNOWN",
                    "reason": reason,
                    "actual_conv_id": actual or None,
                })
                return "OUTCOME_UNKNOWN", conv
            work_item_context["status"] = status
            work_item_context["api"] = api
            work_item_context["link"] = link
            work_item_context["link_error"] = link_error
            if status is None or (
                isinstance(status, int)
                and (status in (408, 425) or status >= 500)
            ):
                # A server error or transport timeout does not prove the
                # remote side rejected the prompt. Stop account rotation here;
                # trying another account could deliver the same prompt twice.
                detail = "send outcome unknown after HTTP %s" % (
                    status if status is not None else "no response"
                )
                work_item_context.update({
                    "link": None,
                    "link_error": detail,
                    "account": name,
                    "outcome_unknown": True,
                })
                attempts.append({
                    "account": name,
                    "status": "OUTCOME_UNKNOWN",
                    "reason": detail,
                })
                return "OUTCOME_UNKNOWN", conv

    except ThinkingModelUnavailable as e:
        # 思考模型不可用是账号级失败，不是整个进程的退出信号。
        # 记录账号状态，让 create_chat_rotating 尝试下一个身份。
        status, conv = "THINKING_UNAVAILABLE", None

        reason = "THINKING_UNAVAILABLE[%s]: %s" % (
            e.reason_code,
            e.detail,
        )

        def thinking_failed(s):
            s["failures"][name] = (
                s.get("failures", {}).get(name, 0) + 1
            )
            mark_unavailable(s, name, reason)

        st.update(mutate_rotation(thinking_failed))

        attempts.append({
            "account": name,
            "status": status,
            "reason_code": e.reason_code,
            "detail": e.detail,
        })

        print(
            "rotate: %s 思考模型不可用（%s），切下一身份"
            % (name, e.reason_code),
            flush=True,
        )

        return status, None

    except Exception as e:
        status, conv = 0, None
        if bool(getattr(api, "last_send_started", False)):
            detail = "send outcome unknown after POST exception: %s: %s" % (type(e).__name__, e)
            if work_item_context is not None:
                work_item_context.update({
                    "status": 0,
                    "api": api,
                    "link": None,
                    "link_error": detail,
                    "conversation_id": None,
                    "account": name,
                    "outcome_unknown": True,
                })
            attempts.append({"account": name, "status": "OUTCOME_UNKNOWN", "reason": detail})
            return "OUTCOME_UNKNOWN", None

        print(
            "rotate: %s 通道异常（%s）" % (name, e),
            flush=True,
        )

    attempts.append({
        "account": name,
        "status": status,
        "conv_id": conv,
    })

    if status == 200:
        return status, conv

    # HTTP 413 terminates this send.
    # It must not modify account failures or cooldown state.
    if status == 413:
        print(
            "rotate: %s 收到 HTTP 413，本次发送终止；不轮换账号、不设置冷却"
            % name,
            flush=True,
        )
        return 413, None

    reason, ttl = None, None

    if status == 401:
        reason = "HTTP 401（认证失效，需重新提取凭据）"
    elif status == 403:
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

    print(
        "rotate: %s 建会话失败(st=%s)，切下一身份" % (name, status),
        flush=True,
    )
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


def create_chat_rotating(
    prompt_file, thinking=None, *, work_item_context=None, account=None
):
    """登录态优先建会话：acct1/acct2/acct3/acct4/acct5/acct6 均分轮转；
    仅当全部都 429 才用匿名兜底。

    匿名会话不进任何账号历史（会话列表恒为空），只按 conv_id 短期可读，
    因此只作为限速时的应急产能，不承担需要留存/续接的任务。

    HTTP 413 是单次发送的终止结果，不允许轮换或匿名兜底。
    """
    if thinking is None:
        thinking_requested = os.environ.get(
            "CHATGPT_THINKING", ""
        ) not in ("", "0", "false", "False")
    else:
        thinking_requested = bool(thinking)

    st = load_rotation()
    if purge_expired(st):
        st = mutate_rotation(purge_expired)

    outcomes, attempts, skipped = {}, [], {}

    for name in ROTATION_PRIMARY:
        if is_unavailable(st, name):
            reason = _as_dict(
                _as_dict(st.get("unavailable")).get(name)
            ).get("reason", "unavailable")
            skipped[name] = reason
            outcomes[name] = "SKIP:" + reason

    if account not in (None, "auto"):
        if account not in ROTATION_PRIMARY:
            raise ValueError("account is not a configured authenticated account")
        plan = [account] if not is_unavailable(st, account) else []
    else:
        plan = [a for a in rotation_plan(st) if a not in skipped]

    print(
        "rotation plan: %s (cursor=%s)%s" % (
            " -> ".join(plan) or "(无可用登录态)",
            st["cursor"],
            "；跳过 " + json.dumps(skipped, ensure_ascii=False)
            if skipped else "",
        ),
        flush=True,
    )

    for name in plan:
        if work_item_context is None:
            status, conv = _attempt_create(
                st,
                name,
                prompt_file,
                attempts,
                thinking_requested,
            )
        else:
            status, conv = _attempt_create(
                st,
                name,
                prompt_file,
                attempts,
                thinking_requested,
                work_item_context=work_item_context,
            )
        outcomes[name] = status

        # An HTTP 200 with an untrusted conversation identity is ambiguous:
        # stop here, preserve outcome_unknown, and never rotate/re-send.
        if status == "OUTCOME_UNKNOWN":
            print(
                json.dumps(
                    {
                        "result": "outcome_unknown",
                        "terminal": True,
                        "account": name,
                        "outcomes": outcomes,
                        "attempts": attempts,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 1

        # Terminal failure: stop immediately.
        # Never attempt another account or anonymous fallback.
        if status == 413:
            print(
                json.dumps(
                    {
                        "result": "failed",
                        "terminal": True,
                        "terminal_cause": "HTTP_413",
                        "account": name,
                        "outcomes": outcomes,
                        "attempts": attempts,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 1

        if conv:
            _mark_success(st, name, conv, prompt_file, attempts)
            if work_item_context is not None:
                work_item_context["account"] = name
                work_item_context["conversation_id"] = str(conv)
            return _send_exit_code(status, conv)

        if status == 200:
            print(
                "WARN: %s 建会话返回 200 但未解析出 conv_id（消息可能已送达），"
                "为免重复建会话停止轮转" % name,
                flush=True,
            )
            print(
                json.dumps(
                    {
                        "result": "ok_no_conv_id",
                        "account": name,
                        "attempts": attempts,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 0

    # 强制思考模式：四个登录态都不能提供 -t- 模型时，
    # 必须明确失败，不允许匿名兜底，也不允许 auto 降级。
    def is_thinking_unavailable(value):
        if value == "THINKING_UNAVAILABLE":
            return True

        if isinstance(value, str):
            return value.startswith(
                "SKIP:THINKING_UNAVAILABLE["
            )

        return False

    all_thinking_unavailable = all(
        is_thinking_unavailable(outcomes.get(name))
        for name in ROTATION_PRIMARY
    )

    if thinking_requested and all_thinking_unavailable:
        print(
            json.dumps(
                {
                    "result": "failed",
                    "terminal": True,
                    "terminal_cause": "THINKING_MODEL_UNAVAILABLE",
                    "outcomes": outcomes,
                    "attempts": attempts,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1

    block = (
        "trusted WorkItem sends cannot use anonymous sessions"
        if work_item_context is not None
        else anon_fallback_block_reason(st, outcomes)
    )

    if block is None:
        print(
            "全部登录态均 429 —— 启用匿名兜底"
            "（不入历史，仅短期可读，勿用于长任务）",
            flush=True,
        )

        status, conv = _attempt_create(
            st, "nologin", prompt_file, attempts, thinking
        )

        if conv:
            _mark_success(st, "nologin", conv, prompt_file, attempts)
            print(
                "WARN: 该会话为匿名会话，不出现在任何账号的会话列表，"
                "勿用于需留存的长任务",
                flush=True,
            )
            return _send_exit_code(status, conv)

        if status == 200:
            print(
                "WARN: 匿名会话已建立但未解析出 conv_id，"
                "停止重试以免重复建会话",
                flush=True,
            )
            print(
                json.dumps(
                    {
                        "result": "ok_no_conv_id",
                        "account": "nologin",
                        "attempts": attempts,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 0

        outcomes["nologin"] = status

    else:
        print("匿名兜底未放行：%s" % block, flush=True)

    print(
        json.dumps(
            {
                "result": "failed",
                "outcomes": outcomes,
                "attempts": attempts,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 1

def rotation_summary():
    st = load_rotation()
    created = st.get("created", {})
    prim = {a: _as_float(created.get(a), 0.0) for a in ROTATION_PRIMARY}   # F-30
    prim_total = sum(prim.values()) or 1
    return {
        "policy": "acct1/acct2/acct3/acct4/acct5/acct6 均分轮转；nologin 仅在全部 429 时兜底（其会话不入历史）",
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


def reconcile_conversation_ownership(
    enumeration_results,
    registry_snapshot,
    configured_accounts,
):
    """Compare complete per-account observations with a registry snapshot.

    enumeration_results maps account names to:
        {"status": int, "error": str, "items": list, "state": str}

    state is "complete", "failed", or "not_attempted".
    A successful result requires status == 200 and an empty error.
    This function is pure: it does not read or write files.
    """
    configured = tuple(configured_accounts)
    registry = (
        registry_snapshot
        if isinstance(registry_snapshot, dict)
        else {}
    )

    enumeration = {}
    all_complete = True
    observations = {}
    invalid_ids = 0

    for account in configured:
        result = enumeration_results.get(account)
        if not isinstance(result, dict):
            result = {
                "status": None,
                "error": "account was not enumerated",
                "items": [],
                "state": "not_attempted",
            }

        status = result.get("status")
        error = result.get("error") or ""
        state = result.get("state") or (
            "complete" if status == 200 and not error else "failed"
        )
        successful = (
            state == "complete"
            and status == 200
            and not error
        )

        enumeration[account] = {
            "state": "complete" if successful else state,
            "status": status,
            "error": error,
        }

        if not successful:
            all_complete = False
            continue

        items = result.get("items")
        if not isinstance(items, list):
            enumeration[account]["state"] = "failed"
            enumeration[account]["error"] = (
                "invalid internal item collection"
            )
            all_complete = False
            continue

        for item in items:
            if not isinstance(item, dict):
                continue

            raw_id = item.get("id")
            try:
                conv_id = safe_id(raw_id, "conversation id")
            except (TypeError, ValueError):
                invalid_ids += 1
                continue

            if conv_id != raw_id:
                invalid_ids += 1
                continue

            observations.setdefault(conv_id, set()).add(account)

    duplicates = []
    observed_not_registered = []
    registered_not_observed = []
    registry_conflicts = []
    malformed_registry_records = []
    eligible_registrations = []

    for conv_id, accounts in sorted(observations.items()):
        observed_accounts = sorted(accounts)

        if len(observed_accounts) > 1:
            duplicates.append({
                "id": conv_id,
                "accounts": observed_accounts,
                "status": "duplicate_observation",
            })
            continue

        observed_account = observed_accounts[0]
        existing = registry.get(conv_id)

        if conv_id not in registry:
            observed_not_registered.append({
                "id": conv_id,
                "account": observed_account,
            })
            if (
                observed_account in configured
                and observed_account != "nologin"
            ):
                eligible_registrations.append({
                    "id": conv_id,
                    "account": observed_account,
                })
            continue

        if not isinstance(existing, dict):
            registry_conflicts.append({
                "id": conv_id,
                "observed_accounts": observed_accounts,
                "registry_account": None,
                "reason": (
                    "existing registry record is not a canonical dictionary"
                ),
            })
            continue

        registry_account = existing.get("account")
        if not validate_account_identity(registry_account):
            registry_conflicts.append({
                "id": conv_id,
                "observed_accounts": observed_accounts,
                "registry_account": None,
                "reason": "existing registry account is invalid",
            })
            continue

        try:
            registry_canonical = normalize_account_identity(
                registry_account
            )
            observed_canonical = normalize_account_identity(
                observed_account
            )
        except (TypeError, ValueError):
            registry_conflicts.append({
                "id": conv_id,
                "observed_accounts": observed_accounts,
                "registry_account": registry_account,
                "reason": "account identity normalization failed",
            })
            continue

        owner = existing.get("owner")
        if validate_account_identity(owner):
            try:
                owner_canonical = normalize_account_identity(owner)
            except (TypeError, ValueError):
                owner_canonical = None

            if (
                owner_canonical is not None
                and owner_canonical != registry_canonical
            ):
                registry_conflicts.append({
                    "id": conv_id,
                    "observed_accounts": observed_accounts,
                    "registry_account": registry_account,
                    "reason": (
                        "registry owner disagrees with registry account"
                    ),
                })
                continue

        if registry_canonical != observed_canonical:
            registry_conflicts.append({
                "id": conv_id,
                "observed_accounts": observed_accounts,
                "registry_account": registry_account,
                "reason": (
                    "observed account disagrees with registered owner"
                ),
            })

    observed_ids = set(observations)

    for conv_id, record in registry.items():
        if conv_id in observed_ids:
            continue

        if isinstance(record, str):
            if not validate_account_identity(record):
                malformed_registry_records.append({
                    "id": conv_id,
                    "reason": "legacy registry account identity is invalid",
                })
                continue

            if record == "nologin":
                continue

            registered_not_observed.append({
                "id": conv_id,
                "account": record,
            })
            continue

        if not isinstance(record, dict):
            malformed_registry_records.append({
                "id": conv_id,
                "reason": (
                    "registry record is neither a dictionary "
                    "nor a legacy account string"
                ),
            })
            continue

        account = record.get("account")
        if not validate_account_identity(account):
            malformed_registry_records.append({
                "id": conv_id,
                "reason": "registry record account is missing or invalid",
            })
            continue

        if account == "nologin":
            continue

        registered_not_observed.append({
            "id": conv_id,
            "account": account,
        })

    if not all_complete:
        eligible_registrations = []

    return {
        "complete": all_complete,
        "enumeration": enumeration,
        "eligible_registrations": eligible_registrations,
        "registered": [],
        "registered_not_observed": registered_not_observed,
        "observed_not_registered": observed_not_registered,
        "duplicates": duplicates,
        "registry_conflicts": registry_conflicts,
        "malformed_registry_records": malformed_registry_records,
        "invalid_ids": invalid_ids,
    }



def require_trusted_worktree_root():
    """Require an explicitly declared B-side Git worktree.

    This function must run before any src.session_ops import.
    The script's own location and the current working directory are not
    trust signals. The caller must explicitly set SO_MCP_TRUSTED_ROOT.
    """
    import subprocess
    from pathlib import Path

    raw_root = os.environ.get("SO_MCP_TRUSTED_ROOT")
    if not raw_root:
        raise RuntimeError(
            "SO_MCP_TRUSTED_ROOT is not set; "
            "--work-item is permitted only from the declared B-side worktree"
        )

    declared = Path(raw_root).expanduser()
    if not declared.is_absolute():
        raise RuntimeError(
            "SO_MCP_TRUSTED_ROOT must be an absolute path"
        )

    try:
        root = declared.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(
            "SO_MCP_TRUSTED_ROOT does not resolve to an existing directory: %s"
            % exc
        ) from exc

    if not root.is_dir():
        raise RuntimeError(
            "SO_MCP_TRUSTED_ROOT is not a directory: %s" % root
        )

    git_marker = root / ".git"
    if not git_marker.exists():
        raise RuntimeError(
            "declared root has no .git worktree marker: %s" % root
        )

    required_files = (
        root / "src" / "session_ops" / "authority.py",
        root / "src" / "session_ops" / "work_items.py",
    )
    for required in required_files:
        if not required.is_file():
            raise RuntimeError(
                "declared worktree is missing required source file: %s"
                % required
            )

    def git_output(*args):
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                "git %s failed in declared root: %s"
                % (" ".join(args), detail or "exit %d" % completed.returncode)
            )
        return completed.stdout.strip()

    reported_root = Path(
        git_output("rev-parse", "--show-toplevel")
    ).resolve(strict=True)

    if reported_root != root:
        raise RuntimeError(
            "declared root is not the Git worktree top level "
            "(declared=%s, git=%s)" % (root, reported_root)
        )

    origin = git_output("remote", "get-url", "origin")
    normalized_origin = origin.strip().lower()

    if normalized_origin.startswith("git@github.com:"):
        normalized_origin = (
            "https://github.com/" + normalized_origin[len("git@github.com:"):]
        )
    elif normalized_origin.startswith("ssh://git@github.com/"):
        normalized_origin = (
            "https://github.com/"
            + normalized_origin[len("ssh://git@github.com/"):]
        )

    if normalized_origin.endswith(".git"):
        normalized_origin = normalized_origin[:-4]
    normalized_origin = normalized_origin.rstrip("/")

    expected_origin = "https://github.com/oahcom/chatgpt-session-ops"
    if normalized_origin != expected_origin:
        raise RuntimeError(
            "Git origin does not identify the trusted repository "
            "(expected=%s, actual=%s)"
            % (expected_origin, origin)
        )

    # Explicitly prioritize this verified worktree. Do not rely on cwd,
    # script-relative import behavior, or an incidental src/ directory.
    root_text = str(root)
    sys.path[:] = [
        entry for entry in sys.path
        if entry and os.path.realpath(entry) != root_text
    ]
    sys.path.insert(0, root_text)

    return root

def main():
    """CLI 入口：投递超时是**进程级**配置，跑完必须还原。

    为什么要还原：同进程内的库调用（以及测试）不该被一次 CLI 调用留下的 600s 影响——
    A16 的分档（非思考 420 / 思考 1800）在库调用路径上必须原样成立。
    """
    prev_timeout, prev_source = SEND_TIMEOUT, SEND_TIMEOUT_SOURCE
    try:
        return _main_impl()
    finally:
        globals()["SEND_TIMEOUT"] = prev_timeout
        globals()["SEND_TIMEOUT_SOURCE"] = prev_source


def _main_impl():
    global SEND_TIMEOUT, SEND_TIMEOUT_SOURCE   # CLI 解析结果（用完由 main 还原）
    args = sys.argv[1:]
    thinking_flag = "--thinking" in args
    if thinking_flag:
        args = [a for a in args if a != "--thinking"]

    # D10：可信 WorkItem 发送参数。只摘出明确支持的旗标；
    # 其他未知旗标保留在 args 中，由原有参数数量/命令校验拒绝。
    work_item_options = {}
    work_item_flags = {
        "--work-item": "work_item_id",
        "--principal": "principal_id",
        "--tenant": "tenant_id",
        "--db": "database_path",
    }
    parsed_args = []
    i = 0
    while i < len(args):
        flag = args[i]
        if flag not in work_item_flags:
            parsed_args.append(flag)
            i += 1
            continue
        key = work_item_flags[flag]
        if key in work_item_options or i + 1 >= len(args):
            print(
                json.dumps(
                    {"result": "error", "error": "invalid or duplicate %s" % flag},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 2
        value = args[i + 1]
        if not value or value.startswith("--"):
            print(
                json.dumps(
                    {"result": "error", "error": "%s requires a value" % flag},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 2
        work_item_options[key] = value
        i += 2
    args = parsed_args

    # ---- 投递超时：命令行 > 环境变量 > --thinking 分档 ----
    # 位置不固定（`--timeout 600` 可以在任何地方），必须先摘出，否则会被
    # `<conv_id> <prompt_file>` 的 `len(args) != 2` 位置校验当成第三个位置参数干掉。
    try:
        timeout_raw, args = _extract_timeout_flag(args)
    except ValueError as e:
        print("ERROR: %s" % e, flush=True)
        return 2
    if timeout_raw is not None:
        cli_timeout = _parse_timeout(timeout_raw)
        if cli_timeout is None:
            print("ERROR: --timeout 需要正整数秒（收到 %r）" % (timeout_raw,), flush=True)
            return 2
        SEND_TIMEOUT, SEND_TIMEOUT_SOURCE = cli_timeout, "cli"
    elif SEND_TIMEOUT is not None:
        # 环境变量是显式配置：只补来源标签，值早就在 import 时解析好了。
        SEND_TIMEOUT_SOURCE = "env"
    else:
        # **没有显式配置就保持 None**，让 _send_prompt_impl 按 --thinking 分档
        # （非思考 420s / 思考 1800s）。旧版在这里无条件写 DEFAULT_SEND_TIMEOUT=600，
        # 等于把 1800s 分档在唯一可被操作者触达的 CLI 路径上打死（DEF-ops-04）：
        # `--thinking` 拿到的是 600s，日志里只看得到 source=default。
        SEND_TIMEOUT_SOURCE = ""

    work_item_id = work_item_options.get("work_item_id")
    if work_item_id is None and any(
        key in work_item_options
        for key in ("principal_id", "tenant_id", "database_path")
    ):
        print(
            json.dumps(
                {
                    "result": "error",
                    "error": "--principal, --tenant and --db require --work-item",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 2

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
    # ---- 投递日志判定（纯本地，不触网；CB-B8 的机读版）----
    if args and args[0] == "--check-send-log":
        rest, prompt_arg, positional = args[1:], None, []
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--prompt":
                if i + 1 >= len(rest):
                    print(json.dumps({"result": "error",
                                      "error": "--prompt 需要一个提示词文件路径"},
                                     ensure_ascii=False))
                    return 2
                prompt_arg = rest[i + 1]
                i += 2
            elif a.startswith("--prompt="):
                prompt_arg = a.split("=", 1)[1]
                i += 1
            else:
                positional.append(a)
                i += 1
        if len(positional) != 1:
            print(json.dumps({"result": "error",
                              "error": "--check-send-log 需要且只需要一个日志文件（收到 %d 个）"
                                       % len(positional)},
                             ensure_ascii=False))
            return 2
        try:
            verdict = judge_send_log(positional[0], prompt_arg)
        except OSError as e:
            print(json.dumps({"result": "error", "error": str(e)}, ensure_ascii=False))
            return 2
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        # 0 = 已送达/仍在飞（不许重投），1 = NOT_SENT（可重投），3 = 已发但截断。
        return {"SENT": 0, "IN_FLIGHT": 0, "NOT_SENT": 1, "TRUNCATED": 3}[verdict["verdict"]]
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
    # ---- 注册表只读审计（DECISION-145 Q3）----
    if args and args[0] == "--registry-audit":
        res = registry_audit()
        if "--json" in args[1:]:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(format_registry_audit(res))
        # 只读命令：可读性不一致不算脚本失败，但仍用非零码提示需要人工看
        return 0 if res["consistency"] == "CONSISTENT" else 4
    if len(args) == 2 and args[0] in ("--rotation-enable", "--rotation-disable"):
        name = args[1]
        if name not in ROTATION_ORDER:
            print("usage: %s acct1|acct2|acct3|acct4|acct5|acct6|nologin" % args[0], flush=True)
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
        payload, exit_code = conversation_owner_cli_result(args[1])
        print(json.dumps(payload, ensure_ascii=False))
        return exit_code
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
    # ---- 会话日志检索（纯本地，不触网）----
    if args and args[0] in ("--log-search", "--log-list", "--log-show"):
        conv = None
        limit = 20
        as_json = "--json" in args
        term = None
        only_pending = "--pending" in args
        only_unassigned = "--unassigned" in args
        only_unresolved = "--unresolved" in args
        if only_pending and only_unassigned:
            print("ERROR: --pending 与 --unassigned 不能同时指定（要两者用 --unresolved）",
                  flush=True)
            return 2
        i = 1
        while i < len(args):
            a = args[i]
            if a == "--conv" and i + 1 < len(args):
                conv = args[i + 1]
                i += 2
            elif a == "--limit" and i + 1 < len(args):
                limit = _as_int(args[i + 1], 20) or 20
                i += 2
            elif a in ("--json", "--pending", "--unassigned", "--unresolved"):
                i += 1
            else:
                if term is None:
                    term = a
                i += 1

        def _bucket_of(fp):
            parent = os.path.dirname(os.path.abspath(fp))
            for label, d in (("conversations", log_dir()),
                             ("pending", log_pending_dir()),
                             ("unassigned", log_unassigned_dir())):
                if parent == os.path.abspath(d):
                    return label
            return "other"

        def _turns_in(fp):
            """单文件的 begin/end 合并（与 iter_turns 同语义，但只读一个文件）。"""
            merged, order = {}, []
            rows = _read_jsonl(fp)
            if rows is None:
                print("WARN: 会话日志读不出来（%s）——本文件跳过" % fp,
                      file=sys.stderr, flush=True)
                return []
            for rec in rows:
                tid = str(rec.get("turn_id") or "")
                key = tid or ("__n%d" % len(order))
                if key not in merged:
                    merged[key] = {}
                    order.append(key)
                slot = merged[key]
                phase = rec.get("phase") or "turn"
                if phase == "begin":
                    for k, v in rec.items():      # 同 iter_turns：begin 不覆盖回复侧
                        if k not in ("reply", "reply_chars", "reply_sha256",
                                     "reply_state", "reply_file", "status", "error"):
                            slot[k] = v
                elif phase == "end":
                    slot.update({k: v for k, v in rec.items()
                                 if k not in ("prompt", "prompt_chars", "prompt_sha256")})
                else:
                    slot.update(rec)
                    slot["reply_state"] = _reply_state_of(
                        rec.get("status"), rec.get("reply"), None)
                slot.setdefault("reply_state", "unrecorded")
                slot.setdefault("status", None)
                slot.setdefault("error", "")
            slot.setdefault("status", None)      # 只有 begin 时也必须显式 None，不能缺字段
            slot.setdefault("error", "")
            return [merged[k] for k in order]

        if args[0] == "--log-list":
            if only_pending:
                want = ("pending",)
            elif only_unassigned:
                want = ("unassigned",)
            elif only_unresolved:
                want = ("pending", "unassigned")
            else:
                want = None      # 默认：全部三桶（行为与之前一致）

            rows, incomplete, files = [], [], {}
            for fp in _all_log_files():
                bucket = _bucket_of(fp)
                if want is not None and bucket not in want:
                    continue
                files[bucket] = files.get(bucket, 0) + 1
                recs = _turns_in(fp)
                if not recs:
                    continue
                states = sorted({r.get("reply_state") or "unrecorded" for r in recs})
                row = {
                    "bucket": bucket,
                    "conv_id": next((r.get("conv_id") for r in recs if r.get("conv_id")), ""),
                    "file": os.path.basename(fp),
                    "turns": len(recs),
                    "last_ts": recs[-1].get("ts"),
                    "first_ts": recs[0].get("ts"),
                    "accounts": sorted({r.get("account") or "-" for r in recs}),
                    "last_prompt_file": recs[-1].get("prompt_file"),
                    "reply_states": states,
                }
                rows.append(row)
                if states != ["captured"]:
                    incomplete.append(row["conv_id"] or row["file"])
            print(json.dumps({
                "root": log_root().replace("\\", "/"),
                "dirs": {"conversations": log_dir().replace("\\", "/"),
                         "pending": log_pending_dir().replace("\\", "/"),
                         "unassigned": log_unassigned_dir().replace("\\", "/")},
                "files": files,
                "count": len(rows),
                "logs": rows,
                "incomplete": incomplete,
                "filter": ("pending" if only_pending else
                           "unassigned" if only_unassigned else
                           "unresolved" if only_unresolved else "all"),
                "note": ("pending=新会话尚未确认 conv_id 的暂存轮次；"
                         "unassigned=身份不可用且无法归属（按原始标识分文件，不合并）；"
                         "incomplete=存在非 captured 回复状态的记录（含只有 begin 的未完成轮）"),
            }, ensure_ascii=False, indent=2))
            return 0

        if args[0] == "--log-show":
            if not term:
                print("usage: --log-show <conv_id> [--json]", flush=True)
                return 1
            recs = list(iter_turns(term))
            if not recs:      # 也接受 pending/unassigned 的文件基名
                for d in (log_pending_dir(), log_unassigned_dir()):
                    cand = os.path.join(d, term + ".jsonl")
                    if os.path.exists(cand):
                        recs = _turns_in(cand)
                        break
            if as_json:
                print(json.dumps({"conv_id": term, "turns": len(recs), "records": recs},
                                 ensure_ascii=False, indent=2))
                return 0
            if not recs:
                print("没有该会话的日志：%s（%s）" % (term, log_root()), flush=True)
                return 1
            for r in reversed(recs):
                state = r.get("reply_state") or "unrecorded"
                print("[%s] %s | %s | status=%s | reply_state=%s"
                      % (r.get("ts"), r.get("account") or "-",
                         r.get("prompt_file"), r.get("status"), state))
                print("--- prompt (%d chars) ---" % (r.get("prompt_chars") or 0))
                print(r.get("prompt") or "")
                print("--- reply (%d chars) ---" % (r.get("reply_chars") or 0))
                print(r.get("reply") or "")
                if r.get("error"):
                    print("--- error ---")
                    print(r["error"])
                if state == "unrecorded":
                    print("--- 注意 ---")
                    print("本轮只有 begin：回复未被留存（发送可能已发生），不得当成功")
                print()
            return 0

        if not term:
            print("usage: --log-search <关键词> [--conv <id>] [--limit N] [--json]", flush=True)
            return 1
        hits = search_logs(term, conv, limit)
        if as_json:
            print(json.dumps({"term": term, "conv_id": conv, "count": len(hits),
                              "hits": hits}, ensure_ascii=False, indent=2))
            return 0
        if not hits:
            print("未命中：%r（目录 %s）" % (term, log_root()), flush=True)
            return 1
        for r in hits:
            low = (term or "").lower()
            in_prompt = low in (r.get("prompt") or "").lower()
            where = "prompt" if in_prompt else "reply"
            text = r.get("prompt") if in_prompt else r.get("reply")
            state = r.get("reply_state") or "unrecorded"
            print("%s  %s  %s  [%s]%s"
                  % (r.get("ts"), (r.get("conv_id") or "-")[:8],
                     r.get("prompt_file") or "-", where,
                     "" if state == "captured" else "  reply_state=%s" % state))
            print("    " + _log_excerpt(text, term))
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
        out = {"conv_id": conv, "status": status}
        if getattr(conv, "mismatch", False):
            # F-13：落点不一致必须把**两个** id 都报出来，只报一个等于没报
            out["landing_mismatch"] = True
            out["target_conv_id"] = getattr(conv, "target_conv_id", cid)
            out["actual_conv_id"] = getattr(conv, "actual_conv_id", "")
            out["reason"] = getattr(conv, "reason", "")
            out["quarantine_path"] = _p
            # F-17：id 歧义时把候选 id 列表也报出来
            if getattr(conv, "candidate_ids", None):
                out["candidate_conv_ids"] = list(conv.candidate_ids)
        print(json.dumps(out, ensure_ascii=False))
        return _send_exit_code(status, conv)
    # ---- 会话清单 ----
    if args and args[0] == "--list-all":
        allowed_flags = {"--apply", "--include-nologin"}
        supplied_flags = set(args[1:])

        if (
            len(supplied_flags) != len(args[1:])
            or not supplied_flags.issubset(allowed_flags)
        ):
            print(
                "usage: --list-all [--apply] [--include-nologin]",
                flush=True,
            )
            return 2

        apply_changes = "--apply" in supplied_flags
        include_nologin = "--include-nologin" in supplied_flags

        configured_accounts = list(ROTATION_PRIMARY)
        if include_nologin:
            configured_accounts.append("nologin")

        combined = []
        errors = []
        quota = {}
        enumeration_results = {}
        stopped_by_rate_limit = False

        for name in configured_accounts:
            if stopped_by_rate_limit:
                enumeration_results[name] = {
                    "status": None,
                    "error": (
                        "not attempted after an earlier account "
                        "returned HTTP 429"
                    ),
                    "items": [],
                    "state": "not_attempted",
                }
                continue

            try:
                api = ChatGPTAPI(account=name)
            except (OSError, json.JSONDecodeError) as e:
                if name == "nologin":
                    raise

                message = "credentials unavailable (%s)" % e
                errors.append("%s: %s" % (name, message))
                enumeration_results[name] = {
                    "status": None,
                    "error": message,
                    "items": [],
                    "state": "failed",
                }
                continue

            try:
                items, status, error = api.list_conversations()
            except (OSError, json.JSONDecodeError) as e:
                message = "enumeration failed (%s)" % e
                errors.append("%s: %s" % (name, message))
                enumeration_results[name] = {
                    "status": None,
                    "error": message,
                    "items": [],
                    "state": "failed",
                }
                continue

            error = error or ""
            successful = status == 200 and not error
            safe_items = items if isinstance(items, list) else []

            combined.extend(safe_items)
            quota[name] = api.quota

            enumeration_results[name] = {
                "status": status,
                "error": error,
                "items": safe_items,
                "state": "complete" if successful else "failed",
            }

            if not successful:
                errors.append(
                    "%s: HTTP %s %s" % (name, status, error)
                )
                if status == 429:
                    stopped_by_rate_limit = True

        registry_snapshot = None
        registry_read_failed = False

        try:
            with _file_lock(
                CONV_MAP_FILE,
                timeout=LOCK_TIMEOUT_SEC,
                strict=True,
            ):
                registry_snapshot = _read_conv_map_for_migration()
        except (OSError, ValueError, LockTimeout) as e:
            registry_read_failed = True
            errors.append(
                "registry: unable to read registry (%s)" % e
            )

        if not registry_read_failed and not isinstance(
            registry_snapshot, dict
        ):
            registry_read_failed = True
            errors.append("registry: invalid registry structure")

        reconciliation = reconcile_conversation_ownership(
            enumeration_results,
            registry_snapshot if not registry_read_failed else {},
            configured_accounts,
        )

        if registry_read_failed:
            reconciliation["complete"] = False
            reconciliation["eligible_registrations"] = []

        if (
            apply_changes
            and reconciliation["complete"]
            and not registry_read_failed
        ):
            registrations = list(
                reconciliation["eligible_registrations"]
            )

            try:
                reconciliation["registered"] = (
                    register_conversations_batch(registrations)
                )
                # DECISION-151：显式给出「本次真正新增数」。
                # registered 含「已存在且归属一致」的候选，不能当幂等性判据。
                reconciliation["added_count"] = getattr(
                    register_conversations_batch, "last_added_count", None
                )

            except (
                OSError,
                TypeError,
                ValueError,
                RuntimeError,
                LockTimeout,
            ) as e:
                errors.append(
                    "registry: batch registration failed (%s)" % e
                )
                reconciliation["registered"] = []
                reconciliation["added_count"] = 0

        by_account = {}
        for conversation in combined:
            if (
                isinstance(conversation, dict)
                and isinstance(conversation.get("account"), str)
            ):
                by_account.setdefault(
                    conversation["account"], []
                ).append(conversation)

        summary = {
            key: len(value)
            for key, value in by_account.items()
        }

        output = {
            "conversations": combined,
            "by_account": by_account,
            "summary": summary,
            "quota": quota,
            "errors": errors,
            "enumeration": reconciliation["enumeration"],
            "reconciliation": {
                "complete": reconciliation["complete"],
                "apply_requested": apply_changes,
                "registered": reconciliation["registered"],
                "eligible_registrations": (
                    reconciliation["eligible_registrations"]
                ),
                "registered_not_observed": (
                    reconciliation["registered_not_observed"]
                ),
                "observed_not_registered": (
                    reconciliation["observed_not_registered"]
                ),
                "duplicates": reconciliation["duplicates"],
                "registry_conflicts": (
                    reconciliation["registry_conflicts"]
                ),
                "malformed_registry_records": (
                    reconciliation["malformed_registry_records"]
                ),
                "invalid_ids": reconciliation["invalid_ids"],
            },
        }

        print(json.dumps(output, ensure_ascii=False, indent=2))
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


    # D10a/D10b: local CLI operator authority is not IdP authentication.
    # Establish the B-side worktree identity before importing src.session_ops.
    work_item_store = None
    work_item_principal = None

    if work_item_id is not None:
        try:
            trusted_root = require_trusted_worktree_root()
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "result": "error",
                        "error": "untrusted WorkItem execution environment: %s"
                        % exc,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 2

        # require_trusted_worktree_root() has inserted this exact root at
        # sys.path[0]. No import from the script directory or current
        # working directory is used as a trust decision.
        from src.session_ops.authority import (
            DenyAllPrincipalDirectory,
            Principal,
            SecureWorkItemStore,
            validate_work_item_id,
        )

        principal_id = (
            work_item_options.get("principal_id")
            or os.environ.get("SO_MCP_PRINCIPAL")
        )
        tenant_id = (
            work_item_options.get("tenant_id")
            or os.environ.get("SO_MCP_TENANT")
        )
        database_path = (
            work_item_options.get("database_path")
            or os.environ.get("SO_MCP_DB")
        )

        if not principal_id or not tenant_id or not database_path:
            print(
                json.dumps(
                    {
                        "result": "error",
                        "error": (
                            "--work-item requires --principal/ SO_MCP_PRINCIPAL, "
                            "--tenant/ SO_MCP_TENANT and --db/ SO_MCP_DB"
                        ),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 2

        if database_path == ":memory:":
            print(
                json.dumps(
                    {
                        "result": "error",
                        "error": "--db must name a persistent database file",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 2

        try:
            validate_work_item_id(work_item_id)
            work_item_principal = Principal(tenant_id, principal_id)
            work_item_store = SecureWorkItemStore(
                database_path,
                principal_directory=DenyAllPrincipalDirectory(),
            )
            work_item_store.initialize()

            # Verify write authority before creating an API client or sending.
            conn = work_item_store._connect()
            try:
                work_item_store._require(
                    conn,
                    work_item_principal,
                    work_item_id,
                    "write",
                )
            finally:
                conn.close()

        except Exception as exc:
            print(
                json.dumps(
                    {
                        "result": "error",
                        "error": "WorkItem preflight failed: %s" % exc,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 2

    def send_work_item(api, target_conv_id, prompt_file, new_chat=False):
        """可信发送入口；保留既有会话归属登记，并单独报告 link 结果。"""
        try:
            result, link, link_error = send_prompt_for_work_item(
                api,
                work_item_store,
                work_item_principal,
                work_item_id,
                target_conv_id,
                prompt_file,
                new_chat=new_chat,
                thinking=True if thinking_flag else None,
            )
        except Exception as e:
            print(
                json.dumps(
                    {
                        "result": "error",
                        "status": None,
                        "work_item_id": work_item_id,
                        "link": None,
                        "link_error": str(e),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 1

        status, path, conv = (tuple(result) + (None, None, None))[:3]

        # 保留既有 record_conv 行为：
        # --new 使用实际创建的会话 ID；续聊使用原目标 ID。
        if status == 200:
            if new_chat and conv:
                record_conv(str(conv), account, prompt_file)
            elif not new_chat:
                record_conv(target_conv_id, account, prompt_file)

        link_value = None
        if link is not None:
            link_value = {
                "link_id": link.link_id,
                "session_id": link.session_id,
            }

        if link_error is None and status == 200 and link is None:
            link_error = (
                "trusted landing was not confirmed; no link was created"
            )

        output = {
            "result": "ok" if status == 200 and link is not None else "error",
            "status": status,
            "work_item_id": work_item_id,
            "link": link_value,
            "link_error": str(link_error) if link_error is not None else None,
        }
        if getattr(conv, "mismatch", False):
            # 保留原 mismatch 分支的所有诊断字段。
            output.update({
                "landing_mismatch": True,
                "target_conv_id": getattr(
                    conv, "target_conv_id", target_conv_id or ""
                ),
                "actual_conv_id": getattr(conv, "actual_conv_id", ""),
                "reason": getattr(conv, "reason", ""),
                "quarantine_path": path,
                "candidate_conv_ids": list(
                    getattr(conv, "candidate_ids", []) or []
                ),
            })
        elif getattr(conv, "candidate_ids", None):
            output["candidate_conv_ids"] = list(conv.candidate_ids)

        if link_error is not None and status == 200:
            output["message"] = (
                "The message was sent and archived, but linking failed; "
                "manual linking is required."
            )

        print(json.dumps(output, ensure_ascii=False), flush=True)
        if getattr(conv, "send_truncated", False):
            # 截断优先于 link：SEND_TRUNCATED 没有送达凭证，exit 0 会让门禁假绿。
            return 3
        return 0 if status == 200 and link is not None else 1

    # ---- 开新会话 ----
    if args[0] == "--new":
        if len(args) != 2:
            print("usage: [--account acct1|acct2|acct3|acct4|acct5|acct6|nologin|auto] [--thinking] --new <prompt_file>", flush=True)
            return 2
        if work_item_id is not None and (
            not explicit_account or account in ("auto", "nologin")
        ):
            print(
                json.dumps(
                    {
                        "result": "error",
                        "error": (
                            "--work-item --new requires an explicit logged-in "
                            "--account acct1..acct6; automatic rotation and "
                            "anonymous fallback cannot issue trusted landing evidence"
                        ),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 2
        if explicit_account and account != "auto":
            api = build(account)
            if api is None:
                return 2
            if account == "nologin":
                print("nologin：走匿名浏览器通道（需 CDP 端口 %s 的未登录浏览器）"
                      % os.environ.get("CHATGPT_ANON_CDP_PORT", "9333"), flush=True)
            try:
                if work_item_id is not None:
                    return send_work_item(
                        api, None, args[1], new_chat=True
                    )
                status, _path, conv = send_prompt(api, None, args[1], new_chat=True,
                                                  thinking=True if thinking_flag else None)

            except ThinkingModelUnavailable as e:
                # 显式指定账号意味着用户明确选择身份。
                # 该账号思考模型不可用时直接失败，不自动切换账号。
                print(
                    json.dumps(
                        {
                            "result": "error",
                            "reason_code": e.reason_code,
                            "account": e.account,
                            "detail": e.detail,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return 1

            except Exception as e:
                print(json.dumps({"result": "error", "detail": str(e)}, ensure_ascii=False))
                return 1
            if status == 200 and conv:
                record_conv(conv, account, args[1])
                print("NEW_CONV %s account=%s" % (conv, account), flush=True)
            if status == 429 and account in ROTATION_PRIMARY:
                # 429 限流 → 换账号，换不动就匿名（用户裁定）。显式指定身份只约束
                # 「用谁起」，不该约束「限速后还能干什么」：429 还没建成会话，
                # 重试不会重复建会话、不会多花配额。
                print("WARN: %s 返回 429 —— 换另一个登录态，都限速则走匿名通道" % account,
                      flush=True)
                def mark(s):
                    mark_unavailable(s, account, "HTTP 429 限速冷却", COOLDOWN_429)
                mutate_rotation(mark)
                return create_chat_rotating(args[1], thinking=True if thinking_flag else None)
            return _send_exit_code(status, conv)
        # 默认：acct1/acct2/acct3/acct4/acct5/acct6 均分轮转，全部 429 才匿名兜底
        return create_chat_rotating(args[1], thinking=True if thinking_flag else None)

    # ---- 既有会话：未显式指定身份时按登记归属路由 ----
    # 先做参数校验，别在缺参数时索引越界（曾导致 `--tail` 空参数直接 IndexError）
    if args[0] == "--list" and len(args) != 1:
        print("usage: [--account acct1|acct2|acct3|acct4|acct5|acct6|nologin] --list", flush=True)
        return 2
    if args[0] == "--tail" and len(args) != 2:
        print("usage: [--account acct1|acct2|acct3|acct4|acct5|acct6|nologin] --tail <conv_id>", flush=True)
        return 2
    raw_id = args[1] if args[0] == "--tail" else (args[0] if not args[0].startswith("--") else "")
    conv_id = cli_id(raw_id) if raw_id else ""
    if raw_id and conv_id is None:
        return 2
    if conv_id:
        resolution = resolve_conversation_account(
            conv_id,
            account if explicit_account else None,
        )
        ownership_status = resolution["status"]

        if ownership_status == "invalid":
            if explicit_account and not validate_account_identity(account):
                print("ERROR: invalid account identity", flush=True)
            else:
                print("ERROR: invalid conversation id", flush=True)
            return 2

        if ownership_status == "conflict":
            # A known registered owner must never be overridden by an
            # explicitly selected, different account. Contradictory registry
            # records are refused by the same fail-closed path.
            print(
                "ERROR: explicit account conflicts with registered conversation owner; refusing to route",
                flush=True,
            )
            return 2

        if ownership_status == "unknown":
            if not explicit_account:
                print(
                    "ERROR: conversation ownership is unknown; specify --account explicitly",
                    flush=True,
                )
                return 2
            account = resolution["account"]

        elif ownership_status == "known":
            account = resolution["account"]

    # D10c: reject caller-supplied landing evidence before credentials,
    # account state, or any other external action is touched.
    # Keep this as a narrow denylist: main() also serves legacy non-send
    # commands whose flag semantics must not be changed by a global whitelist.
    forbidden_evidence_flags = {
        "--evidence",
        "--match",
        "--confirmed",
    }
    forbidden_evidence_prefixes = (
        "--evidence-",
        "--match-",
        "--confirmed-",
    )
    supplied_forbidden = []
    for arg in args:
        flag = arg.split("=", 1)[0]
        if (
            flag in forbidden_evidence_flags
            or flag.startswith(forbidden_evidence_prefixes)
        ):
            supplied_forbidden.append(flag)
    if supplied_forbidden:
        print(
            "usage: --work-item <work-item-id> [options]",
            flush=True,
        )
        print(
            json.dumps(
                {
                    "result": "error",
                    "error": "caller-supplied evidence flags are forbidden",
                    "flags": sorted(set(supplied_forbidden)),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 2

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
        print("usage: [--account acct1|acct2|acct3|acct4|acct5|acct6|nologin] <conv_id> <prompt_file>", flush=True)
        return 2
    if work_item_id is not None:
        if account == "nologin":
            print(
                json.dumps(
                    {
                        "result": "error",
                        "error": "--work-item does not support anonymous sends",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 2
        return send_work_item(api, conv_id, args[1], new_chat=False)
    try:
        status, _path, conv = send_prompt(api, conv_id, args[1],
                                          thinking=True if thinking_flag else None)
    except Exception as e:
        print(json.dumps({"conv_id": conv_id, "result": "error", "detail": str(e)}, ensure_ascii=False))
        return 1
    if status == 200:
        record_conv(conv_id, account, args[1])
    elif status == 429:
        # 既有会话不能换身份：会话归属固定（换账号 404，匿名更读不到）。
        # 这里只报「怎么解」——换账号/匿名只对 --new 有效，别让人以为能重投。
        print("FAIL: 429 限速，但会话 %s 归属 %s —— 换账号或匿名都读不到这条会话。"
              "按返回体时限退避后重试，或用 --new 开新会话（429 会自动换账号/走匿名）"
              % (conv_id[:8], account), flush=True)
    elif getattr(conv, "mismatch", False):
        # F-13：落点不一致 → 业务失败，且必须把两个 id 都带出来（目标 vs 实际落点）
        print(json.dumps({
            "result": "error",
            "status": status,
            "landing_mismatch": True,
            "target_conv_id": getattr(conv, "target_conv_id", conv_id),
            "actual_conv_id": getattr(conv, "actual_conv_id", ""),
            "reason": getattr(conv, "reason", ""),
            "quarantine_path": _path,
            # F-17：id 歧义时把候选 id 列表也报出来
            "candidate_conv_ids": list(getattr(conv, "candidate_ids", []) or []),
        }, ensure_ascii=False), flush=True)
    return _send_exit_code(status, conv)


if __name__ == "__main__":
    sys.exit(main())
