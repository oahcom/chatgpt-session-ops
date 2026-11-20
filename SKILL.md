---
name: chatgpt-session-ops
description: ChatGPT 会话操控与生命周期管理（API 直连 + PoW + CDP 续接）。当需要向 ChatGPT 会话投递提示词、收割回复、管理多会话协调（开新会话、满员续接、限速重试）、扮演 Owner 裁决会话、或跨 WorkBuddy 会话同步操控规范时使用。触发词：操控 ChatGPT、投递提示词、chatgpt 会话管理、收割回复、PoW、Unusual activity、conversation_too_large、开始新聊天、会话满员、定时任务排程、automation 粒度、1 小时维度。核心纪律：所有逻辑规划实现必须由 ChatGPT 主导，提示词正文由 ChatGPT 自产，操控层只做输入输出与生命周期管理。
agent_created: true
---

# chatgpt-session-ops

ChatGPT 会话操控与生命周期管理。把"手和眼"（I/O、投递、收割、续接）与"脑"（逻辑规划、实现方案、提示词内容）严格分离：**所有逻辑规划与实现必须由 ChatGPT 主导**，提示词正文必须由 ChatGPT 会话自产（其回复中的"下一步/FollowUp/自包含提示词"原文引用），操控层只做选项拾取、事实转达、路由与投递，不自撰任务内容、不自做规划与实现、不改写自产提示词。

## 开发基线（用户裁定 2026-11-19，最高优先级）

**不存在人类决策点，这是开发基线。所有操作都不用用户处理。**

- **全链自动闭环**：代码、验收、合并、登记由 ChatGPT 会话生态自行闭环，持续到收敛；用户零参与操作。
- **禁止停等人类**：不得把 R3 真人专家审查、main 合并批准、配额解除、生产激活等列为停点抛回用户等待；此类生产级门槛一律标 `OUT-OF-SCOPE(dev-baseline)`——**不伪造其已完成，也不以其阻塞收敛**。
- **收敛口径**：开发基线全链闭环（代码链 + Owner 验收 + PR/合并 + 登记归档）全部完成即收敛；错误模式是把生产级门槛当开发基线停点、把操作抛回用户。
- 用户已授权：**所有 Issue/PR 由 ChatGPT 会话自行闭环**；Git 写入等机械操作由协调方代执行（会话产内容、协调方提交），属正常分工不算人类决策点。
- **逻辑主导权（用户裁定 2026-11-19，与本节同级）**：**所有逻辑规划与实现必须由 ChatGPT 主导**。方案设计、规划拆解、判定口径、实现内容全部出自 ChatGPT 会话自产；协调方/操控层**不得自行规划、自行实现、自行补全方案**——包括未来对应的开发仓库与子仓库（同套规划同样只由 ChatGPT 主导落地）。操控层职责仅有四类：事实转达（API/git 实测证据原文逐字转达）、选项拾取（按 ChatGPT 回复给出的选项继续）、Git/文件机械操作（提交、落盘、回填）、投递与收割。ChatGPT 给的方案不完整时**回问 ChatGPT 补全**，不自行脑补。
- **会话管理层级（用户裁定 2026-11-18，SESSION-HIERARCHY v1.0）**：**Owner ≠ 会话**——A–I 是身份定义，会话=身份实例（N:1），同身份可并发多实例；每需求一个总理人（A 实例）负责分解分配、构建子工作提示词、统筹总审，同一工作项同时仅一个 A 实例持派工写租约。层级 L0–L4：L0 管理器（路由/投递，无专业决策权）→ L1 总理人 → L2 领域工作会话 → L3 独立审查 → L4 验收裁决；**专业提示词只出自 L1/L2 自产**。管理器判断必须以「管理器观点」显式提交 ChatGPT 挑战收敛，不得当影子作者强加结论。
- **主理人膨胀收敛职责（用户明示 2026-11-19，强制）**：**每个需求的主理人对该需求范围 PR/Issue 膨胀直接负责**——开工前预检、增量先查归并、发现趋势立即收敛、WIP 超线先收敛再开新工单；不等巡检窗口。配额纪律：open PR 超线即冻结新建 PR/Issue，只做既有载体补提交/合并/归并，解除按协调器宣布。

## 基线（A/B 类，可判定）

**详细条款、判定口径、违例后果、现成配方见 `references/baselines.md`。** 这里是索引，不是全文——条款冲突时以该文件为准，用户明示的管理基线又高于该文件。

| 类 | 主题 | 编号 | 一句话 |
|---|---|---|---|
| **A1–A4** | 内容主权 | A1–A4 | 逻辑规划/实现/提示词全部由 ChatGPT 主导自产、逐字节搬运、裁决必投全新会话 |
| **A5–A9** | 通道与身份 | A5–A9 | 登录态优先、401/403 不兜底、匿名只认设备身份、可写窗口=原标签页 |
| **A10–A14** | 投递与收割 | A10–A14 | 必带 `conversation_id`、200 即停轮转、大交付对 raw mapping、全文落盘、先落设备 |
| **A15–A18** | 限速与失败 | A15–A18 | 单 GET 进程、会话间 ≥60s、**`Retry-After` 是下限（不得向下截 180s）**、401/403/429 分级 |
| **A19–A22** | 存储与安全 | A19–A22 | id 白名单、原子写、损坏隔离、锁按线程判重入 + **回收前校验 PID 启动时间** |
| **A23–A26** | 出口 | A23–A26 | 单一出口、fail-closed(exit 3)、探活真握手、探活要验目标站、**WebRTC 不许漏真实 IP** |
| **A27–A28** | 思考模式 / 流截断 | A27–A28 | 要思考就必须真开（解析不出 `-t-` 抛 `ThinkingModelUnavailable`：默认轮转换账号、全废才 `exit 1`，显式 `--account` 直接失败；无降级口子）；**`IncompleteRead` ≠ 失败**，保留半截 + GET 补全文 |
| **B1–B5** | 排程 | B1–B5 | 1 小时维度、条件巡检、validFrom/Until 收窗、看门狗 ≤1h、过期即 PAUSED |
| **B6–B9** | 执行可靠性 | B6–B9 | prompt 自包含（含路径**类型**校验）、task/step 状态机幂等续跑、**RUNNING ≤3 且原子领槽**、删前先 PAUSED |
| **B10–B15** | 行为契约 | B10–B15 | 命名正则即契约、argv 白名单只读、无事静默、重试上限 N=3、不与 A 类打架、**实例按 PID+启动时间+run_id 回收** |

**§0 验收三分法（审查补入）**：命令成功（exit 0）≠ 业务成功（服务端确实做了）≠ 证据成功（落盘 + SHA-256 + id 对齐）。**把前两层当第三层，是本 skill 最危险的失败模式。**

