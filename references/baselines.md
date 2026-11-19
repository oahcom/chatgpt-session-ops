# 基线与纪律（可判定版 v1.1）

本文件把 `chatgpt-session-ops` 跑过的真实事故与 WorkBuddy automation 的真实运行日志，收敛成**带编号、带判定口径、带违例后果**的基线。SKILL.md 讲「怎么做」，本文件讲「什么算做对、什么算做错」。

**版本**：v1.1（2026-11-19）。v1.0 经 ChatGPT 思考模式对外对抗审查，裁定 `NEEDS-REVISION`（A 类 11 PASS / 12 WEAK / 2 MISSING，B 类 0 PASS / 10 WEAK / 4 MISSING）。v1.1 已按审查逐条改写判定口径，**未采纳项与理由见 §F**，不静默丢弃。

**用法**：动手前扫 A0/B0；出问题时按编号回查；改脚本前先读对应条款的判定口径——它是回归测试的依据。

**优先级**：B 类 > A 类（排程错会让 A 类在无人值守时反复踩坑）；用户 2026-11-19 明示的管理基线（开发基线、1 小时维度）> 本文件一切技术条款。

---

## §0 验收三分法（审查补入，最高优先解释原则）

任何「做完了」的声明，必须区分三层，**不可互相替代**：

| 层 | 含义 | 可验证载体 |
|---|---|---|
| **命令成功** | 进程正常结束 | exit code = 0 |
| **业务成功** | 目标服务确实完成了动作 | 服务端返回值（HTTP 200 + 目标字段） |
| **证据成功** | 能用持久化记录证明业务成功 | 落盘文件 + SHA-256 + 服务端 id 对齐 |

**本文件最危险的失败模式**，就是把「命令成功」或「一个本地标记」当成后两层。凡声明完成，必须指明达到哪一层、凭什么是那一层。

---

## A. 操控基线

### A0 开工检查清单（30 秒）

| # | 检查 | 命令 | 通过判据 |
|---|---|---|---|
| 1 | 出口通不通 | `--proxy-show` + 对 `chatgpt.com` 发一次 GET | 状态码 ∈ {200, 403-challenge}；只有 ipify 通不算（A26） |
| 2 | 这个会话归谁 | `--owner <conv_id>` | 输出唯一账号 |
| 3 | 配额有没有冷却 | `--rotation` | `unavailable` 为空或目标账号不在其中 |
| 4 | 匿名要用先看设备 | `--anon-devices` | 目标设备 `jar=true` |
| 5 | 改代码了吗 | `python -m unittest discover -s tests` | 全绿（当前 64 条） |

### A1–A4 内容主权（不可协商）

| 编号 | 基线 | 判定口径（机器可判定） | 违例后果 |
|---|---|---|---|
| **A1** | 提示词正文必须由 ChatGPT 会话自产，操控层只搬运 | 上游回复存 UTF-8 原文件；提取的正文存独立文件；**两者 SHA-256 相等**。只允许在提取阶段去掉围栏外首尾空行，不得改正文内部字节，不得转换 CRLF/LF，不得做 Unicode 规范化。提取不唯一或为空 → `exit 2` 禁止投递 | 正文来源不可追溯，等于操控层替 ChatGPT 写任务 |
| **A2** | 不当编辑者 | 只提取**上游指定的唯一交付块**（按交付标识/约定定位，不按「最长」）；候选数 ≠ 1 → `exit 2`；禁止润色/补全/合并/重排；提取结果存原始字节 + SHA-256 | 拿到手的不是被批准的交付正文 |
| **A3** | Owner 裁决必须投给**全新**会话 | `--new` 且请求体**不含** `conversation_id` 字段 | 自己给自己判分 |
| **A4** | 裁决结论原文回灌 | 回灌源 = 已存的原始裁决文件；回灌正文与约定交付范围**字节一致**；记录源文件 SHA-256、起止字节偏移、回灌文件 SHA-256；不一致 → `exit 2` 禁止回灌；禁止转成摘要 | 执行边界与 AMENDMENTS 丢失 |

### A5–A9 通道与身份

