# 基线与纪律（可判定版 v1.8）

本文件把 `chatgpt-session-ops` 跑过的真实事故与 WorkBuddy automation 的真实运行日志，收敛成**带编号、带判定口径、带违例后果**的基线。SKILL.md 讲「怎么做」，本文件讲「什么算做对、什么算做错」。

**版本**：v1.8（2026-11-19）。v1.0 起经 ChatGPT 思考模式**十七轮对外对抗审查**逐条改写（每轮裁定见 §F；**第十四轮 `PASS`**，第八轮亦 `PASS`，其余均为 `NEEDS-REVISION` 后返修；第九轮把「可判定」本身当成攻击面；第十五至十七轮分别抓出 stale 锁回收 TOCTOU（F-02）、进程存活二态误判（F-03）、假值 pid 三处绕过（F-04/05/06），均已修复待复审），**未采纳项与理由见 §F**，不静默丢弃。**每条判定口径的证据载体（用例名/符号/实测日期/人判理由）见 `references/evidence-matrix.md`，该矩阵由回归校验，引用不存在的用例会直接红。**当前 186 条回归。

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
| 5 | 改代码了吗 | `python -m unittest discover -s tests` | 全绿（当前 186 条） |

### A1–A4 内容主权（不可协商）

| 编号 | 基线 | 判定口径（机器可判定） | 违例后果 |
|---|---|---|---|
| **A1** | 提示词正文必须由 ChatGPT 会话自产，操控层只搬运 | 上游回复存 UTF-8 原文件；提取的正文存独立文件；**两者 SHA-256 相等**。只允许在提取阶段去掉围栏外首尾空行，不得改正文内部字节，不得转换 CRLF/LF，不得做 Unicode 规范化。提取不唯一或为空 → `exit 2` 禁止投递 | 正文来源不可追溯，等于操控层替 ChatGPT 写任务 |
| **A2** | 不当编辑者 | 只提取**上游指定的唯一交付块**（按交付标识/约定定位，不按「最长」）；候选数 ≠ 1 → `exit 2`；禁止润色/补全/合并/重排；提取结果存原始字节 + SHA-256 | 拿到手的不是被批准的交付正文 |
| **A3** | Owner 裁决必须投给**全新**会话 | `--new` 且请求体**不含** `conversation_id` 字段 | 自己给自己判分 |
| **A4** | 裁决结论原文回灌 | 回灌源 = 已存的原始裁决文件；回灌正文与约定交付范围**字节一致**；记录源文件 SHA-256、起止字节偏移、回灌文件 SHA-256；不一致 → `exit 2` 禁止回灌；禁止转成摘要 | 执行边界与 AMENDMENTS 丢失 |

**A1 的空白口径（唯一解，别的都算篡改）**：删掉**围栏外首尾整行空白**；首尾**非空行**的全部原始字节（**含它自己的行终止符**）保留；内部所有字节原样。这条不写死就会出现「裁掉最后换行算不算改正文」的争论——现在的答案是：**不裁，保留**，因为行终止符属于原文。回归 `test_byte_exact_matrix` 用 LF / CRLF / 多空行 / 尾随空格四种输入断言精确字节。

**A1/A2/A4 实现口径（v1.8 落地，此前是纯人判）**：`--extract <src> --between <起> <止>` 或 `--fence <n>` 取**唯一**交付块，`--out` 字节级落盘（`_atomic_write_bytes`），输出 `src_sha256 / sha256 / offset_start / offset_end`；标记数 ≠ 1、顺序反、块为空、定位器缺失 → `exit 2`。回灌前 `--verify-copy <a> <b>` 比对两份文件，SHA-256 不等即 `exit 2`。**不做 Unicode 规范化、不转 CRLF、只裁首尾空行**——这三件事一旦做进代码，正文就与原稿不是同一份字节，而失败是静默的。回归：`TestCustody`（10 条）。

**`--fence` 已降级为非合规辅助（第九轮审查结论）**：n 是**位置**，不是交付身份；即使调用方显式给出，也证明不了「这一块就是上游批准的那块」。因此 **A2 的合规路径只有 `--between`**（唯一标记行 = 交付标识）。`--fence` 保留给「上游明说第 n 块」的辅助场景，但**不得单独作为 A2 的证据**，用完仍须 `--verify-copy` 对齐。两个定位器都在 `exit 2` 上硬失败：宁可停下来问，也不许「取到一块看着像的就投」。

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
| **A11** | 200 即停止轮转 | 200 后立刻停止；id 解析顺序 SSE → 整包 JSON → 严格字段匹配；只接受**唯一、非空、符合 A19 白名单**的 id；解析不出或不合规 → **返回 1、不落盘、不重投**（不许回落到传入 id 粉饰成功） | 同一 prompt 建出多个会话 |
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

**A12 实现（v1.8）**：`reply_digest(raw_body)` 从 mapping 按 `create_time` 取最新 assistant，输出 `message_id / parts各段字符数 / chars合计 / 拼接 SHA-256`；流截断补全文时打印这四项并与 `len(full)` 对账，缺 part 即判「不完整」。旧版只比「GET 比 SSE 长」——缺一个 part 但总长仍变长时会误判完整。

**A22 事故数据**：4 线程 × 20 次，修前只落 59/80，修后 80/80；双进程各写 40 条 → 81 条零丢失。

**A20 六项保证与对应断言（第十轮审查要求逐项核验）**：

| 保证 | 断言载体 |
|---|---|
| tmp 与目标同文件系统 | tmp 就建在目标同目录（`path + ".tmp-…"`），用例断言落盘位置 |
| tmp 名唯一 | `test_atomic_write_uses_unique_tmp_names`（4 线程各写一次，断言 `os.replace` 收到的 src 互不相同） |
| flush + fsync(file) | `test_tmp_is_in_same_dir_and_fsync_failure_cleans_up`（注入 `os.fsync` 抛错，断言确实调用过且失败后清 tmp） |
| `os.replace` 原子替换 | `test_replace_failure_cleans_up_and_keeps_old_content`（注入 `os.replace` 失败，断言旧内容完好、无 tmp 残留） |
| fsync(dir) | 同一次 `_atomic_write` 内在 `os.replace` 后对父目录 fsync（**仅 POSIX**；Windows 上 `os.open(dir)` 直接 PermissionError，实现显式降级并在注释里写明） |
| 失败清理且不得记成成功 | 上述两个注入用例都断言「失败抛异常 + 目标文件不被替换」 |

