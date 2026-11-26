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
| 匿名会话不入历史、换浏览器就读不到 | `conv_id` 对照表 + 全文归档 + 按设备分档的 cookie jar（含 HttpOnly）|

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
python --version   # 需要 3.11+（fromisoformat 的紧凑/无秒形式在 3.11 才有）
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

提取方式：启动带 CDP 的 Edge（默认端口 9222，`CHATGPT_CDP_PORT` 可改），**用户本人**登录 ChatGPT 后运行

```bash
python scripts/extract_auth.py
```

一次写出两个文件：`cookies.json`（只保留 chatgpt.com / openai.com 域，含 HttpOnly）与 `access_token.txt`（取自 `/api/auth/session`），落盘即 chmod 600。只处理当前浏览器已登录的默认账号。**不要**在聊天、issue、commit 里粘贴 cookie 或 access token。

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
# 注意：占位符必须整段替换，不能连尖括号一起粘贴——bash 会把 `<...>` 当成输入重定向
CDP_DIR="$HOME/.workbuddy/skills/browser-automation-cdp/scripts"
python "$CDP_DIR/start_chrome.py" --temp --port 9333

python scripts/chatgpt_send.py --account nologin --new prompts/x.md
python scripts/chatgpt_send.py --anon-list                 # 对照表：live / archived / 设备是否留存
python scripts/chatgpt_send.py --anon-send <conv_id> prompts/y.md   # 复用原标签页续聊
python scripts/chatgpt_send.py --anon-read <conv_id>       # 读全文并归档（标签页没了也能读）
python scripts/chatgpt_send.py --anon-devices              # 本地留存了哪些设备身份
python scripts/chatgpt_send.py --anon-relink <conv_id> <device>     # 手工修正归属设备
python scripts/chatgpt_send.py --anon-restore <device>     # 把留存身份回灌浏览器
```

留存四件套：

| 留存物 | 路径 | 作用 |
|---|---|---|
| id 对照表 | `anon-sessions.json` | `conv_id → targetId / prompt / 回复摘要 / 设备号 / 状态` |
| 全文归档 | `anon-sessions/<conv_id8>.md` | `--anon-read` 落盘全部轮次 |
| 设备身份（最新） | `accounts/nologin/device-cookie.txt` | 当前浏览器的 `oai-did` |
| 设备身份（分档） | `accounts/nologin/devices/<oai-did>.{txt,json}` | Cookie 头 + 完整 jar（含 HttpOnly），换 profile 后旧会话仍可读 |

设备身份是这条通道唯一的「钥匙」，所以脚本把它当一等公民：

- 每次投递**先留存再发送**：走 CDP `Storage.getCookies` 抓完整 jar（`document.cookie` 读不到 HttpOnly），按 `oai-did` 落盘；发送中途异常也用浏览器侧的 did 登记，不留读不回的死档。
- 读取时**按设备逐个试**：先试对照表登记的 `device`，不匹配再试其他留存设备（新→旧，≤8），命中即自动修正归属；429/0/5xx 立刻停手，不拿限速配额去遍历。
- 落盘前**剔除登录态 token**（`*-session-token`/`authjs`/`next-auth`/`access_token`）：匿名通道只要 `oai-did` 与 CF 系列。

**边界（实测）**：同标签页可多轮；重开 `/uc/<id>` 会被重定向到首页，所以续聊只能复用原标签页；会话只与 `oai-did` 绑定，丢设备 jar 即永久失联。**需要长期留存、归档、跨机续接的会话，请用 acct1/acct2。**

---

## 健壮性加固

这一层是踩过坑之后补的，测试都写在 `tests/` 里（离线跑，不触网）：

| 加固点 | 行为 |
|---|---|
| id 校验 | `conv_id` / 设备号只允许字母数字与 `_-`；非法值 CLI 直接 exit 2。它们会拼进文件名，`--anon-read ../../evil` 曾能写到工作目录外 |
| 状态文件损坏 | 损坏的 json 隔离成 `.corrupt-<ts>` 并告警，而不是静默清空后覆盖（`conv-accounts` 最多 500 条归属） |
| 原子落盘 | 一律 `tmp → fsync → replace`。匿名归档是唯一副本，写半截等于会话丢失 |
| 回复全文 | 默认不截断，只有显式设 `CHATGPT_REPLY_MAX_CHARS` 才截并在文件头标注 |
| 凭据权限 | POSIX 上落盘后 chmod 600 |
| 429 退避 | 优先服从 `Retry-After`，夹在 [5,180] 秒 |
| 文件锁 | 跨进程 `O_EXCL` + 持有者死活判定（DEAD 立即回收，存活永不回收；带 `pid` 的锁年龄不参与，无 `pid` 的老格式仍按 mtime 60s），重入按**线程**判定（按路径记会让同进程第二个线程跳过加锁，实测丢 21/80 次更新） |
| 设备身份 | CDP 抓完整 jar 落 `.txt` + `.json`，读旧会话时多设备回退并自愈归属；jar 落盘前剔除 session token |
| 出口代理 | API / 匿名浏览器 / 自启动 Chrome / git push 共用一次解析结果；取不到出口就拒绝联网（`CHATGPT_ALLOW_DIRECT=1` 才放行直连） |
| 代理探活 | 自动探测先做 CONNECT/SOCKS 隧道 + TLS 握手，失败即跳过（端口在听 ≠ 能用：实测 7890 通 200 后断流、7897 才活） |
| 413 续接 | 填词改走 CDP `Input.insertText`；`navigate` 用正确的 `timeout` 参数；CDP 不可用给人话 |
| 凭据提取 | 只落 ChatGPT 域 cookie，并真正抓 `accessToken` 写 `access_token.txt` |
| PoW | 解不出时返回空 proof 并告警，不再返回写死的假 token |

环境变量：

| 变量 | 默认 | 作用 |
|---|---|---|
| `CHATGPT_AUTH_DIR` | 脚本目录 | acct1 凭据根 |
| `CHATGPT_BASE` | `https://chatgpt.com` | API 根（可指向本地闸口） |
| `CHATGPT_PROXY` | 空 | 出口代理，最高优先级；`http/https/socks5/socks5h` |
| `CHATGPT_ALLOW_DIRECT` | 空 | 设 `1` 才允许直连（默认 fail-closed，拒绝联网） |
| `CHATGPT_ANON_CDP_PORT` | 9333 | 匿名通道 CDP 端口 |
| `CHATGPT_ANON_FALLBACK` | 1 | 设 0 彻底禁用匿名兜底 |
| `CHATGPT_ANON_AUTOSTART` | 1 | 匿名通道是否自动拉起浏览器 |
| `CHATGPT_CDP_DIR` | `~/.workbuddy/skills/browser-automation-cdp/scripts` | `cdp.py` 所在目录 |
| `CHATGPT_CDP_PORT` | 9222 | `continue_full.py` / `extract_auth.py` 用的调试端口 |
| `CHATGPT_REPLY_MAX_CHARS` | 0（全文） | 回复落盘上限 |
| `CHATGPT_ANON_IDLE_SEC` | 900 | 匿名等待生成结束的预算 |