| 编号 | 基线 | 判定口径 | 违例后果 |
|---|---|---|---|
| **A5** | 登录态优先，匿名只兜底 | 正常池只有 `acct1/acct2`，**确定性轮询**，主游标仅在正常账号成功后推进；每账号持久化 `cooldown_until`；**仅当两账号都满足「本次 429」或「now < cooldown_until」** 才允许匿名；401/403/网络异常不计入；匿名用独立计数器 `fallback_uses` 不推进主游标；`share = acct1成功 / (acct1+acct2成功)` | 兜底条件不可复现，匿名被当成正常产能 |
| **A6** | 401/403 不触发兜底 | 兜底判定函数只读 429 与 nologin 冷却两个输入 | 认证坏了却绕去建匿名会话 |
| **A7** | 会话归属自动路由 | 未指定 `--account` → 从 `conv-accounts.json` 取**唯一**登记；显式与登记冲突 → WARN **且 `exit 2` 停止**；登记缺失/JSON 损坏/重复登记 → 禁止猜测 | 拿错账号读私有会话 |
| **A8** | 匿名会话只认设备身份 | 记录里的 `device` 必须与 `devices/<oai-did>.{txt,json}` 同时存在；缺失即 `device_known=false` | 换 profile 后旧会话读不回 |
| **A9** | 匿名可写窗口 = 创建它的那个标签页 | 创建后记录 `targetId + oai-did + conv_id`；写入前校验三者；targetId 不存在/被替换/浏览器重启/conv_id 不一致 → `exit 1`，**禁止自动 navigate、重建或重试写入**；不得复用该页建其他匿名会话 | 重开 `/uc/<id>` 即永久毁掉可写通道 |

### A10–A14 投递与收割

| 编号 | 基线 | 判定口径 | 违例后果 |
|---|---|---|---|
| **A10** | 发进既有会话**必带** `conversation_id` | 请求体含该字段；发送后 SSE 首个 `data:` 的 `conversation_id` 与目标**相等** | 消息静默落入全新会话（已发生事故） |
| **A11** | 200 即停止轮转 | 200 后立刻停止；id 解析顺序 SSE → 整包 JSON → 严格字段匹配；只接受**唯一、非空、符合 A19 白名单**的 id；解析不出 → WARN **且 `exit 1`**，禁止换身份重发 | 同一 prompt 建出多个会话 |
| **A12** | 大交付用 raw mapping 核对 | GET 全量 → 按原始顺序取目标 assistant 全部 parts → 记录 `message_id / part数 / 各part字符数 / 拼接 SHA-256`；与 `DONE chars` 同口径比对；缺 part、重复消息、口径不一致 → 判**不完整**（结论：补采，不是要模型重发）；保留原始 mapping 备查 | 流截断与真实生成失败无法区分 |
| **A13** | 回复默认全文落盘 | `CHATGPT_REPLY_MAX_CHARS` 默认 0；非 0 时文件头必须写明截断点与原始长度 | 归档只留尾巴，结论丢失 |
| **A14** | 投递前先落设备身份 | 发送**开始前**经 CDP `Storage.getCookies` 取 jar（含 HttpOnly），从目标浏览器上下文读 `oai-did`（**不得**用可能为空的 `api.cookie`）；写盘后**重新读取并校验 SHA-256 与 oai-did 一致**；任一失败 `exit 1` 禁止发送 | 会话建出来但读不回来（死档） |

### A15–A18 限速与失败分级

| 编号 | 基线 | 判定口径 | 违例后果 |
|---|---|---|---|
| **A15** | 同账号同刻只允许一个 GET 进程 | 账号级锁（跨 automation 共享，见 B8/B15）；发送器与补采器错峰 | 并发只换来一串 429 |
| **A16** | 会话间 ≥60s；同会话必须等前一条终止 | 同账号相邻 POST 起始间隔 ≥60s；同一 `conversation_id` 在前一条**出现 SSE 终止标记**前不得再 POST；超时上限：非思考 7 分钟、思考 30 分钟（实测单条可达 19 分钟）；超时标 `TIMEOUT`，**禁止自动重复 POST**，只能按 A12 补采判断 | 抢发导致上一条回复截断/重复投递 |
| **A17** | `Retry-After` 是**下限**，不是上限 | 秒数 → 等待 ≥ 该值；HTTP 日期 → 等到该时刻；缺失/不可解析 → ≥90s；本地最小 5s；**禁止把服务端要求向下截到 180s**；等待期内禁止对同账号主动探测（只读本地缓存）；冷却截止时间必须持久化 | 早于服务端允许时间恢复 → 持续 429 |
| **A18** | 失败分级，别一锅端 | 401→封 6h；403→补 cookie + 重跑 PoW **重试 1 次**→仍败冷却 15min；429→按 A17；匿名浏览器不可用→30min；401/403 **不触发匿名兜底** | 把 403 当永久封禁白停身份；把 401 当瞬时错反复撞墙 |