**A20 的阶段与边界（第十一轮审查要求写清）**：

- **Windows 降级语义**：目录 fsync 无对应语义，代码跳过；因此 A20 **跨平台只能记 PARTIAL**，不能宣称「无条件持久化」。
- **replace 之后失败 ≠ 回滚**：`os.replace` 成功后若目录 fsync 失败，函数抛异常，但目标文件**已经是新内容**。调用方必须理解为「可能已生效但未确认」，而不是「保持旧值」——这与 §0 的业务成功/证据成功分层是同一条道理。

**A20 二次事故（v1.8，被新回归抓出）**：文档写着「文件名唯一（禁止共用固定 `.tmp`）」，代码里却一直是 `path + ".tmp"`。同进程 4 线程并发写同一状态文件时，两个线程写同一个临时文件、`os.replace` 在 Windows 上抛 `WinError 32`，线程被打死、锁文件留在原地，后来者等 8s 超时后「跳过加锁」继续写 → **4 × 20 次只落 60 条**（断言要求 80）。修法：tmp 名改成 `<path>.tmp-<pid>-<thread ident>`。教训写进 §0：**「文档里写了」不等于「代码里是」**，这条是本次把文档当受审对象后才暴露的。

### A23–A26 出口

| 编号 | 基线 | 判定口径 | 违例后果 |
|---|---|---|---|
| **A23** | 一切出网继承统一出口（含 WebRTC） | API、CDP 浏览器、匿名浏览器、自启动浏览器、git push 全部读**同一份** `proxy.json`，禁止任一子进程回退系统直连；浏览器启动必带 `--webrtc-ip-handling-policy=disable_non_proxied_udp`（**只有这个开关名有效**；旧名 `--force-webrtc-ip-handling-policy` 可作冗余附带，但**不得作为依赖**——实测被 Edge 157 忽略）；`--proxy-check --browser` 同时记录 API 出口、浏览器 HTTP 出口、`webrtc.status`；任一项不通过或**未验证** → `exit 3` 且 `full_chain_verified=false`；DNS：`socks5h` 远端解析（本地不解析），`http` 代理的 DNS 路径记为 `UNVERIFIED`，不得写成已验 | 「API 走隧道、浏览器裸连」= 身份与出口不一致；WebRTC UDP 是最隐蔽的泄漏面 |
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
| **B4** | 一次性看门狗寿命 ≤1h | `scheduledAt` 带时区；创建时 `0 < scheduledAt - now ≤ 3600s`；已过去的不得建成 ACTIVE；更远的需求改 B2；单次运行上限 3600s，超时先取消执行体、**确认执行体已停**才记 `TIMEOUT` 并释放租约（执行体不响应取消 → `TIMEOUT_ORPHAN` 保留租约，见 B15，不得在此处擅自释放） | 远期单点触发 / 长期占并发槽 |
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
| **B10** | 命名即契约（含权限映射表） | 名称 strip 后按空白（含全角空格）**分词**：只读 = **恰好 2 段**且第 2 段以全角「（只读）」结尾；写入 = **恰好 3 段**；动作词取第 2 段（只读，**先去掉末尾全角「（只读）」再整段比对**）/ 第 3 段（写入），必须与词表中的词**整段精确相等**（不做包含/前缀匹配，`查看并删除` 不算只读）；**段数不符、动作词未分类、名称类型与 argv 权限分类不一致 → `exit 2`** 禁止启用（正则只作人眼提示，不作判定依据） | 权限无法从名称交叉验证 |
| **B11** | 只读看门狗走 argv 白名单 | 白名单（完整 argv）：`--list`、`--tail <id>`、`--owner <id>`、`--anon-list`、`--anon-devices`、`--proxy-show`、`--rotation`、`--list-all`；禁止 `--new / --anon-send / --proxy-set` 及任何发送/修改/删除；**禁止 shell 字符串拼接绕过**；执行前解析完整 argv；**禁止前缀/正则包含匹配**（`startswith("--rotation")` 会把写操作 `--rotation-reset/-enable/-disable` 一起放行，`--anon` 会吞掉 `--anon-send`）；不匹配 `exit 2`；记录实际命令与退出码 | 白名单与巡检骨架冲突（v1.0 内部矛盾，已修） |
| **B12** | 无事静默 | 正常巡检 stdout ≤1 行、stderr 为空；无待处置 `exit 0`；有待处置且成功 `exit 0`；业务失败 `exit 1`；参数/配置错 `exit 2`；出口不满足 `exit 3`；**异常不得被吞成 0**；诊断只写 `watchdog.log` | 异常被静默吞掉 / 持续刷屏 |
| **B13** | 重试有固定上限 | 同 `task_id+step_id` 每自然小时最多 1 次处置；`attempt_count` 持久化跨实例共享；默认 `N=3`；每次失败记时间/退出码/摘要/副作用状态；达 3 次 → `MANUAL_REVIEW`，**禁止自动重投**；恢复需新授权记录（task/step/时间/批准人）；429 仍按 A17，不因小时重置提前重试 | 无限重投烧配额，或失败后永久停滞 |
| **B14** | 与 A 类的联动红线 | 巡检不得并发跑多个 `--tail/--list`（A15）；无 `--owner` 结论不得 `--new`；429 只记不重投（A17）；留存型工作不得用匿名（A5/A9）；任何 automation 出网必须继承同一出口（A23） | 巡检自己把自己限速 |
| **B15** | 运行实例必须有租约与超时回收 | 每次运行生成唯一 `run_id`；同 `task_id` 同时只允许一个有效 RUNNING；实例记 `run_id/task_id/PID/started_at/heartbeat_at/lease_until`；**每 15s 心跳**，默认租约 60s；回收必须同时校验 `PID + process_start_time + run_id`（PID 会被复用，单看 PID 存在性不够）；只有「租约过期 **且** 原实例身份确认不存在」才可回收；**身份无法确认 → 保留租约并 `exit 1`**，禁止启动第二个；单次上限 3600s；超时先取消执行体并给宽限，**执行体确认已停**才记 `TIMEOUT` 并释放租约，执行体不响应取消 → 记 `TIMEOUT_ORPHAN` 并**保留租约**（两档语义与实现段一致）；重启必须读 B7 状态，不得从第一步重做；租约冲突 `exit 1` 禁止副作用 | 僵尸实例占槽位 / 并发重复处置 |

