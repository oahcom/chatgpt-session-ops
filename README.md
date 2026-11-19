# chatgpt-session-ops

> ChatGPT 会话操控与生命周期管理：API 直连投递 + PoW 绕过 + SSE 收割 + CDP 满员续接 + 匿名通道留存。
> Session I/O and lifecycle tooling for ChatGPT — direct backend API, proof-of-work, SSE harvesting, CDP tab handoff, and self-persisted anonymous sessions.

**English TL;DR** — A CLI for driving ChatGPT conversations from an agent: send a prompt into an existing conversation, harvest the reply, create fresh owner-style sessions, rotate across multiple accounts, survive 413 / 429 / 403, and keep anonymous (`/backend-anon`) sessions readable after the tab is gone. Pure Python stdlib plus one optional CDP helper. No third-party packages.

---

## 它解决什么问题

按官方 UI 的用法，一个 agent 想「投递提示词 → 等回复 → 收割全文」会遇到一串硬墙：

| 墙 | 本仓库的解法 |
|---|---|
| 网页自动化慢、易碎、窗口不可见就挂起 | 直接打 `POST /backend-api/conversation` 走 SSE |
| 请求被要求携带 PoW | `pow_solver.py`（FNV-1a + `gAAAAAB` 前缀）毫秒级解算 |
| 会话满员 413 `conversation_too_large` | `continue_full.py` 真实鼠标点「开始新聊天」→ 摘要续接 |
| 单账号 429 就全线停摆 | `acct1 / acct2` 均分轮转 + 冷却 + 兜底 |
| 匿名态 `POST` 恒 403 `Unusual activity` | UI 建会话 + API 收割 |
| 匿名会话不入历史、换浏览器就读不到 | `conv_id` 对照表 + 全文归档 + 按设备分档的 cookie 快照 |

核心纪律写在 [SKILL.md](SKILL.md)：**提示词正文必须由 ChatGPT 会话自产，操控层只做输入输出与生命周期管理**——不自撰、不改写、不替换任务内容。

---

## 通道优先级

| 优先级 | 通道 | 适用 | 入口 |
|---|---|---|---|
| P0 | API 直连 | 发消息、收割、开新会话 | `scripts/chatgpt_send.py` |
| P1 | CDP 浏览器 | 满员续接点按钮、登录态 | `scripts/continue_full.py` |
| P2 | Playwright | 存量脚本兼容，不扩展 | — |

页面请求层整体抖动（XHR 全挂）时直接切 P0，不在 P1/P2 死磕。

---

## 安装

```bash
git clone https://github.com/oahcom/chatgpt-session-ops.git
cd chatgpt-session-ops
python --version   # 需要 3.8+
```

只需 Python 标准库。`continue_full.py` / `extract_auth.py` 额外依赖 CDP 助手模块（`from cdp import CDPClient`），按下面任一方式提供：