**A18 配套**：403 先数 cookie——只有 `session-token + oai-did`（2 条）时 GET 全 200 但 POST 恒 403，缺 `__cf_bm/_cfuvid/__cflb`，`warm_cookies()` 访问首页补回可自愈，不必重新登录。

### A19–A22 存储与安全

| 编号 | 基线 | 判定口径 | 违例后果 |
|---|---|---|---|
| **A19** | id 一律过白名单 | `^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$`；CLI 入口非法 `exit 2`；内部非法退 sha1 前 8 位 | id 会拼进 4 个目录，不校验等于允许 `../../x` |
| **A20** | 落盘原子 | tmp 与目标**同文件系统**、**文件名唯一**（禁止共用固定 `.tmp`）；`flush → fsync(file) → os.replace → fsync(dir)`；任一步失败清 tmp 并返回失败；失败不得把该状态记成成功；并发写先取 A22 锁 | 断电/并发导致状态文件丢失 |
| **A21** | 状态 json 损坏**不得**以空状态继续写 | 解析失败 → 原子隔离为 `<name>.corrupt-<ts>`，记录原文件 SHA-256/大小/异常 → 状态置 `STATE_CORRUPT`；此状态下**禁止改业务状态、禁止以空状态写**；只能从有效备份恢复，或由**独立批准标记文件**（含批准时间+原文件哈希）初始化；两者完成前不得解除 | 下次落盘把 `conv-accounts` 全表清空 |
| **A22** | 锁 = 线程重入 + 跨进程租约 | 线程锁 `dict[path] = thread.ident`（不被其他线程继承）；跨进程 `O_EXCL`；锁文件写 `pid / process_start_time / created_at / thread`；回收 = `now - created_at > 60s` **且**（原 PID 不存活 **或** 当前 PID 启动时间 ≠ 记录值）；**进程身份读不到 → 禁止回收**（可读不到就不动手，等超时）；无 `pid` 的老格式退化为 mtime 判据；**禁止持锁做网络 I/O**（60s 租约要留足余量，长任务必须拆小状态） | 长任务持锁超 60s 被误判死锁 → 双写；PID 复用被当活锁 → 永久卡死 |

**A22 事故数据**：4 线程 × 20 次，修前只落 59/80，修后 80/80；双进程各写 40 条 → 81 条零丢失。

### A23–A26 出口

| 编号 | 基线 | 判定口径 | 违例后果 |
|---|---|---|---|
| **A23** | 一切出网继承统一出口（含 WebRTC） | API、CDP 浏览器、匿名浏览器、自启动浏览器、git push 全部读**同一份** `proxy.json`，禁止任一子进程回退系统直连；浏览器启动必带 `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`；`--proxy-check --browser` 同时记录 API 出口、浏览器 HTTP 出口、`webrtc.status`；任一项不通过或**未验证** → `exit 3` 且 `full_chain_verified=false`；DNS：`socks5h` 远端解析（本地不解析），`http` 代理的 DNS 路径记为 `UNVERIFIED`，不得写成已验 | 「API 走隧道、浏览器裸连」= 身份与出口不一致；WebRTC UDP 是最隐蔽的泄漏面 |
**A23 WebRTC 判定（实测 2026-11-19）**：浏览器里一条 `new RTCPeerConnection({iceServers:[stun:…]})` 就能从 UDP 把公网 IP 漏出去，`--proxy-server` 拦不住。**正确开关是 `--webrtc-ip-handling-policy=disable_non_proxied_udp`**；带 `force-` 前缀的旧名字（`--force-webrtc-ip-handling-policy`）被 Edge 157 忽略，实测仍漏出 `59.34.82.8` 与本机 IPv6。`webrtc_probe()` 在一次性标签页跑真实 ICE，`_classify_webrtc()` 出结论：

| status | 含义 | 处置 |
|---|---|---|
| `clean` | 没拿到任何候选（策略屏蔽或只有 mDNS 名） | 通过 |
| `private_only` | 只有 10./192.168./fe80 之类 | 通过 |
| `via_proxy` | 公网候选 == 代理出口 IP | 通过 |
| `leaked` | 公网候选 ≠ 代理出口 | `exit 3`：浏览器没按策略启动 |
| `public_unknown` | 有公网候选但没代理 IP 可比 | 不通过（`full_chain_verified=false`） |
| `unverified` | JS 失败/无返回/建标签页失败 | 不通过，**不得声称全链路走代理** |