**B10 权限映射表（可机器解析，不是措辞约定）**：
| 分词后段数 | 末段 | 动作词（唯一判定依据） | argv 权限分类 | 判定 |
|---|---|---|---|---|
| 2 | 以「（只读）」结尾 | 查看 / 列出 / 收割 / 校验 / 盘点 / 巡检 / 比对 / 读取 | 必须 ∈ B11 只读白名单 | 通过 |
| 3 | 无后缀 | 投递 / 新建 / 写入 / 切换 / 重置 / 删除 / 归档 / 修复 / 设置 / 处置 / 执行 / 补采 | 可出现写命令 | 通过 |
| 其他（1 段 / ≥4 段 / 2 段无后缀 / 3 段带后缀 / 动作词未分类） | — | — | — | `exit 2` |

解析顺序：`strip()` → 按空白分词 → 段数分支 → 去后缀 → **动作词整段相等比对** → argv 权限交叉验证。矛盾一律 `exit 2`：①「（只读）」+ 写词表动词；② 无后缀 + 读词表动词；③ 只读名称的 argv 里出现白名单外命令（B11）；④ 段数不是 2 或 3。**对象信息不得含空格**（复合就写在一段里或用连字符），因为段数是判定依据。合规样例：`协调器 收敛 处置`（写入，3 段）、`到期项 处置 执行`（写入，3 段）、`配额 盘点（只读）`（只读，2 段）；反例：`每小时收敛巡检（协调器值班）`——按空白只有 1 段，必须 `exit 2`。

**不要用 `^.+ .+ .+$` 这类贪婪正则代替分词**——它连四段、五段一起放行，正是本轮审查抓到的口径漏洞。

**B10/B11 实现（v1.8）**：`--check-name "<名称>" [--argv "<命令串>"]` 把上面两张表落成代码——分词判段数、动作词与词表**整段相等**比对、只读名的 argv 必须全部命中 `READONLY_ARGV`；任一条不成立 `exit 2`。别再自己写正则：`startswith("--rotation")` 会放行 `--rotation-reset`，`查看并删除` 会被包含匹配当成只读。回归 `TestScheduleContract`（19 条）覆盖四段名、全角空格、`（只读）+ 写动词`、白名单四个越权命令。

**B1/B4/B5 实现（v1.8）**：`--check-schedule "FREQ=HOURLY;INTERVAL=1"` 只认 HOURLY：`INTERVAL` **缺省按 RFC 语义等价 1，允许**；显式给出则必须为 1（`INTERVAL=0` 表示高频、`INTERVAL=2` 一律 `exit 2`），天维度直接 `exit 2`。**只验 rrule 本身**——「实际触发间隔 ≥60 分钟」与「时区 Asia/Shanghai」两项依赖调度器观测与参数，仍属人判（B1 等级 FULL 指 rrule 这一半）；`--check-oneshot <ISO8601>` 要求带时区、距今 `0 < Δ ≤ 3600s`——已过点（本该按 B5 归档）和超过 1 小时（本该走 B2 条件巡检）两头都在建之前堵住。

**B8/B15 实现**：`acquire_slot()` / `release_slot()` 在同一把跨进程锁内完成「回收死槽 + 计数 + 占位」，返回 `(slot_path, run_id)`，满员返回 `(None, run_id)` 由调用方记 `QUEUED`；`lease_acquire/heartbeat/release/reclaim` + `_lease_reclaimable()` 覆盖 `lease_valid / process_gone / pid_reused / identity_unreadable / run_id_mismatch / alive` 六种回收判定。回归含 6 进程同时抢槽（断言有效槽位 ≤3 且互不重叠）与租约六态。超时分两档（B15 硬要求）：执行体响应 cancel 且确实停下 → `TIMEOUT` 并释放租约；**执行体不响应取消、仍在跑 → `TIMEOUT_ORPHAN` 且保留租约**（此时释放租约等于放第二个实例进来，双跑比僵尸更危险）。心跳与超时由 `lease_run()` 落地：`HEARTBEAT_SEC=15`、`LEASE_SEC=60`、`RUN_MAX_SEC=3600` 三个常量可注入，回归用**按比例缩小的秒数**（如 lease 0.4s / heartbeat 0.1s / max 0.2s）验证同一套逻辑——证明「运行超过租约时心跳确实续期、不会被判过期」以及「超 `RUN_MAX_SEC` 时，执行体已停 → `TIMEOUT` + 释放租约，执行体不停 → `TIMEOUT_ORPHAN` + 保留租约」，不需要真等 15 分钟和 1 小时。

**B8 六条补充口径（v1.8，由真实超发事故倒推）**：