最容易被忽略的六条（写在这里，因为踩过）：**A10**（缺 `conversation_id` 静默落新会话）、**A22**（锁按路径记 → 多线程丢更新；PID 复用被当活锁）、**A23**（WebRTC UDP 绕过代理，实测漏出本机公网 IP `59.34.82.8`）、**A26**（`ipify` 通 ≠ 目标站可达）、**A28**（23MB 流被截断判失败 → 换身份重投）、**B8**（同分钟挂超过 3 个 → 被 enqueue 等效漏跑）。

> v1.1 已吸收 ChatGPT 思考模式对外对抗审查（裁定 `NEEDS-REVISION`，A 类 12 WEAK / 2 MISSING，B 类 10 WEAK / 4 MISSING）；未采纳项（Asia/Tokyo、7 分钟统一超时、WebRTC 硬门禁）与理由见 `references/baselines.md` §F。

## 通道优先级（固定）

| 优先级 | 通道 | 适用 | 工具 |
|---|---|---|---|
| P0 | API 直连 | 发消息、收割、开新会话 | `scripts/chatgpt_send.py` |
| P1 | CDP 浏览器 | 满员续接点按钮、登录态 | `scripts/continue_full.py` |
| P2 | browser-agent/Playwright | 存量脚本兼容，新工作不扩展 | — |

页面请求层抖动（XHR 全挂）时直接切 P0，不在 P1/P2 死磕。

## 前置：认证

账号凭据隔离：acct1 复用既有默认目录（`CHATGPT_AUTH_DIR` 或脚本同目录），acct2 / acct3 / acct4 使用 `scripts/accounts/acct2/`、`scripts/accounts/acct3/`、`scripts/accounts/acct4/`，各自读取 `cookies.json` 与 `access_token.txt`。**凭据属个人数据，不随仓库分发**：按 `scripts/accounts/README.md` 的结构自行放入即可，acct2 / acct3 / acct4 装好即与 acct1 等价参与轮转。

**身份优先级：登录态优先，匿名只做兜底。** `--new` 默认在 `acct1 / acct2 / acct3 / acct4` 之间均分轮转；`nologin`（匿名）**仅当全部登录态都 429**（本次命中或处于 429 冷却）时才启用，401/403 不触发兜底。`CHATGPT_ANON_FALLBACK=0` 可彻底禁用。需要匿名时也可显式 `--account nologin --new`。

**429 限流的处置（用户裁定：换账号，或直接用匿名渠道）。** 四条硬规则：

1. **显式指定主身份也照样换号。** `--account acct1 --new` 遇 429 时，先给 acct1 记 429 冷却，再转轮转（换 acct2 / acct3 / acct4；全部 429 则走匿名）。理由：429 时会话**尚未建成**，重试不会重复建会话、不会多花配额——显式身份只约束「用谁起」，不约束「限速后还能干什么」。
2. **401 / 403 不换号。** 401 是凭据失效（该重新提取），403 是 PoW/风控（短冷却自愈）；换身份只会把病因藏起来。
3. **显式 `--account nologin` 遇 429 不再兜底。** 再兜底还是匿名，等于无限自递归。
4. **既有会话遇 429 只能退避。** 会话归属固定：换账号 404、匿名更读不到这条会话。按返回体的真实时限退避后重试，或 `--new` 另起（那条路才会换号/走匿名）。

**匿名会话（nologin）可用，但由 skill 自己留存。** 实测（2026-11-19）：能建会话、能在同一标签页内多轮对话（session id 不变）、能按 id 经 API 读全文。服务端**不提供历史**，所以留存完全靠本技能的三件套：

| 留存物 | 路径 | 作用 |
|---|---|---|
| id 对照表 | `scripts/anon-sessions.json` | `conv_id → 标签页 targetId / prompt / 最后回复摘要 / 设备号 / 状态(live\|archived)` |
| 全文归档 | `scripts/anon-sessions/<conv_id8>.md` | `--anon-read` 时把 user/assistant 全轮次落盘 |
| 设备身份（最新） | `scripts/accounts/nologin/device-cookie.txt` | 当前浏览器的 `oai-did` 等 |
| 设备身份（分档） | `scripts/accounts/nologin/devices/<oai-did>.txt` | 每个设备一份 Cookie 头快照，**读旧会话时按对照表里的设备号取回** |
| 设备身份（完整 jar） | `scripts/accounts/nologin/devices/<oai-did>.json` | 含 HttpOnly 的完整 cookie，`--anon-restore` 可回灌浏览器 |

匿名会话只认设备身份，所以「分档快照」是跨浏览器重启留存的关键。脚本现在把设备身份当一等公民：

- **每次投递都留存**：`send_anon` 一启动就先抓浏览器 cookie jar（走 CDP `Storage.getCookies`，含 HttpOnly；`document.cookie` 读不到那部分）并按 `oai-did` 落盘。发送中途炸掉也不丢设备号——异常路径用浏览器侧的 did 登记会话，不会出现「会话建出来了但读不回来」的死档。
- **读取按设备顺序尝试**：`--anon-read` 先试对照表登记的 `device`，不匹配再依次试其他留存设备（新→旧，最多 8 个），命中即自动修正归属；429/0/5xx 立即停手（通道问题，换设备无意义，不烧配额）。
- **不落凭据**：jar 落盘前剔除 `*-session-token` / `authjs` / `next-auth` / `access_token` 一类条目——匿名通道只要 `oai-did` 与 CF 系列，浏览器若恰好是登录态，也不能把 session token 写进 nologin 目录。
- **可诊断、可修复**：`--anon-devices` 看留存了哪些设备（各自绑几条会话、是否完整 jar）；`--anon-relink <conv_id> <device>` 手工修归属；`--anon-restore <device>` 把身份回灌当前浏览器。

已修的坑（2026-11-19 审查）：

1. 新建匿名会话时若当前页正挂着旧 `/uc/<id>`，**必须另开标签页**而不能导航（导航会永久毁掉旧会话的可写通道）→ 脚本按 URL 判断并 `Target.createTarget` 新页。
2. 轮转兜底成功**不推进游标**，否则登录态恢复后会白跳过一个账号的轮次。
3. `--anon-send` 探测分级：只有 **404/403 才判定会话失效**；429/0/5xx 视为通道抖动，重试后报「暂时读不到，不代表会话丢失」。
4. CDP 端口没起时**不把会话标 archived**（浏览器没起 ≠ 会话丢了），改为明确提示先启浏览器；会话仍可 `--anon-read`。
5. 匿名发送中途异常若已拿到 conv_id，仍登记进对照表，避免查不到的孤儿会话。
6. 三个 json 的读-改-写走**跨进程文件锁**（`*.lock` + stale 60s + 进程内重入），并发建会话不再互相覆盖计数/游标。
7. POST 200 但 SSE 未解析出 id 时，依次用整包 JSON、`"conversation_id":"…"` 正则兜底，减少「200 无 id」。**只认首个带 id 的事件**，`[DONE]` 之后的事件不采信；同一响应出现两个不同 id → 无法确认落点，判失败关闭（失败结果带 target 与候选 id），不取最后一个也不回落到传入 id。
8. 兜底前检查 `nologin` 冷却，避免浏览器坏掉时每次兜底都白等自动拉起。