实测对照（同一台机器、同一出口 `152.175.33.8`、Edge 157）：

| 浏览器启动参数 | `webrtc.status` | 结论 |
|---|---|---|
| 只有 `--proxy-server` | `leaked`（`59.34.82.8` + IPv6） | HTTP 走代理，UDP 直连漏真 IP |
| `--force-webrtc-ip-handling-policy=…` | `leaked`（同上） | 旧开关名无效 |
| `--webrtc-ip-handling-policy=disable_non_proxied_udp` | `clean` | 生效 |

**所以 A23 的硬门禁落在「必须探测」而不是「假设 flag 有效」**：flag 是缓解，`--proxy-check --browser` 的 `leaked/unverified` 才是判定。另有一条环境坑：`--temp` 的 profile 目录是固定的，多端口并存时第二实例会被单实例机制吞掉（CDP 永不就绪），自启动必须按端口用独立 profile 目录。

| **A24** | 解析不到出口就拒绝联网 | `SystemExit(3)`（1=业务失败 2=参数错 3=未满足联网条件）；只有 `CHATGPT_ALLOW_DIRECT=1` 放行直连 | 静默直连最坏：任务「成功」了，真实 IP 已暴露 |
| **A25** | 探活必须端到端握手 | 代理 CONNECT/SOCKS 协商 + 目标 HTTPS TLS 握手 + **证书链/有效期/域名校验**；每阶段超时 ≤10s；`UNEXPECTED_EOF`/证书错误/超时判失败；bypass 不算代理成功；`--proxy-check --browser` 两侧公网 IP 不一致 → `exit 3` | 端口在听 ≠ 能用（7890 回 200 后断流，7897 才通） |
| **A26** | 探活必须验**目标站**，不能只量 IP | `probe_site()`：对 `https://chatgpt.com/` 发一次 GET，`verdict ∈ {ok, challenge}` 才算通过（`blocked_vpn/forbidden/unreachable/http_<code>` 一律不通过）；`--proxy-check` 的 `ok` 必须同时满足「代理生效」且 `site.ok`，否则 `exit 3`（`full_chain_verified=false`）；只有 `ipify` 通不算 | 见下 |

**A26 实现（`probe_site`）**：`ok`=200；`challenge`=403 且页面含 `just a moment / challenge-platform / cf_chl / enable javascript / cf-mitigated`（出口没被封，浏览器能过）；`blocked_vpn`=403 且含 `unable to load site`（换节点，别再查 cookie/PoW）；`forbidden`=403 其他页型（附正文前 160 字，不许猜）；`http_<code>`；`unreachable`（隧道断/TLS 失败）。

**A26 与 A18 的 403 必须分域**：探活 GET 的 `403-challenge` 记为 `TARGET_REACHABLE_CHALLENGED`，**只证明出口没被封**，不触发补 cookie / 重跑 PoW；只有**业务 POST** 的 403 才进 A18 的「补 cookie + PoW + 单次重试」。两处用不同字段记录（`site.verdict` vs `last_status`），混用会把正常挑战当账号故障、把 POST 403 当探活成功。

**A26 事故脉络（2026-11-19 13:37）**：`ipify` 判「代理生效」，业务请求全 403，页面写明 `Unable to load site … If you are using a VPN, try turning it off`——出口 IP 被 OpenAI 按 VPN 封。两账号建会话同时 `st=0/403`，脚本正确拒绝匿名兜底。**诊断顺序：先证出口，再查凭据**，否则会在 cookie 与 PoW 上白折腾半小时。本机同时有 Clash Verge（mixed-port 7000）与别的隧道（7897 出口被封），**不要按端口号猜**，按 A26 实测选，确定后 `--proxy-set` 固定。

### A27–A28（v1.1 新增）

| 编号 | 基线 | 判定口径 | 违例后果 |
|---|---|---|---|
| **A27** | **新建会话必须开思考模式**（评审/裁决/审查类硬要求，用户明示 2026-11-19） | API：输出必须出现 `thinking model: *-t-*`；浏览器：`思考` 按钮 `aria-pressed="true"` | 回复更浅、更「防御性」，评审流于表态、抓不到真缺陷 |
| **A28** | **流被截断 ≠ 发送失败** | 收到 `IncompleteRead` 时保留 `e.partial` 字节并返回 200 语义（`last_truncated=True`），从中解析 `conversation_id`；随后 **GET 补全文**，不得据此换身份重投 | 同一 prompt 建出多个会话 + 白烧一轮配额 |

