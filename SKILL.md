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
| **A0** | 开工检查清单 | A0 | 30 秒五道卡口：出口通不通（`--proxy-show` + GET `chatgpt.com`）、会话归谁（`--owner`）、配额有无冷却（`--rotation`）、匿名设备 `jar=true`（`--anon-devices`）、改代码后回归全绿。**开工前跑，任一项不过不投递** |
| **B0** | 建/改 automation 前的六问 | B0 | 1 小时维度够不够、RUNNING 会不会 >3、跑一半被掐会怎样、无事会不会刷屏、到期谁归档、同账号 GET 会不会撞。**建任务前逐项过一遍，不通过就改设计** |
| **A1–A4** | 内容主权 | A1–A4 | 逻辑规划/实现/提示词全部由 ChatGPT 主导自产、逐字节搬运、裁决必投全新会话 |
| **A5–A9** | 通道与身份 | A5–A9 | 登录态优先、401/403 不兜底、匿名只认设备身份、可写窗口=原标签页 |
| **A10–A14** | 投递与收割 | A10–A14 | 必带 `conversation_id`、200 即停轮转、大交付对 raw mapping、全文落盘、先落设备 |
| **A15–A18** | 限速与失败 | A15–A18 | 单 GET 进程、会话间 ≥60s、**`Retry-After` 是下限（不得向下截 180s）**、401/403/429 分级 |
| **A19–A22** | 存储与安全 | A19–A22 | id 白名单、原子写、损坏隔离、锁按线程判重入 + **回收前校验 PID 启动时间** |
| **A23–A26** | 出口 | A23–A26 | 单一出口、fail-closed(exit 3)、探活真握手、探活要验目标站、**WebRTC 不许漏真实 IP** |
| **A27–A28** | 思考模式 / 流截断 | A27–A28 | 要思考就必须真开（解析不出 `-t-` 抛 `ThinkingModelUnavailable`：默认轮转换账号、全废才 `exit 1`，显式 `--account` 直接失败；无降级口子）；**`IncompleteRead` ≠ 失败**，保留半截 + GET 补全文 |
| **A29** | Cookie 落盘边界 | A29 | `cookies.json` 里每条 cookie 的 `domain` 必须**精确等于或后缀匹配** `chatgpt.com`/`openai.com`（禁止字符串包含）；`warm_cookies()` 回写前对内存与磁盘同步过滤；`extract_auth.py` 导入同语义。请求头过滤只管「发不发得出去」，管不住「存不存得下来」 |
| **B1–B5** | 排程 | B1–B5 | 1 小时维度、条件巡检、validFrom/Until 收窗、看门狗 ≤1h、过期即 PAUSED |
| **B6–B9** | 执行可靠性 | B6–B9 | prompt 自包含（含路径**类型**校验）、task/step 状态机幂等续跑、**RUNNING ≤3 且原子领槽**、删前先 PAUSED |
| **B10–B15** | 行为契约 | B10–B15（+ B15-R） | 命名正则即契约、argv 白名单只读、无事静默、重试上限 N=3、不与 A 类打架、**实例按 PID+启动时间+run_id 回收**；**B15-R**：损坏租约**永不自动回收**，只能走 `lease_force_release()` 人工批准通道（六项授权 + 隔离归档 + 留痕，R1–R8 见 baselines） |

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

账号凭据隔离：acct1 复用既有默认目录（`CHATGPT_AUTH_DIR` 或脚本同目录），acct2 / acct3 / acct4 / acct5 / acct6 使用 `scripts/accounts/acct2/`、`scripts/accounts/acct3/`、`scripts/accounts/acct4/`、`scripts/accounts/acct5/`、`scripts/accounts/acct6/`，各自读取 `cookies.json` 与 `access_token.txt`。**凭据属个人数据，不随仓库分发**：按 `scripts/accounts/README.md` 的结构自行放入即可，acct2 / acct3 / acct4 / acct5 / acct6 装好即与 acct1 等价参与轮转。

**身份优先级：登录态优先，匿名只做兜底。** `--new` 默认在 `acct1 / acct2 / acct3 / acct4 / acct5 / acct6` 之间均分轮转；`nologin`（匿名）**仅当全部登录态都 429**（本次命中或处于 429 冷却）时才启用，401/403 不触发兜底。`CHATGPT_ANON_FALLBACK=0` 可彻底禁用。需要匿名时也可显式 `--account nologin --new`。

**身份状态变更记录（历史不倒签）：**

| 身份 | 日期 | 事件 | 结论 |
|---|---|---|---|
| `acct5` | 2026-11-21 15:39 | 探测 401 `token_revoked` | 凭据被服务端吊销，不落地、不参与轮转 |
| `acct5` | 2026-11-21 18:28 | 新凭据重新验证通过 | **已恢复，作为第六身份加入轮转**（DECISION-154） |

`acct5` 第一次探测的 401 `token_revoked` **不是**本地代码缺陷、**不是**网络故障、**不是**暂时性思考模型不可用——是该账号在导出后被新登录覆盖了旧 token，属可恢复状态。吊销期间凭据故意不写入 `accounts/acct5/`，也不创建 `.disabled` 标记（空目录会制造「已配置」的假象）。

**恢复判定必须以凭据内容为准，不得按文件名。** 恢复时提供的源文件名为 `acct6-export (1).json`，但其中 `sub` 为 `auth0|Z2FcYnmK629mFbuXSAVsKttc`、邮箱 `2651259457@qq.com`，与 acct5 旧凭据（`auth0|Z2FcYnmK629mFb`）同源，而 acct6 的真实身份是 `auth0|E5AnMUe6YmIaabJ1` / `oachom0831@proton.me`。**若按文件名当 acct6 覆盖安装，会挤掉正常账号并净损失一个身份。** 启用评估三项判据：`/backend-api/me` 200、`/backend-api/models` 200、13 个模型含 `-t-` 变体——全部满足方可恢复。

**当前六个登录态真实身份（2026-11-21 实测）：**

| 身份 | 邮箱 | 认证方式 |
|---|---|---|
| `acct1` | 1491856051@qq.com | google-oauth2 |
| `acct2` | oahcom0831@gmail.com | auth0 |
| `acct3` | 2293780783@qq.com | google-oauth2 |
| `acct4` | oahcom@protonmail.com | auth0 |
| `acct5` | 2651259457@qq.com | auth0 |
| `acct6` | oachom0831@proton.me | auth0 |

`acct1` 与 `acct3` 走 `google-oauth2`，刷新凭据的路径与其他四个（auth0）不同，需走 Google 授权而非邮箱密码登录。其余四个是 auth0。

**429 限流的处置（用户裁定：换账号，或直接用匿名渠道）。** 四条硬规则：