**按 id 启用匿名通道**：

```bash
python scripts/chatgpt_send.py --anon-list                       # 看对照表：live / archived / 设备是否留存
python scripts/chatgpt_send.py --anon-send <conv_id> <prompt>     # 用该 id 续聊（复用原标签页）
python scripts/chatgpt_send.py --anon-read <conv_id>              # 读全文并归档（标签页没了也能读，自动试各设备快照）
python scripts/chatgpt_send.py --anon-devices                    # 看本地留存了哪些设备身份
python scripts/chatgpt_send.py --anon-relink <conv_id> <device>  # 手工修正会话归属设备
python scripts/chatgpt_send.py --anon-restore <device>           # 把留存身份回灌当前浏览器
```

边界（实测）：

| 检查项 | 结果 |
|---|---|
| 同一标签页多轮 | **可以**，`/uc/<id>` 不变，消息连续累积 |
| 重新打开 `/uc/<id>`（新标签页/重导航） | **不行**，会被重定向到首页 → 续聊只能复用原标签页（脚本按 targetId 找） |
| 按 id 经 API 读 | 可以：建会话 13 分钟后仍 `200`；陌生 `oai-did` 访问返回 `404 conversation_inaccessible` |
| 是否进服务端历史 | **否**：`GET /backend-anon/conversations` 恒 0 条 |

因此：**匿名通道适合一次性、短周期、即问即取的任务（如 Owner 裁决）以及全部登录态都 429 时的应急产能**；需要长期留存、归档、跨机续接的会话仍用 acct1/acct2/acct3/acct4。标签页丢失时脚本显式报错并指向 `--anon-read`，不会静默新建。

**nologin 走浏览器通道，不是纯 API。** 匿名态建/发必须过 Cloudflare Turnstile：纯 API 的 `POST /backend-anon/conversation` 恒 403 `Unusual activity`（`chat-requirements` 返回 `turnstile.required=true` + `force_login=true`）。实测可行路径是 **UI 建会话 + API 收割**：

- 用临时 profile 启动调试浏览器：`python scripts/start_chrome.py --temp --port 9333`（在 browser-automation-cdp 技能目录下；脚本在 CDP 未就绪时会自动拉起）
- 脚本经 CDP 在未登录页面建会话（落点 `https://chatgpt.com/uc/<id>`），再用页面 cookie 走 `GET /backend-anon/conversation/<id>` 收割
- 两个隐坑：窗口未显示时页面 `visibilityState=hidden`，应用会挂起发送 → 脚本已启用 `Emulation.setFocusEmulationEnabled`；无头模式会被 Cloudflare「请稍候」挡住 → 必须用有头浏览器
- 端口用 `CHATGPT_ANON_CDP_PORT` 覆盖（默认 9333）；device cookie 落盘 `scripts/accounts/nologin/device-cookie.txt` 与分档快照 `accounts/nologin/devices/<oai-did>.{txt,json}`（扫掉 `devices/` 就永久失去那些设备上的历史匿名会话）
- `start_chrome.py --temp` 的 profile 其实是**固定目录** `%TEMP%/cdp-browser-temp`：重启浏览器但不清目录时 `oai-did` 不变，设备身份通常能延续；只有清目录/换机才真的换设备。这也说明本地设备快照才是可靠兜底

**凭据不全的身份会在 POST 时 403**：acct2 的 cookies.json 只有 `session-token` + `oai-did`（2 条），GET 全部正常但 POST 判 `Unusual activity`——缺 `__cf_bm`/`_cfuvid`/`__cflb`。脚本在首次 403 时自动 `warm_cookies()`（访问首页接收 Set-Cookie 并回写 cookies.json）再重试一次，实测可自愈。

提取流程：启动已配置 CDP 的 Edge（默认端口 9222，`CHATGPT_CDP_PORT` 可改），用户本人登录 ChatGPT 后运行 `python scripts/extract_auth.py`，一次写出 `cookies.json`（仅 chatgpt.com / openai.com 域，含 HttpOnly）与 `access_token.txt`（来自 `/api/auth/session`），两者都 chmod 600。脚本只处理当前浏览器已登录的默认账号；acct2 / acct3 / acct4 凭据通过用户已登录的 InPrivate 窗口经 DevConsole 提取——在该窗口控制台同源 `fetch('/api/auth/session')` 取 accessToken、`document.cookie` 取可读 cookie，POST 到本机回环接收服务（或下载 JSON 再由本技能读取），落盘到 `accounts/<acct>/`（注意导出文件名要带对应账号编号，避免覆盖）。重新提取时仍需用户本人在浏览器完成登录，不要在聊天中发送或粘贴 cookie、access token。实测（2026-11-20，acct3/acct4）：浏览器 CSP/PNA 会把发往 `127.0.0.1` 的 `fetch` 拦下，兜底走「下载 `<acct>-export.json`」再读文件即可；导出的 cookie 缺 CF 系列，首轮 `warm_cookies()` 会补 `__cflb`/`__cf_bm`/`_cfuvid` 后 POST 恢复正常。

## 核心工作流

### 1. 投递到既有会话

```bash
python scripts/chatgpt_send.py <conv_id> <prompt_file>
python scripts/chatgpt_send.py --account acct2 <conv_id> <prompt_file>
```

#### 会话归属与路由

每个会话只属于创建它或明确授权访问它的账号。未显式指定 `--account` 时，脚本必须根据 `conv-accounts.json` 中经过验证的登记归属自动路由；登记缺失、记录无效或归属矛盾时必须 fail closed，禁止隐式回落到 `acct1`、自动探测其他账号或继续访问，并要求用户明确提供或确认归属。显式指定 `--account` 时，必须验证其与已登记归属一致；不一致时以退出码 `2` 拒绝执行，不得仅打印 WARN 后继续。`default` 与 `acct1` 按既有配置定义作为同一 credential root 的别名处理。匿名态只用于明确公开、且服务端允许匿名访问的内容；不用于访问或探测私有会话。

各账号限速彼此独立。429 时对原身份退避至少 90 秒，GET 可用该身份缓存；不要并发运行同一账号请求。

查询会话清单：`python scripts/chatgpt_send.py --account acct1 --list` 查询单一账号；`python scripts/chatgpt_send.py --list-all` 顺序查询本机已配置的 acct1、acct2、acct3、acct4 与匿名态，并为每条结果标注身份。`--list-all` 输出 `by_account`（按账号分组，便于归档）、`summary`（各账号计数）、`quota`（各账号最近响应的限速观测头，只读）与 `errors`。不可用的账号和接口错误会进入 errors；遇到 429 即停止剩余扫描。聚合结果只覆盖各身份自身有权看到的会话，不代表匿名态能读取私有会话。