**A27 实现口径（实测）**：`GET /backend-api/models` 里同一标题有**两个 slug**，带 `-t-` 的 `max_tokens=262144`、不带的是 `34834` —— `-t-` 即 thinking 变体；默认会话发 `"model": "auto"`（不思考）。脚本 `--thinking`（或 `CHATGPT_THINKING=1`）把 body 的 `model` 换成解析出的 `-t-` slug；解析不出 → **`exit 1` 拒绝投递**（否则「审查会话」会以不思考身份跑完还被当正常），要降级必须显式 `CHATGPT_ALLOW_NON_THINKING=1`。浏览器侧按钮靠 `innerText === "思考"`（英文 UI `Thinking`）命中，未开时 `aria-pressed="false"`，点一次变 `true` 且 class 增加 `accentSubtle-*`；`enable_thinking()` 幂等返回 `ALREADY / ENABLED / NO_TOGGLE / FAILED_TO_TOGGLE`。**顺序：先开思考再填词**——切换模型可能重置输入框。关闭：`CHATGPT_NO_THINKING=1`。代价：单条 5–19 分钟，`_idle` 预算要放宽（A16）。

**A28 事故数据（2026-11-19 14:05）**：思考模式单条 SSE 收到 **23,772,819 字节**后被隧道截断，`st=0` → 脚本判失败 → 换身份重投 → 差点建出第二个会话。修复后：`last_truncated` 标记 + GET 补全文（本次实际从 SSE 半截补到完整回复）。

---

## B. 定时任务基线（automation）

> 来自 WorkBuddy automation 真实调度日志：并发上限 3、jitter、`unknown_interrupted`、`Automation deleted`。**B 类优先于 A 类。**

### B0 建/改任何 automation 前的六问

1. **1 小时维度够不够？** 不够就改条件巡检，不要升到天维度。
2. **实际 RUNNING 实例会不会 >3？** 调度器全局并发就是 3。
3. **跑一半被掐会怎样？** 必须有 B7 状态机，不能只靠上下文。
4. **无事可做会不会刷屏？** 无事务必静默 `exit 0`。
5. **一次性任务到期后谁归档？** 没人 → 现在就写进 prompt（B5）。
6. **同账号 GET 会不会撞？** B8 的并发与 A15 的账号锁是两回事。

### B1–B5 排程

| 编号 | 基线 | 判定口径 | 违例后果 |
|---|---|---|---|
| **B1** | 周期任务固定 1 小时 | `rrule` 必须 `FREQ=HOURLY;INTERVAL=1`；禁止 `DAILY/WEEKLY/MONTHLY`；**禁止用 `INTERVAL<1` 表示高频**；同一 automation 实际触发间隔 ≥60 分钟；时区统一 `Asia/Shanghai`，缺失即禁止启用 | 天维度过久 / 语义漂移 |
| **B2** | 到期项走每小时条件巡检 | 状态文件持久化三态 `PENDING/RUNNING/DONE`：未到点→`exit 0` 静默；到点且 `PENDING`→**原子领取**后才可执行；`RUNNING` 且租约有效→退出；`DONE`→`exit 0`；`FAILED`→按 B13；**先持久化 `DONE` 再结束**；状态文件读写失败→`exit 1` 禁止处置 | 重复处置、漏处置 |
| **B3** | 长周期用 `validFrom/validUntil` 收窗 | 带时区 ISO 8601，窗口**左闭右开** `[from, until)`；窗口外 `exit 0` 不做业务；`until` 到点标 `EXPIRED`，不得再执行（除非新实例） | 窗外执行或永久残留 |
| **B4** | 一次性看门狗寿命 ≤1h | `scheduledAt` 带时区；创建时 `0 < scheduledAt - now ≤ 3600s`；已过去的不得建成 ACTIVE；更远的需求改 B2；单次运行上限 3600s，超时记 `TIMEOUT` 并释放租约 | 远期单点触发 / 长期占并发槽 |
| **B5** | 过期 one-shot 一律 PAUSED | 判定 = `scheduledAt < now AND status == ACTIVE`；每小时巡检盘点全量 one-shot；置 `PAUSED` 后**重新读取确认**；失败写 `watchdog.log`（id/原状态/目标状态/错误码）；**禁止凭接口返回成功就认定已归档**；存在运行实例时先按 B9 | 过期任务重放 / 占资源 |