1. **抢不到锁 = 不启动**。`_file_lock(..., strict=True)` 超时抛 `LockTimeout`，`acquire_slot` 捕获后按 `QUEUED` 返回 `(None, run_id)`。旧版超时后打印 WARN 就「跳过加锁继续跑」，6 进程抢槽实测出现过 **6 个槽位同时被占**——并发上限这种硬约束上，「WARN 不等于阻断」是致命的。
2. **空/半截槽位 5s 内不得当死槽**。槽位是「先 `O_EXCL` 占名、再写内容」，读的人必然能读到半截 JSON；5s 内一律视作活槽。
3. **同进程多线程互斥用 `threading.Lock`，文件锁只管跨进程**（v1.8 补）。纯文件锁（`O_EXCL` + fsync + `os.remove`）在 Windows 上高竞争下极慢：4 线程 × 20 次读-改-写能把 strict 3s 超时全部吃满（实测死锁级挂起）。解法是加一层 per-path `threading.Lock`（`_thread_lock_for`），同进程线程先拿线程锁再拿文件锁；线程锁零 I/O、O(1)，天然不会因磁盘排队。文件锁仍保留，兜跨进程场景。`_update_json` 的落盘直接用 `_atomic_write`（不再嵌套 `_save_json` 的 `_file_lock`），消除嵌套锁开销。回归：`test_thread_lock_serialises_same_process`（同路径返回同一把锁且互斥）、`test_strict_update_raises_locktimeout_not_silent`、`test_strict_save_raises_locktimeout_not_silent`（strict 超时抛 `LockTimeout` 不静默写）。
4. **stale 锁回收的「判定」与「删除」必须原子化**（v1.8 补，第十五轮审查 F-02）。旧实现「`_lock_holder_stale()` 判定 → `os.remove()`」两步之间存在 TOCTOU：两个进程同时判定同一份残留锁可回收，先删后建的**新锁**会被迟到者按旧判定删掉，跨进程互斥失效（审查原话：「O_EXCL 只能保证创建时互斥，不能自动保证 stale 锁回收过程安全」）。修法：回收动作（守卫内**重新判定** + 删除）收进**内核文件锁守卫**（`_kernel_lock`：Windows `msvcrt.locking` / POSIX `fcntl.flock`，`_try_reclaim_stale`）——内核锁在持有进程死亡时自动释放，天然没有 stale 问题，避免了「回收互斥自身又需要 stale 回收」的递归坑。守卫内重新判定是关键：迟到的回收者此刻看到的是活锁（`_lock_holder_stale` 为假），拒绝删除。守卫文件（`<lock>.reclaim`）**永不删除**（内核锁载体，删了重建会各锁一个 inode）。同时把 `_file_lock` 默认翻成 `strict=True`，业务代码不显式写 `strict=False` 就拿不到宽松路径（误用边界，审查第 2 点要求）。回归：`test_reclaim_removes_only_stale_lock`、`test_reclaim_does_not_delete_fresh_lock`、`test_reclaim_race_two_threads_never_double_enter`、`test_reclaim_race_multi_process_mutual_exclusion`（**真两进程**同时回收同一份 stale 锁，断言临界区绝不重叠且事件数不丢）、`test_file_lock_defaults_to_strict`。
5. **B15-R 损坏租约恢复闸口**（第十轮审查要求，已落地）。租约读不懂就永不自动回收，会带来「永久占位」；解法不是加 TTL，而是留一条**需人批准且留痕**的通道 `lease_force_release(path, approval_path)`：

| 步骤 | 落地 |
|---|---|
| R1 记录原哈希/大小/路径/时间 | `_record_corruption()` 写 `corrupt-registry.jsonl`（append-only，带 sha256）。**顺序在 R3/R4/R6 之后**：前置检查没过就登记，会留下「已批准恢复」的假记录 |
| R2 同一把 strict 锁 | 闸口本身只做文件操作，不进入租约临界区；真正的写入路径仍由 `lease_acquire` 的 strict 锁兜住 |
| R3/R4 原实例身份与存活 | 能解析出 `pid` 且 `_proc_alive` 为真 → `process_alive` 拒绝（**不许凭「文件坏了」推断进程死了**） |
| R5 B7 状态核对 | 恢复前必须人工确认无「已提交未确认」的副作用（写进恢复记录，不由脚本猜） |
| R6 独立授权 | 批准文件必须有 `sha256 + approved_at + approver + reason`，缺一项即 `approval_incomplete` |
| R7 隔离归档 | `os.replace` 改名为 `<lease>.corrupt-<ts>`，**保留原字节** |
| R8 恢复后复核 | 隔离后复查原路径确已空出，否则 `quarantine_failed`（并补一条 `lease-recovery-failed / path_still_present` 审计行） |

**返回值是稳定枚举，不是错误消息**（第十三轮审查 F-01）：`reason` 只能是 `FORCE_RELEASE_REASONS` 之一——`absent / no_approval / approval_incomplete / unreadable / sha256_mismatch / process_alive / identity_unconfirmed / audit_registration_failed / quarantine_failed / lock_timeout / quarantined`。**禁止把异常文本拼进 reason**（原实现写过 `unreadable:%s`、`quarantine_failed:%s`、`quarantine_failed:path_still_present`）：调用方没法用等值比较分流，且换个 Python 版本错误消息一变、判定跟着变，等于把实现细节泄进对外契约。异常细节走 `_diag()` 打 WARN 日志。批准通过但隔离失败时，另补一条 `lease-recovery-failed` 行——否则审计上只看到 `approved_recovery`，看不到恢复其实没落地，等于谎报一次已完成的恢复。

**为什么不能只看 PID**：原执行体可能已派生子进程，或副作用已提交但未持久化确认；因此 R5 是人工步骤，脚本只负责「不让你在没有记录的情况下悄悄放行」。回归：`test_corrupt_lease_needs_approval_to_release`、`test_process_alive_blocks_force_release`、`test_corrupt_slot_is_registered`、`test_force_release_reason_is_stable_enum`、`test_force_release_unreadable_hash_is_stable`、`test_quarantine_failure_is_stable`、`test_rejected_recovery_leaves_no_audit_row_for_hash_and_identity`。
5. **瞬时文件禁止用 `_load_json` 读**。`_load_json` 会把「解析失败的非空文件」隔离改名（A21 对长期状态文件是对的），但用在槽位上等于**把别人刚占的槽位挪走**，文件名一空出来第二个进程立刻能占——这是超发的真正根因。瞬时文件走 `_read_ephemeral()`：读不懂就当它还在，不回收也不改名。租约同理：不可读 → 按占用处理，禁止回收。