`--list-all` 默认 **dry-run（只读）**，加 `--apply` 才把「唯一观察、枚举完整、无冲突」的候选登记进 `conv-accounts.json`；`--include-nologin` 显式把匿名态纳入枚举。写入一律走 `record_conv()`，写完在同一把锁下回读磁盘填 `registered`。未知或重复 flag 直接 exit 2，不触网。

##### Legacy conversation registry migration

The supported conversion path for legacy bare-string records in `conv-accounts.json` is `migrate_conv_accounts()` in `scripts/chatgpt_send.py`.

Legacy bare-string records remain invalid for normal ownership reconciliation. Do not interpret them as canonical ownership records, and do not convert them through `record_conv()` or by editing the JSON file manually. `migrate_conv_accounts()` is the sole supported conversion path.

The migration function:

- Acquires the existing cross-process registry lock.
- Reads the latest registry while holding the lock.
- Converts valid legacy account strings to canonical dictionary records.
- Preserves malformed records and reports them in `invalid`.
- Preserves existing dictionary metadata.
- Writes only when a conversion is needed, using atomic replacement.
- Is idempotent.

**This function performs a real write. It has no dry-run mode.** Run it only during an authorized maintenance window, after taking and verifying a registry backup and pausing other registry writers.

From the repository root, invoke it with:

```bash
python -c "import importlib.util,json,sys; p=r'scripts\chatgpt_send.py'; s=importlib.util.spec_from_file_location('chatgpt_send_migration',p); m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); print(json.dumps(m.migrate_conv_accounts(),ensure_ascii=False,sort_keys=True))"
```

Stop and do not proceed to bulk registration if the returned summary differs from the expected `status=ok` / `migrated` count / `invalid=[]` / `changed=true`.

After migration, run `--list-all` without `--apply`. Confirm that enumeration is complete, `registry_conflicts` is empty, `malformed_registry_records` is empty, `duplicates` is zero, and `invalid_ids` is zero. Only then may the separately authorized `--list-all --apply` registration operation proceed.

After applying, run `--list-all` again and confirm that no eligible registrations remain. Do not restore a pre-migration backup over a registry that may have received subsequent writes. For a partial registration failure, prefer re-running reconciliation and applying the remaining eligible IDs after verifying the registry state.

##### Production cutover baseline correction — 2026-11-20

The previously documented bulk-registration expectation of 685 records with account distribution `549/116/10/8` was arithmetically inconsistent and is superseded.

The approved production dry-run baseline is:

- `eligible_registrations`: 684
- acct1: 549
- acct2: 117
- acct3: 10
- acct4: 8

The account counts sum to 684.

The migration converted 7 legacy string records without changing the registry ID set. All 7 migrated IDs were absent from the observed conversation set. Two independent post-migration dry-runs produced identical `(id, account)` observation sets.

The approved expected post-registration registry count is 1180, calculated as 496 existing records plus 684 new registrations, assuming no other registry writes or deletions during the maintenance window.

The previous 685 expectation and `549/116/10/8` distribution must not be reused.

##### Fail-closed behavior: incomplete account enumeration

When `--list-all` encounters HTTP 401 or `token_revoked` for any account:

- `complete` MUST be false.
- That account's enumeration status MUST be `failed`.
- The eligible candidate set MUST be cleared.
- No partial candidate may be treated as a complete enumeration.
- This is expected fail-closed behavior, not an implementation failure.

The operator must not read a partial enumeration as a complete account inventory, and must not relax fail-closed in order to let the remaining accounts proceed.

Credential renewal after a server-side OAuth revocation (`token_revoked`) requires the user to re-authenticate in a browser and re-export `access_token.txt` / `cookies.json`. There is no non-interactive path; do not retry, rotate proxy, or edit the token file.

Consequence for the registry: an account stuck at `failed` keeps its last-known records and its visible conversations stay unverified. Report it as `BLOCKED_CREDENTIAL_RENEWAL` and do not count the unverified remainder as either complete or zero.

自动走完整链路：live tail（GET 429 时缓存 tail 兜底）→ requirements + PoW → POST（**必带 conversation_id**）→ **校验落点会话一致**（取 SSE **首个带 id 的** `data:` 事件；落点不一致 → **判失败**，回复写入 `quarantine/` 隔离档案（**完整原始响应**另存并记 sha256）并返回 target/actual 两个 id，实际落点会话另记待认领登记，不静默改归属）→ SSE 收割 → 落盘 `replies/reply-*.md`。

### 2. 开全新会话（Owner 裁决模式，登录态优先）

```bash
python scripts/chatgpt_send.py --thinking --new <prompt_file>       # 思考模式（评审/裁决必开）
python scripts/chatgpt_send.py --new <prompt_file>                  # 默认：acct1/acct2/acct3/acct4 均分
python scripts/chatgpt_send.py --account acct2 --new <prompt_file>  # 指定身份
python scripts/chatgpt_send.py --rotation                           # 游标/计数/兜底次数
python scripts/chatgpt_send.py --rotation-enable acct2              # 解封某身份
python scripts/chatgpt_send.py --owner <conv_id>                    # 查会话归属账号
```

轮转规则：`acct1 → acct2 → acct3 → acct4 → acct1 …` 均分（游标持久化在 `scripts/account-rotation.json`，只在登录态之间推进）。匿名仅在全部登录态都 429 时兜底，且创建后打印 `WARN` 提示不入历史。每次成功都把 `conv_id → 账号` 登记到 `scripts/conv-accounts.json`；之后投递/收割该会话、未显式指定 `--account` 时自动路由。

轮转容错：

| 情况 | 处置 |
|---|---|
| 401 | 该身份认证失效，封 6h（`--rotation-enable` 可解封）；**不触发匿名兜底** |
| 403 | 先补 cookie + 重跑 PoW 重试一次；仍失败冷却 15 分钟，不长期封禁 |
| 429 | 限速，冷却 180 秒；**显式主身份也照样换号**（换另一个登录态，都 429 则走匿名兜底）；既有会话只能按返回体时限退避（归属固定，换号/匿名都读不到） |
| 匿名浏览器通道不可用 | 冷却 30 分钟 |

故意不带 conversation_id。用于：各工作会话自产"Owner 裁决提示词"（自包含：背景、精确基线、待裁决项、可选裁决、记录格式）→ **原样**收集（回复中最长代码块，一字不改）→ 投递给全新 ChatGPT 会话扮演 Owner → 收割裁决记录（ACCEPT / ACCEPT WITH AMENDMENTS / NEEDS REVISION / REJECT + 理由 + 执行边界）→ 原文引用回灌工作会话。

### 3. 满员续接（413 conversation_too_large）

```bash
python scripts/continue_full.py <old_conv_id> <prompt_file>
```

填入→发送→横幅出现后**真实鼠标**点「开始新聊天」→摘要续接新会话→重发→新旧映射记入 `newchat-map.json`。不要在满员会话硬发重试，也不要弃任务。