### B6–B9 执行可靠性

| 编号 | 基线 | 判定口径 | 违例后果 |
|---|---|---|---|
| **B6** | prompt 自包含 | 必须写死：工作目录绝对路径、Python 解释器绝对路径、仓库与分支、`conv_id` 与账号、执行命令与退出码含义、验收条件、失败处理、状态文件绝对路径、时区；执行前做**路径类型校验**：工作目录/仓库必须是目录，解释器必须是**可执行文件**，状态文件**父目录存在且可写**；缺字段、类型不符或不满足 → `exit 2`，禁止执行任务；禁止依赖创建时的会话上下文 | 运行时不知道指哪个会话 → 空转或误操作 |
| **B7** | 有副作用的任务必须幂等可续跑 | 不可变 `task_id` + 每步 `step_id`；状态文件记 `task_id/step_id/status/attempt_count/started_at/updated_at/result_hash/副作用已提交?`；状态机 `PENDING→RUNNING→DONE`、`RUNNING→FAILED`、`FAILED→PENDING`（仅 B13 允许）；`RUNNING` 必须**原子领取**，同 `task_id+step_id` 不得有两个有效 RUNNING；**副作用已提交但结果未确认 → 先查实际结果，不得直接重做**；中断后从持久化状态恢复 | 中断后重复投递/重复提交/静默跳过 |
| **B8** | 实际运行实例 ≤3（不是 BYMINUTE ≤3） | 启动许可必须**原子领取**：用跨进程锁（A22 同一套 `O_EXCL`）保护的槽位文件统计**当前 RUNNING 实例**，「读计数 + 占槽位」必须是**同一次加锁内完成的原子操作**（否则两个任务同时读到 2 会一起启动）；容量 3；排队记 `QUEUED` 不得当已执行；每实例记 `automation_id/run_id/PID/started_at`；跨分钟仍计入；禁止改 `BYMINUTE` 绕过；超窗未启动 → `MISSED`，不得算成功 | 「看起来跑了」实则漏跑 |
| **B9** | 删前先 PAUSED 并等实例结束 | 置 `PAUSED` → 重读确认 → 查全部运行实例 → 等 `COMPLETED/FAILED/CANCELLED`，上限 3600s → 超时**禁止删除**记 `DELETE_BLOCKED` → 删除后重查确认不存在；`Automation deleted` 不得当成执行完成 | 运行中被删，无法区分人为取消与失败 |

**B8 实例证据**：日志 `concurrency limit active=3, concurrency=3`；**U-052**：13:25 到期被 enqueue，13:43 才真正启动，跑 18 分钟被 `unknown_interrupted` 掐断——排程「跑了」，交付为零。

### B10–B15 行为契约