> **B14/B15 编号说明**：v1.0 的 B14 是联动红线（无独立判定口径），审查据此判「B14 缺失」。v1.1 保留 B14 为红线表，把审查提出的「运行实例租约」独立为 **B15**——两类约束不同（红线是跨类禁令，B15 是调度器级生命周期），不合并。

---

## C. 现成配方

### C1 每小时收敛巡检（协调器值班）

```text
rrule: FREQ=HOURLY;INTERVAL=1        名称: 协调器 收敛 处置
prompt 骨架：
1) 只读盘点：--rotation、--list-all（单进程串行）、目标仓库 open PR/Issue
2) 若存在待处置项且本轮未处置过 → 原子领取后执行一次（每小时上限 1 次，B13）
3) 429 冷却中 → 只记录，不重投（A17/B14）
4) 结论追加 <工作目录>/watchdog.log（时间/对象/动作/结论/exit code）
5) 无待处置项 → 不输出，exit 0（B12）
```

### C2 回复落盘看门狗（寿命 ≤1h，名称：`协调器 回复 补采`）

```bash
python scripts/chatgpt_send.py --owner <conv_id>   # 先确认身份（A7）
python scripts/chatgpt_send.py --tail <conv_id>    # 无内容 → exit 1，属正常
# 只在 replies/ 缺该 conv 的 reply-*.md 时才补采；已有文件不覆盖
```

判定：`0` 且有文件 → 已落盘，静默；`1` → 回复未完成，**不重投**，等下个巡检点；`2`/`3` → 参数或出口问题，立即报。

### C3 到期处置（远期事项，避免天维度；名称：`到期项 处置 执行`）

```text
rrule: FREQ=HOURLY;INTERVAL=1
validFrom: 2026-12-01T00:00:00+08:00   validUntil: 2026-12-03T00:00:00+08:00
prompt：窗口外 → exit 0 静默（B3）；状态 DONE → exit 0；PENDING → 原子领取后执行，
        先落副作用证据再写 DONE（B2/B7）。
```

### C4 配额哨兵（429 零漏出；只读，名称：`配额 盘点（只读）`）

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
| **4 线程 ×20 次只落 60 条（v1.8 抓出）** | **A20** | 文档写「tmp 名唯一」，代码用固定 `.tmp`；`os.replace` WinError 32 打死线程 |
| **6 进程抢槽出现 6 个槽位（v1.8 抓出）** | **B8** | 锁超时后「跳过加锁继续跑」；半截槽位被当损坏隔离改名，文件名空出即被二次占用 |
| **租约文件不可读被当空租约删除** | **B15** | 读不懂的身份按「空」处理 → 放第二个实例进来；正确姿态是禁止回收 |
| **租约操作抢不到锁照样进临界区** | **B15** | `lease_acquire/lease_reclaim` 用非 strict 锁，超时后无锁写租约 = 自毁互斥；槽位那条修了、租约这条漏了——同一类错误要按「所有临界区」排查，不能只修报出来的那个 |
| **不可读槽位「超 5s」被当死槽删除** | **B8/B15** | 文件年龄不是进程死亡证据；只有 size=0（占名未写）才允许按时间回收 |

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
| 交付正文提取 / 回灌 | 同上 + `--extract` 与 `--verify-copy` 各跑一次 | A1/A2/A4 |
| automation 名称 / 排程 | 同上 + `--check-name`、`--check-schedule`、`--check-oneshot` | B1/B4/B5/B10/B11 |
| 排程口径 | `automation_update list` 全量盘点 | B1–B5 |

**写测试的硬要求**：任何「已修 bug」都要有能复现旧行为的用例，注释写明事故场景。**改了哪条基线，就要同步 `references/evidence-matrix.md` 的证据行**——矩阵由 `TestEvidenceMatrix` 校验，新增条款不补证据会直接红。当前 186 条。

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
| A23 要求校验 WebRTC 出口 | **v1.1 曾降级为提示，第二轮审查驳回 → v1.2 改回硬门禁并落地探针** | 驳回成立：实测浏览器 HTTP 走代理而 WebRTC UDP 漏出真 IP `59.34.82.8`；「取证难」不是「风险不存在」。现由 `webrtc_probe()` 判定，漏即 `exit 3` |
| B14 判为「原文缺失」 | **保留 B14 并新增 B15** | v1.0 有 B14（联动红线表），只是缺判定口径；两类约束不合并 |
| B11 白名单增 `--rotation/--list-all` | **采纳** | 两者均为只读，是 v1.0 文档内部矛盾（配方要求用、白名单没列） |

**审查轮次记录（同一会话 `6afe91c4`，全程思考模式）**：

| 轮 | 受审版本 | 裁定 | 结论要点 |
|---|---|---|---|
| 1 | v1.0 | `NEEDS-REVISION`（A 11 PASS / 12 WEAK / 2 MISSING；B 0 / 10 / 4） | B 类缺可执行判定；A 类多处「WARN 当阻断」 |
| 2 | v1.1 | `NEEDS-REVISION`（8 个开放项） | A22 进程身份、A23 WebRTC、B6 路径类型、B10 命名正则、A27 硬失败、A26/A18 403 分域、B8 原子领槽、B15 实例身份 |
| 3 | v1.2 | `NEEDS-REVISION`（4 项 PARTIAL） | 抓到真矛盾：A23 正文仍把已证无效的 `--force-…` 当必需参数；B10 缺语义映射；B8/B15 缺实现与竞争回归证据 |
| 4 | v1.3 | `NEEDS-REVISION`（2 项 PARTIAL + 2 处文档矛盾） | B10 贪婪正则放行四段以上；B15 缺心跳/超时验证；测试数 64 vs 90 不一致 |
| 5 | v1.4 | `NEEDS-REVISION`（2 项 PARTIAL + 版本标记冲突） | 动作词未要求整段相等；超时测试未证明执行体已停；开头 v1.1 / 结尾 v1.4 混用 |
| 6 | v1.6 | `NEEDS-REVISION`（3 项） | 配方里的 automation 名称本身违反 B10；B4「超时释放租约」与 B15 orphan 保留冲突；轮次表不全 |
| 7 | v1.7 | `NEEDS-REVISION`（2 项纯一致性） | B15 主条款仍写「超时释放租约」，与实现段 orphan 保留冲突；标题 v1.6 / 结尾 v1.7 |
| 8 | v1.7 | **`PASS`** | B15 两档语义一致、版本号统一，无新矛盾 |
| 9 | v1.8 | `NEEDS-REVISION`（6 项 P1 + 2 项 P2） | 攻击面换成「可判定」本身：证据矩阵只验证用例名存在、不验证断言覆盖（A2/A20/B5 三处确认夸大）；`--fence` 是位置索引不是交付身份；B1 缺 INTERVAL 与另两项；**B15 的 `lease_acquire/lease_reclaim` 仍用非 strict 锁，拿不到锁照样进临界区**；B5 存量归档没实现却挂了实测证据；不可读槽位「超 5s 按年龄回收」与「身份不明禁回收」自相矛盾 |