- 装 [browser-automation-cdp](https://github.com/oahcom) 技能，默认路径 `~/.workbuddy/skills/browser-automation-cdp/scripts`；
- 或用环境变量 `CHATGPT_CDP_DIR` 指向你自己的 `cdp.py` 所在目录。

作为 WorkBuddy / Claude Skill 使用时，把本目录整个放进 `~/.workbuddy/skills/chatgpt-session-ops/` 即可，`SKILL.md` 的 frontmatter 会被识别。

---

## 认证

凭据不进仓库。按下面结构自行放置（详见 [scripts/accounts/README.md](scripts/accounts/README.md)）：

```
scripts/
├── cookies.json                 # acct1：默认身份
├── access_token.txt             # acct1 的 Bearer token
└── accounts/
    └── acct2/
        ├── cookies.json         # acct2：第二身份
        └── access_token.txt
```

`CHATGPT_AUTH_DIR` 可把 acct1 的凭据目录指到别处（便于多机共用或把密钥放在加密盘）。

提取方式：启动带 CDP 的 Edge（默认端口 9222），**用户本人**登录 ChatGPT 后运行

```bash
python scripts/extract_auth.py
```

只处理默认账号。**不要**在聊天、issue、commit 里粘贴 cookie 或 access token。

---

## 快速上手

### 投递到既有会话并收割

```bash
python scripts/chatgpt_send.py <conv_id> prompts/01-topic.md
python scripts/chatgpt_send.py --account acct2 <conv_id> prompts/01-topic.md
python scripts/chatgpt_send.py --tail <conv_id>          # 只收割，不发送
```

完整链路：live tail（GET 429 时用缓存 tail 兜底）→ `chat-requirements` + PoW → POST（**必带 `conversation_id`**）→ 校验落点会话一致 → SSE 收割 → 落盘 `replies/reply-<conv_id8>-<topic>.md`，全文不截断。

### 开全新会话（登录态均分）

```bash
python scripts/chatgpt_send.py --new prompts/owner-ruling.md
python scripts/chatgpt_send.py --rotation                  # 看游标 / 计数 / 兜底次数
python scripts/chatgpt_send.py --rotation-enable acct2     # 解封某身份
python scripts/chatgpt_send.py --owner <conv_id>           # 查会话归属
```

`acct1 → acct2 → acct1 …` 均分，游标持久化在 `account-rotation.json`，只在登录态之间推进。每次成功都登记 `conv_id → 账号`，之后不写 `--account` 时自动路由到归属身份。

轮转容错：

| 情况 | 处置 |
|---|---|
| 401 | 认证失效 → 封 6h（`--rotation-enable` 解封）；**不触发**匿名兜底 |
| 403 | 补 cookie + 重跑 PoW 重试一次；仍失败冷却 15 分钟 |
| 429 | 冷却 180 秒；**两个登录态都 429** 才走匿名兜底 |
| 匿名通道不可用 | 冷却 30 分钟 |

### 满员续接（413）

```bash
python scripts/continue_full.py <old_conv_id> prompts/02-next.md
```

填入 → 发送 → 横幅出现后**真实鼠标**点「开始新聊天」→ 摘要续接新会话 → 重发 → 新旧映射记入 `newchat-map.json`。不要在满员会话里硬重试，也不要弃任务。

### 匿名通道（登录态 429 时的应急产能）

匿名态可用，但服务端**不提供历史**，留存全靠本技能自己。它不能纯 API 建会话（Turnstile 门槛），所以走 **UI 建/发 + API 收割**：

```bash
# 起有头临时 profile（脚本在 browser-automation-cdp 技能目录下；无头模式会被 Cloudflare 挡）
python <CDP_DIR>/start_chrome.py --temp --port 9333

python scripts/chatgpt_send.py --account nologin --new prompts/x.md
python scripts/chatgpt_send.py --anon-list                 # 对照表：live / archived
python scripts/chatgpt_send.py --anon-send <conv_id> prompts/y.md   # 复用原标签页续聊
python scripts/chatgpt_send.py --anon-read <conv_id>       # 读全文并归档（标签页没了也能读）
```

留存四件套：

| 留存物 | 路径 | 作用 |
|---|---|---|
| id 对照表 | `anon-sessions.json` | `conv_id → targetId / prompt / 回复摘要 / 设备号 / 状态` |
| 全文归档 | `anon-sessions/<conv_id8>.md` | `--anon-read` 落盘全部轮次 |
| 设备身份（最新） | `accounts/nologin/device-cookie.txt` | 当前浏览器的 `oai-did` |
| 设备身份（分档） | `accounts/nologin/devices/<oai-did>.txt` | 每设备一份快照，换 profile 后旧会话仍可读 |

**边界（实测）**：同标签页可多轮；重开 `/uc/<id>` 会被重定向到首页，所以续聊只能复用原标签页；会话只与 `oai-did` 绑定，丢设备 cookie 即永久失联。**需要长期留存、归档、跨机续接的会话，请用 acct1/acct2。**

---

## 运行态文件

本机生成，不入库：

| 文件 | 内容 |
|---|---|
| `account-rotation.json` | 轮转游标、各身份计数、失败计数、不可用标记及 TTL、兜底次数 |
| `conv-accounts.json` | `conv_id → 归属账号`，驱动自动路由 |
| `anon-sessions.json` | 匿名会话唯一索引 |
| `replies/` | 收割的回复全文 |
| `*.lock` | 三个 json 的跨进程写锁（`O_EXCL`，stale 60s 自动回收） |

三个状态文件的读-改-写都走 `_file_lock` + `_update_json`，并发建会话不会互相覆盖计数或游标。

---

## 故障速查

| 症状 | 根因 | 处置 |
|---|---|---|
| 403 `Unusual activity` | cookie 残缺，或缺 PoW | 脚本自动 `warm_cookies()` + 重跑 PoW；仍失败说明该身份被风控 |
| 只有 2 条 cookie 就 403 | 缺 `__cf_bm` / `_cfuvid` / `__cflb` | 访问首页接 Set-Cookie（脚本已自动）或重新提取 |
| 匿名建会话 403 | 纯 API 过不了 Turnstile | 必须浏览器通道（`--temp` 有头 + CDP） |
| 匿名会话 404 `conversation_inaccessible` | 设备身份变了或会话被回收 | 不可恢复；改用登录态重建（脚本显式报错，不静默分叉） |
| 匿名续聊「标签页已不存在」 | 匿名会话只在原标签页可写 | `--anon-read` 读全文；继续对话请用登录态 |
| 匿名页点发送无反应 | 窗口不可见 → `visibilityState=hidden` | 已启用 `Emulation.setFocusEmulationEnabled`；仍无效则让窗口可见 |
| GET 429 | 配额紧张 | 退避 ≥90s + `conv-api/` 缓存 tail |
| 413 `conversation_too_large` | 会话满员 | `continue_full.py` |
| 消息落入全新会话 | POST 缺 `conversation_id` | 补字段重发，核对 SSE 返回的 id |

---

## 安全与使用边界

- **本仓库只放代码与协议文档**。cookie、access token、`oai-did`、对话正文一律 `.gitignore`。
- 仅供账号持有人管理自己的会话。不要用它访问他人会话、绕过他人配额，或做任何违反 ChatGPT 服务条款的事。
- 匿名通道只用于明确公开、服务端允许匿名访问的一次性短任务。
- 匿名会话的设备 cookie 等于该会话的全部凭据（`devices/` 被扫掉 = 永久失联）。

---

## 目录

```
SKILL.md                    技能定义与完整操作手册（纪律、工作流、边界）
references/api-protocol.md  实测协议细节：端点、PoW、请求体全字段、SSE 解析
scripts/chatgpt_send.py     统一投递 / 收割 / 轮转 / 匿名通道（1600 行，核心）
scripts/continue_full.py    413 满员续接
scripts/pow_solver.py       PoW 解算器
scripts/extract_auth.py     从 Edge CDP 提取凭据
```

改脚本前先读 `references/api-protocol.md`，源码里的血泪注释（`conversation_id` 必填、勿提前关连接等）都对应一次真实故障。

## License

MIT