| 编号 | 基线 | 判定口径 | 违例后果 |
|---|---|---|---|
| **B10** | 命名即契约 | 只读名称必须匹配 `^.+ .+（只读）$`（全角括号）；写入名称必须匹配 `^.+ .+ .+$`（三段）；名称解析出的类型必须与 argv 权限分类一致；无法解析或不一致 → `exit 2` 禁止启用 | 权限无法从名称交叉验证 |
| **B11** | 只读看门狗走 argv 白名单 | 白名单（完整 argv）：`--list`、`--tail <id>`、`--owner <id>`、`--anon-list`、`--anon-devices`、`--proxy-show`、`--rotation`、`--list-all`；禁止 `--new / --anon-send / --proxy-set` 及任何发送/修改/删除；**禁止 shell 字符串拼接绕过**；执行前解析完整 argv；**禁止前缀/正则包含匹配**（`startswith("--rotation")` 会把写操作 `--rotation-reset/-enable/-disable` 一起放行，`--anon` 会吞掉 `--anon-send`）；不匹配 `exit 2`；记录实际命令与退出码 | 白名单与巡检骨架冲突（v1.0 内部矛盾，已修） |
| **B12** | 无事静默 | 正常巡检 stdout ≤1 行、stderr 为空；无待处置 `exit 0`；有待处置且成功 `exit 0`；业务失败 `exit 1`；参数/配置错 `exit 2`；出口不满足 `exit 3`；**异常不得被吞成 0**；诊断只写 `watchdog.log` | 异常被静默吞掉 / 持续刷屏 |
| **B13** | 重试有固定上限 | 同 `task_id+step_id` 每自然小时最多 1 次处置；`attempt_count` 持久化跨实例共享；默认 `N=3`；每次失败记时间/退出码/摘要/副作用状态；达 3 次 → `MANUAL_REVIEW`，**禁止自动重投**；恢复需新授权记录（task/step/时间/批准人）；429 仍按 A17，不因小时重置提前重试 | 无限重投烧配额，或失败后永久停滞 |
| **B14** | 与 A 类的联动红线 | 巡检不得并发跑多个 `--tail/--list`（A15）；无 `--owner` 结论不得 `--new`；429 只记不重投（A17）；留存型工作不得用匿名（A5/A9）；任何 automation 出网必须继承同一出口（A23） | 巡检自己把自己限速 |
| **B15** | 运行实例必须有租约与超时回收 | 每次运行生成唯一 `run_id`；同 `task_id` 同时只允许一个有效 RUNNING；实例记 `run_id/task_id/PID/started_at/heartbeat_at/lease_until`；**每 15s 心跳**，默认租约 60s；回收必须同时校验 `PID + process_start_time + run_id`（PID 会被复用，单看 PID 存在性不够）；只有「租约过期 **且** 原实例身份确认不存在」才可回收；**身份无法确认 → 保留租约并 `exit 1`**，禁止启动第二个；单次上限 3600s，超时记 `TIMEOUT` 并释放租约；重启必须读 B7 状态，不得从第一步重做；租约冲突 `exit 1` 禁止副作用 | 僵尸实例占槽位 / 并发重复处置 |

> **B14/B15 编号说明**：v1.0 的 B14 是联动红线（无独立判定口径），审查据此判「B14 缺失」。v1.1 保留 B14 为红线表，把审查提出的「运行实例租约」独立为 **B15**——两类约束不同（红线是跨类禁令，B15 是调度器级生命周期），不合并。

---

## C. 现成配方

### C1 每小时收敛巡检（协调器值班）

```text
rrule: FREQ=HOURLY;INTERVAL=1        名称: 每小时收敛巡检（协调器值班）
prompt 骨架：
1) 只读盘点：--rotation、--list-all（单进程串行）、目标仓库 open PR/Issue
2) 若存在待处置项且本轮未处置过 → 原子领取后执行一次（每小时上限 1 次，B13）
3) 429 冷却中 → 只记录，不重投（A17/B14）
4) 结论追加 <工作目录>/watchdog.log（时间/对象/动作/结论/exit code）
5) 无待处置项 → 不输出，exit 0（B12）
```

### C2 回复落盘看门狗（只读，寿命 ≤1h）

```bash
python scripts/chatgpt_send.py --owner <conv_id>   # 先确认身份（A7）
python scripts/chatgpt_send.py --tail <conv_id>    # 无内容 → exit 1，属正常
# 只在 replies/ 缺该 conv 的 reply-*.md 时才补采；已有文件不覆盖
```

判定：`0` 且有文件 → 已落盘，静默；`1` → 回复未完成，**不重投**，等下个巡检点；`2`/`3` → 参数或出口问题，立即报。

### C3 到期处置（远期事项，避免天维度）

```text
rrule: FREQ=HOURLY;INTERVAL=1
validFrom: 2026-12-01T00:00:00+08:00   validUntil: 2026-12-03T00:00:00+08:00
prompt：窗口外 → exit 0 静默（B3）；状态 DONE → exit 0；PENDING → 原子领取后执行，
        先落副作用证据再写 DONE（B2/B7）。
```

### C4 配额哨兵（429 零漏出）

```text
rrule: FREQ=HOURLY;INTERVAL=1
prompt：读 --rotation 冷却表 + watchdog.log 近 24h 的 429 记录；
        有新增 → 一行告警（账号+恢复时间）；无新增 → 静默。
        绝不主动发包探测（探测本身消耗配额，A17）。
```

---

## D. 事故库（真实事故 → 条款）