1. **显式指定主身份也照样换号。** `--account acct1 --new` 遇 429 时，先给 acct1 记 429 冷却，再转轮转（换 acct2 / acct3 / acct4 / acct5 / acct6；全部 429 则走匿名）。理由：429 时会话**尚未建成**，重试不会重复建会话、不会多花配额——显式身份只约束「用谁起」，不约束「限速后还能干什么」。
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
6. 三个 json 的读-改-写走**跨进程文件锁**（`*.lock` + 持有者死活判定 + 进程内重入），并发建会话不再互相覆盖计数/游标。
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

因此：**匿名通道适合一次性、短周期、即问即取的任务（如 Owner 裁决）以及全部登录态都 429 时的应急产能**；需要长期留存、归档、跨机续接的会话仍用 acct1/acct2/acct3/acct4/acct5/acct6。标签页丢失时脚本显式报错并指向 `--anon-read`，不会静默新建。

**nologin 走浏览器通道，不是纯 API。** 匿名态建/发必须过 Cloudflare Turnstile：纯 API 的 `POST /backend-anon/conversation` 恒 403 `Unusual activity`（`chat-requirements` 返回 `turnstile.required=true` + `force_login=true`）。实测可行路径是 **UI 建会话 + API 收割**：

- 用临时 profile 启动调试浏览器：`python scripts/start_chrome.py --temp --port 9333`（在 browser-automation-cdp 技能目录下；脚本在 CDP 未就绪时会自动拉起）
- 脚本经 CDP 在未登录页面建会话（落点 `https://chatgpt.com/uc/<id>`），再用页面 cookie 走 `GET /backend-anon/conversation/<id>` 收割
- 两个隐坑：窗口未显示时页面 `visibilityState=hidden`，应用会挂起发送 → 脚本已启用 `Emulation.setFocusEmulationEnabled`；无头模式会被 Cloudflare「请稍候」挡住 → 必须用有头浏览器
- 端口用 `CHATGPT_ANON_CDP_PORT` 覆盖（默认 9333）；device cookie 落盘 `scripts/accounts/nologin/device-cookie.txt` 与分档快照 `accounts/nologin/devices/<oai-did>.{txt,json}`（扫掉 `devices/` 就永久失去那些设备上的历史匿名会话）
- `start_chrome.py --temp` 的 profile 其实是**固定目录** `%TEMP%/cdp-browser-temp`：重启浏览器但不清目录时 `oai-did` 不变，设备身份通常能延续；只有清目录/换机才真的换设备。这也说明本地设备快照才是可靠兜底

**凭据不全的身份会在 POST 时 403**：acct2 的 cookies.json 只有 `session-token` + `oai-did`（2 条），GET 全部正常但 POST 判 `Unusual activity`——缺 `__cf_bm`/`_cfuvid`/`__cflb`。脚本在首次 403 时自动 `warm_cookies()`（访问首页接收 Set-Cookie 并回写 cookies.json）再重试一次，实测可自愈。

提取流程：启动已配置 CDP 的 Edge（默认端口 9222，`CHATGPT_CDP_PORT` 可改），用户本人登录 ChatGPT 后运行 `python scripts/extract_auth.py`，一次写出 `cookies.json`（仅 chatgpt.com / openai.com 域，含 HttpOnly）与 `access_token.txt`（来自 `/api/auth/session`），两者都 chmod 600。脚本只处理当前浏览器已登录的默认账号；acct2 / acct3 / acct4 / acct5 / acct6 凭据通过用户已登录的 InPrivate 窗口经 DevConsole 提取——在该窗口控制台同源 `fetch('/api/auth/session')` 取 accessToken、`document.cookie` 取可读 cookie，POST 到本机回环接收服务（或下载 JSON 再由本技能读取），落盘到 `accounts/<acct>/`（注意导出文件名要带对应账号编号，避免覆盖）。重新提取时仍需用户本人在浏览器完成登录，不要在聊天中发送或粘贴 cookie、access token。实测（2026-11-20，acct3/acct4）：浏览器 CSP/PNA 会把发往 `127.0.0.1` 的 `fetch` 拦下，兜底走「下载 `<acct>-export.json`」再读文件即可；导出的 cookie 缺 CF 系列，首轮 `warm_cookies()` 会补 `__cflb`/`__cf_bm`/`_cfuvid` 后 POST 恢复正常。

## 核心工作流

### 1. 投递到既有会话

```bash
python scripts/chatgpt_send.py <conv_id> <prompt_file>
python scripts/chatgpt_send.py --account acct2 <conv_id> <prompt_file>
```

#### 会话归属与路由

每个会话只属于创建它或明确授权访问它的账号。未显式指定 `--account` 时，脚本必须根据 `conv-accounts.json` 中经过验证的登记归属自动路由；登记缺失、记录无效或归属矛盾时必须 fail closed，禁止隐式回落到 `acct1`、自动探测其他账号或继续访问，并要求用户明确提供或确认归属。显式指定 `--account` 时，必须验证其与已登记归属一致；不一致时以退出码 `2` 拒绝执行，不得仅打印 WARN 后继续。`default` 与 `acct1` 按既有配置定义作为同一 credential root 的别名处理。匿名态只用于明确公开、且服务端允许匿名访问的内容；不用于访问或探测私有会话。

各账号限速彼此独立。429 时对原身份退避至少 90 秒，GET 可用该身份缓存；不要并发运行同一账号请求。

查询会话清单：`python scripts/chatgpt_send.py --account acct1 --list` 查询单一账号；`python scripts/chatgpt_send.py --list-all` 顺序查询本机已配置的**六个登录态**（`acct1`–`acct6`，即 `ROTATION_PRIMARY`），并为每条结果标注身份。**默认不含匿名态**——匿名态只在显式加 `--include-nologin` 时才纳入枚举。`--list-all` 输出 `by_account`（按账号分组，便于归档）、`summary`（各账号计数）、`quota`（各账号最近响应的限速观测头，只读）与 `errors`。不可用的账号和接口错误会进入 errors；遇到 429 即停止剩余扫描。聚合结果只覆盖各身份自身有权看到的会话，不代表匿名态能读取私有会话。

`--list-all` 默认 **dry-run（只读）**，加 `--apply` 才把「唯一观察、枚举完整、无冲突」的候选登记进 `conv-accounts.json`；`--include-nologin` 显式把匿名态纳入枚举。写入一律走 `record_conv()`，写完在同一把锁下回读磁盘填 `registered`。未知或重复 flag 直接 exit 2，不触网。

##### 会话日志（提示词 + 回复全量留存，按会话拆分，可检索）

**每一次经本插件发出的提示词与收割到的回复正文都会被留存**，以 `conv_id` 为维度拆分。目的是「事后能完整复原当时发了什么、回了什么」：`prompts/` 会被新编号覆盖，`replies/` 是扁平命名、要按会话归集得靠猜。