### 4. 只收割

```bash
python scripts/chatgpt_send.py --tail <conv_id>
```

## 协调纪律（多会话场景）

1. **防撞车**：开工前查目标仓库 open PR/Issue；撞车即停，走"证据归并到主 PR + 关重复件"。
2. **事实仲裁**：跨会话冲突一律以 GitHub API / git log 的 exact head 为准，不以任何会话转述为准；中间提交的 CI 失败不代表当前 head。
3. **限速纪律（先定性，再退避；用户裁定 2026-11-19，取代固定 90 秒口径）**：同一账号同时只允许一个 GET 进程；发送器与补采器错峰；会话间发送 ≥60s。任何失败**先定性**（`ChatGPTAPI.classify_failure`）：
   - **代理/网络问题**（`proxy`：status 0、SSL EOF、连接重置/超时、`EXC …`）→ **跟进修复代理**（`repair_proxy()` 重新探测出口/换端口重建连接，或 `--proxy-check` 诊断后修代理工具），修好即重试，**不做计时死等**；反复探测无出口要明确报"代理需人工跟进"，不得伪装成限速。
   - **上游限速**（`ratelimit`：429，或响应体带限速字段）→ 退避时长**按返回体/头里的真实时限**（取值顺序：响应体 `resets_in` / `retry_after` / `rate_limit.reset` 等 → `Retry-After` 头 → 都没有才用默认值并注明），禁止固定 90 秒；服务端声明值夹在 [5,3600] 秒。
   - **风控**（`risk`：403 Unusual activity、Cloudflare 质询页）→ 补 cookie（`warm_cookies()`）+ 重跑 PoW 流程，仍失败进冷却，不定时硬闯。
   - GET 被限速时用 `conv-api/` 缓存 tail 兜底 parent_message_id。
4. **账号路由**：`--new` 默认登录态均分；既有会话按 `conv-accounts.json` 自动路由；显式 `--account` 优先，与登记冲突时告警。
5. **匿名纪律**：匿名只做限速兜底与一次性短任务；**需要留存、归档、跨机续接的会话必须用 acct1/acct2/acct3/acct4**。匿名会话丢设备 cookie 即永久失联，且从不进历史。
6. **落盘约定**：`prompts/`（编号前缀 NN-topic.md）、`replies/`（reply-<conv_id8>-<topic>.md 全文不截断）、`conv-api/`（GET 缓存）、`newchat-map.json`、`sessions-registry.json`、`send-queue-log.md`。
7. **禁止**：自撰/改写自产提示词；**自行做逻辑规划/实现方案/补全设计**（一切逻辑规划实现必须由 ChatGPT 主导，方案不完整回问 ChatGPT 补全）；POST 不带 conversation_id 发进既有任务会话；并发抢 GET 配额；未查 open PR 就开同主题实现；删除任何会话产物。
8. **定时任务粒度（用户明示强制 2026-11-19）**：**所有定时任务一律按 1 小时维度排程，禁止天维度（天维度过久）**。落地口径：循环任务用每小时（间隔 ≤1h）；到期处置/远期复核不得挂天级 one-shot，改为**每小时条件巡检**（提示词内置"未到点→静默退出；已到点且未处置→执行；已处置→静默退出"门控）；一次性看门狗/闹钟 ≤1h。违规排程（DAILY、数日后 one-shot）一律改小时维度。**本条为管理基线**（管理所有 AI 代理的 WorkBuddy automation 排程）：创建/修改任何 automation 前先读本条；过期 one-shot 一律 PAUSED 归档，不得留 ACTIVE 尸体；跨天/跨周的到期项用每小时条件巡检 + validFrom/validUntil 收窗，避免无效空转。完整条款见 `references/baselines.md` 的 **B1–B15**（含并发上限 3、`unknown_interrupted` 幂等续跑、只读看门狗零写副作用、无事静默）。

## 健壮性加固（2026-11-19 第三轮）

| 加固点 | 行为 | 为什么 |
|---|---|---|
| conv_id / 设备号校验 | `safe_id()`：只允许字母数字与 `_-`，长度 1-80；CLI 入口非法直接 exit 2 | 这些 id 会拼进 `replies/`、`conv-api/`、`anon-sessions/`、`devices/`，不校验等于把 `../../x` 当文件名 |
| 非法 id 退化命名 | `id8()`：非法 id 用 sha1 前 8 位兜底，永不产出路径分隔符 | 宁可文件名难看，也不能写到工作目录外 |
| 状态 json 损坏 | 隔离为 `<name>.corrupt-<ts>` 并 WARN，再按空状态重建 | 旧版静默返回空，下次落盘就把 `conv-accounts` 的 500 条归属永久清空 |
| 落盘原子性 | 一律 `tmp → fsync → replace`；失败清 tmp | 匿名归档是唯一副本，写半截等于会话彻底丢失 |
| 回复全文 | 默认不截断；只有显式设 `CHATGPT_REPLY_MAX_CHARS` 才截，且头部注明 | 旧版静默只留**末尾** 2 万字，结论常常在前半段 |
| 凭据权限 | POSIX 上 `cookies.json` 等落盘后 chmod 600 | 默认 umask 会让同机其他用户可读 |
| 429 退避 | 先定性再退避；上游限速按返回体 `resets_in`/`retry_after`/`Retry-After` 的真实时限（缺省才默认，封顶 3600 秒）；代理类失败不退避、改修代理 | 固定 90 秒既可能太快也可能白等，且会把代理故障误当限速 |
| 文件锁重入 | 按**线程**记持有者（`path -> thread ident`） | 旧版按路径记集合，同进程第二个线程会跳过加锁，多线程下照样丢更新（已由单测锁死） |
| `--tail` 退出码 | 没取到内容返回 1 | 否则脚本编排层无法区分「读到了」和「被限速」 |
| 匿名等待预算 | `CHATGPT_ANON_IDLE_SEC`（默认 900） | 超长回复不必改代码 |
| `continue_full.py` | 参数缺失/`old_conv_id` 非法/提示词为空均 exit 2；CDP 连接失败给人话；`newchat-map.json` 原子写 | 早先空参直接 IndexError，映射写半截就丢 |
| `continue_full.py` 填词 | 聚焦后走 CDP `Input.insertText` | 旧版用字符串替换把提示词塞进 JS，会把函数签名里的同名字符串一起换掉 → 语法错误，脚本从来没真正跑通过 |
| `extract_auth.py` | 只落 chatgpt.com / openai.com 的 cookie；附带抓 `/api/auth/session` 的 accessToken；落盘 chmod 600 | 旧版把全站 cookie 都写进 `cookies.json`，等于顺手备份全站登录态；且文档说有 token 实际没抓 |
| PoW 失败 | 返回空 proof 并 WARN | 旧版返回写死的假 token，服务端必然 403，还看不出是解算失败 |
| 设备身份留存 | 每次投递先抓 CDP `Storage.getCookies`（含 HttpOnly）按 `oai-did` 落 `.txt` + `.json`；jar 落盘前剔除登录态 token | 只存 `document.cookie` 会丢 HttpOnly，换 profile 后旧匿名会话再也读不回；存整包 jar 又可能把 session token 写进 nologin |
| 设备归属自愈 | `--anon-read` 依次试登记设备与其余快照（≤8），429/0/5xx 立刻停 | 设备记录缺失或换机时不必人工比对；遍历到底会把限速配额烧光 |
| 出口代理覆盖 | API、匿名浏览器、自启动浏览器共用同一次解析结果；浏览器启动带 `--proxy` | 只给 API 配代理等于「请求走隧道、浏览器裸连」，设备身份与 cookie 从直连泄出去 |
| 代理探活 | 自动探测先做 CONNECT/SOCKS 隧道 + TLS 握手，失败即跳过该端口 | 端口在听 ≠ 能用：实测 7890 回 `200 Connection established` 后立刻断流，7897 才通 |
| fail-closed | 解析不到出口直接 `SystemExit`，只有 `CHATGPT_ALLOW_DIRECT=1` 放行直连 | 静默直连是最坏的失败方式：任务「成功」了，但真实 IP 已暴露 |
| 本机绕过 | `127.0.0.1 / localhost / ::1 / 0.0.0.0` 永不过代理 | CDP 与本地闸口（`CHATGPT_BASE`）被塞进代理后，代理一挂全链路瘫痪 |