```bash
python -m unittest discover -s tests -v      # 全绿即通过；条数见 references/baselines.md 的 §G 快照
```

---

## 统一出口代理

所有出网动作共用一个出口，不允许「API 走代理、浏览器裸连」。解析顺序：
`CHATGPT_PROXY` → `scripts/proxy.json` → 系统 `https_proxy/http_proxy/all_proxy` → 本机端口探测（**必须真握手通过**）。

```bash
python scripts/chatgpt_send.py --proxy-show              # 出口来自哪里
python scripts/chatgpt_send.py --proxy-check             # 量出口 IP（代理侧 vs 直连侧）
python scripts/chatgpt_send.py --proxy-check --browser   # 再验浏览器侧是否同一出口
python scripts/chatgpt_send.py --proxy-set socks5h://user:pass@1.2.3.4:1080
```

- **fail-closed**：一条都解析不到就报错退出，只有 `CHATGPT_ALLOW_DIRECT=1` 才直连。
- `127.0.0.1 / localhost` 永不过代理：CDP 与本地闸口必须直连。
- 换代理要重启浏览器（`--proxy-server` 只在启动时生效）；`--proxy-check --browser` 会比对两侧 IP。
- `socks5h://` 是真支持（自定义 `http.client` 连接类做 RFC1928 握手）：urllib 原生只认 http 代理。

## 运行态文件

本机生成，不入库：

| 文件 | 内容 |
|---|---|
| `account-rotation.json` | 轮转游标、各身份计数、失败计数、不可用标记及 TTL、兜底次数 |
| `conv-accounts.json` | `conv_id → 归属账号`，驱动自动路由 |
| `anon-sessions.json` | 匿名会话唯一索引 |
| `replies/` | 收割的回复全文 |
| `proxy.json` | 出口代理配置（可能含账号密码，chmod 600、不入库） |
| `*.lock` | 三个 json 的跨进程写锁（`O_EXCL`；持有者已死下一次抢锁立即回收，持有者存活永不回收；无 `pid` 的老格式仍按 mtime 60s） |

三个状态文件的读-改-写都走 `_file_lock` + `_update_json`，并发建会话不会互相覆盖计数或游标。

---

## 故障速查

| 症状 | 根因 | 处置 |
|---|---|---|
| 403 `Unusual activity` | cookie 残缺，或缺 PoW | 脚本自动 `warm_cookies()` + 重跑 PoW；仍失败说明该身份被风控 |
| 只有 2 条 cookie 就 403 | 缺 `__cf_bm` / `_cfuvid` / `__cflb` | 访问首页接 Set-Cookie（脚本已自动）或重新提取 |
| 匿名建会话 403 | 纯 API 过不了 Turnstile | 必须浏览器通道（`--temp` 有头 + CDP） |
| 匿名会话 404 `conversation_inaccessible` | 设备身份变了、快照丢失或会话被回收 | `--anon-devices` 看留存设备；`--anon-read` 会自动逐个快照试并修好归属；没有快照就只能重建 |
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
- 匿名会话的设备 jar 等于该会话的全部凭据（`devices/` 被扫掉 = 永久失联），因此落盘即 chmod 600，且剔除登录态 token。

---

## 目录

```
SKILL.md                    技能定义与完整操作手册（纪律、工作流、边界）
references/api-protocol.md  实测协议细节：端点、PoW、请求体全字段、SSE 解析
scripts/chatgpt_send.py     统一投递 / 收割 / 轮转 / 匿名通道 / 设备身份留存（核心）
scripts/continue_full.py    413 满员续接
scripts/pow_solver.py       PoW 解算器
scripts/extract_auth.py     从 Edge CDP 提取凭据
scripts/accounts/README.md  凭据目录结构（不含任何真实凭据）
tests/test_hardening.py     离线回归（id 校验、损坏隔离、原子写、锁并发、兜底判定、设备回退、投递主流程）
tools/sync.sh               从本机技能目录同步 + 提交前敏感信息拦截
```

改脚本前先读 `references/api-protocol.md`，源码里的血泪注释（`conversation_id` 必填、勿提前关连接等）都对应一次真实故障。

## License

MIT