| 10 | v1.8 修订 | `NEEDS-REVISION`（返修要求 4 条 + 新发现 N1–N6） | A20/B15 的 FULL 名不副实（缺故障注入与异常路径断言）；损坏槽位/租约没有统一登记；缺 B15-R 完整恢复闭环；等级元测试仍只校验字符串；`lease_acquire` 不区分 `lease_conflict` 与 `lock_timeout` |

| 11 | v1.8 修订 | `NEEDS-REVISION`（A20/B15 仍判 PARTIAL；R2 锁未实现；登记失败未阻断） | 只认贴出来的断言，不认矩阵里的用例名；TOCTOU、审计失败、身份不可读三点均属实现未闭环 |

**第十一轮改了什么**：见下方第十轮表格末尾 7 行（A20/B15 降级、审计阻断、R2 加锁、身份证据、超时保留字节、同目录与目录 fsync 断言）。

| 12 | v1.8 修订 | `NEEDS-REVISION`（1 项实现级：审计登记顺序） | 八项返修全部核销，A20/B15 的 PARTIAL 声明被判「成立」；唯一实现缺陷是**先登记恢复、再做存活/身份检查**，被拒绝时留下假「已批准」记录 |

**第十二轮改了什么**：`lease_force_release` 把登记移到「哈希 → 存活 → 身份证据」全部通过之后；返回值枚举补全（`identity_unconfirmed` / `audit_registration_failed` / `lock_timeout` / `quarantine_failed`）；新增 `test_rejected_recovery_leaves_no_fake_audit_row` 断言被拒绝时 registry 里没有 `lease-recovery` 行。

| 13 | v1.8 修订 | `NEEDS-REVISION`（F-01 唯一实现级：返回值枚举不一致） | 假恢复登记缺陷**核销**（「`process_alive` 与 `identity_unconfirmed` 两种拒绝路径均发生在审计登记之前……原先指出的先登记后检查缺陷已经修复」）；只剩 reason 里拼异常文本（`unreadable:%s` / `quarantine_failed:%s` / `quarantine_failed:path_still_present`），与文档枚举不符 |

**第十三轮改了什么**：① `reason` 收敛为 `FORCE_RELEASE_REASONS` 稳定枚举，三条拼异常文本的返回全部改成 `unreadable` / `quarantine_failed`，细节改由 `_diag()` 打 WARN；② 批准通过但隔离失败时补一条 `lease-recovery-failed` 审计行，避免 registry 里只剩 `approved_recovery`、看起来像恢复已落地；③ 新增四条用例把「枚举稳定性」本身变成闸口：`test_force_release_reason_is_stable_enum`（七类拒绝路径逐一断言 `reason ∈ 枚举` 且不含 `:`）、`test_force_release_unreadable_hash_is_stable`、`test_quarantine_failure_is_stable`、`test_rejected_recovery_leaves_no_audit_row_for_hash_and_identity`。

| 14 | v1.8 修订 | **`PASS`** | F-01 核销；无新实现级缺陷；三条残余风险（PID 复用/派生进程/检测时序不可穷举、哈希校验与隔离之间的跨进程竞态、补记行自身写失败未被证明）均属已披露的 PARTIAL 边界 |

| 15 | v1.8 修订 | `NEEDS-REVISION`（F-02 唯一实现级：stale 锁回收 TOCTOU） | 第十四轮 PASS 后按「继续挖 flaky」动作抓到并发丢更新真 bug（4 线程×20 次只落 45/10，伴随「跳过加锁」WARN）；修复过程分两层（两层锁 + fail-closed）提交审查，审查认可两层锁设计方向，但抓到 stale 回收竞态：「判定 stale → `os.remove()`」两步间另一进程可能已删旧建新，迟到者把**新锁**删掉 = 跨进程互斥失效；另要求补真多进程竞争测试、约束非 strict 误用 |

**第十五轮改了什么**：① stale 回收改为「内核文件锁守卫内**重新判定** + 删除」（`_try_reclaim_stale` + `_kernel_lock`：msvcrt.locking/fcntl.flock，进程死亡自动释放，无递归 stale 问题）；迟到的回收者守卫内看到活锁即拒绝删除；守卫文件永不删除。② `_file_lock` 默认翻 `strict=True`（误用边界）。③ 补五条回归：`test_reclaim_removes_only_stale_lock` / `test_reclaim_does_not_delete_fresh_lock` / `test_reclaim_race_two_threads_never_double_enter` / `test_reclaim_race_multi_process_mutual_exclusion`（**真两进程**竞争，断言临界区绝不重叠、事件数不丢）/ `test_file_lock_defaults_to_strict`。