环境变量：`CHATGPT_PROXY`（出口代理，最高优先级）、`CHATGPT_ALLOW_DIRECT=1`（显式放行直连，仅调试）、
`CHATGPT_REPLY_MAX_CHARS`（默认 0=全文）、`CHATGPT_ANON_IDLE_SEC`（默认 900）、
`CHATGPT_CDP_PORT`（`continue_full.py` / `extract_auth.py` 用，默认 9222）、`CHATGPT_ANON_CDP_PORT`（匿名通道，默认 9333）、
`CHATGPT_THINKING=1`（等价 `--thinking`，新会话走 `-t-` 思考模型）、`CHATGPT_MODEL`（直接指定 model slug，优先级最高）、`CHATGPT_NO_THINKING=1`（浏览器侧显式不点「思考」）。

回归测试（离线，不触网、不需要凭据）：

```bash
python -m unittest discover -s tests -v
```

308 条，覆盖 id 校验与路径穿越、状态文件损坏隔离（语法坏与顶层类型坏同罪）与字段形状收敛（`_as_int` 等五件套，畸形字段一律「没有这个值」不抛异常）、原子写（tmp 名唯一，防同进程多线程互踩）与并发 80 次不丢更新、锁重入/stale 回收/持有者身份、轮转兜底判定矩阵、SSE/正则解析、PoW 稳定性、设备身份留存与多设备回退（含凭据剔除、429 停手、归属自愈），以及用离线桩跑通的投递主流程（含 413、空提示词、非法 id、发送异常时的孤儿会话登记）；出口代理（优先级、fail-closed、http/socks 真分流、死节点探测、出口判定、目标站探活、WebRTC 泄漏分类；坏端口＝配置非法在入口拒，探活对垃圾端口一律 False）；B8 六进程并发抢槽（抢不到锁即 QUEUED、半截槽位不回收）、B15 租约心跳与超时两档；交付正文托管（`--extract` 字节级、CRLF 不改、标记不唯一退 2、`--verify-copy` 单字节差异即拒）；调度口径（`--check-name` 12 条命名契约、`--check-schedule` 1 小时维度、`--check-oneshot` 一次性窗口）；证据矩阵自校验（条款与证据一一对应，引用不存在的用例即红）。**改锁、改落盘、改 id、改设备身份、改代理、改基线条款前先跑一遍。**

### 交付正文与排程口径的机器闸口（v1.8）

```bash
# 交付正文：按显式定位器取唯一块，字节级落盘；标记不唯一/顺序反/块为空 → exit 2
python scripts/chatgpt_send.py --extract <src.md> --between "<<<BEGIN>>>" "<<<END>>>" --out body.md
python scripts/chatgpt_send.py --extract <src.md> --fence 2      # 第 2 个围栏块（不按「最长」取）
python scripts/chatgpt_send.py --verify-copy body.md backfill.md  # 回灌前比对，不一致 exit 2

# 排程口径：建 automation 之前先过一遍，别用正则自己判
python scripts/chatgpt_send.py --check-name "配额 盘点（只读）" --argv "--rotation"
python scripts/chatgpt_send.py --check-schedule "FREQ=HOURLY;INTERVAL=1"
python scripts/chatgpt_send.py --check-oneshot 2026-11-19T16:00:00+08:00
```

三个 `--check-*` 都是纯本地、不触网：命名按空白（含全角空格）分词判段数、动作词整段比对、只读名 argv 必须在白名单内；排程只认 `FREQ=HOURLY;INTERVAL=1`；一次性窗口必须带时区且落在未来 1 小时内。**任何一条不成立都 `exit 2`，不允许「先建了再说」。**

## 统一出口代理（强制）

**一切出网动作共用一个出口**，不允许「API 走代理、浏览器裸连」这种半吊子状态。解析顺序：

1. `CHATGPT_PROXY`（环境变量，最高优先）
2. `scripts/proxy.json` 的 `url`（`--proxy-set` 写入，落盘 chmod 600，已 gitignore）
3. 系统 `https_proxy / http_proxy / all_proxy`
4. 本机常见端口探测 `7890 / 7897 / 10809 / 1080 / 8888` —— **必须真握手通过才采用**

| 动作 | 出口来源 |
|---|---|
| API 请求（`ChatGPTAPI.req`：建会话、发送、收割、list、probe） | `build_opener_for(resolve_proxy())`，支持 http/https/socks5/socks5h |
| 匿名浏览器（`AnonBrowser`） | 启动时带 `--proxy <url>`；已运行的浏览器不会被改造 |
| 自启动的 Chrome（`_autostart`） | 同上，与 API 同一出口，宁可起不来也不裸连 |
| git push（`tools/sync.sh`） | 调 `--proxy-url` 拿探活后的结果写进本仓库 `http.proxy` |

```bash
python scripts/chatgpt_send.py --proxy-show                 # 出口来自哪里、绕过了什么
python scripts/chatgpt_send.py --proxy-check                # 量出口 IP + 验目标站（A26），ok 需两者同时满足
python scripts/chatgpt_send.py --proxy-check --browser      # 再验浏览器侧出口是否一致
python scripts/chatgpt_send.py --proxy-set socks5h://u:p@1.2.3.4:1080
python scripts/chatgpt_send.py --proxy-clear                # 清掉 proxy.json 的 url
```