| 事故 | 归属 | 教训 |
|---|---|---|
| 消息落入全新会话 | A10 | 缺 `conversation_id` 时服务端不报错，只在 SSE 里露新 id |
| 同一 prompt 建出两个会话 | A11 | 200 后没解析出 id 就换身份重试 |
| **思考模式 23MB 流被截断 → 判失败重投** | **A28** | `IncompleteRead` 里有半截数据，换身份重投＝重复建会话 |
| 匿名会话建出来后读不回 | A14 | 异常路径用了当时为空的 `api.cookie` 登记设备 |
| 新建匿名会话毁掉旧会话 | A9 | 直接 `navigate` 覆盖 `/uc/<id>` 标签页 |
| `conv-accounts` 500 条归属被清空 | A21 | 损坏 json 静默返回 default 后又落盘 |
| 多线程并发只落 59/80 | A22 | 锁按路径记，第二线程跳过 |
| 端口 7890 通但全链路 TLS 失败 | A25 | 只看 TCP 连通就采用死节点 |
| `ipify` 报「代理生效」，两账号同时建会话失败 | A26 | 出口被按 VPN 封；量 IP ≠ 目标站可用 |
| 文档写 exit 3、实际退 1 | A24 | `SystemExit("msg")` 不等于 `SystemExit(3)` |
| 413 续接脚本从未跑通 | A20 邻近 | `str.replace` 把 JS 签名一起换掉；`navigate(wait=)` 参数名错 |
| 每小时任务并发被 enqueue 而漏跑 | B8 | 同点挂超过 3 个 |
| 长巡检被 `unknown_interrupted` 掐断 | B7 | 进度只在上下文里，重跑从零 |
| 过期 one-shot 长期 ACTIVE | B5 | 没人负责归档（本次盘点 4 个） |

---

## E. 变更纪律

| 改动对象 | 必跑 | 条款 |
|---|---|---|
| 锁 / 落盘 / 状态 json | `python -m unittest discover -s tests` | A20–A22 |
| id 处理 / 路径拼接 | 同上 + 手测 `--anon-read ../../evil` | A19 |
| 设备身份 / 匿名通道 | 同上 + `--anon-devices` 实盘 | A8/A9/A14 |
| 出口代理 | 同上 + `--proxy-check --browser` + 目标站可达 | A23–A26 |
| 思考模式 / 模型 | 同上 + `--new` 实盘看 `thinking model:` 行 | A27 |
| 流解析 | 同上 + 构造 `IncompleteRead` 用例 | A28 |
| 排程口径 | `automation_update list` 全量盘点 | B1–B5 |

**写测试的硬要求**：任何「已修 bug」都要有能复现旧行为的用例，注释写明事故场景。当前 64 条。

---

## F. 审查记录与未采纳项

**审查来源**：ChatGPT 新会话（思考模式，`model=gpt-5-6-t-mini`），受审对象 v1.0 全文，审查提示词 `scripts/prompts/04-baseline-adversarial-review.md`，回复 `scripts/replies/review-baselines-chatgpt.md`。

**裁定**：`NEEDS-REVISION`。优先修 5 项：B7（副作用幂等）、B15（实例租约）、A21（损坏保护）、A22（锁租约）、B8（实际并发）。

**未采纳 / 已改写的建议**：

| 审查建议 | 处置 | 理由 |
|---|---|---|
| 时区统一 `Asia/Tokyo` | **改为 `Asia/Shanghai`** | 本机与用户都在 UTC+8；照抄会把所有排程错开 1 小时 |
| A16 统一「7 分钟超时」 | **改为分档**：非思考 7min / 思考 30min | 实测思考模式单条 19 分钟仍在正常流式输出，7 分钟会误杀（A27/A16） |
| A1 要求 SHA-256 覆盖换行符不转换 | 采纳，但注明「上游回复原文件」是唯一基准 | 若无原文件则无从校验，先落原文再提取 |
| A23 要求校验 WebRTC 出口 | **降级为提示** | Chrome 侧无法在不改启动参数下可靠取证；硬门禁会误报 exit 3 |
| B14 判为「原文缺失」 | **保留 B14 并新增 B15** | v1.0 有 B14（联动红线表），只是缺判定口径；两类约束不合并 |
| B11 白名单增 `--rotation/--list-all` | **采纳** | 两者均为只读，是 v1.0 文档内部矛盾（配方要求用、白名单没列） |

**审查提出的三条易忽略语义（已并入正文）**：①「WARN 不等于阻断」→ A7/A11 改为 WARN + 非 0 退出；②「文件存在 ≠ 业务完成」→ B2/B7 要求副作用与标记绑定；③「PAUSED ≠ 已停止」→ B5/B9 区分调度状态与 run 实例状态。