| 16 | v1.8 修订 | `NEEDS-REVISION`（F-03 唯一实现级：进程存活二态误判） | F-02 核销，回收守卫设计成立，守卫文件永久保留被判「接受」；但抓出 `_proc_alive()` 把「进程不存在」与「无权限查询/系统调用失败」折叠成同一个 False，`_lock_holder_stale` 一看 False 就判死回收——「查不到」被当成「死了」，活锁会被误删（内核守卫只保证回收者互斥，纠正不了错误的死亡判定） |

**第十六轮改了什么**：① `_proc_alive` 二态 → `_proc_state` 三态（`PROC_DEAD` / `PROC_ALIVE` / `PROC_UNKNOWN`），只有确认 DEAD 才可回收；UNKNOWN（EPERM / ERROR_ACCESS_DENIED / 查询失败 / pid 不可解析）一律按「可能还活着」保守处理。Windows 按 `OpenProcess` 错误码分桶（`_win_open_error_to_state`：87/1168 → DEAD，5 等 → UNKNOWN），POSIX 按 errno 分桶（ESRCH → DEAD，EPERM 等 → UNKNOWN）。② 三条回收路径同步阻断：`_lock_holder_stale`（UNKNOWN 不判 stale）、`_lease_reclaimable`（UNKNOWN → `identity_unreadable` 禁回收）、`lease_force_release`（UNKNOWN 无 `identity_evidence` → `identity_unconfirmed` 拒绝）。③ 补四条回归：`test_proc_state_three_way_semantics` / `test_proc_state_unknown_blocks_lock_reclaim` / `test_proc_state_unknown_blocks_lease_reclaim` / `test_proc_state_unknown_blocks_force_release`；多进程竞争测试加启动屏障（两进程同起跑才构成真实竞争）。

| 17 | v1.8 修订 | `NEEDS-REVISION`（F-04 / F-05 / F-06 三项） | F-03 三态核心判「基本修好」；但抓出三个绕过点：**F-04** 假值 pid（0/None/空串/垃圾串）被当旧格式退化 mtime 判据，可借 mtime 误删；**F-05** `lease_force_release` 用 `if pid` 短路，pid 缺失/假值时跳过状态判定与 `identity_evidence` 检查，身份不完整的可读租约只凭 approval 就能进强制释放；**F-06** ctypes 未声明 `OpenProcess` restype=HANDLE，64 位下句柄被 c_int 截断，后续查询全失败并被折叠成「进程不存在」 |

**第十七轮改了什么**：① **F-04**：`_lock_holder_stale` 改为只认「无 pid **字段**」为老格式（mtime 判据），字段存在但值假/不可解析一律走 `_proc_state` → UNKNOWN → 不回收；② **F-05**：`lease_force_release` 对可读租约**无条件**三态判定（`_proc_state(info.get("pid"))` 对 None/0/垃圾天然返回 UNKNOWN），UNKNOWN 无 `identity_evidence` 即拒，不再有 `if pid` 隐式短路；③ **F-06**：新增 `_win_k32()` 集中声明 ctypes 原型（`OpenProcess.restype = HANDLE`、`GetExitCodeProcess`/`GetProcessTimes`/`CloseHandle` 的 argtypes/restype），`_proc_state` 与 `_proc_start_time` 共用；补 `test_win_k32_declares_handle_prototypes`（断言 restype + 真实句柄往返：开当前进程→查退出码 STILL_ACTIVE→关句柄→读启动时间）。④ 补六条回归（180→186）：`test_lock_pid_field_falsy_not_reclaimed_by_mtime` / `test_lock_pid_field_garbage_not_reclaimed` / `test_lock_no_pid_field_uses_mtime`（老格式兼容保留）/ `test_force_release_missing_pid_needs_identity_evidence`（含带 identity_evidence 放行的正路径）/ `test_force_release_pid_zero_needs_identity_evidence` / `test_win_k32_declares_handle_prototypes`。

**第十四轮之后补的一条**：审查把「`_record_corruption("lease-recovery-failed")` 自己失败会怎样」列为残余风险 3，虽不构成裁定理由，但这是能离线证明的，于是补 `test_quarantine_failed_row_write_failure_is_survivable`（第一次登记成功、第二次返回 `None`，断言不抛异常、仍返回 `quarantine_failed`、租约原样在）。**属于 PASS 后的加证，不改变任何条款口径**，故不重开一轮审查。

**第十轮改了什么（逐条对应）**：

| 审查项 | 处置 |
|---|---|
| A20 FULL 名不副实 | 补 `fsync` / `os.replace` 双层故障注入用例，并把 `fsync(dir)` 真正写进 `_atomic_write`；六项保证逐项列表给出断言载体（见 A20 段） |
| B15 FULL 名不副实 | 补 `test_lease_ops_never_run_unlocked`（四个入口超时都拒绝）、损坏登记、B15-R 闸口；心跳/释放超时断言「租约文件仍在」 |
| 损坏文件无登记 | 新增 `_record_corruption()`，槽位/租约/恢复三个入口统一写 `corrupt-registry.jsonl`（带 sha256/大小/时间），用例断言登记内容 |
| 缺恢复闭环 | 落 `lease_force_release()` + R1–R8 表；`process_alive` 拒绝、批准四字段缺一不可、隔离改名保留原字节 |
| 等级元测试只校验字符串 | 保留字符串校验，同时把「六项保证 ↔ 用例」写成表格（人可核）；FULL 的语义真实性仍靠对照表，不再声称测试能自动证明 |
| `lease_acquire` 失败原因混同 | 日志区分 `lease_conflict`（有效租约）与 `lock_timeout`（抢不到锁）；两者都拒绝启动，但审计可分辨 |
| 第十一轮：A20/B15 仍不够 FULL | **接受**：两条同时降为 PARTIAL，并在矩阵写明不可离线证明的部分（Windows 目录 fsync、断电持久化、PID 复用/子进程/检测时序）。不再争 FULL |
| 第十一轮：审计登记失败未阻断 | `_record_corruption()` 返回 `None` 表示写失败；`lease_force_release` 检查到 `None` 即 `audit_registration_failed` 拒绝恢复；用例 `test_force_release_blocks_when_audit_registration_fails` |
| 第十一轮：R2 未真正加锁 / TOCTOU | 整段恢复进 `_file_lock(strict=True)`；抢不到锁返回 `lock_timeout`；用例 `test_force_release_refuses_without_lock` |
| 第十一轮：身份不可读被当死亡 | 不可读时必须有批准人给出的 `identity_evidence`，否则 `identity_unconfirmed`；用例 `test_force_release_needs_identity_evidence_when_unreadable` |
| 第十一轮：心跳/释放超时未断言文件仍在 | `test_heartbeat_and_release_timeout_keep_lease_bytes` 断言「文件仍在 + 字节不变」 |
| 第十一轮：A20 缺同目录与目录 fsync 断言 | `test_tmp_is_created_in_target_directory`（spy `os.replace` 取 src 目录）；`test_atomic_write_fsyncs_parent_directory`（POSIX 专用，Windows 记 skip） |

