# -*- coding: utf-8 -*-
"""session_ops.chatgpt_cli — MCP 进程内的受限 CLI 适配层。

对应方案（reply-6b083c56-220811.md）§2.3、§2.6、§3 PR-2。

本模块只做一件事：把固定子进程接口包成 ``ChatGPTCLIAdapter`` 的四个方法。
调用方（worker / app）无法传入任意命令、路径或环境变量；CLI 参数协议全部来自
``docs/CHATGPT-MCP.md`` 的「CLI 契约实测」一节，依据是 b74d9ff 的
``scripts/chatgpt_send.py`` 源码与 ``tests/test_aid_2026_011_stage3_cli.py``。

硬约束（方案 §2.3 第 1–8 条）：

1. CLI 路径在构造时以绝对路径固定，相对路径直接拒绝。
2. 只允许 ``create / send / read / list`` 四个操作，每个操作对应一条冻结的参数模板。
3. 调用方不能传任意命令、路径或 env：prompt 由本模块写入自己的 0700 临时目录。
4. 禁止匿名兜底：``nologin`` 在参数层显式拒绝，且子进程显式带
   ``CHATGPT_ANON_FALLBACK=0``；CLI 若仍然回落到 nologin，按失败处理。
5. 不继承调用方 env：只按白名单重建最小 env。
6. stdout 只解析受约束结构，解析失败一律抛错，不把「有输出」当成功。
7. stderr（以及错误详情）在抛出前脱敏 token / cookie / 邮箱。
8. 子进程有超时、进程组终止与并发上限。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import Future
import threading
import uuid

__all__ = [
    "ChatGPTCLIAdapter",
    "SenderError",
    "CODE_INVALID_ARGUMENT",
    "CODE_UNAUTHENTICATED",
    "CODE_NOT_FOUND",
    "CODE_CONFLICT",
    "CODE_QUARANTINED_SESSION",
    "CODE_RESOURCE_EXHAUSTED",
    "CODE_DEPENDENCY_UNAVAILABLE",
    "CODE_TIMEOUT",
    "CODE_INTERNAL",
]

# 公共错误码（方案 §1.6，沿用 v0.1 的十个码，不扩展集合）。
CODE_INVALID_ARGUMENT = "INVALID_ARGUMENT"
CODE_UNAUTHENTICATED = "UNAUTHENTICATED"
CODE_NOT_FOUND = "NOT_FOUND"
CODE_CONFLICT = "CONFLICT"
CODE_QUARANTINED_SESSION = "QUARANTINED_SESSION"
CODE_RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
CODE_DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
CODE_TIMEOUT = "TIMEOUT"
CODE_INTERNAL = "INTERNAL"

# ---------------------------------------------------------------------------
# CLI 契约常量（证据见 docs/CHATGPT-MCP.md「CLI 契约实测」）
# ---------------------------------------------------------------------------

# scripts/chatgpt_send.py:5939-5940：conv_id 必须是带连字符的小写 UUID。
_CONV_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

# scripts/chatgpt_send.py:7178 / :8837 —— 建会话成功的机器可读行。
_NEW_CONV_RE = re.compile(
    r"^NEW_CONV ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r" account=([A-Za-z0-9_-]+)$"
)

# scripts/chatgpt_send.py:6643 —— 投递成功的唯一结构化证据行。
_SEND_OK_RE = re.compile(r"^SEND \S+ -> 200 respLen=\d+ in ")

# scripts/chatgpt_send.py:8949 —— --tail 的首行。
_TAIL_RE = re.compile(r"^tail: ?(.*)$")

# scripts/chatgpt_send.py:3291-3300：CLI 认得的身份集合。
def _discover_logged_in_accounts():
    """Discover account identities from the configured credential directory."""
    from pathlib import Path
    configured_root = os.environ.get("SO_SENDER_ACCOUNTS_DIR")
    if not configured_root:
        return frozenset()
    root = Path(configured_root)
    names = set()
    try:
        entries = tuple(root.iterdir()) if root.is_dir() else ()
    except OSError:
        entries = ()
    for entry in entries:
        try:
            if (entry.is_symlink() or not entry.is_dir()
                    or not re.fullmatch(r"acct[1-9][0-9]*", entry.name)):
                continue
            cookies, token = entry / "cookies.json", entry / "access_token.txt"
            if (cookies.is_symlink() or token.is_symlink()
                    or not cookies.is_file() or not token.is_file()):
                continue
            names.add(entry.name)
        except OSError:
            continue
    return frozenset(names)


_LOGGED_IN_ACCOUNTS = _discover_logged_in_accounts()
_VALID_IDENTITIES = frozenset({"default", "nologin", *_LOGGED_IN_ACCOUNTS})

_OPERATIONS = frozenset(("create", "send", "read", "list"))

# read/list 走独立的并发槽位：长 send 占满 send 槽时读取不被饿死（Owner 裁决）。
_READ_OPERATIONS = frozenset(("read", "list"))

# 子进程 env 白名单：只保留进程启动必需项，其余一律不继承。
_ENV_ALLOWLIST = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "HOME",
    "SO_SENDER_ACCOUNTS_DIR",
    "CHATGPT_STATE_DIR",
    "CHATGPT_PROXY_FILE",
    "CHATGPT_AUTH_DIR",
    "CHATGPT_BASE",
    "CHATGPT_PROXY",
    "CHATGPT_ALLOW_DIRECT",
    "CHATGPT_SEND_TIMEOUT",
    "CHATGPT_ANON_CDP_PORT",
    "CHATGPT_STATE_DIR",
    "SO_SENDER_ACCOUNT_LOCK_DIR",
)

# 显式写死的子进程 env（不是从调用方继承）。
_ENV_FORCED = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    # 方案 §2.3 第 4 条：禁止匿名兜底。双保险（参数层已拒绝 nologin）。
    "CHATGPT_ANON_FALLBACK": "0",
}

# 建会话没有 CLI 侧 prompt 语义（--new 必带 prompt_file），这里固定引导语；
# 真正的会话内容由后续 send 提供。TODO 见 docs/CHATGPT-MCP.md「缺口」。
_DEFAULT_CREATE_PROMPT = "ping"

_PROMPT_MAX_CHARS = 32768
_DETAIL_MAX_CHARS = 1200

# 上游 HTTP 状态码：只认这几个已知语义码，用于错误分类与日志关联。
_UPSTREAM_STATUS_CODES = frozenset((401, 403, 404, 413, 429, 500, 502, 503, 504))
_STATUS_IN_TEXT = re.compile(r"(?<![0-9])([1-5][0-9]{2})(?![0-9])")


def _upstream_status(text):
    """从 CLI 输出里取第一个可识别的上游 HTTP 状态码（不取响应体）。"""
    for match in _STATUS_IN_TEXT.finditer(text or ""):
        status = int(match.group(1))
        if status in _UPSTREAM_STATUS_CODES:
            return status
    return None

_SECRET_PATTERNS = (
    # sk-... / sess-... / JWT（eyJ...）
    (re.compile(r"\b(?:sk|sess|rk)-[A-Za-z0-9_\-]{8,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]+){0,2}"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]{8,}"), r"\1[REDACTED_TOKEN]"),
    # cookie / token 头
    (
        re.compile(
            r"(?i)((?:access_token|refresh_token|id_token|session[-_]token"
            r"|__Secure-[A-Za-z0-9_.\-]+|cookie|set-cookie|authorization)"
            r"\s*[:=]\s*)([^\s,;\"']+)"
        ),
        r"\1[REDACTED]",
    ),
    # 邮箱
    (
        re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
        "[REDACTED_EMAIL]",
    ),
)


def _redact(text):
    """对 stderr / 错误详情做敏感字段过滤（方案 §2.3 第 7 条）。"""
    if not text:
        return ""
    out = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    if len(out) > _DETAIL_MAX_CHARS:
        out = out[:_DETAIL_MAX_CHARS] + "…[truncated]"
    return out


class SenderError(RuntimeError):
    """适配层错误：携带公共错误码（方案 §1.6），详情已脱敏。

    ``upstream_status`` 只保留上游 HTTP 状态码本身（整数或 None），
    ``operation`` 只保留 read/send/create/list 之一；两者都不含响应体。
    """

    def __init__(self, code, detail="", *, upstream_status=None, operation=None, retry_after_seconds=None):
        self.code = code
        self.upstream_status = upstream_status
        self.operation = operation
        self.retry_after_seconds = retry_after_seconds
        self.detail = _redact(detail)
        super().__init__("%s: %s" % (code, self.detail))


class ChatGPTCLIAdapter:
    """固定 CLI 的窄接口。构造参数全部可注入，便于测试与部署。"""

    def __init__(
        self,
        cli_path,
        *,
        timeout=300.0,
        max_concurrency=4,
        admission_timeout=0.25,
        read_timeout=None,
        allowed_accounts=None,
        read_concurrency=2,
        read_semaphore=None,
        owner_lookup=None,
        cwd=None,
        prompt_dir=None,
    ):
        raw_cli = os.fspath(cli_path)
        if not os.path.isabs(raw_cli):
            raise ValueError("cli_path must be an absolute path")
        cli = os.path.abspath(raw_cli)
        if not os.path.isfile(cli):
            raise ValueError("cli_path does not exist: %s" % cli)

        timeout = float(timeout)
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        admission_timeout = float(admission_timeout)
        if admission_timeout <= 0 or admission_timeout > timeout:
            raise ValueError("admission_timeout must be > 0 and <= timeout")
        read_timeout = min(10.0, timeout) if read_timeout is None else float(read_timeout)
        if read_timeout <= 0 or read_timeout > timeout:
            raise ValueError("read_timeout must be > 0 and <= timeout")
        max_concurrency = int(max_concurrency)
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        read_concurrency = int(read_concurrency)
        if read_concurrency < 1:
            raise ValueError("read_concurrency must be >= 1")

        self._cli_path = cli
        self._argv_prefix = (
            (sys.executable, cli) if cli.lower().endswith(".py") else (cli,)
        )
        self._timeout = timeout
        self._read_timeout = read_timeout
        self._allowed_accounts = allowed_accounts
        self._admission_timeout = admission_timeout
        self._max_concurrency = max_concurrency
        self._slots = threading.BoundedSemaphore(max_concurrency)
        # read/list 独立槽位（可注入），与 send/create 的槽位互不竞争。
        self._read_limit = read_concurrency
        self._read_slots = (
            read_semaphore
            if read_semaphore is not None
            else threading.BoundedSemaphore(read_concurrency)
        )
        self._read_lock = threading.Lock()
        self._reads_inflight = {}
        self._cwd = os.path.abspath(cwd) if cwd else os.path.dirname(cli)

        # 会话归属的唯一权威来源是 SQLite 的 session_owners（经
        # AccountRouter.get_owner 注入）。JSON 注册表不参与任何运行时判定。
        # 没有注入查询器时一律按「无归属」处理，绝不回退到 JSON。
        self._owner_lookup = owner_lookup

        if prompt_dir is None:
            prompt_dir = tempfile.mkdtemp(prefix="chatgpt-sender-prompts-")
        self._prompt_root = os.path.abspath(prompt_dir)
        os.makedirs(self._prompt_root, mode=0o700, exist_ok=True)

    # -- 生命周期 -----------------------------------------------------------

    def close(self):
        shutil.rmtree(self._prompt_root, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    # -- 公开接口（方案 §2.3 的四个方法） ----------------------------------

    def create_session(self, account="auto", *, prompt=None):
        """同步新建会话；auto 由仓库内 CLI 的账号轮转器解析。"""
        prompt_path = self._write_prompt(
            _DEFAULT_CREATE_PROMPT if prompt is None else prompt
        )
        result = self._run(
            "create", account=account, prompt_path=prompt_path
        )
        conv_id, resolved, receipt = self._parse_create(result, expected_account=account)
        return {
            "conversation_id": conv_id,
            "account": resolved,
            "attempts": [],
            **(receipt or {}),
        }

    def send(
        self,
        conversation_id,
        account,
        prompt,
        *,
        tenant_id=None,
        principal_id=None,
    ):
        """向既有会话投递；account=None/"auto" 时按 SQLite 归属记录路由。"""
        conv_id = self._require_conv(conversation_id)
        resolved = self._resolve_account(
            conv_id, account, tenant_id=tenant_id, principal_id=principal_id
        )
        prompt_path = self._write_prompt(prompt)
        result = self._run(
            "send",
            account=resolved,
            conversation_id=conv_id,
            prompt_path=prompt_path,
        )
        receipt = self._parse_send(result) or {}
        return {
            "conversation_id": receipt.get("conversation_id") or conv_id,
            "account": resolved,
            "status": 200,
            "sent": True,
            **receipt,
        }

    def read(
        self,
        conversation_id,
        account,
        after_message_id,
        limit,
        *,
        tenant_id=None,
        principal_id=None,
    ):
        conv_id = self._require_conv(conversation_id)
        key = (
            tenant_id, principal_id, account or "auto", conv_id,
            after_message_id, limit,
        )
        with self._read_lock:
            future = self._reads_inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._reads_inflight[key] = future
        if not owner:
            return copy.deepcopy(future.result())
        try:
            value = self._read_uncached(
                conversation_id, account, after_message_id, limit,
                tenant_id=tenant_id, principal_id=principal_id,
            )
            future.set_result(value)
            return copy.deepcopy(value)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._read_lock:
                if self._reads_inflight.get(key) is future:
                    self._reads_inflight.pop(key, None)

    def _read_uncached(
        self,
        conversation_id,
        account,
        after_message_id,
        limit,
        *,
        tenant_id=None,
        principal_id=None,
    ):
        """读取消息区间（--json --tail … --after-message-id … --limit …）。"""
        conv_id = self._require_conv(conversation_id)
        if after_message_id is not None and (
            not isinstance(after_message_id, str) or not after_message_id
        ):
            raise SenderError(CODE_INVALID_ARGUMENT, "invalid after_message_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise SenderError(
                CODE_INVALID_ARGUMENT, "limit must be an integer in 1..50"
            )
        resolved = self._resolve_account(
            conv_id, account, tenant_id=tenant_id, principal_id=principal_id
        )
        result = self._run(
            "read",
            account=resolved,
            conversation_id=conv_id,
            after_message_id=after_message_id,
            limit=limit,
        )
        if result.exit_code != 0:
            self._parse_tail(result)  # preserve the existing CLI failure mapping
        try:
            lines = result.stdout.splitlines()
            data = json.loads(lines[0]) if len(lines) == 1 else None
            if (
                not isinstance(data, dict)
                or "complete" not in data
                or type(data["complete"]) is not bool
                or type(data.get("has_more")) is not bool
                or not isinstance(data.get("messages"), list)
                or not (
                    data.get("next_after_message_id") is None
                    or isinstance(data.get("next_after_message_id"), str)
                )
            ):
                raise ValueError("invalid read JSON")
            messages = []
            for item in data["messages"]:
                if (
                    not isinstance(item, dict)
                    or not all(
                        isinstance(item.get(k), str) and item[k]
                        for k in ("id", "role")
                    )
                    or not isinstance(item.get("content"), str)
                ):
                    raise ValueError("invalid message")
                messages.append(
                    {
                        "message_id": item["id"],
                        "role": item["role"],
                        "content": item["content"],
                        "created_at": None,
                    }
                )
        except (ValueError, TypeError, KeyError, IndexError):
            # 结构不符：先按输出里的上游状态码分类，401 必须是
            # UNAUTHENTICATED，不能被 tail 解析吞成 INTERNAL（生产缺陷）。
            raise self._classify_failure(
                result, "sender stdout for read has an unexpected shape"
            )
        return {
            "conversation_id": conv_id,
            "account": resolved,
            "messages": messages,
            "complete": data["complete"],
            "has_more": data["has_more"],
            "next_after_message_id": data["next_after_message_id"],
        }

    def list_sessions(self, page_size, cursor):
        """列会话：CLI 一次性返回全量，这里在本地分页。"""
        if (
            not isinstance(page_size, int)
            or isinstance(page_size, bool)
            or not 1 <= page_size <= 100
        ):
            raise SenderError(
                CODE_INVALID_ARGUMENT, "page_size must be an integer in 1..100"
            )
        offset = 0
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor.isdigit():
                raise SenderError(
                    CODE_INVALID_ARGUMENT, "cursor must be a decimal offset string"
                )
            offset = int(cursor)

        result = self._run("list")
        data = self._parse_list(result)

        items = []
        for row in data["conversations"]:
            conv = row.get("id")
            if not isinstance(conv, str) or not _CONV_ID_RE.fullmatch(conv):
                continue
            items.append(
                {
                    "conversation_id": conv,
                    "account": row.get("account") or data.get("account"),
                    "title": row.get("title") or "",
                    "last_message_at": row.get("updated"),
                    # CLI 不返回消息条数，不猜测（方案 §1.3：complete/count 不得推断）。
                    "message_count": None,
                }
            )

        window = items[offset : offset + page_size]
        next_offset = offset + page_size
        next_cursor = str(next_offset) if next_offset < len(items) else None
        return {
            "items": window,
            "next_cursor": next_cursor,
            "partial": bool(data.get("errors")),
            "errors": [_redact(item) for item in data.get("errors", [])[:10]],
        }

    # -- 账号归属（方案 §1.5，SQLite session_owners 为唯一权威） --------------

    def _resolve_account(self, conv_id, requested, *, tenant_id=None, principal_id=None):
        """SQLite ``session_owners`` 是唯一权威；执行边界复核三元组。

        这个函数在启动任何 CLI 子进程之前运行：没有 owner、身份不匹配、
        查询失败都直接拒绝，绝不以 JSON 或调用参数兜底。
        """

        explicit = None
        if requested is not None and requested != "auto":
            explicit = self._normalize_account(requested)

        for name, value in (("tenant_id", tenant_id), ("principal_id", principal_id)):
            if not isinstance(value, str) or not value:
                # 没有认证上下文就不能确认归属：拒绝，不猜测。
                raise SenderError(
                    CODE_INVALID_ARGUMENT,
                    "%s is required to resolve session ownership" % name,
                )

        if self._owner_lookup is None:
            raise SenderError(
                CODE_QUARANTINED_SESSION,
                "conversation %s has no owner source configured" % conv_id,
            )
        try:
            owner = self._owner_lookup(conv_id)
        except Exception as exc:  # noqa: BLE001 - store failure is not an owner
            raise SenderError(
                CODE_DEPENDENCY_UNAVAILABLE,
                "session owner lookup failed: %s" % type(exc).__name__,
            ) from exc
        if owner is None:
            raise SenderError(
                CODE_QUARANTINED_SESSION,
                "conversation %s has no trusted account ownership record" % conv_id,
            )
        if not isinstance(owner, dict):
            raise SenderError(
                CODE_DEPENDENCY_UNAVAILABLE,
                "session owner record for %s is not a mapping" % conv_id,
            )
        if not all(
            isinstance(owner.get(key), str) and owner.get(key)
            for key in ("tenant_id", "principal_id", "account")
        ):
            # 记录残缺 = 结果不确定：拒绝，不把它当成「不匹配」。
            raise SenderError(
                CODE_DEPENDENCY_UNAVAILABLE,
                "session owner record for %s is incomplete" % conv_id,
            )
        if (
            owner["tenant_id"] != tenant_id
            or owner["principal_id"] != principal_id
        ):
            # 别人的会话：不泄露它是否存在。
            raise SenderError(CODE_NOT_FOUND, "conversation %s not found" % conv_id)

        stored = owner.get("account")
        if stored == "nologin":
            raise SenderError(
                CODE_QUARANTINED_SESSION,
                "conversation %s is bound to the anonymous channel" % conv_id,
            )
        try:
            stored = self._normalize_account(stored)
        except SenderError as exc:
            raise SenderError(
                CODE_DEPENDENCY_UNAVAILABLE,
                "session owner record for %s is invalid: %s" % (conv_id, exc.code),
            ) from exc
        if explicit is not None and explicit != stored:
            raise SenderError(
                CODE_CONFLICT,
                "explicit account %s conflicts with recorded owner %s"
                % (explicit, stored),
            )
        return stored

    def _normalize_account(self, account):
        """校验并归一账号身份；nologin 显式拒绝（方案 §2.3 第 4 条）。"""
        if (not isinstance(account, str)
                or account not in ("default", "nologin")
                and not re.fullmatch(r"acct[1-9][0-9]*", account)):
            raise SenderError(
                CODE_INVALID_ARGUMENT,
                "unknown account identity",
            )
        if account == "nologin":
            raise SenderError(
                CODE_INVALID_ARGUMENT,
                "nologin anonymous channel is disabled; an explicit logged-in "
                "account is required",
            )
        # scripts/chatgpt_send.py:3310-3314：default 与 acct1 共用同一凭据根。
        normalized = "acct1" if account == "default" else account
        if normalized not in _LOGGED_IN_ACCOUNTS:
            raise SenderError(
                CODE_INVALID_ARGUMENT,
                "account credentials are not available",
            )
        if self._allowed_accounts is not None:
            # Defense in depth: a direct adapter call must not bypass the
            # Gateway's persisted health filter for a pinned account.
            if normalized not in set(self._allowed_accounts()):
                raise SenderError(
                    CODE_RESOURCE_EXHAUSTED,
                    "account is not active for routing",
                )
        return normalized

    # -- argv 构造：唯一允许生成命令的地方 ---------------------------------

    def _build_argv(
        self,
        operation,
        *,
        account=None,
        conversation_id=None,
        prompt_path=None,
        after_message_id=None,
        limit=None,
    ):
        """冻结参数模板；operation 不在白名单内立即拒绝（方案 §2.3 第 2 条）。"""
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            raise SenderError(
                CODE_INVALID_ARGUMENT,
                "unsupported sender operation: %r" % (operation,),
            )
        # list 通过 CLI 聚合所有已配置登录账号。
        if operation == "list":
            return ("--list-all",)

        if operation == "create" and account == "auto":
            return ("--new", self._require_prompt(prompt_path))
        resolved = self._normalize_account(account)
        if operation == "create":
            return ("--account", resolved, "--new", self._require_prompt(prompt_path))
        if operation == "read":
            # CLI 只认 `--tail` 作为首个位置参数（chatgpt_send.py 结构化读取
            # 分流要求 args[0] == "--tail"），故 --json 必须排在 conv_id 之后。
            argv = [
                "--account",
                resolved,
                "--tail",
                self._require_conv(conversation_id),
                "--json",
            ]
            if after_message_id is not None:
                argv.extend(("--after-message-id", after_message_id))
            argv.extend(("--limit", str(limit)))
            return tuple(argv)
        return (
            "--account",
            resolved,
            self._require_conv(conversation_id),
            self._require_prompt(prompt_path),
        )

    def _require_conv(self, conversation_id):
        if (
            not isinstance(conversation_id, str)
            or not _CONV_ID_RE.fullmatch(conversation_id)
        ):
            raise SenderError(
                CODE_INVALID_ARGUMENT,
                "conversation_id must be a lowercase hyphenated UUID",
            )
        return conversation_id

    def _require_prompt(self, prompt_path):
        if not isinstance(prompt_path, str) or not os.path.isabs(prompt_path):
            raise SenderError(
                CODE_INVALID_ARGUMENT, "prompt path must be an absolute path"
            )
        real = os.path.realpath(prompt_path)
        root = os.path.realpath(self._prompt_root)
        try:
            inside = os.path.commonpath([real, root]) == root
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(real):
            raise SenderError(
                CODE_INVALID_ARGUMENT,
                "prompt path must be a file inside the adapter prompt directory",
            )
        return real

    def _write_prompt(self, text):
        if not isinstance(text, str) or not text:
            raise SenderError(CODE_INVALID_ARGUMENT, "prompt must be a non-empty string")
        if len(text) > _PROMPT_MAX_CHARS:
            raise SenderError(
                CODE_INVALID_ARGUMENT,
                "prompt exceeds %d characters" % _PROMPT_MAX_CHARS,
            )
        name = uuid.uuid4().hex + ".txt"
        path = os.path.join(self._prompt_root, name)
        # O_EXCL：绝不覆盖既有文件；0600：提示词只对本进程可读。
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return os.path.abspath(path)

    # -- 子进程执行 ---------------------------------------------------------

    def _child_env(self):
        env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
        env.update(_ENV_FORCED)
        if self._allowed_accounts is not None:
            env["CHATGPT_ALLOWED_ACCOUNTS"] = ",".join(self._allowed_accounts())
        return env

    def _popen_kwargs(self):
        kwargs = {
            "cwd": self._cwd,
            "env": self._child_env(),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        return kwargs

    def _run(self, operation, **kwargs):
        # One monotonic budget covers admission and subprocess execution. Reads
        # have a separate short budget so an abandoned read cannot outlive the
        # MCP read-response window; send/create keep the longer CLI budget.
        operation_timeout = (
            self._read_timeout if operation in _READ_OPERATIONS else self._timeout
        )
        deadline = time.monotonic() + operation_timeout
        argv = self._build_argv(operation, **kwargs)
        full_argv = tuple(self._argv_prefix) + tuple(argv)

        # read/list 与 send/create 使用彼此独立的槽位，互不抢占。
        if operation in _READ_OPERATIONS:
            slots, limit = self._read_slots, self._read_limit
            kind = "read/list"
        else:
            slots, limit = self._slots, self._max_concurrency
            kind = "sender"
        remaining = max(0.0, deadline - time.monotonic())
        admission_budget = min(self._admission_timeout, remaining)
        if not slots.acquire(timeout=admission_budget):
            raise SenderError(
                CODE_RESOURCE_EXHAUSTED,
                "%s concurrency limit (%d) reached" % (kind, limit),
            )
        try:
            proc = subprocess.Popen(list(full_argv), **self._popen_kwargs())
            try:
                remaining = max(0.0, deadline - time.monotonic())
                out, err = proc.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._kill_tree(proc)
                try:
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    out, err = "", ""
                raise SenderError(
                    CODE_TIMEOUT,
                    "sender CLI exceeded %.1fs (%s); process group terminated; "
                    "stderr=%s"
                    % (operation_timeout, operation, _redact(err)),
                )
        finally:
            slots.release()

        return _CliResult(
            exit_code=proc.returncode,
            stdout=out or "",
            stderr=err or "",
            argv=full_argv,
            operation=operation,
        )

    def _kill_tree(self, proc):
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except OSError:
                pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    # -- stdout 解析：只接受受约束结构 -------------------------------------

    def _parse_send_receipt(self, result):
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line.startswith("SEND_RECEIPT "):
                continue
            try:
                payload = json.loads(line[len("SEND_RECEIPT "):])
            except ValueError:
                return None
            if not isinstance(payload, dict) or payload.get("receipt_version") != 1:
                return None
            turn_id = payload.get("turn_id")
            status = payload.get("status")
            reply_state = payload.get("reply_state")
            reply_source = payload.get("reply_source")
            reply_chars = payload.get("reply_chars")
            reply_sha256 = payload.get("reply_sha256")
            receipt_durable = payload.get("receipt_durable")
            if (
                not isinstance(turn_id, str) or not turn_id or len(turn_id) > 64
                or status != 200
                or not isinstance(reply_state, str) or not reply_state
                or not isinstance(reply_source, str) or not reply_source
                or isinstance(reply_chars, bool) or not isinstance(reply_chars, int) or reply_chars < 0
                or not isinstance(reply_sha256, str)
                or (reply_sha256 and not re.fullmatch(r"[0-9a-f]{64}", reply_sha256))
                or not isinstance(receipt_durable, bool)
            ):
                return None
            conversation_id = payload.get("conversation_id")
            if conversation_id is not None and (
                not isinstance(conversation_id, str)
                or not _CONV_ID_RE.fullmatch(conversation_id)
            ):
                return None
            message_id = payload.get("message_id")
            if message_id is not None and (
                not isinstance(message_id, str) or len(message_id) > 256
            ):
                return None
            return {
                "turn_id": turn_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "reply_state": reply_state,
                "reply_source": reply_source,
                "reply_chars": reply_chars,
                "reply_sha256": reply_sha256,
                "receipt_durable": receipt_durable,
                "receipt_version": 1,
            }
        return None

    def _parse_create(self, result, expected_account):
        expected = (
            None if expected_account in (None, "auto")
            else self._normalize_account(expected_account)
        )

        json_conv = None
        json_account = None
        line_conv = None
        line_account = None
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = _NEW_CONV_RE.match(line)
            if match:
                line_conv, line_account = match.group(1), match.group(2)
                continue
            if line.startswith("{") and line.endswith("}"):
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(payload, dict)
                    and payload.get("result") == "ok"
                    and isinstance(payload.get("conv_id"), str)
                    and _CONV_ID_RE.fullmatch(payload["conv_id"])
                ):
                    json_conv = payload["conv_id"]
                    json_account = payload.get("account")

        conv_id = json_conv or line_conv
        resolved = json_account or line_account
        if conv_id is None or resolved is None:
            raise self._classify_failure(
                result,
                "sender stdout did not contain a constrained NEW_CONV record",
            )

        if resolved == "nologin":
            raise SenderError(
                CODE_RESOURCE_EXHAUSTED,
                "CLI fell back to the anonymous nologin channel; refused",
            )
        try:
            resolved = self._normalize_account(resolved)
        except SenderError as exc:
            raise SenderError(
                CODE_INTERNAL,
                "CLI reported an unusable account identity: %s" % exc.code,
            )
        if expected is not None and resolved != expected:
            raise SenderError(
                CODE_RESOURCE_EXHAUSTED,
                "CLI resolved account %s but %s was requested"
                % (resolved, expected),
            )
        return conv_id, resolved, self._parse_send_receipt(result)

    def _parse_send(self, result):
        if result.exit_code != 0:
            raise self._classify_failure(result, "sender CLI exited non-zero")
        send_ok = any(
            _SEND_OK_RE.match(raw_line.strip())
            for raw_line in result.stdout.splitlines()
        )
        if not send_ok:
            raise SenderError(
                CODE_INTERNAL,
                "sender stdout did not contain a constrained SEND ... -> 200 record; "
                "refusing to treat exit 0 as success",
            )
        return self._parse_send_receipt(result)

    def _parse_tail(self, result):
        if result.exit_code != 0:
            raise self._classify_failure(result, "sender CLI exited non-zero")
        for raw_line in result.stdout.splitlines():
            match = _TAIL_RE.match(raw_line.strip())
            if match:
                text = match.group(1).strip()
                return "" if text == "None" else text
        raise SenderError(
            CODE_INTERNAL,
            "sender stdout did not contain a constrained tail record",
        )

    def _parse_list(self, result):
        try:
            data = json.loads(result.stdout.strip())
        except ValueError:
            raise SenderError(
                CODE_INTERNAL, "sender stdout for --list is not valid JSON"
            )
        if not isinstance(data, dict) or not isinstance(
            data.get("conversations"), list
        ):
            raise SenderError(
                CODE_INTERNAL, "sender stdout for --list has an unexpected shape"
            )
        errors = data.get("errors", [])
        if not isinstance(errors, list):
            raise SenderError(
                CODE_INTERNAL, "sender stdout for --list-all has invalid errors"
            )
        if result.exit_code != 0 and not errors:
            raise self._classify_failure(result, "conversation list failed")
        return data

    def _classify_failure(self, result, fallback):
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        lowered = text.lower()
        status = _upstream_status(text)
        retry_after_seconds = None
        for line in (result.stdout or "").splitlines():
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                status = payload.get("http_status") or status
                retry_after_seconds = payload.get("retry_after_seconds", retry_after_seconds)
        if "account_busy" in lowered or "account_lock_unavailable" in lowered:
            code = CODE_RESOURCE_EXHAUSTED
        elif "nologin" in lowered and ("fallback" in lowered or "匿名" in text):
            code = CODE_RESOURCE_EXHAUSTED
        elif "429" in text:
            code = CODE_RESOURCE_EXHAUSTED
        elif status == 401:
            # 上游账号凭据被拒（CLI: `-> 401`）：认证失败，不是 INTERNAL。
            code = CODE_UNAUTHENTICATED
        elif "auth unavailable" in lowered:
            code = CODE_DEPENDENCY_UNAVAILABLE
        elif "403" in text:
            code = CODE_DEPENDENCY_UNAVAILABLE
        elif "413" in text:
            code = CODE_INVALID_ARGUMENT
        elif result.exit_code == 2 or "usage:" in lowered:
            code = CODE_INVALID_ARGUMENT
        else:
            code = CODE_INTERNAL
        return SenderError(
            code,
            "%s (exit=%s) %s" % (fallback, result.exit_code, _redact(text)),
            upstream_status=status,
            operation=getattr(result, "operation", None),
            retry_after_seconds=retry_after_seconds,
        )


class _CliResult:
    """一次子进程调用的受约束结果。"""

    __slots__ = ("exit_code", "stdout", "stderr", "argv", "operation")

    def __init__(self, exit_code, stdout, stderr, argv, operation=None):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.argv = argv
        self.operation = operation