- **取不到出口就拒绝联网**（`SystemExit`，exit 3）。要直连必须显式 `CHATGPT_ALLOW_DIRECT=1` —— 直连是「明示的例外」，不是默认行为。
- **本机地址永不过代理**：`127.0.0.1 / localhost / ::1` 直连，保证 CDP（9333/9222）与本地闸口不受代理死活影响。
- **换了代理要重启浏览器**：Chrome 的 `--proxy-server` 只在启动时生效；`--proxy-check --browser` 会对比 API 侧与浏览器侧出口，不一致直接判失败。
- **socks 不是装饰**：urllib 的 `ProxyHandler` 只认 http 代理，`socks5h://` 塞进去会 `unknown url type`；这里用自定义 `http.client` 连接类做 RFC1928 握手（含用户名密码认证，`socks5h` = 远端 DNS）。

> 实测坑（2026-11-19）：本机 **7890 端口还在监听、CONNECT 也回 200，但隧道立刻断流**，TLS 一律 `UNEXPECTED_EOF`；同期 **7897 正常**。所以自动探测只「看端口通不通」会稳定选到死节点 —— 现在必须先握手成功才采用，`--proxy-check` 也会把两侧 IP 摆出来给你看。
>
> 更坑的一层（同日 13:37）：**IP 量得通、目标站回 403**。`curl api.ipify.org` 一切正常，业务请求全 403，页面写 `Unable to load site … If you are using a VPN, try turning it off`（出口被按 VPN 封）。所以 `--proxy-check` 现在多打一次 `https://chatgpt.com/`，输出 `site.verdict`：`ok` / `challenge`（CF 挑战，浏览器能过）/ `blocked_vpn`（换节点）/ `forbidden` / `unreachable` / `http_<code>`；只有 `ok|challenge` 才算通过。

## 故障速查

| 症状 | 根因 | 处置 |
|---|---|---|
| 403 "Unusual activity" | 先看 cookie 是否残缺，再看 PoW | 脚本会自动 `warm_cookies()` + 重跑 PoW；仍失败说明该身份被风控 |
| 403 且 cookie 只有 2 条 | 凭据只存了 session-token，缺 CF cookie | 访问首页补 `__cf_bm/_cfuvid/__cflb`（脚本已自动）或重新提取完整 cookie |
| 匿名建会话 403 | 纯 API 过不了 Turnstile | 必须走浏览器通道（`--temp` 有头浏览器 + CDP） |
| 匿名会话 404 `conversation_inaccessible` | 设备身份(oai-did)变了、快照丢失或会话被回收 | 先 `--anon-devices` 看留存设备，确认目标设备的快照是否还在；`--anon-read` 会自动逐个快照试，命中即修好归属；确实没有快照只能重建 |
| `--anon-list` 里某条 `device_known=false` | 该会话登记的设备没有本地快照（老版本建的、或 devices/ 被清过） | 有 jar 快照时 `--anon-read` 会试其他设备；否则无法读回，只能按归档 `anon-sessions/<id8>.md` 取内容 |
| 匿名会话续聊报「标签页已不存在」 | 匿名会话只在原标签页可写，重开必被重定向 | 用 `--anon-read <id>` 读全文归档；继续对话请用登录态账号 |
| 匿名续聊报「CDP 端口未运行」 | 浏览器没起，无法确认标签页（会话未必丢） | 先 `start_chrome.py --temp --port 9333` 再试；只读需求直接用 `--anon-read` |
| 匿名续聊报「暂时读不到（429/0/5xx）」 | 限速或网络抖动，**不是**会话失效 | 等几分钟重试；`--anon-read` 直接读 |
| 状态文件出现 `*.lock` 残留 | 进程被强杀，锁文件没清 | 超过 60 秒自动视为 stale 并回收；急用可手动删 `*.lock` |
| 出现 `conv-accounts.json.corrupt-<ts>` | 上次写入被强杀，json 只写了一半 | 损坏件已隔离，状态按空重建；需要旧数据就从隔离文件里捞（它是合法前缀时手工补 `}`） |
| 报 `conv_id 非法：'…'` | id 含 `/`、`..`、空格或超长 | 复制完整 id（形如 `11111111-2222-4333-8444-555555555555`），别手改 |
| 回复文件明显比预期短 | 设了 `CHATGPT_REPLY_MAX_CHARS` | 取消该变量即全文落盘；文件头有截断标注 |
| 匿名页点击发送无反应 | 窗口未显示 → `visibilityState=hidden` | 脚本已启用 `Emulation.setFocusEmulationEnabled`；仍无效则确认窗口可见 |
| 消息落入全新会话 | POST 缺 conversation_id | 补字段重发；核对 SSE 返回的 conversation_id |
| GET 429 | 上游配额紧张 | **按返回体/`Retry-After` 的真实时限退避**（不固定 90s）+ `conv-api/` 缓存 tail 兜底 parent_message_id |
| status 0 / `EXC SSL EOF` / 连接重置、超时 | **代理/网络问题**，不是上游限速 | 先修代理：`--proxy-check` 诊断、换端口/换出口、重启代理工具；修好立即重试，不计时死等；探测仍无出口要报"代理需人工跟进" |
| 403 Cloudflare HTML 质询页 | 出口 IP/指纹被拦（多为代理出口劣化） | 视同代理问题跟进修复（换出口）；或借已登录浏览器过质询后取 `cf_clearance`/走浏览器通道；单纯 sleep 无用 |
| 收割 DONE chars 远小于会话实际产出 | 收割器只截到部分（流式截断/轮询取短） | **大交付必用 raw mapping**（GET `/backend-api/conversation/<id>` 取全部 assistant parts 拼合）核对总长，勿凭 DONE chars 判截断要求重发 |
| `FAIL: no requirements token` | 先定性：代理故障（定性=proxy）→ 修代理；上游 sentinel 限速（定性=ratelimit）→ **按返回体时限退避**后重投 | 修代理或按返回体时限错峰重投；先单独探测 `chat-requirements` 返回 200 再发 |
| 私有仓库事实包 raw URL 404 | 未认证 raw.githubusercontent 对私有仓恒 404；分支名含 `/` 的 ref 也可能解析不到 | 事实包**内联字节原文**（Git Blob SHA + sha256 供自校验）或走已认证 GitHub 通道；规范类文件注意所在分支（如 AI-EXEC 只在 main） |
| 大事实包（约 100KB）入树后模型说"取不回全文" | 超长上下文被压缩后原文不可恢复，复验/裁决失去依据 | **复验包必须精简自包含**（≈4–5KB：delta 清单 + 变更段源码 + 全 SHA256/Blob SHA），让模型经 GitHub 连接器**按 SHA 自读仓**取全文；禁止把 100KB 全文塞进提示词再要求引用 |
| 复验方反复 HOLD/ACCEPT_WITH_AMENDMENTS 要求"独立核验" | 摘要包不含完整函数体/完整测试断言/完整 SHA256，文本复验方无法确认断言覆盖 | 给**完整代码段**（27KB 级：完整函数+完整测试方法体+完整 SHA256+原始测试输出）比 4-5KB 摘要更有效；实证 D 三轮才 PASS（摘要→补证→全文）、Storage 两轮。复验方要"独立核验"时给完整函数+完整测试体+完整 SHA256 即可推进 |
| 上下文压缩/续接后发现仓库出现自己没写的提交 | **上下文压缩不杀后台进程**：丢失段启动的协调驱动仍存活，与新代理并行写同一 repo | 恢复后**先盘点**（`git log`、文件 mtime）再动手；发现并行写者改为协作（审其产物、补缺口），不重做不回滚 |
| 构建脚本 git HEAD 报旧 SHA / 产物指向旧树 | 同工作区有双 repo：`workspace/repo`（可能滞留旧头）vs `workspace/state/repo`（真头） | 构建/取证脚本显式指对 repo 路径；每次开工先 `git log -1` 确认头与预期一致 |
| 413 conversation_too_large | 会话满员 | continue_full.py 摘要续接 |
| 流读取为空 | `with` 提前关连接 | 读到 EOF（chatgpt_send.py 已正确处理） |
| 输入框填入失败 | 用了 innerHTML 或裸赋值 | 文本域用原生 setter + InputEvent，或 CDP `Input.insertText` |
| 401 | 认证失效 | 重跑 extract_auth.py |
| Cloudflare「请稍候」 | 质询（无头模式必现） | 用有头浏览器；重启实例一次 |