**第九轮改了什么（逐条对应）**：

| 审查项 | 处置 |
|---|---|
| 证据只验名字不验断言 | 矩阵**加等级列** `FULL/PARTIAL/CODE_ONLY/HUMAN`，并加 `test_levels_are_consistent_with_evidence`：FULL 必须有用例、CODE_ONLY 不许有 test:、PARTIAL 必须写清缺哪一半。按等级统计：FULL 21 / PARTIAL 10 / CODE_ONLY 5 / HUMAN 13 |
| A2 的 `--fence` | **降级为非合规辅助**，A2 合规路径只剩 `--between`；矩阵 A2 改 PARTIAL 并写明理由（未直接删功能，因为「上游按序号指定」确有其事） |
| A20 证据不符 | 补 `test_atomic_write_uses_unique_tmp_names`（4 线程各写一次断言 tmp 名互不相同）与并发不丢更新的 4×20 用例，A20 升 FULL |
| B1 严宽不一 | 文档改为「INTERVAL 缺省按 RFC 等价 1，允许；显式必须=1」；并声明实际间隔与时区仍是人判，B1 降 PARTIAL |
| B15 无锁进临界区 | `lease_acquire/lease_reclaim/lease_heartbeat/lease_release` 全部 `strict=True`，`LockTimeout` 分别转成「冲突不启动」与「不回收（lock_timeout）」；新增 `test_lease_ops_never_run_unlocked` |
| B5 证据夸大 | 拆成两件事：创建前预防（机器）+ 存量归档（人工），B5 降 PARTIAL |
| 不可读槽位按年龄回收 | 改为**只有 size=0 且超 5s 才回收**；非空但读不懂 → 永不自动回收；新增 `test_unreadable_nonempty_slot_is_never_auto_reclaimed` 与 `test_empty_slot_older_than_ttl_is_reclaimed` |
| 未来日期 | 矩阵补日期口径：`live:` 按本机 UTC+8 记，校验取 `time.strftime` 本地日期，不按 UTC 折算 |

**多轮对抗审查下来最值钱的一条**：审查对 A23 的「取证难 ≠ 风险不存在」的驳回，直接换来一次真实泄漏的发现——只带 `--proxy-server` 时浏览器 HTTP 出口是 `152.175.33.8`，WebRTC 候选却是本机 `59.34.82.8` + IPv6。**防御性强的审查不是障碍，是外部审计。**

**审查提出的三条易忽略语义（已并入正文）**：①「WARN 不等于阻断」→ A7/A11 改为 WARN + 非 0 退出；②「文件存在 ≠ 业务完成」→ B2/B7 要求副作用与标记绑定；③「PAUSED ≠ 已停止」→ B5/B9 区分调度状态与 run 实例状态。

---

## G. 证据矩阵（把「可判定」逐条对账）

`references/evidence-matrix.md` 是 §0 的延伸：**凡是声称「可判定」的条款，必须指得出是哪一条用例、哪一个符号、哪一次实测；指不出的，就写「人判」并给理由。** 不许用「文档里已经规定了」冒充机制。

矩阵的四类证据与校验方式：

| 类型 | 写法 | 校验 |
|---|---|---|
| 用例 | `test:test_xxx` | 用例名必须存在于 `tests/test_hardening.py` |
| 符号 | `code:func_name` | 必须在 `scripts/*.py` 里找得到 `def/class/赋值` |
| 实测 | `live:YYYY-MM-DD` | 日期不得晚于今天 |
| 人判 | `human:<理由>` | 理由非空 |

**矩阵自身有回归**：`TestEvidenceMatrix` 三条断言——条款集合 = 矩阵行集合（不漏、不多、不重复）；每个 `test:`/`code:` 引用都能解析；每行至少一种证据。它的作用是让「文档漂移」变成红灯，而不是等下一次审查才被发现。

**当前分布（49 条，按等级）**：**FULL 20 条**（核心判定 + 失败分支都有独立断言）；**PARTIAL 12 条**（含 A20/B15：跨平台持久化与进程身份无法离线穷举，见矩阵）；**CODE_ONLY 4 条**（A7 A9 A15 A18，有实现无回归）；**HUMAN 13 条**（A0 B0 B2 B3 B6 B7 B9 B13 B14 C1–C4）。**只有 FULL 才算「已机械化」**——按「有没有 test:」计数会系统性高估，这是第九轮审查的结论。

**已知缺口（按危害排序，写着就是为了下次被追）**：

1. **B2/B7 三态状态机没有共享实现**——每个任务各写一份，最容易在续跑时重复提交副作用；这是目前最大的机械化空白。
2. **B6 路径类型校验只在 prompt 里**——工作目录写错时，报错发生在半小时后的业务步骤，而不是 preflight。
3. **A7 缺「显式账号与登记冲突退 2」的回归**——代码路径存在，没有用例钉住。
4. **B9/B13/B5 的「已存在」判定依赖接口**——本地只能判「不该建」，判不了「该归档」。

补上一条，就把它从人判列移到测试列；这是唯一被承认的收敛方向。