**覆盖范围（R373-01）**：P0 API 通道（`send_prompt`：成功 / 落点不一致隔离 / 抛异常）、匿名通道（`send_anon`、`--anon-send`）、**P1 CDP 通道（`continue_full.py`）**。任何新增投递入口都必须接入同一套日志协议——漏一个入口，"全量留存"就不成立。

**两阶段落盘（R373-02）**：

| 阶段 | 时机 | 内容 |
|---|---|---|
| `begin` | **发送之前** | `turn_id` + 提示词全文。**这一步失败就拒绝发送**——内容已被使用却无记录，是不可补救的数据丢失 |
| `end` | 发送之后 | 回复全文 + `reply_state` + `status` + `error` |

两行用 `turn_id` 关联，读取时合并成一"轮"。**只有 `begin` 没有 `end` = 该轮未完成**，`reply_state` 保持 `unrecorded`、`status` 为 `null`——这是可查询的可见状态，不会被当成"看起来成功"。

**目录（三桶，不合并）**：

| 目录 | 含义 |
|---|---|
| `logs/conversations/<conv_id>.jsonl` | 已确认 `conv_id` 的会话 |
| `logs/pending/` | 新会话尚未取得真实 id 的暂存轮次；拿到 id 后整轮迁移并删暂存 |
| `logs/unassigned/` | 身份不可用且无法归属：**按原始标识分文件**，绝不合并成一个桶 |

**`reply_state` 语义（R373-05）**：`captured`（完整正文）/ `empty`（200 但明确空回复）/ `partial`（收割不完整，不得冒充完整）/ `unavailable`（无回复：413、非 200、异常）/ `quarantined`（落点不一致但正文已保全）/ `unrecorded`（只有 begin）。

**`reply_source`（R374-01）**：`api`（SSE/mapping 收割）或 `dom`（P1 CDP 页面文本）。两者证据强度不同，检索时应可辨识。**P1 的 DOM 证据必须先在发送前取 assistant 消息条数作基线，只认新增的那条**——上一轮的回复会留在 DOM 里，本轮发送失败时最后一条仍是旧回复，直接取会把它记成本轮回复（内容真实但归属错误）。仍在流式生成记 `partial`，没有新增记 `unavailable`。

| 命令 | 作用 |
|---|---|
| `--log-list` | 三桶文件数、每份日志的轮次与 `reply_states`、`incomplete` 列表 |
| `--log-show <conv_id>` | 读某会话全部交互（提示词与回复全文，按时间正序） |
| `--log-search <关键词> [--conv <id>] [--limit N] [--json]` | 全库或按会话检索（大小写不敏感，匹配提示词或回复正文） |
| `--log-list --pending` | 只列 pending（新会话尚未确认 conv_id 的暂存轮次） |
| `--log-list --unassigned` | 只列 unassigned（身份不可用且无法归属，按原始标识分文件） |
| `--log-list --unresolved` | 同时列 pending 与 unassigned——人工处置入口。**与 `--pending`/`--unassigned` 同给不会报错**：分派顺序是 `--pending` → `--unassigned` → `--unresolved`，先命中的那一个生效，另一桶静默不列（实测 `--unresolved --pending` 退出码 0，只出 pending）。唯一被代码拒绝的组合是 `--pending` + `--unassigned`（exit 2）。要两桶就用 `--unresolved` 单给 |

轮次形状：`turn_id / ts / conv_id / target_conv / account / prompt_file / prompt(全文) / prompt_chars / prompt_sha256 / reply(全文) / reply_chars / reply_sha256 / reply_state / reply_file / status / error`。