## Resources

### scripts/

- `chatgpt_send.py` — 统一发送/收割器（P0）。`--extract/--verify-copy` 交付正文字节级托管（A1/A2/A4）；`--check-name/--check-schedule/--check-oneshot` 命名与排程口径（B1/B4/B10/B11）；`<conv_id> <prompt_file>` 投递既有会话；`--new <prompt_file>` 开 Owner 新会话（登录态均分，匿名兜底）；`--tail <conv_id>` 只收割；`--list/--list-all` 会话清单；`--rotation*` 轮转管理；`--owner <conv_id>` 查归属；`--anon-list/--anon-read/--anon-send/--anon-devices/--anon-relink/--anon-restore` 匿名会话对照表、按 id 续聊、设备身份留存与修复；`--proxy-show/--proxy-set/--proxy-check/--proxy-url` 统一出口的查看、写入、校验与给 shell 用的纯值输出。内含 `AnonBrowser`（匿名浏览器通道：同标签页多轮 + CDP 抓 jar + API 收割）、`probe_site`（A26 目标站探活）、`webrtc_probe`（A23 WebRTC 出口）、`acquire_slot/release_slot`（B8 原子领槽）、`lease_acquire/heartbeat/release/reclaim`（B15 实例租约）。
- `continue_full.py` — 413 满员续接器（P1，依赖 browser-automation-cdp 的 cdp.py）。
- `pow_solver.py` — PoW 解算器（FNV-1a + gAAAAAB 前缀，毫秒级），被 chatgpt_send.py 引用。
- `extract_auth.py` — 从 Edge CDP 提取 cookies/accessToken。

### 运行态文件（本机生成，勿删）

- `account-rotation.json` — 登录态均分游标、各身份建会话计数、失败计数、不可用标记及 TTL、匿名兜底次数。
- `conv-accounts.json` — `conv_id → 归属账号` 登记表，驱动自动路由。
- `anon-sessions.json` — 匿名会话 id 对照表（标签页、prompt、回复摘要、设备号、状态），是匿名会话的唯一留存索引。
- `anon-sessions/<conv_id8>.md` — 匿名会话全文归档（`--anon-read` 生成）；8 位前缀撞车时自动加 `-<后4位>` 后缀，不会互相覆盖。
- `*.lock` — 上面三个 json 的跨进程写锁（O_EXCL），正常用完即删；进程被强杀留下的锁 60 秒后自动失效。
- `proxy.json` — 出口代理配置（`{"url": "socks5h://..."}`，可带账号密码 → chmod 600 且不入库）。没有它时走上表第 3/4 档。
- `accounts/nologin/device-cookie.txt` — 匿名态最新 device cookie（`oai-did` 等），使 API 侧 `GET /backend-anon/conversation/<id>` 可持续收割。
- `accounts/nologin/devices/<oai-did>.txt` — 按设备分档的 Cookie 头快照；`--anon-read` 优先用它，保证浏览器换 profile 后旧会话仍可读。
- `accounts/nologin/devices/<oai-did>.json` — 同设备的完整 cookie jar（含 HttpOnly），`--anon-restore` 用它把身份回灌浏览器；已剔除 session token 一类凭据。

### references/

- `baselines.md` — **A/B 类基线（可判定版 v1.8，49 条）**：每条带判定口径与违例后果，附 C 类现成配方（每小时巡检、落盘看门狗、到期处置、配额哨兵）、D 类事故→条款映射、E 类变更纪律、G 类证据矩阵说明。**开工前扫 A0/B0 清单，排程前读 B1–B15，改代码前读对应条款。**
- `evidence-matrix.md` — **逐条对账「这条基线由什么证明」**：`test:` 用例名 / `code:` 符号 / `live:` 实测日期 / `human:` 人判理由，四类分开标注；33 条有离线用例、3 条只有代码、13 条纯人判，缺口按危害排序列在文末。改基线必须同步这张表——`TestEvidenceMatrix` 会校验引用是否真实存在。
- `api-protocol.md` — 实测协议细节：端点、PoW 算法、POST 请求体全字段、SSE 解析、mapping tail 计算、状态码处置、限速纪律。改脚本前先读。
- `INCIDENT-2026-11-20-CONVERSATION-REGISTRY-LOSS-AND-RECOVERY.md` — **事故记录**：旧 500 条裁剪器删掉 387 条归属记录 → 并集最小动作恢复 904。含时间切面、并发写入者、证据 SHA-256 清单、`restore_conv_map_minimal()` vs `register_conversations_batch()` 的职责边界，以及「不得宣称四账号已收敛 / 不得宣称批量登记已生产验证」的禁止结论。

### tests/

- `test_hardening.py` — 离线回归（308 条）：id 校验/路径穿越、状态文件损坏隔离、原子写、锁重入与 stale、并发不丢更新、轮转兜底判定、SSE 解析、PoW、设备身份留存与多设备回退、出口代理（含本地假 http/socks 代理真分流）。

## 协议细节

PoW 算法、POST 请求体全字段、SSE 解析、mapping tail 计算等实现级细节见 `references/api-protocol.md`。脚本内部有注释标注血泪条款（conversation_id 必填、勿提前关连接等），修改前先读。

## 代码仓库

代码与协议文档已仓库化：<https://github.com/oahcom/chatgpt-session-ops>（公开）。仓库只含代码与文档，凭据、`replies/`、状态 json 全部由 `.gitignore` 排除；同步时只拷 `SKILL.md`、`references/`、`scripts/*.py`，勿带本机运行态。