纪律：
- **提示词必须先落盘再发送**；回复补写失败无法撤销已发生的交互，因此不阻断返回，但该轮必须表现为未完成（`unrecorded`），并打 `FAIL`。
- **正文必须是发送那一刻的快照**：一次读取、整轮复用，绝不在发送后重读文件补日志（文件可能已被改写或删除）。
- **同会话追加按文件加锁**（进程内 + 跨进程）；不同会话互不阻塞。
- **`fsync` 失败必须抛**，不得把持久化未确认伪装成成功。
- **日志含私密对话内容，与 `replies/` 同等级，`logs/` 已由 `.gitignore` 排除，绝不入库**；文件权限收紧为仅本用户可读写。
- 坏行（半行/非法 JSON）读取时跳过，不影响其余记录。
- 旧版单行记录（无 `phase`/`turn_id`）仍可读，`reply_state` 按内容推导，历史成功轮次不会被误报。

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
python scripts/chatgpt_send.py --new <prompt_file>                  # 默认：acct1/acct2/acct3/acct4/acct5/acct6 均分
python scripts/chatgpt_send.py --account acct2 --new <prompt_file>  # 指定身份
python scripts/chatgpt_send.py --account auto --new <prompt_file>   # 自动挑（仅 --new 分支支持）
python scripts/chatgpt_send.py --rotation                           # 游标/计数/兜底次数
python scripts/chatgpt_send.py --rotation-enable acct2              # 解封某身份
python scripts/chatgpt_send.py --rotation-disable acct2             # 手动封某身份（不触网）
python scripts/chatgpt_send.py --rotation-reset                     # 清零游标与全部计数（写操作，见下）
python scripts/chatgpt_send.py --owner <conv_id>                    # 查会话归属账号
```

**`--rotation-enable` / `--rotation-disable`**（一对，纯本地状态，不触网）：

| flag | 改什么 | 说明 |
|---|---|---|
| `--rotation-enable <name>` | 从 `unavailable` 移除该身份，`failures` 清零 | 解封。`<name>` 必须在 `ROTATION_ORDER` 内（`acct1`–`acct6` / `nologin`），否则退 2 |
| `--rotation-disable <name>` | 标记 `manually disabled` 并计入 `unavailable` | 手动封。`reason` 写死为 `manually disabled`，与 401/429 的自动冷却区分开——`anon_fallback_block_reason()` 会单独识别这个字符串 |

两者都立即打印完整 `rotation_summary()`。**`--rotation-enable` 不能解除「凭据真的失效」**：它只清本地标记，401 的病因在服务端（A18）。

**`--rotation-reset`**（写操作，不可逆）：

```python
s["cursor"] = 0
s["fallback_uses"] = 0
s["created"] = {a: 0 for a in s["order"]}
s["failures"] = {a: 0 for a in s["order"]}
s.pop("last", None)
```

- **清什么**：主游标、匿名兜底计数、各身份建会话计数、各身份失败计数、最近一次记录。
- **不清什么**：`unavailable`（不可用标记与 TTL **原样保留**）。所以「想解封」要用 `--rotation-enable`，`--rotation-reset` 解不了封。
- **什么时候用**：均分严重偏斜（某身份计数远超其他）且确认不是失败累积导致时；或测试后恢复干净起点。
- **风险**：计数清零后 `share` 观测值归零重算，历史配额分布不可追溯。**不要在排查 429 期间用**——会把失败证据一起抹掉。
- **与 `--rotation-disable` 的区别**：`reset` 动的是**统计量**，`disable` 动的是**可用性**。两者正交，别混用。

**`--account auto` 的边界**：`auto` **不是**合法账号身份（不在 `_VALID_ACCOUNT_IDENTITIES`），只在 `--new` 分支被特殊处理为「走默认轮转」。所以：

- ✅ `--account auto --new <file>` —— 等同不指定 `--account`
- ❌ `--account auto --list` / `--account auto <conv_id>` —— 走 `validate_account_identity` 校验失败，**退 2**

用法文案里列了 `auto` 是为了 `--new` 的可读性，不代表它是通用身份。

轮转规则：`acct1 → acct2 → acct3 → acct4 → acct5 → acct6 → acct1 …` 均分（游标持久化在 `scripts/account-rotation.json`，只在登录态之间推进）。匿名仅在全部登录态都 429 时兜底，且创建后打印 `WARN` 提示不入历史。每次成功都把 `conv_id → 账号` 登记到 `scripts/conv-accounts.json`；之后投递/收割该会话、未显式指定 `--account` 时自动路由。

轮转容错：

| 情况 | 处置 |
|---|---|
| 401 | 该身份认证失效，封 6h（`--rotation-enable` 可解封）；**不触发匿名兜底** |
| 403 | 先补 cookie + 重跑 PoW 重试一次；仍失败冷却 15 分钟，不长期封禁 |
| 429 | 限速，冷却 180 秒；**显式主身份也照样换号**（换另一个登录态，都 429 则走匿名兜底）；既有会话只能按返回体时限退避（归属固定，换号/匿名都读不到） |
| 匿名浏览器通道不可用 | 冷却 30 分钟 |

**失败必须先分类，再下结论（DECISION-159 要求）**。同一个「失败了」背后是四种完全不同的病因，处置方式互斥——混为一谈会导致「网络抖一下就说账号到期了」这种误判：

| 症状 | 病因 | 正确结论 | 错误结论 |
|---|---|---|---|
| 连接超时 / `ProxyError` / `Tunnel connection failed` / DNS 失败 | 网络或代理故障 | 「网络不通，非账号问题」，先跑 `--proxy-check` 确认出口 | ❌ 账号到期 / 凭据失效 |
| HTTP 401 / `token_revoked` / `invalid_grant` | 认证失效 | 「需本人重新登录该账号并重新导出凭据」 | ❌ 网络问题，重试就好 |
| HTTP 403 `Unusual activity` / 429 | 风控或限流 | 「风控/限流，按冷却处理」，不要催用户重新登录 | ❌ 账号到期 |
| 5xx 等其余状态码 | 服务端异常 | 如实报状态码，**不下「到期」结论** | ❌ 自行脑补原因 |

**绝不把一次网络故障报告为账号到期**——这会让人白跑一趟重新登录，而真正的病因（代理挂了）还在。

**「账号能不能用」必须用写路径判，不能用读路径判**（2026-11-22 实测）。`--list` 只走会话列表 GET，`status:200` 只证明「token 还能读」；它读不出「能不能发」。完整判据分三层，逐层加码：

| 层 | 命令 | 通过说明什么 | 不通过说明什么 |
|---|---|---|---|
| L0 凭据存在 | 解 `access_token.txt` 的 JWT `exp` | 未过期，且有 `email` 可核对身份 | 过期 → 需本人重新登录（**唯一**可下「到期」结论的情形） |
| L1 读路径 | `--account <acct> --list` | token 被服务端接受，会话列表可拉 | 401 → 认证失效；403/429 → 风控；超时 → 网络 |
| L2 写路径 | `--account <acct> --new <probe.txt>` | **完整链路可用**：PoW、发送、流式响应、落盘 | 才叫「不可用」 |

L2 的探针文件内容写成 `Reply with exactly: PROBE_OK`，回复正文（`replies/reply-*.md` 去掉首行注释）应恰好是 `PROBE_OK`——**收到非空且符合预期的正文**才算通过，`status:200` 本身不算。单账号 L2 约 5–8 秒，六账号串行约 45 秒；L1 单账号 30–45 秒，六账号约 90 秒（都要后台跑，前台 120s 会截断）。

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
5. **匿名纪律**：匿名只做限速兜底与一次性短任务；**需要留存、归档、跨机续接的会话必须用 acct1/acct2/acct3/acct4/acct5/acct6**。匿名会话丢设备 cookie 即永久失联，且从不进历史。
6. **落盘约定**：`prompts/`（编号前缀 NN-topic.md）、`replies/`（reply-<conv_id8>-<topic>.md 全文不截断）、`conv-api/`（GET 缓存）、`newchat-map.json`、`sessions-registry.json`、`send-queue-log.md`、`logs/conversations/<conv_id>.jsonl`（**会话日志**）：每轮由两条记录组成，`begin` 记录提示词，`end` 记录回复全文；读取时按 `turn_id` 合并为完整轮次（见下条）。
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
| Cookie 持久化边界 | `cookies.json` **只允许存 `chatgpt.com` / `openai.com` 域**（含子域）。`_cookie_domain_allowed()` 走精确域名或后缀匹配，**不做字符串包含**；**四个入口全部走同一判定**：`ChatGPTAPI.__init__` 拼请求头、`warm_cookies()` 回写与重建头、`AnonBrowser` 的 `Storage.getCookies` 导入、`extract_auth.py` 导出 | 请求头过滤只管「能不能发出去」，管不住「能不能存下来」。实测 acct1 的 `cookies.json` 混进 50 条 Google 会话（`accounts.google.com` / `.google.com` / `.youtube.com` / `.google.co.jp` / `.google.com.hk`），`warm_cookies()` 每次补 CF cookie 都把整包回写磁盘——等于把全站登录态反复备份。字符串包含判定更糟：`notchatgpt.com`、`chatgpt.com.evil.io` 的 cookie 会**被真的发进 chatgpt.com 的请求头**（变异验证实测 `evil=x; evil2=y` 出现在出站头里）。**品牌归属 ≠ 凭据授权**：`.oaistatic.com` 是 OpenAI 自己的域，但不在白名单（不参与认证，且 `chatgpt.com` 域已有同名 `_cfuvid`）——扩白名单必须有功能需求和验证证据 |
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

环境变量：`CHATGPT_PROXY`（出口代理，最高优先级；`CHATGPT_PROXY_URL` 是它的等价别名，两者同时设时 `CHATGPT_PROXY` 先读）、`CHATGPT_ALLOW_DIRECT=1`（显式放行直连，仅调试）、
`CHATGPT_REPLY_MAX_CHARS`（默认 0=全文）、`CHATGPT_ANON_IDLE_SEC`（默认 900）、
`CHATGPT_CDP_PORT`（`continue_full.py` / `extract_auth.py` 用，默认 9222）、`CHATGPT_ANON_CDP_PORT`（匿名通道，默认 9333）、
`CHATGPT_THINKING=1`（等价 `--thinking`，新会话走 `-t-` 思考模型）、`CHATGPT_NO_THINKING=1`（浏览器侧显式不点「思考」）。

`CHATGPT_MODEL`（指定思考模型 slug）**只在 `--thinking` / `CHATGPT_THINKING=1` 生效时被读取**，且必须是含 `-t-` 的 slug，否则抛 `ThinkingModelUnavailable`。它不是「覆盖一切的最高优先级」：不开启思考时设它完全无效果，也无法用它指定非思考模型。

回归测试（离线，不触网、不需要凭据）：

```bash
python -m unittest tests.test_hardening -v
```

`tests/test_hardening.py` 466 条，覆盖 id 校验与路径穿越、状态文件损坏隔离（语法坏与顶层类型坏同罪）与字段形状收敛（`_as_int` 等五件套，畸形字段一律「没有这个值」不抛异常）、原子写（tmp 名唯一，防同进程多线程互踩）与并发 80 次不丢更新、锁重入/stale 回收/持有者身份、轮转兜底判定矩阵、SSE/正则解析、PoW 稳定性、设备身份留存与多设备回退（含凭据剔除、429 停手、归属自愈），以及用离线桩跑通的投递主流程（含 413、空提示词、非法 id、发送异常时的孤儿会话登记）；出口代理（优先级、fail-closed、http/socks 真分流、死节点探测、出口判定、目标站探活、WebRTC 泄漏分类；坏端口＝配置非法在入口拒，探活对垃圾端口一律 False）；B8 六进程并发抢槽（抢不到锁即 QUEUED、半截槽位不回收）、B15 租约心跳与超时两档；交付正文托管（`--extract` 字节级、CRLF 不改、标记不唯一退 2、`--verify-copy` 单字节差异即拒）；调度口径（`--check-name` 12 条命名契约、`--check-schedule` 1 小时维度、`--check-oneshot` 一次性窗口）；证据矩阵自校验（条款与证据一一对应，引用不存在的用例即红）。**改锁、改落盘、改 id、改设备身份、改代理、改基线条款前先跑一遍。**

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

## MCP 连接器资格管理

`scripts/mcp_connector.py`：检查任意 MCP 端点，并判定某个 ChatGPT 身份有没有资格为它创建自定义连接器。

```bash
python scripts/mcp_connector.py inspect --url https://mcp.example.com/mcp
python scripts/mcp_connector.py eligibility --account acct1
python scripts/mcp_connector.py eligibility --accounts acct1,acct2,acct3,acct4,acct5,acct6
python scripts/mcp_connector.py eligibility --account acct1 --capability CUSTOM_MCP_READ_FETCH
```

`--capability` 的取值只有 `CUSTOM_MCP_FULL`（默认）与 `CUSTOM_MCP_READ_FETCH` 两个；给其他值 argparse 直接拒。

`inspect` 做四层检查：HTTPS 与 SSRF 防护（解析 DNS 后拒绝私网/回环/链路本地）、端点可达性与 401 探测、`resource_metadata` 与授权服务器元数据、匿名端点才做 `initialize` + `tools/list`。OAuth 端点会明确输出 `protocol handshake skipped`，不冒充客户端替用户完成授权。

`eligibility` 读 `/backend-api/me` 与 `/backend-api/accounts/check/v4-2023-04-27`，按套餐判定：`business` / `enterprise` / `edu` 合格；`pro` 只有 `CUSTOM_MCP_READ_FETCH`；其余为 `PERMISSION_DENIED` + `PLAN_NOT_ELIGIBLE`。

**连接器注册（用户裁定 2026-11-23，已更正）：**

- **已废止**此前「Free 套餐不支持 MCP 连接器 / 六账号 OUT-OF-SCOPE / 批量验收目标为权限不足」的裁定——该判定错误。
- 实测（2026-11-23）：access_token 可正常驱动 `/backend-api/aip/connectors/*`（`list_accessible` 返回 422 缺参数而非 403，`mfa_requirement` 返回 200 `not_required`），账号可对既有连接器走 OAuth 完成连接。
- 注册路径：`links/oauth` **只对账号自己拥有的 connector 生效**。拿别人的私有 connector（如 acct1 的 `asdk_app_6b02f952…`，`status=ONLY_ME`）去 link，任何账号都返回 `403 Unauthorized`；这跟 session 无关（acct2 用 token+cookie+PoW 对自己的 connector 直接 200）。**403 的正确解读是「无权访问该 connector」，不是「缺 session」。**
- `links/oauth` 需要 cookie + sentinel（PoW）。纯 Bearer 不带 PoW 会 403。直接用 `ChatGPTAPI.req()` 并附 `OpenAI-Sentinel-Chat-Requirements-Token` / `-Proof-Token` 即可。完整链路是四步：`links/oauth` 拿 authorize URL → Keycloak 登录 → 把带 `code` 的最终回调 URL 提交给 `links/oauth/callback` → `list_accessible` 校验。
- `list_accessible` 的 body 必须是 `{"principals": []}`；传 `[{"type":"USER","id":…}]` 会 422（枚举只认大写 USER/WORKSPACE/ORG/PROJECT/ENT_TENANT）。
- **创建 connector 的请求契约（2026-11-23 实测，单一部署）**：`POST /aip/connectors/mcp` 只接受 `mcp_url`（不是 `base_url`）。`auth_request` **不是** `oauth_config` 的平铺对象，而是 `{"supported_auth": [<oauth_config>]}`，body 还必须带 `"logo_url": null`。平铺形式照样返回 200，但 `supported_auth` 会退化成 `[{"type":"NONE"}]`。详见 `references/mcp-oauth-connector-contract.md`。
- **`type` 由服务端匿名探测决定**（2026-11-23 实测；不是对所有后端版本的承诺）。`openai-mcp/1.0.0` 匿名 `POST {mcp_url}`：200 → `NONE`；401 且能完成 OAuth 元数据发现 → `OAUTH`；401 但发现失败 → `424 mcp_error upstream_status=401`。这三条是已验证路径，不是穷尽状态机。实测中单独把 `auth_request.type` 写成 `API_KEY` / `BEARER` / `CUSTOM` / `NONE` 都没有改变最终判定——但别推广成「`auth_request` 无意义」，`supported_auth` 的嵌套结构直接决定 OAuth 配置能否被识别。
- 同一 UA 的 401 也解释了 `MCP_ACTION_DISCOVERY_FAILED`（2026-11-22 `openai-mcp/1.0.0` 拉 tools 收到 401，nginx access.log 原文可查）。**actions 为空不是认证没走通**，是 ChatGPT 那次发现被 401 拒了之后不再重拉。
- 判定认证是否真的通，方法是走完整 PKCE 拿真 token → `initialize` → `tools/list`，且必须用 Chromium 的请求上下文（`p.request.new_context`）——urllib/curl 会被 Cloudflare 拦成 `1010 Access denied`（浏览器签名），那是探测工具的假阴性，不是服务端拒绝。**执行该探测需自备工具**：曾经用的 `probe_mcp_cf.py` 属于本机诊断脚本，`#88` 已裁定**不发布、不随仓库提供**（见 `docs/UNTRACKED-SCRIPTS-INVENTORY.md`）；**仓库内没有使用 Chromium 请求上下文的等价实现**——`scripts/mcp_connector.py` 是 `urllib` 实现，只能做端点/发现/`initialize`/`tools/list` 的结构性检查，过不了 Cloudflare 那一关，不能当作替代品。checkout 之后并不等于可以直接跑这条验证。
- acct2–6 的 cookie 里没有 `__Secure-next-auth.session-token`（acct2 有）。**这不影响任何 `aip/*` 操作**：身份来自 access_token 的 Bearer + sentinel PoW，浏览器会话本身可以是匿名的（`/backend-api/me` 返回 `ua-…`、`email:""`）。不需要为「浏览器要登录」纠结，也不需要批量登录六个邮箱。
- **UI 入口是 `/plugins`**：`/settings/connectors` 对免费账号白屏（body 为空）。`/plugins` → 顶栏「添加」→「添加自定义 MCP 服务器」。侧栏那个「添加」是另一回事，用 JS `.click()` 会打到侧栏，必须用真实鼠标坐标。
- 匿名放行 `initialize` / `tools/list` **已被证伪且不应再做**：放行 `initialize` 会让匿名探测变 200 → `type=NONE`；`tools/list` 返回空列表只会让 actions 为空。正确做法是在网关把匿名请求的 `WWW-Authenticate` 换成纯挑战，不提供任何匿名 200 服务。判据只有一条：**只对完全没带 `Authorization` 的请求合成挑战，其余透传上游原话**。别按状态码一刀切，也别按「有没有 Authorization」就给 `error="invalid_token"`——`Basic`、空 `Bearer`、以及带合法 token 但权限不足的 401，都不是 token 无效，硬写就是伪造拒绝原因。
- **网关改 `WWW-Authenticate` 是有代价的补丁**，不是干净解法（干净解法是 MCP 服务端自己对匿名请求回纯挑战）：`proxy_hide_header` 不分状态码，会吞掉上游在非 401 上的挑战；location 级 `add_header` 会阻断 server 层头的继承，实测导致 `/mcp` 丢掉 HSTS 和 X-Frame-Options。改完必须验四态并复查安全头，细节见 `references/mcp-oauth-connector-contract.md` §2.2。
- 不修改 `account-rotation.json`，不读取其游标决定 Connect 任务顺序。
- `ELIGIBILITY_UNKNOWN`（接口异常）不得当作最终结论，需结合实测 API 响应判定。

退出码：`0` 全部检查完成、`1` 部分检查操作失败、`2` 参数或凭据缺失、`4` 资格无法确定。

## 可信 WorkItem 投递（D10）

`chatgpt_send.py` 有第四条投递入口，与普通投递的区别是**先验权限再发送**：它要求一个存在的 WorkItem 与一份写入授权，发送成功后写一条 landing event 和一条 session link。普通 `--new` / `<conv_id>` 路径不碰这套东西。

```bash
SO_MCP_TRUSTED_ROOT=/path/to/chatgpt-session-ops \
python scripts/chatgpt_send.py --work-item wi_<uuid> \
  --principal <principal_id> --tenant <tenant_id> --db /var/lib/so-mcp/work-items.sqlite3 \
  --account acct1 --new prompt.md
```

四个参数各有环境变量等价物，任一组缺失即 `exit 2` 且**不发任何请求**（`api.send_calls == 0`，连账号客户端都不创建）：

| 参数 | 环境变量 |
|---|---|
| `--work-item` | 无（必须显式给） |
| `--principal` | `SO_MCP_PRINCIPAL` |
| `--tenant` | `SO_MCP_TENANT` |
| `--db` | `SO_MCP_DB`（`--db`，不是 `--db-path`；后者属 `tools/gen_manifest.py`） |

**`SO_MCP_TRUSTED_ROOT` 是硬前置，不是可选优化**。它是本仓库工作树根，必须绝对路径、必须存在、必须有 `.git`、必须含 `src/session_ops/authority.py` 与 `work_items.py`、且 `git rev-parse --show-toplevel` 要等于它本身、`origin` 要等于 `https://github.com/oahcom/chatgpt-session-ops`。任一不满足 → `exit 2`，报 `untrusted WorkItem execution environment`。**这是设计意图：本地 CLI 操作者身份不等于 IdP 认证**，脚本自己也不把脚本目录或 cwd 当信任信号。

`--db` 必须指向持久文件，`:memory:` 被拒（`--db must name a persistent database file`）。`--evidence` / `--match` / `--confirmed` 是**禁用参数**：给了就 `exit 2`，防止调用方自带“已确认”证据冒充权威记录。

输出是 JSON：`result` / `status` / `work_item_id` / `link` / `link_error`。`link` 成功时是 `{link_id, session_id}`；link 失败时 `link_error` 非空、`link` 为 null，而发送结果仍然保留（`status` 仍是 200），并多一个 `message` 提示需要手工关联。**两者分开报，不把一个折叠成另一个**。退出码 `0` 要求**发送成功且 link 已建**，只有其一时是 `1`。

`--work-item --new` 强制要求显式登录账号（`--account acct1..acct6`）：给 `auto` 或 `nologin` 直接 `exit 2`，因为自动轮转与匿名通道发不出可信的 landing 证据。

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
| 状态文件出现 `*.lock` 残留 | 持有者进程已退出，锁文件没清 | 不用等：下一次抢锁发现持有者已死会立即回收。判据是**持有者死活**，不是年龄——锁文件里写着 `pid` 与 `process_start_time`，两者一致且进程在跑就绝不可回收（等多久都不会失效，手工删等于破坏跨进程互斥）。删不掉时内容会变成 `{"released": true}`，下一次抢锁同样立即回收。唯一仍看年龄的是**读不出身份**的老格式锁（空文件，或 JSON 里没有 `pid` 字段）：它们按 mtime 判，超过 `LOCK_STALE_SEC`(60s) 才算残留 |
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
| **投递的代码正文里 `#[derive]` / `#[test]` / `#[cfg]` / markdown 标题全部消失** | 投递器把 `#` 开头行当注释删（旧版 `[l for l in text.splitlines() if not l.strip().startswith("#")]`）；Rust 属性行恰好全部以 `#` 开头 | 该行已改为只做空正文判定、不删任何行（2026-11-21）。**症状识别**：会话反馈"正文里没有属性行"时先查投递器，别怀疑会话。回归自检：投递前后 `text.count("#[derive")` 必须相等 |
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
- `*.lock` — 上面三个 json 的跨进程写锁（O_EXCL），正常用完即删；残留锁按**持有者死活**判定，不按年龄：进程已退出（DEAD）的下一次抢锁立即回收，进程仍在的永不回收。释放时若因别的进程正读该文件而删不掉，锁内容会被改写成 `{"released": true}`，效果同上。例外：**读不出身份的老格式锁**（空文件，或 JSON 里没有 `pid` 字段）没有持有者可判，仍走 mtime 判据，超过 `LOCK_STALE_SEC`(60s) 才算残留。
- `proxy.json` — 出口代理配置（`{"url": "socks5h://..."}`，可带账号密码 → chmod 600 且不入库）。没有它时走上表第 3/4 档。
- `accounts/nologin/device-cookie.txt` — 匿名态最新 device cookie（`oai-did` 等），使 API 侧 `GET /backend-anon/conversation/<id>` 可持续收割。
- `accounts/nologin/devices/<oai-did>.txt` — 按设备分档的 Cookie 头快照；`--anon-read` 优先用它，保证浏览器换 profile 后旧会话仍可读。
- `accounts/nologin/devices/<oai-did>.json` — 同设备的完整 cookie jar（含 HttpOnly），`--anon-restore` 用它把身份回灌浏览器；已剔除 session token 一类凭据。

### references/

- `baselines.md` — **基线条款（全部唯一条款，含 A/B/C/E 类，共 51 条）**：每条带判定口径与违例后果，附 C 类现成配方（每小时巡检、落盘看门狗、到期处置、配额哨兵）、D 类事故→条款映射、E 类变更纪律、G 类证据矩阵说明。**开工前扫 A0/B0 清单，排程前读 B1–B15，改代码前读对应条款。**
- `evidence-matrix.md` — **逐条对账「这条基线由什么证明」**：`test:` 用例名 / `code:` 符号 / `live:` 实测日期 / `human:` 人判理由，四类分开标注；34 条有离线用例、3 条只有代码、14 条纯人判，缺口按危害排序列在文末。改基线必须同步这张表——`TestEvidenceMatrix` 会校验引用是否真实存在。
- `security-acceptance-matrix.md` — **安全验收矩阵（SO MCP 面，仅存在于仓库、不随发布拷贝到 A）**：逐场景写「攻击者/误操作会得到什么、实际发生什么」，是负面测试台账而非功能清单。行数以表格本身为准（本文不复述，复述必漂移），每行带 `test:`/`code:` 证据与状态（`PROVEN`、`PROVEN mutation-checked`、`partial` 等）；**`PROVEN` 不等于 `mutation-checked`**，只有带 `mutation-checked` 标记的行真的做过「删掉实现看它变红」。改这张表必须同步——`TestSecurityMatrix` 会校验引用与状态列。**A 侧没有本文件，也没有它引用的 `test_mcp_*` 用例**：这张表的每一行都以「哪个用例证明了它」为证，脱离那些用例就只剩结论没有依据，所以它留在 B，不跟着 `SKILL.md` 发布出去。
- `mcp-oauth-connector-contract.md` — **ChatGPT 侧 MCP 连接器 / OAuth 契约（2026-11-23 实测，单一部署 rmc-hub + nginx、六账号）**：`/backend-api/aip/connectors/*` 从创建、`type` 判定、授权回调、校验到删除的全流程实测。三个反直觉点：`POST /aip/connectors/mcp` 只认 `mcp_url`（不是 `base_url`）；`auth_request` 必须是 `{"supported_auth": [<oauth_config>]}` 而不是平铺对象——平铺也回 200，但 `type` 会静默退化成 `NONE`；网关用 `proxy_hide_header` / location 级 `add_header` 改 `WWW-Authenticate` 会连带吞掉上游其它挑战头和 HSTS/X-Frame-Options。**§7 的「不要相信调用成功」是最该先读的一节**：连接器建成、`type=OAUTH` 都不等于授权链路真的通。
- `mcp-tools.md` — **SO MCP 十个工具的运维契约**：`session.context/link/list/unlink` + `work_item.create/get/grant/list/revoke/update`。每个工具给真实 `inputSchema`（required / properties / `additionalProperties: false`）、成功与失败返回形状、真实错误码集合（5 个通用 `UNAUTHENTICATED` / `PERMISSION_DENIED` / `RESOURCE_EXHAUSTED` / `TIMEOUT` / `INTERNAL`，5 个工具级 `INVALID_ARGUMENT` / `NOT_FOUND` / `CONFLICT` / `QUARANTINED_SESSION` / `DEPENDENCY_UNAVAILABLE`，全部取自代码实测；`SCOPE_VIOLATION` 等 14 个码属于 **Memory Adapter 协议** 的 `KNOWN_ERROR_CODES`，不是 MCP 工具面的码，两者不得混用）、以及 keyset 分页与幂等语义。**§12 讲终态 WorkItem 的解链与 `session.auto_unlink_terminal` 审计**，含按 `link_id` 追溯的 SQL 和部署后核验查询。**调任何工具、或排查 401/403/`QUARANTINED_SESSION` 之前先读这篇**——错误码语义在这里，不在报错文本里。
- `TESTING-mutation-verification.md` — **安全测试方法论（DECISION-156 血的教训）**：一条没做过变异验证的安全测试等于没有测试。含五步法（正常 → 恶意 → 边界 → **修复回滚后的变异验证** → **确认执行了目标分支**）、本轮四条用例的变异记录、五个反面模式（mock 参数没人用、断言恒真、测试自己抛异常被当成红、只测纯函数、同语义多实现只测一份）、两份实现的漂移检测写法。**写任何安全类用例前先读这篇。**
- `api-protocol.md` — 实测协议细节：端点、PoW 算法、POST 请求体全字段、SSE 解析、mapping tail 计算、状态码处置、限速纪律。改脚本前先读。
- `INCIDENT-2026-11-20-CONVERSATION-REGISTRY-LOSS-AND-RECOVERY.md` — **事故记录**：旧 500 条裁剪器删掉 387 条归属记录 → 并集最小动作恢复 904。含时间切面、并发写入者、证据 SHA-256 清单、`restore_conv_map_minimal()` vs `register_conversations_batch()` 的职责边界，以及「不得宣称四账号已收敛 / 不得宣称批量登记已生产验证」的禁止结论。
- `INCIDENT-2026-11-21-CONVERSATION-REGISTRY-LEGACY-TRUNCATION.md` — **事故记录（P2，独立第二病因，已 CLOSED）**：11-20 修复后注册表仍在丢条目。根因是旧项目脚本 `WorkBuddy/2026-11-18-13-49-20/chatgpt_send_local.py` 按 `ts` 只保留最近 500 条，与主脚本共用同一个 `conv-accounts.json`。含「存活条目 100% 被刷新过 ts」的刀切证据、F1 全写入者清单、F2 快照交叉比对、F3 restore 锁行为。处置：legacy 四处封死为只读（含 3 个裸写脚本）+ 主脚本 `RegistryShrinkRefused` 单调性守卫（删除阈值 0，跨进程高水位基准按 A/B/C/D 四态 fail-closed，基准永不领先磁盘）+ 586 条历史恢复。**不得与 11-20 合并为同一根因；关闭本事故不等于注册表治理全部完成。**
- `GATE-B-BATCH-REGISTRATION-VERIFICATION.md` — **门槛 B 生产验证记录（DECISION-151 正式 PASS）**：`register_conversations_batch()` 首次执行登记 315 条（1112 → 1427，高水位同步，`audit=CONSISTENT`）；第二次 `eligible_registrations` 315 → 0，幂等性成立。含一项**方法论修正**：注册表字节相等不能作为跨会话断言——实测字节变化来自并发 `record_conv()` 改写既有记录的 `prompt`/`ts`，已用隔离实验证明 batch 对已存在候选**不写盘**（50 条候选，sha 前后相等）。**`registered` ≠ `added_count`**：前者是「已验证归属的候选数」，后者才是「本次新增数」，幂等性判据只能用后者（已实现 `reconciliation.added_count`）。`registered_not_observed=23` 按裁决保留，不得以「当前不可观测」推定历史登记无效。
- `memory-adapter-contract-v0.1.md` — **SO ↔ MA 直连记忆契约 v0.1（FROZEN FOR DEVELOPMENT，2026-11-21 生效）**：本技能作为 protocol consumer，Memory Adapter 作为 provider，为单一权威 WorkItem 提供跨会话记忆连续性。**职责边界**：SO 拥有 WorkItem 身份/生命周期/会话关联/当前状态/「存什么」的选择权；MA 只负责记忆的持久化与检索，**不是** WorkItem 管理系统。改协议前先读——它已冻结，变更需双方同步升版本号。
- `REGISTRY-ANOMALY-AUDIT-2026-11-21.md` — **注册表 9 条异常记录的审计说明（DECISION-158，保持原样不修改）**：4 条 8 位短 ID + 2 条 UUID `ts:null`（早期写入路径）、2 条标 `DO NOT dispatch` 的重复运行取证、1 条投递任务被杀后人工收割。含**安全性质核验**：`lookup_conversation_owner()` 完全不读 `ts`，`account` 非法即 fail-closed，不存在靠 `ts` 猜测归属的路径。**不要为这 9 条再发起清理任务**（补 `ts` 等于往审计链注入伪证，删除会解除 `DO NOT dispatch` 约束），除非出现新的安全证据。
- **注册表写入纪律**：`conv-accounts.json` 只允许 `scripts/chatgpt_send.py` 写入。任何新增写入路径必须先过 `_registry_guard()`，落盘后再 `_registry_commit_baseline()`。删除既有 ID 一律拒绝（阈值 0）。高水位基准损坏时 fail-closed。
- **高水位重建是数据安全动作**（DECISION-145 Q2）：必须 `CHATGPT_REGISTRY_HIGHWATER_RECONCILE=1` **加** `CHATGPT_REGISTRY_RECONCILE_REASON="人工核验说明"`，且**同进程只生效一次**（一次性授权，不是永久宽松模式）。每次重建先写 `conv-accounts.json.audit.jsonl`（追加式 JSONL + fsync）再改基准，记录 `ts_utc / op / basis / baseline_before{count,sha256} / registry{count,sha256} / lost_ids 完整差集 / git_sha / result`。审计写不成即中止重建。
- **注册表只读审计**（DECISION-145 Q3）：`python scripts/chatgpt_send.py --registry-audit [--json]`。只读，不碰注册表也不碰基准，输出完整差集（不是只有数量），`Consistency` 取 `CONSISTENT / BASELINE_AHEAD / REGISTRY_AHEAD / BASELINE_MISSING / BASELINE_UNREADABLE / REGISTRY_UNREADABLE`，非 CONSISTENT 退 4。审计结果**不得**自动转成恢复动作。



### tests/

- `test_hardening.py` — 离线回归（466 条）：id 校验/路径穿越、状态文件损坏隔离、原子写、锁重入与 stale、并发不丢更新、轮转兜底判定、SSE 解析、PoW、设备身份留存与多设备回退、出口代理（含本地假 http/socks 代理真分流）。

## 协议细节

PoW 算法、POST 请求体全字段、SSE 解析、mapping tail 计算等实现级细节见 `references/api-protocol.md`。脚本内部有注释标注血泪条款（conversation_id 必填、勿提前关连接等），修改前先读。

## 代码仓库

代码与协议文档已仓库化：<https://github.com/oahcom/chatgpt-session-ops>（公开）。仓库只含代码与文档，凭据、`replies/`、状态 json 全部由 `.gitignore` 排除；**发布拷哪些文件以 B 侧 `docs/RELEASE-AUTHORITY.md` §2 的显式白名单为唯一权威**（本文不复述清单——复述必与政策分叉，此前那句「只拷 `SKILL.md`、`references/`、`scripts/*.py`」就与白名单相矛盾，已删）；勿带本机运行态。

**备份文件（`.bak-<ts>` / `.corrupt-<ts>`）同属敏感物**：它们是凭据与私有会话 id 的历史快照，与正本同等级。`.gitignore` 已用 `*.bak-*` / `*.corrupt-*` 覆盖（此前只有精确文件名规则，`cookies.json.bak-20261121-191330` 这类副本能溜进仓库）。处置纪律：审计确认正本已完全覆盖备份内容后即删，不留档；**不要**把凭据备份丢进云同步目录。

**`chmod 600` 只在 POSIX 生效**（`_harden_perms()` 在 Windows 直接 return）。本机是 Windows，凭据文件的实际访问控制靠 NTFS ACL（`icacls` 显示仅 SYSTEM / Administrators / 当前用户可读写），不要以为跑过 `chmod` 就安全了；把技能目录放进多用户共享盘时先查 `icacls`。**这是当前这台机器经核验的状态，不可推广成「Windows 默认安全」**（DECISION-156 Q4：接受文档方案，本轮不做 ACL 自动化；将来若做，必须先实现「设权限后仍能读凭据」的自检，失败明确报错而不是静默继续）。

**安全类回归测试的硬要求（DECISION-156 Q6，长期有效）**：安全用例必须证明「缺陷重新出现时会失败」。五步：正常路径 → 恶意输入 → 边界输入 → **修复回滚后的变异验证** → **确认实际执行了目标代码分支**。最后两条是这次的血泪——第一版 `_NullOpener` 根本没把 cookie 写进 `warm_cookies()` 自己 new 的 jar，`changed` 为空直接提前 return，测试「跑了一遍但什么都没验证」，是靠变异验证（回滚后断言 `54 != 4` 失败）才发现的。没有变异验证的安全测试，等于没有测试。
