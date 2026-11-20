# 基线与纪律（可判定版 v1.8）

本文件把 `chatgpt-session-ops` 跑过的真实事故与 WorkBuddy automation 的真实运行日志，收敛成**带编号、带判定口径、带违例后果**的基线。SKILL.md 讲「怎么做」，本文件讲「什么算做对、什么算做错」。

**版本**：v1.8（2026-11-19）。v1.0 起经 ChatGPT 思考模式**四十二轮对外对抗审查**逐条改写（第三十三～四十二轮为收敛后终检自检，其余为对外会话裁定；每轮裁定见 §F；**第二十五轮 `PASS`——锁子系统全文最终确认·收敛**，第二十二轮 `PASS`（F-10 残留定向复审），**第十九轮 `PASS`**（锁/进程身份子系统），第十四轮 `PASS`（B15-R 全文），第八轮亦 `PASS`，其余均为 `NEEDS-REVISION` 后返修；第九轮把「可判定」本身当成攻击面；第十五至二十三轮分别抓出 stale 锁回收 TOCTOU（F-02）、进程存活二态误判（F-03）、假值 pid 绕过（F-04/05）、ctypes 句柄截断（F-06）、int() 折叠与损坏内容借老格式逃生（F-07/08）、锁元数据写入失败进临界区（F-09）、损坏启动时间误判 pid_reused（F-10）、归档覆盖（F-11）、超长 pid 串 int() 异常逃逸（F-07 残留）、**收割/对账解析器畸形 JSON 崩溃（F-12，41 处）**、**落点不一致只 WARN 导致会话身份与回复归属分裂（F-13）**、**RRULE 校验语义 ≠ 下发语义（F-14）**、**解析边界与证据保全缺口（F-15～F-19）**、**落点正则绕过与回复完整性 fail-open（F-13/F-16 残留 + F-23～F-25）**、**浮点精度与首尾换行边界（F-12c/F-22 残留 + N-01/N-02）**、**对账器跨角色畸形漏检（D-01）**——前 11 组已在第二十五轮核销，**F-12～F-25 + D-01 全部核销，三十二轮对抗审查收敛**），**未采纳项与理由见 §F**，不静默丢弃。**每条判定口径的证据载体（用例名/符号/实测日期/人判理由）见 `references/evidence-matrix.md`，该矩阵由回归校验，引用不存在的用例会直接红。**当前 308 条回归。

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
| 5 | 改代码了吗 | `python -m unittest discover -s tests` | 全绿（当前 308 条） |

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
| **A10** | 发进既有会话**必带** `conversation_id`，且**落点必须一致** | 请求体含该字段；发送后 SSE **首个带 `conversation_id` 的** `data:` 事件的 id 与目标**相等**；同一响应出现两个不同 id → 无法确认落点，**判失败**；**禁止正文正则兜底**——落点只信 SSE 首个结构化事件里的 id，不得从 `[DONE]` 之后或整包正文捞 id；落点不一致 → **判失败**（不返回成功、不归档为目标会话的正常回复）、回复写入**独立隔离归档**（**目录在 `replies/` 之外**，记 `target_conv_id`/`actual_conv_id`/异常原因；**完整原始响应**另存并记路径/长度/sha256；**隔离文件名必须避唯一名**——同秒同对会话的两次异常投递不得互相覆盖证据）、实际落点会话另记**待认领登记**（**不自动登记归属**，人工确认后再登记）、失败返回**携带两个 id**，**禁止静默改归属**；`[DONE]` 是**解析终点**，其后的数据不参与落点证明；同一响应出现多个 id → 失败关闭且**带候选 id 列表** | 消息静默落入全新会话（已发生事故）；回复落在别的会话却被当成 A 的回复继续操作 |
| **A11** | 200 即停止轮转 | 200 后立刻停止；id 解析顺序 SSE（**首个带 id 的事件**，坏事件跳过但不得改用后面的 id）→ 整包 JSON → 严格字段匹配；只接受**唯一、非空、符合 A19 白名单**的 id；解析不出、不合规、或同一响应出现多个不同 id → **返回 1、不落盘、不重投**（不许回落到传入 id 粉饰成功）；失败结果**带 target 与候选 id**，不得把诊断信息丢掉 | 同一 prompt 建出多个会话 |
| **A12** | 大交付用 raw mapping 核对 | GET 全量 → 按原始顺序取目标 assistant 全部 parts → 记录 `message_id / part数 / 各part字符数 / 拼接 SHA-256`；与 `DONE chars` 同口径比对；缺 part、重复消息、口径不一致 → 判**不完整**（结论：补采，不是要模型重发）；保留原始 mapping 备查 | 流截断与真实生成失败无法区分 |
| **A13** | 回复默认全文落盘（SSE 不完整时内容照收但**归档文本必须标注「完整性未证明」**，不得冒充定稿） | `CHATGPT_REPLY_MAX_CHARS` 默认 0；非 0 时文件头必须写明截断点与原始长度 | 归档只留尾巴，结论丢失 |
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
| **A21** | 状态 json 损坏**不得**以空状态继续写 | 解析失败 → 原子隔离为 `<name>.corrupt-<ts>`，记录原文件 SHA-256/大小/异常 → 状态置 `STATE_CORRUPT`；此状态下**禁止改业务状态、禁止以空状态写**；只能从有效备份恢复，或由**独立批准标记文件**（含批准时间+原文件哈希）初始化；两者完成前不得解除。**「损坏」含顶层类型坏**（F-34）：合法 JSON 但不是预期形状（手改成 `[]`/`"x"`）与语法坏同罪同罚——否则同一份不可信输入两种待遇（`conv_owner` 有守卫、`record_conv` 没有） | 下次落盘把 `conv-accounts` 全表清空 |
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

**状态文件字段形状坏 = 「没有这个值」，不是崩溃（F-34，第三十九轮）**：`_load_json` 只挡了语法坏，**合法 JSON、字段类型坏**（`cursor="abc"`、`created="x"`、`order=5`、`sessions="x"`）会原样穿进 `_normalize_rotation`/`is_unavailable`/`record_anon_session`——`ValueError`/`OverflowError`/`TypeError`/`AttributeError` 一路逃逸，且 `int(3.5)==3`、`int(True)==1` 是 F-07 明令禁止的「把非法折叠成合法」。硬要求：① 收敛族完备——`_as_dict`/`_as_seq`/`_as_float`/`_as_text`/`_as_int` 五件套，任何不可信字段先收敛再用；`_as_int` 只认「非 bool 的 int」与「纯十进制数字串」，不折叠 float/bool。② 读-改-写入口（`_normalize_rotation`）必须**全函数收敛、永不抛异常**，形状不符就当「没有这个值」（F-12 口径）。③ 公开 API 各自兜底（`is_unavailable`/`purge_expired`/`mark_unavailable`/`rotation_plan`/`load_anon_sessions`/`record_anon_session`/`mark_anon_archived`/`anon_session`），不假设调用方先 normalize——「文档承诺返回值就是契约」（F-32 教训）在形状维度的复用。④ 顶层类型坏进 A21 的隔离通道（与语法坏同罪）。

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

**A27 实现口径（实测）**：`GET /backend-api/models` 里同一标题有**两个 slug**，带 `-t-` 的 `max_tokens=262144`、不带的是 `34834` —— `-t-` 即 thinking 变体；默认会话发 `"model": "auto"`（不思考）。脚本 `--thinking`（或 `CHATGPT_THINKING=1`）把 body 的 `model` 换成解析出的 `-t-` slug。解析不出时抛 `ThinkingModelUnavailable`（**不再提供 `CHATGPT_ALLOW_NON_THINKING` 降级口子**，见下），由调用方按作用域处置：默认轮转视为**该身份不可用**并换下一个，四个登录态全不可用才 `exit 1`；显式 `--account` 则直接失败、不换号。三种原因码必须可区分——`AUTH_INVALID`（models 端点 401）、`MODEL_ENDPOINT_UNAVAILABLE`（非 200/网络异常/畸形 JSON）、`THINKING_MODEL_UNAVAILABLE`（清单正常但无 `-t-` 变体）。失败结果**不写入** `_THINKING_MODEL_CACHE`，只有成功解析才缓存（旧实现把失败缓存成 `"auto"`，同进程内后续重试再也拿不到真值）。浏览器侧按钮靠 `innerText === "思考"`（英文 UI `Thinking`）命中，未开时 `aria-pressed="false"`，点一次变 `true` 且 class 增加 `accentSubtle-*`；`enable_thinking()` 幂等返回 `ALREADY / ENABLED / NO_TOGGLE / FAILED_TO_TOGGLE`。**顺序：先开思考再填词**——切换模型可能重置输入框。关闭：`CHATGPT_NO_THINKING=1`。代价：单条 5–19 分钟，`_idle` 预算要放宽（A16）。

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
| **B1** | 周期任务固定 1 小时 | `rrule` 必须 `FREQ=HOURLY;INTERVAL=1`；禁止 `DAILY/WEEKLY/MONTHLY`；**禁止用 `INTERVAL<1` 表示高频**；**禁止重复 rule part**（RFC 5545 §3.3.10，如 `FREQ=DAILY;FREQ=HOURLY` 一律拒绝）；`INTERVAL` 按 RFC 十进制整数严格解析、不得含内部空白（**不调用 `int()`**——超长十进制串会让 `int()` 抛 ValueError 逃逸；rule part 正则**不得配 `re.S`**，换行不得当 VALUE 放行）；**下发必须是校验后重建的规范化 RRULE**，不得下发用户原始串；**唯一可执行字段是 `rrule`**，原始串只作审计留档；枚举值大小写不敏感（`freq=hourly` 合法，规范化为 `HOURLY`）；同一 automation 实际触发间隔 ≥60 分钟；时区统一 `Asia/Shanghai`，缺失即禁止启用 | 天维度过久 / 语义漂移；校验按「最后一个同名 key」而下发按「第一个」——周期被确定性绕过 |
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

**B10/B11 实现（v1.8）**：`--check-name "<名称>" [--argv "<命令串>"]` 把上面两张表落成代码——分词判段数、动作词与词表**整段相等**比对、只读名的 argv 必须全部命中 `READONLY_ARGV`；任一条不成立 `exit 2`。别再自己写正则：`startswith("--rotation")` 会放行 `--rotation-reset`，`查看并删除` 会被包含匹配当成只读。回归 `TestScheduleContract`（26 条）覆盖四段名、全角空格、`（只读）+ 写动词`、白名单四个越权命令，以及 RRULE 的重复属性/内部空白/严格十进制/规范化下发。

**B1/B4/B5 实现（v1.8，第二十七轮修订）**：`--check-schedule "FREQ=HOURLY;INTERVAL=1"` 只认 HOURLY：`INTERVAL` **缺省按 RFC 语义等价 1，允许**；显式给出则必须为 1（`INTERVAL=0` 表示高频、`INTERVAL=2` 一律 `exit 2`），天维度直接 `exit 2`。**校验语义必须等于下发语义**（F-14）：①解析阶段**拒绝任何重复 rule part**（旧版用字典解析、同名取最后一个，`FREQ=DAILY;FREQ=HOURLY` 校验通过却下发原始串——调度器取第一个就变成 DAILY）；②**下发用已验证字段重建的规范化 RRULE**（`FREQ=HOURLY;INTERVAL=1`），不再下发用户原始串；③`INTERVAL` 按 RFC 十进制整数严格解析（`int()` 会接受 `1_0` 这类串，必须用正则）；④**拒绝内部空白**（旧版 `.replace(" ", "")` 会把 `FREQ=HO URLY` 悄悄修补成合法输入）；⑤**枚举值大小写不敏感**（RFC 5545 §2）——`freq=hourly` 合法，接受后规范化为 `HOURLY`，大小写本身不是违规，不能拿来当拒绝理由。**只验 rrule 本身**——「实际触发间隔 ≥60 分钟」与「时区 Asia/Shanghai」两项依赖调度器观测与参数，仍属人判（B1 等级 FULL 指 rrule 这一半）；`--check-oneshot <ISO8601>` 要求带时区、距今 `0 < Δ ≤ 3600s`——已过点（本该按 B5 归档）和超过 1 小时（本该走 B2 条件巡检）两头都在建之前堵住。

**B8/B15 实现**：`acquire_slot()` / `release_slot()` 在同一把跨进程锁内完成「回收死槽 + 计数 + 占位」，返回 `(slot_path, run_id)`，满员返回 `(None, run_id)` 由调用方记 `QUEUED`；`lease_acquire/heartbeat/release/reclaim` + `_lease_reclaimable()` 覆盖 `lease_valid / process_gone / pid_reused / identity_unreadable / run_id_mismatch / alive` 六种回收判定。回归含 6 进程同时抢槽（断言有效槽位 ≤3 且互不重叠）与租约六态。超时分两档（B15 硬要求）：执行体响应 cancel 且确实停下 → `TIMEOUT` 并释放租约；**执行体不响应取消、仍在跑 → `TIMEOUT_ORPHAN` 且保留租约**（此时释放租约等于放第二个实例进来，双跑比僵尸更危险）。心跳与超时由 `lease_run()` 落地：`HEARTBEAT_SEC=15`、`LEASE_SEC=60`、`RUN_MAX_SEC=3600` 三个常量可注入，回归用**按比例缩小的秒数**（如 lease 0.4s / heartbeat 0.1s / max 0.2s）验证同一套逻辑——证明「运行超过租约时心跳确实续期、不会被判过期」以及「超 `RUN_MAX_SEC` 时，执行体已停 → `TIMEOUT` + 释放租约，执行体不停 → `TIMEOUT_ORPHAN` + 保留租约」，不需要真等 15 分钟和 1 小时。

**租约 API 的返回值是契约，磁盘失败不得破约（F-32，第三十八轮）**：四个对外函数承诺确定结局，`OSError`（磁盘满 WinError 112 / 只读盘 / 权限）必须落成承诺值，诊断细节走 `_diag()` 只进日志：

| 函数 | 承诺 | 磁盘失败时 | 为什么是这一侧 |
|---|---|---|---|
| `lease_acquire` | `run_id` 或 `None` | `None` | 没写成 = 没拿到租约。带病继续跑会绕过 B15 单实例保证 |
| `lease_heartbeat` | `bool` | `False` | 线程里没人接异常 → 线程静默死亡、租约悄悄过期 |
| `lease_release` | `bool` | `False` | 保守侧：宁可留僵尸等人工闸口，也不谎报已释放（调用方记 `TIMEOUT_ORPHAN`） |
| `lease_reclaim` | `(bool, 稳定枚举)` | `(False, "write_failed")` | 自动化巡检入口：抛异常会崩掉整轮，而不是跳过这一份继续看下一份 |


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

**写测试的硬要求**：任何「已修 bug」都要有能复现旧行为的用例，注释写明事故场景。**改了哪条基线，就要同步 `references/evidence-matrix.md` 的证据行**——矩阵由 `TestEvidenceMatrix` 校验，新增条款不补证据会直接红。当前 308 条。

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

| 18 | v1.8 修订 | `NEEDS-REVISION`（F-07 / F-08 两项） | F-04/05/06 被判「主要修复均已落地」；但抓出两个新绕过点：**F-07** `_proc_state` 用 `int(pid)` 折叠非法输入（`int(999999.9)==999999`、`int(True)==1`），非法 pid 会被折成合法 pid 查死活，可能借截断触发回收；**F-08** `_lock_holder_stale` 把「JSON 损坏/读取失败/顶层非字典」与「老格式无 pid 字段」合并进同一条 mtime 兼容路径，损坏数据借老格式逃生 |

**第十八轮改了什么**：① **F-07**：新增 `_parse_pid()` 严格解析（只认正整数 int 且拒 bool、纯十进制数字串；float/bool/带空白串/0x 串/0/负数一律 None），`_proc_state` 与 `_proc_start_time` 共用，float/bool pid 一律 UNKNOWN。② **F-08**：`_lock_holder_stale` 读取段重写——OSError（读失败）/ ValueError（JSON 损坏）/ 顶层非字典 → 保守不回收；只有「成功解析的字典且无 pid 字段」与「空文件（老格式锁的字面形态）」走 mtime 兼容。③ 补六条回归（186→192）：`test_parse_pid_rejects_lossy_coercion`（12 个折叠通道逐一断言 None）/ `test_proc_state_rejects_float_and_bool_pid` / `test_lock_float_pid_not_reclaimed`（事故复现：999999.9 截断成 999999 判 DEAD 误删）/ `test_lock_corrupt_json_not_reclaimed_by_mtime` / `test_lock_non_dict_json_not_reclaimed` / `test_lock_unreadable_not_reclaimed`（路径是目录 = 读失败）。

| 19 | v1.8 修订 | **`PASS`** | F-07/F-08 均判「已修复」；空文件 mtime 兼容取舍被明确接受（「区分解析失败与历史格式本身没有身份内容……我接受这个取舍」，但要求措辞上不得与新格式三态身份验证等同）；无新确定性实现级缺陷。残余风险 2 条：①历史空锁/无 pid 锁的 mtime 兼容回收不具备身份级证明（退化恢复机制）；②进程状态查询与回收之间存在不可完全消除的系统级时序窗口。裁定范围限已展示路径（`_try_reclaim_stale`/`_lease_reclaimable`/`lease_force_release` 未全文展示），最终收敛以全文复审为准 |

**第十九轮遗留的口径修正（PASS 后按审查要求改措辞，不重开审查）**：空文件/无 pid 字段老格式锁的 mtime 兼容回收是**历史格式的退化恢复机制**，不具备与新格式三态身份验证（`_proc_state` DEAD/ALIVE/UNKNOWN）同等的保证——文档各处不得把两者等同描述。

| 20 | v1.8 修订 | `NEEDS-REVISION`（F-09 / F-10 / F-11 三项 + F-08 补充） | 全文复审 `_try_reclaim_stale`/`_lease_reclaimable`/`lease_force_release` + `_file_lock`。F-02 内核锁守卫、F-03 三态判定核心方案均判成立；但抓出：**F-09** `_file_lock` 锁元数据写入失败/短写被 `except OSError: pass` 静默吞掉后仍进临界区，留下空锁触发 mtime 兼容回收，可能双进程同时进临界区；**F-10** `_lease_reclaimable` 与 `_lock_holder_stale` 把损坏的 `process_start_time`（`"corrupted"`/bool/dict/NaN）当作 pid_reused 证据，把身份不可读错误升级成允许回收；**F-11** `_force_release_locked` 用 `os.replace` 归档会覆盖同名已有归档，违反 R7 不覆盖承诺；**F-08 补** `_lock_holder_stale` 的 `except OSError` 拦不住 `UnicodeDecodeError`（非法 UTF-8 锁文件读取异常会穿透） |

**第二十轮改了什么**：① **F-09**：新增 `_write_all()` 完整写入（循环处理短写）；写入失败时清理半成品锁 + 关 fd + 抛 `LockTimeout`，绝不 yield 进临界区（strict 与非 strict 均阻断——身份缺失 ≠ 锁竞争超时）。② **F-10**：新增 `_parse_start_time()` 严格验证（只认 int/float 且拒 bool/str/dict/NaN；`None` 为旧格式兼容；原值返回不转 float 防 FILETIME 精度丢失），`_lock_holder_stale` 与 `_lease_reclaimable` 均接入，损坏值一律 `(False, "identity_unreadable")` / 不回收。③ **F-11**：新增 `_rename_no_overwrite()`（Windows `os.rename` 天然不覆盖；POSIX `os.link`+`os.remove`），`_force_release_locked` 归档冲突时追加序号找唯一名，绝不覆盖已有归档。④ **F-08 补**：`_lock_holder_stale` 读取段 `except OSError` → `except (OSError, ValueError)`（覆盖 `UnicodeDecodeError`）。⑤ 补 13 条回归（192→205）：F-09 三条（写失败/短写/非 strict 均不进临界区）、F-10 七条（损坏 start_time 不回收×2 侧 + 合法不等→pid_reused + None→alive + `_parse_start_time` 契约）、F-11 三条（归档不覆盖 + 多冲突 + `_rename_no_overwrite` 契约）、F-08 补一条（非法 UTF-8 不回收不抛异常）。

| 21 | v1.8 修订 | `NEEDS-REVISION`（F-10 残留） | F-09/F-11/F-08补 均判「修复到位」；`_write_all`/`_rename_no_overwrite` 核心正确（但 `_rename_no_overwrite` POSIX 路径非原子重命名——硬链接+删源两步，docstring 措辞需修正）。**F-10 残留**：`_parse_start_time` 只拒 NaN 未拒 ±inf 和负数，`float("inf")`/`-1` 等能通过验证后 `!= cur` 触发 pid_reused 回收 |

**第二十一轮改了什么**：① `_parse_start_time` 补拒 ±inf（`float("inf")`/`float("-inf")`）与负数（int/float 均查 `< 0`）；② `_rename_no_overwrite` docstring 改为「目标不覆盖的归档移动」并注明 POSIX 两步非原子；③ 测试集扩充 ±inf/-1/-0.5 至三处（`_parse_start_time` 契约 + `_lock_holder_stale` 侧 + `_lease_reclaimable` 侧），218 条不变（用例集扩参不加新 ID）。

| 22 | v1.8 修订 | **`PASS`** | F-10 完全闭环（None/bool/str/NaN/±inf/负数全覆盖，bool 先于 int 排除顺序正确）；`_rename_no_overwrite` docstring 措辞修正可接受（POSIX 两步非原子的描述准确）。无新确定性实现级缺陷。残余风险 2 条：①POSIX 归档硬链接成功后、删源前崩溃可能留下双路径（不丢数据不覆盖归档，需人工/恢复逻辑识别）；②历史空锁/无 pid 锁的 mtime 兼容回收不具备身份级证明（退化恢复机制） |

**第二十二轮说明**：定向复审（仅 F-10 残留 + docstring），非全文裁定。第二十轮「全文最终确认」曾抓出 F-09/F-10/F-11，修复后需全文确认才能收敛。

| 23 | v1.8 修订 | `NEEDS-REVISION`（F-07 残留） | 全文最终确认轮。F-02～F-11 组合审查全部通过、残余风险描述准确；但抓出 **F-07 残留**：`_parse_pid` 对超长十进制串（Python 3.11+ int() 位数限制）`int()` 抛 ValueError，异常从 `_parse_pid` 穿透 `_proc_state`（`_proc_state` 在 `try` 之前调用 `_parse_pid`），违反「非法 pid 一律 UNKNOWN」契约 |

**第二十三轮改了什么**：`_parse_pid` 数字串分支 `int(pid)` 包 `try/except (ValueError, OverflowError)` → `None`；补一条回归 `test_parse_pid_overflow_string_returns_none`（5000 位 `"9"*5000`，四路断言：`_parse_pid` None / `_proc_state` UNKNOWN / `_lock_holder_stale` 不回收 / `_lease_reclaimable` identity_unreadable），205→206。

| 24 | v1.8 修订 | **`PASS`** | F-07 完全闭环：`int()` 异常被捕获返回 None，异常传播链闭合，四路回归覆盖充分（解析/进程状态/锁回收/租约回收均验超长 pid 不抛不误删）。无新缺陷 |
| 25 | v1.8 修订 | **`PASS`** | **全文最终确认·收敛轮**。逐函数（12 项）全部通过；逐组合（F-02×F-03 / F-03×F-07 / F-07×F-08 / F-08×F-09 / F-03×F-10 / F-02×F-11 / 强制恢复链）全部无漏洞。残余风险描述准确。**F-09/F-10/F-11 与 F-07 在最终代码中均已核销**。无新确定性实现级缺陷 |

**第二十五轮说明**：定向复审（仅 F-10 残留 + docstring），非全文裁定。第二十轮「全文最终确认」曾抓出 F-09/F-10/F-11，修复后需全文确认才能收敛。

| 26 | v1.8 修订 | `NEEDS-REVISION`（F-12） | **换靶到回复完整性/收割链路（A10/A12/A13/A14）**——前二十五轮火力全在锁子系统，这批条款从未做过实现级对抗审查。判 F-12 成立：`reply_digest`/`harvest_mapping`/`harvest_transcript`/`tail_from_mapping`/`extract_reply_sse` 对「JSON 合法但形状不符」的响应体抛未捕获异常（探针实测 41 处）；`reply_digest` 的异常确实阻断 `save_reply`（归档唯一副本丢失）；`extract_reply_sse` 在主收割路径 7/7 全崩。**并给出差异化降级要求**：①`reply_digest` 是完整性证明器——任何畸形**整体判不可用**，不得跳过坏 node 拿剩余 part 算摘要；②收割类必须**暴露不完整状态**；③`tail_from_mapping` 是尾部确认——畸形即整体 None；④SSE 须**记录坏事件数与是否 [DONE] 收尾**，不得把部分流冒充完整回复；⑤**辅助对账器不得成为归档的前置硬依赖**。第 3 项（全文件枚举同类位置）未完成——审查方把「通读全文」理解为去读 GitHub 仓库 `oahcom/AiMemoryBrainArchitecture`，该仓库 main 无此文件 |
| 27 | v1.8 修订 | `NEEDS-REVISION`（F-12a/b/c + F-13 + F-14） | **F-12 尚有完整性缺口**：①**F-12a**——`"".join(p for p in parts if isinstance(p, str))` 把 `parts` 里的非字符串元素**静默丢掉**，内容凭空消失而完整性仍报 True；`_scan_mapping` 应跳过该节点并 `complete=False`，`reply_digest` 应整体 `None`，`_scan_sse` 应跳过该事件并 `bad += 1`。②**F-12b**——SSE 里带 `message` 却缺 `author`/`author` 非 dict 的事件未计入坏事件，`complete` 可能错误地为 True。③**F-12c**——`_as_ctime` 的 `float(value)` 未捕获 `OverflowError`、接受 NaN/±inf，且大整数转 float 会静默丢精度。**F-13（实现级）**：落点不一致只 WARN 一句，随后 `save_reply(conv_id or real_conv)` 把落在 B 的回复归档在 A 名下、返回 A + 200 —— **会话身份与回复归属分裂**，下游会据错误的 conversation id 继续操作。要求四件事一起做：判失败、隔离保全（记两个 id 与原始响应证据）、禁止静默改归属、失败返回携带两个 id；并确认 `parsed` 确实来自**首个** `data:` 事件而非最后一次事件/GET 响应，无法确认唯一 id 时失败关闭。**F-14（实现级）**：①`check_schedule` 用字典解析（同名 key 取最后一个）却下发原始串——RFC 5545 §3.3.10 规定 rule part 不得重复，`FREQ=DAILY;FREQ=HOURLY` 可绕过「周期固定 1 小时」；②应下发由**已验证字段重建**的规范化 RRULE；③`INTERVAL` 应按 RFC 十进制数字语法严格解析，缺省按 1；④`norm = raw.upper().replace(" ", "")` 会删除内部空白（`FREQ=HO URLY` 被修补成合法输入），应拒绝。**并纠正一处错误依据**：审查方指出 RFC 5545 §2 枚举属性值**不区分大小写**，故 `freq=hourly` 不构成 RFC 违规——不能把大小写本身当拒绝理由，接受后规范化即可 |

**第二十六轮改了什么**：① 新增 `_as_dict`/`_as_seq`/`_as_ctime` 三个形状收敛工具（`_as_ctime` 返回 `(ok, float)`，`ok=False` 表示时间值不可用、记录无效不得参与排序）；② 新增 `_scan_sse`（返回 text/complete/detail，记坏事件数与 [DONE]）与 `_scan_mapping`（返回 rows/complete，坏 node 跳过但**必须标记不完整**）两个扫描层；③ `reply_digest` 改**严格整体判定**：任何畸形（mapping/node/message/author/content/parts 形状不对或时间戳不可用）一律返回 None；④ `tail_from_mapping` 改**严格尾部确认**：任何畸形整体返回 None；⑤ 新增 `extract_reply_sse_checked`/`harvest_mapping_checked`/`harvest_transcript_checked` 三个带完整度信号的伴随接口（旧接口保留为薄包装，调用方不受影响）；⑥ `send_prompt` 用带完整度的接口，**坏流/不完整 mapping 明确打 WARN**；⑦ `reply_digest` 调用包 try/except 且**对账失败也继续 `save_reply`**——辅助对账器不再是归档的前置硬依赖；⑧ 匿名留存（`read_anon`）在归档头部写入「留存不完整」标注；⑨ `requirements`（PoW 关键路径）/ 模型清单 / 对话列表 / `conv_owner` 补形状守卫；⑩ 补 12 条回归（206→218）。
| 28 | v1.8 修订 | `NEEDS-REVISION`（F-15/F-16/F-17/F-18/F-19） | **F-12a/b/c 的核心修正基本闭环**（F-12a 承认「非字符串 part 必然畸形」不是对所有 ChatGPT 消息都成立——多模态 part 是合法对象）；**F-14 的重复项、规范化下发与大小写处理已闭环**，但要求明确契约边界。新判五处：**F-15** 显式 `message: null` 与「没有 message 字段」被混为一谈——前者是畸形事件却按正常事件放过；**F-16** `[DONE]` 之后的事件仍参与落点证明——「先 [DONE]、后补另一个 id」会被接受；**F-17** id 歧义分支返回 `(1, None, None)`，丢掉了 target 与候选 id，机器调用方拿不到诊断信息；**F-18** 隔离档案只留 `resp_head` 前 800 字符，不满足「原始响应证据完整保全」——关键 id 可能正好落在截断部分；**F-19** `_scan_mapping` 把**用户消息**里的合法多模态 part 判成畸形，误报「assistant 回复残缺」。并对 F-13 四问定调：①「首个带 id 事件」符合 A10 条文，但须 `[DONE]` 即停；②409 在所贴调用点均按失败处理，但歧义分支要带诊断；③隔离目录必须**移出正常回复树**，不能只靠文件名 glob；④**不应自动 `record_conv(B)`**——服务器实际落点 ≠ 已确认归属，应另记**待认领登记**。对 F-14 四问定调：严格白名单（只允许 FREQ+INTERVAL）合理，但表达的是「只接受最简单的每小时重复规则」；`INTERVAL=+1` 合法（RFC integer ABNF 允许前导 `+`）；`rrule_input_echo` 有误用风险，应改成明确的审计字段名；**下游只读 `rrule`**，不要另加 `rrule_canonical` 制造第二个可执行字段 |

**第二十七轮改了什么**：① **F-13 落点不一致改 fail-closed**：新增 `_sse_conv_ids()`（只取**首个**带 id 的 `data:` 事件，并汇总全部出现过的 id 供唯一性检查）、`ConvRef`（str 子类，失败返回同时携带 `target_conv_id`/`actual_conv_id`/`mismatch`）、`_quarantine_reply()`（写入 `replies/quarantine/` 独立隔离档案，头部记两个 id、HTTP 状态、原始响应摘要、回复 sha256 与全文）；`send_prompt` 里落点不一致时**判失败（状态 409）**、**不归档为目标会话的正常回复**、**不静默改归属**，截断补全文的 GET 目标改为**服务器实际写入的会话**（旧版按目标会话拉回来的是别的会话内容）；CLI 两个入口在失败时输出 `landing_mismatch` + 两个 id + 隔离档案路径。② **F-14 重写 `check_schedule`**：解析阶段**拒绝重复 rule part**、拒绝空/畸形 rule part、拒绝内部空白、`INTERVAL` 按 RFC 十进制整数正则严格解析（缺省按 1）、枚举值大小写不敏感但规范化为大写、拒绝 FREQ/INTERVAL 之外的 rule part；**下发改为由已验证字段重建的规范化 RRULE**（`FREQ=HOURLY;INTERVAL=1`），原始串只作为 `rrule_input_echo` 留档不下发。③ **F-12a/b/c**：`parts` 里混进非字符串元素时，`_scan_mapping` 跳过该节点并置 `complete=False`、`reply_digest` 整体返回 `None`、`_scan_sse` 跳过该事件并 `bad += 1`；SSE 里带 `message` 却缺 `author`/`author` 非 dict 的事件计入坏事件；`_as_ctime` 捕获大整数溢出、拒 NaN/±inf、拒超出 float64 精确整数范围的值；新增 `_parts_are_strings()` 统一判定。④ 补 22 条回归（218→240），新增 `TestMalformedParts`（8 条）、`TestLandingMismatch`（7 条）与 7 条 RRULE 契约用例。
X=2` 会被当成单个合法 VALUE 放行；改为 `([^;

]+)$` 并去掉 re.S，换行/分号一律畸形。补 2 条回归（257→259）。|
| 29 | v1.8 修订 | `NEEDS-REVISION`（新会话独立复审：F-13/F-16 残留 + F-18/F-21/F-25 + F-23/F-24） | 原审查会话累积 113 节点/1MB，78k 提示词投递后 45 分钟无响应；改用**精简版提示词（30k）另开新会话** `6aff29ae`，63 秒返回裁定。新判：①**F-13/F-16 未闭环**——删掉 SSE 结构化解析后仍有**正文正则兜底**，能捞到 `[DONE]` 之后的 id（反例：先 start、再 `[DONE]`、后写 B）；②**F-18** 完整响应保全失败仍返回成功路径；③**F-21** 序号避唯一名是 TOCTOU，需 O_EXCL 原子独占；④**F-23（新）** SSE 不完整（无 `[DONE]` 且无 IncompleteRead）仍直接正常归档——**WARN 不是完整性保护**；⑤**F-24（新）** `reply_digest` 算出 chars/sha256 却没接进放行门禁，只拿同一批 full 自证；⑥**F-25（新）** 待认领登记失败被忽略。另要求补交 F-12/F-12c/F-22 的实现供独立核验。对收敛的裁定：锁子系统本轮未贴代码不重新确认；**回复完整性/收割链路未收敛**；RRULE 核心契约通过但 F-22 待核。|

**第二十八轮改了什么**：① **F-15**——`_scan_sse` 改用 `"message" in obj` 区分「没有 message 字段」（合法非消息事件）与「显式 `message: null`/非 dict」（畸形事件，计入坏事件）。② **F-16**——`[DONE]` 改为**解析终点**：`_sse_conv_ids` 与 `_scan_sse` 都在见到 `[DONE]` 时 `break`，其后的 id 与内容一律不采信。③ **F-17**——`ConvRef` 增加 `candidate_ids`；id 歧义分支不再返回 `(1, None, None)`，而是返回带 `target_conv_id` 与 `candidate_ids` 的失败结果；「解析不出 id」「id 不合规」两条失败分支同样改为返回带 target 的 `ConvRef`；CLI 两个失败出口都补 `candidate_conv_ids`。④ **F-18**——`_quarantine_reply` 把**完整原始响应**另存为 `quarantine/*.resp.txt`，隔离头部记 `resp_path`/`resp_chars`/`resp_sha256`（800 字符的头部摘要只作速览）。⑤ **F-19**——新增 `_part_is_structured()` / `_split_parts()`，把 part 分成「文本 / 合法但不可文本化的结构化对象 / 非法」三类：用户消息里的结构化 part **不算畸形**（不再误报 assistant 回复残缺，其文本部分照收）；assistant 消息里的结构化 part 仍判对账整体不可用；非法 part 仍按 F-12a 处理。⑥ **F-20（自审发现，审查方要求确认）**——`check_schedule` 的 `INTERVAL` 改**不调用 `int()`**（Python 3.11+ 对超长十进制串会抛 ValueError，与 F-07 同型的异常逃逸），改为直接比数字串并加长度上限。⑦ **F-13 补强**——隔离目录从 `replies/quarantine/` 移到与 `replies/` **同级**的 `quarantine/`（下游按 `replies/**` 递归扫描也够不到，不靠 glob 兜底）；新增 `pending-claims.jsonl` **待认领登记**（记 target/actual/时间/隔离档案路径/原因/状态），**不自动 `record_conv(B)`**——服务器实际落点 ≠ 已确认归属。⑧ **F-14 补强**——审计字段改名 `original_input_for_audit_only` 并加 `executable_field: "rrule"`，**唯一可执行字段是 `rrule`**；并在 B1 条款里写明「严格白名单表达的是只接受最简单的每小时重复规则」，避免被当成「所有实际间隔一小时的 RRULE 都接受」。⑨ 补 17 条回归（240→257）。

**第二十九轮改了什么（自审，待复审确认）**：① **F-21**——`_quarantine_reply` 的文件名加序号避唯一名：秒级时间戳不足以区分同秒同对会话的两次异常投递，旧实现会覆盖前一次的隔离档案与完整响应（证据永久丢失）；新实现从 `seq=0` 起找 `name`/`name-1`/`name-2`… 直到 `.md` 与 `.resp.txt` 都不存在。② **F-22**——`_RRULE_PART_RE` 由 `^([A-Za-z][A-Za-z0-9-]*)=(.+)$` 配 `re.S` 改为 `^([A-Za-z][A-Za-z0-9-]*)=([^;

]+)$`（无 re.S），杜绝把换行后内容当合法 VALUE 放行。③ 补 2 条回归（`test_quarantine_same_second_never_overwrites` / `test_rrule_part_regex_rejects_newline_in_value`），257→259。
| 30 | v1.8 修订 | `NEEDS-REVISION`（新会话 6aff2c63 增量复审：F-12c/F-18/F-22 + N-01/N-02） | 本轮 F-13/F-16/F-21/F-23/F-24/F-25 主要设计方向成立；仍判五处：**F-12c** 只对 int 查 ±2^53−1，整数值型 float（`9007199254740992.0`/`1e20`）仍放行；**F-22** `.strip()` 先吃掉首尾换行再查 `\s`，`FREQ=HOURLY
` 可用绕过；**F-18/N-01** 响应保全失败时隔离档案仍无条件写『完整原始响应另存于…』——失败的保全操作不得在正式证据里声称成功；**N-02** O_EXCL 占名后普通 `open(path,"w")` 写内容，磁盘满会留空壳/部分档案，非原子事务；另要求补 JSONL 并发登记测试。裁定明确：完成后**增量复审**即可，不需重审前 25 轮已 PASS 的锁/进程身份问题。|

**第二十九轮改了什么（依新会话裁定）**：① **F-13/F-16**——**删除整包 JSON 与正文正则 id 兜底**：落点证明只信 SSE 首个带 id 的结构化事件，解不出即失败关闭；反例「先 `[DONE]` 后写 B」实测返回 status=1、不落盘。② **F-21**——新增 `_atomic_create_exclusive()`（`O_WRONLY|O_CREAT|O_EXCL` 内核级占名），`_quarantine_reply` 改用它原子占名，冲突就换序号，**绝不覆盖**已存在的隔离证据。③ **F-18/F-25**——隔离三件事（完整响应、隔离档案、待认领登记）任一失败即 `evidence_complete=False`，随 `ConvRef` 返回给机器调用方，不再只打 WARN。④ **F-23/F-24**——完整性证明真正参与放行：GET 补全触发条件由「仅 last_truncated」扩到「SSE/mapping 任一不完整」；`digest_ok` 要求 `gcomplete` + parts 合计==拼接长度 + **摘要 sha256 == 按 parts 拼出的全文 sha256**（不再同源自证）；仍无法证明完整时**内容照收但归档文本自带「完整性未证明」声明**、`ConvRef.reply_complete=False`——既不违反第二十六轮「解析失败不能阻断归档」口径，也不让部分流冒充定稿。⑤ 自审另修 **F-22**：`_RRULE_PART_RE` 去掉 `re.S`、拒换行。⑥ 补 6 条回归（259→265）。
| 31 | v1.8 修订 | `NEEDS-REVISION`（新会话 6aff2ded 增量复审：D-01） | 五项最小修复 F-12c/F-22/F-18/N-01/N-02 **逐条成立**；**定时任务契约宣告本轮增量收敛**（CR/LF 首尾绕过、内部空白、重复 rule part、非法/超长 INTERVAL、大小写、规范化下发、缺省语义全覆盖，无新绕过路径）。仅剩一处：**D-01**——`reply_digest` 把 `_split_parts` 放在 `author.role != assistant` 判断之后，user 节点的**非法 part**被 continue 跳过，摘要仍对一份含畸形节点的 mapping 出具可信外观，与文档「body 有一处畸形就整体 None」的承诺矛盾。修法：`_split_parts` 移到角色判断前，对所有角色先查非法 part；用户合法结构化 part（多模态）不算畸形仍放行。验收三对照：user 非法→None、user 合法结构化→摘要成立、assistant 非法→None。|

**第三十轮改了什么（依新会话裁定）**：① **F-12c**——`_as_ctime` 的 float 分支补安全整数边界：`abs(value)>2^53−1 且 value==int(value)` 判不可用，非整数型 float（1.5）仍接受；实测 `9007199254740992.0`/`1e20`→`(False,0.0)`。② **F-22**——`check_schedule` 在 `.strip()` **之前**检查原始串 CR/LF，含换行一律拒绝（首尾换行也不许预处理替用户消除）。③ **F-18/N-01**——响应保全移到 body 构造**之前**，`header.resp_preserved`/`resp_error` 随结果变化；档案正文按 `resp_ok` 动态生成「已保全 / 保全失败 + 需人工重取」两种声明，失败时不再声称成功。④ **N-02**——占名后改 `_atomic_write(path, body)`（临时文件完整写 + fsync + os.replace 发布），失败则删除占名空壳，不留部分档案。⑤ **`_record_pending_claim` 并发**——JSONL append 纳入 `_file_lock(path, strict=True)` 跨进程互斥，LockTimeout 与 OSError 都返回 False（fail-closed）。⑥ 补 3 条回归（265→268）。
| 32 | v1.8 修订 | **`PASS`（三子系统收敛）** | 新会话 6aff2f4d 单点增量复审 D-01：修改位置正确（`_split_parts` 已移到角色判断前，所有节点先查非法 part，用户合法结构化 part 仍放行）；三对照全部一致（user 非法→None、user 合法结构化→非空摘要、assistant 非法→None）。裁定原文：『可以宣告回复完整性/收割链路增量收敛；定时任务契约维持已宣告的增量收敛；锁子系统不重审』。|
| 33 | v1.8 修订 | **`PASS`（收敛后终检自检：F-26）** | 第 32 轮宣告收敛后，终检对「回复完整性/收割链路」做实现级复核，抓出 F-26：`reply_digest` 按**未 strip** 的 parts 算 chars/sha，`_scan_mapping`/`harvest_mapping_checked` 返回 **strip 后**的 full，而 `send_prompt` 的 F-24 放行门禁拿两者逐字节比对（`digest.sha == sha256(full)`）。合法完整回复只要首尾带空白（LLM 输出极常见），两侧拼接永不等 → `digest_ok=False` → 被误标「完整性未证明」，F-24 门禁失去区分度。方向保守（不 fail-open），但属刚收敛子系统的实现级口径缺陷。修复：抽 `code:_assistant_text`（拼接+strip）为唯一口径，`_scan_mapping` 与 `reply_digest` 共用。补 2 条回归（269→271）：`test_reply_digest_matches_harvest_with_whitespace`（直接层）+ `test_whitespace_padded_full_reply_is_proven_complete`（端到端门禁层）。|

**第三十三轮改了什么（收敛后终检自检，非新会话裁定）**：**F-26**——① 新增 `_assistant_text(texts)` = `"".join(texts).strip()`，作为 assistant 文本的唯一规范化口径；② `_scan_mapping` 的 `text = "".join(texts).strip()` 与 `reply_digest` 的摘要拼接（chars/sha）都改走 `_assistant_text`，消灭「落盘 strip / 对账不 strip」的口径分裂；③ 探针实测三种输入（无空白／首尾空白／多 part 尾部换行）修复后 `chars==len(full)` 且 `sha256` 逐字节相等、`digest_ok` 全 True；④ 补 2 条回归钉住直接层与门禁层。**自检裁定：F-26 关闭；该缺陷严格保守（只误标完整→不完整，不把不完整放行成完整），修复未触动三子系统的其它判定口径；回归 271 全绿（1 条 POSIX-only skip）。** 本轮为收敛后主动终检的一处自检修复，修复面仅限回复完整性对账口径。
| 34 | v1.8 修订 | **`PASS`（收敛后终检自检：F-27）** | 第 33 轮修复 F-26 后，沿「两函数对同一份数据各自实现」模式继续终检，抓出 F-27：`reply_digest` 用 `(ct, id字符串)` 选最新 assistant，`harvest_mapping_checked` 经 `_scan_mapping` 稳定排序 + reversed 取插入序最后者；多条 assistant 的 ct 相同（含都缺 create_time→归 0）时两者选出不同节点 → sha 不等 → 合法完整回复被误标「完整性未证明」，是 F-24 门禁继 F-26 后的第二处口径分裂（选节点口径）。方向保守（不 fail-open）。修复：选节点 key 改 `(ct, order)`（ct 最大、ct 同取插入序最后），与 `_scan_mapping` 稳定排序对齐。补 2 条回归（271→273）：`test_reply_digest_selects_same_assistant_as_harvest_on_ct_tie`（直接层）+ `test_ct_tie_full_reply_is_proven_complete`（端到端门禁层）。|

**第三十四轮改了什么（收敛后终检自检，非新会话裁定）**：**F-27**——① `reply_digest` 的选节点 key 从 `(ct, str(id))` 改为 `(ct, order)`（`order`=mapping 插入序号），与 `_scan_mapping` 的 `rows.sort(key=ct)` 稳定排序 + `reversed` 取插入序最后一条 assistant 的语义对齐；② 探针实测四种场景（同 ct 缺失两种插入序、同 ct 显式、正常不同 ct）digest 与 harvest 全部选中同一节点、`digest_ok` 全 True；③ 补 2 条回归钉住直接层（选中 id）与门禁层（端到端 `reply_complete=True`）。**自检裁定：F-27 关闭；与 F-26 同为严格保守缺陷（只误标完整→不完整），修复面仅限选节点口径，未触动三子系统其它判定；回归 273 全绿（1 条 POSIX-only skip）。** 本轮与第 33 轮同属收敛后主动终检的自检修复。

**第三十四轮之后补的一条（PASS 后加证，不重开审查）**：用随机化对拍验证 F-26/F-27 修复的普适性——600 组随机 mapping（不同 ct/插入序/part 结构/首尾空白）中 491 组可判定全一致、109 组双保守、0 不一致；再 800 组混入结构化 part 与非法 part，验证「digest 判不可用 → harvest 不报 complete=True」的降级对称性，0 单侧放行。据此补 `test_digest_none_never_leaves_harvest_complete`（固定用例钉住非法 part / assistant 结构化 part / 不可用时间戳三类畸形的降级对称）。**纯加证，不改变任何条款口径或实现，273→274。**
| 35 | v1.8 修订 | **`PASS`（收敛后终检自检：F-28/F-29）** | 第 33/34 轮只审了「回复完整性对账族」，本轮换靶到同一子系统的另一半——定时任务契约的 `check_oneshot`（B4/B5），该函数此前从未在终检中被逐行复核。抓出两处：**F-28**（异常逃逸，与 F-07 同型）——`base = now or datetime.now(dt.tzinfo)` 直接参与减法，`now` 为 naive datetime 抛 `TypeError: can't subtract offset-naive and offset-aware datetimes`、为字符串/非 datetime 抛 `TypeError: unsupported operand type(s)`，调用方按 `ScheduleError` 处理会漏接，等于校验器自己崩了；`check_schedule` 已专门防这类（`INTERVAL` 不调 `int()`），`check_oneshot` 漏了。**F-29**（校验语义≠下发语义，与 F-14 同一条契约）——`fromisoformat` 接受空格分隔/紧凑 `20261119T181819+0000`/无秒等写法，旧版把**原始串**回显成 `scheduled_at` 供下游排程，同一时刻多种字符串表示，下游按字符串比较/透传即错。修复：`now` 先验类型与时区（naive/非 datetime 一律 `ScheduleError`）；回显改 `dt.isoformat()` 规范化值，原始串只留 `original_input_for_audit_only`，并给 `executable_field: "scheduled_at"`，与 `check_schedule` 字段命名对齐。补 2 条回归（274→276）。|

**第三十五轮改了什么（收敛后终检自检，非新会话裁定）**：**F-28 + F-29**——① `check_oneshot` 的 `now` 参数加两道守卫：非 `datetime` 一律 `ScheduleError("now 必须是 datetime（收到 %s）")`；`tzinfo is None or utcoffset() is None` 一律 `ScheduleError("now 必须带时区偏移")`。校验器对任何输入只允许两种结局——返回结果或抛 `ScheduleError`，绝不让 `TypeError` 逃逸。② 回显从 `raw`（用户原始串）改为 `dt.isoformat()`（规范化 ISO），原始串移入 `original_input_for_audit_only`，新增 `executable_field: "scheduled_at"`，把「唯一可执行字段」这一契约显式写进返回值（此前只有 `check_schedule` 有）。③ 探针实测：naive/字符串/int 三种坏 `now` 全部落到 `ScheduleError`；空格分隔/紧凑/Z 后缀/无秒四种写法回显全部规范化为同一 `+00:00` 形式且 `fromisoformat` 后等值，原串完整保留在 audit 字段。④ 补 2 条回归：`test_oneshot_bad_now_never_escapes_typeerror`、`test_oneshot_echoes_canonical_iso`；反证已做——回退修复后前者 error（TypeError 逃逸）、后者 failure（回显原串），精确复现旧行为。**自检裁定：F-28/F-29 关闭；F-28 是可用性缺陷（校验器崩溃而非放行），F-29 是契约一致性缺陷（不 fail-open，但违反本子系统已确立的「规范化下发」）；修复面仅限 `check_oneshot`，未触动 `check_schedule` 与其它两子系统；回归 276 全绿（1 条 POSIX-only skip）。**
| 36 | v1.8 修订 | **`PASS`（收敛后终检自检：F-30）** | 第 35 轮审完定时契约后换靶到锁/进程身份子系统做 fuzz 加固复核。抓出 **F-30**：`float()` 对**超大整数**抛的是 `OverflowError`（`ArithmeticError` 子类），而全仓 **6 处**数值收敛点只写了 `except (TypeError, ValueError)`——`{"created_at": 10**400}` 这类损坏字段让异常直接穿出 `_lock_holder_stale` → `_try_reclaim_stale` → `_file_lock`。实测确认：`_file_lock` 抛的是 `OverflowError` 而非 `LockTimeout`，调用方 `except LockTimeout` 漏接；出现在**锁判定路径**上意味着「判定器一崩，互斥失去确定性结局」。同一模式还在 `lease_until`、`ttl`/`epoch`（3 处）、`retry-after` 解析。修复：抽 `code:_as_float`（与 `_as_dict`/`_as_seq` 同族）统一收敛，非数值/NaN/±inf/溢出全部降级为默认值、永不抛异常；6 处调用点全部改走它。补 4 条回归（276→280）。|

**第三十六轮改了什么（收敛后终检自检，非新会话裁定）**：**F-30**——① 新增 `_as_float(value, default=0.0)`：`bool`/`None` 直接返回 default；`float()` 包在 `except (TypeError, ValueError, OverflowError)` 里；结果再查 `NaN`/`±inf`，不合格同样返回 default。与 `_as_dict`/`_as_seq` 同一原则——状态文件是不可信输入，字段损坏应降级成「没有这个值」，而不是让异常穿过临界区。② 六处调用点全部替换：`_lock_holder_stale` 的 `created_at`、`_lease_reclaimable` 的 `lease_until`、`is_unavailable`/`purge_expired`/`anon_fallback_block_reason` 的 `ttl`+`epoch`、`retry-after` 头的解析（后者额外获得「`"9"*400` → inf → 退回默认」的正确语义）。③ 探针实测：`10**400`/`-10**400`/`inf`/`-inf`/`nan`/`None`/`True`/`"abc"`/dict/list 全部返回默认；`"1.5"`/`1.5`/`10**18` 原样通过。端到端：坏 `created_at` 的残留锁 → `_lock_holder_stale=False`、`_file_lock` 抛 `LockTimeout`（不是 `OverflowError`）、临界区未进入。④ 补 4 条回归：`test_as_float_never_raises_on_overflow`、`test_overflow_created_at_does_not_break_lock_judgement`、`test_overflow_lease_and_ttl_do_not_raise`、`test_overflow_in_server_rate_fields_does_not_raise`；反证已做——回退调用点后相关用例均 error（`OverflowError` 逃逸），精确复现旧行为。**自检裁定：F-30 关闭；属可用性/确定性缺陷（校验器崩溃而非放行），修复面为「数值收敛」这一横切关注点，未改动锁的两层互斥结构、stale 判定顺序与三态身份语义；回归 280 全绿（1 条 POSIX-only skip）。**
| 37 | v1.8 修订 | **`PASS`（收敛后终检自检：F-31）** | 前三轮修完「不可信输入 → 确定性结局」在校验器与锁判定上的漏网后，本轮用**结构化 fuzz**（4000 组随机 JSON 形状：嵌套 dict/list/超大整数/NaN/±inf/bytes/None/int）打四个解析器。抓出 **F-31**：同一份不可信输入两种待遇——`reply_digest`/`harvest_mapping_checked` 走 `json.loads` 的 `except Exception`，任意类型降级成 None；而 `_scan_sse`/`_sse_conv_ids` 直接 `(raw_text or "").splitlines()`，收到 **bytes** 抛 `TypeError: startswith first arg must be bytes or a tuple of bytes, not str`、收到 int/float/bool/object 抛 `AttributeError: ... has no attribute 'splitlines'`。当前 HTTP 层统一 `.decode("utf-8","replace")` 后再传入，故**不可达**；但上层一旦改流式/二进制读取即变成可复现崩溃。修复：抽 `code:_as_text`（str 原样；bytes/bytearray 解码；其余空串），两处解析器共用。补 3 条回归（280→283）。|

**第三十七轮改了什么（收敛后终检自检，非新会话裁定）**：**F-31**——① 新增 `_as_text(value)`：`str` 原样返回；`bytes`/`bytearray` 按 `utf-8`/`replace` 解码（HTTP 体的天然形态）；其余类型一律空串。与 `_as_dict`/`_as_seq`/`_as_float` 同族——「没有可解析内容」是保守结论（不完整），不是猜测。② `_scan_sse` 与 `_sse_conv_ids` 的 `(raw_text or "").splitlines()` 全部改为 `_as_text(raw_text).splitlines()`。③ 探针实测：`None`/`123`/`1.5`/`True`/`[]`/`{}`/`object()` 全部降级为 `("", False, ...)` 与 `(None, [])`，无异常；`bytes` 形态与 `str` 形态解析结果**逐字节等价**（`_sse_conv_ids` 取到同一 id、`_scan_sse` 取到同一正文与 `saw_done`）。④ 补 3 条回归：`test_sse_parsers_accept_non_str_without_raising`、`test_sse_parsers_decode_bytes`、`test_lock_judgement_rejects_non_path`；反证已做——回退两处调用点后前者报 `TypeError`/`AttributeError`、后者报 `TypeError`，精确复现旧行为。**F-31b（同轮附带）**：结构化 fuzz 打 `_lock_holder_stale` 时暴露 `open(True)` ≡ `open(1)`（把 stdout 当文件读，实测挂住）与 `open(10**400)` 抛 `OverflowError`（既非 OSError 也非 ValueError），`_lock_holder_stale` 与 `_try_reclaim_stale` 入口补路径类型守卫，非路径一律返回 False（保守，不动锁）。**自检裁定：F-31/F-31b 关闭；当前不可达（HTTP 层已解码、调用方恒传路径），属健壮性与一致性缺陷（同一不可信输入两种待遇，违反 F-12 已确立口径），修复面仅限两个 SSE 解析器的入口类型收敛与锁判定入口守卫，未改动解析逻辑、落点判定、`[DONE]` 终点语义与 stale 判定顺序；回归 283 全绿（1 条 POSIX-only skip）。**
| 38 | v1.8 修订 | **`PASS`（收敛后终检自检：F-32 + F-33）** | 本轮两靶：① 锁子系统**磁盘失败面**——`lease_heartbeat`/`lease_release`/`lease_reclaim`/`lease_acquire` 文档承诺 `bool` / `(bool, 稳定枚举)` / `run_id|None`，实际只 catch `LockTimeout`，`_atomic_write`/`os.remove` 的 `OSError`（磁盘满 WinError 112 / 只读盘 / 权限）直接穿出去。心跳线程里没包 `try` 就是**线程静默死亡、租约悄悄过期**（B15 的单实例保证失效），包了 `try` 也只是吞异常、调用方拿不到「本次续期失败」。`lease_reclaim` 更重：它是**自动化巡检入口**，异常穿出去会崩掉整轮巡检，而不是跳过这一份租约继续看下一份。② 429 处置**身份侧**——用户裁定「429 限流换账号或者直接用匿名渠道」，此前只对 `--new` 隐式轮转成立；**显式 `--account acct1 --new` 遇 429 直接 exit 1**（明明另一个账号可用），既有会话遇 429 也只报一句 `resp:` 不解释为什么换号无用。修复：租约四函数各补 `except OSError` 分支（`False` / `False` / `(False,"write_failed")` / `None`），诊断细节走 `_diag` 落日志不入返回值；`--new` 显式主身份 429 → 记入冷却后转轮转（换另一个登录态，都 429 则匿名）；既有会话 429 → 明确告知「会话归属固定，换号/匿名都读不到，只能按服务端时限退避或 `--new`」。补 7 条回归（283→290）。|
| 39 | v1.8 修订 | **`PASS`（收敛后终检自检：F-34）** | 本轮靶：**状态文件形状/类型畸形**——`_load_json` 只隔离语法坏（`JSONDecodeError`），「合法 JSON、字段类型坏」原样穿进下游：`_normalize_rotation` 的裸 `int(cursor)` 收 `"abc"` 抛 ValueError、收 `float("inf")` 抛 OverflowError、收 list 抛 TypeError，`order=5` 不可迭代、`created="x"`/`failures=5` 让 `.setdefault` 抛 AttributeError；`is_unavailable`/`purge_expired`/`mark_unavailable` 对 `unavailable` 非 dict 抛 AttributeError/TypeError；`fallback_uses="5"` 让 `+1` 抛 TypeError；匿名会话族（`sessions="x"`、条目 `"notadict"`、顶层 `[1,2]`）同样逃逸；且 `cursor=3.5→3`、`True→1` 违反 F-07「不许借 `int()` 把非法折叠成合法」。更糟的是**同一份不可信输入两种待遇**：`conv_owner` 按 F-12 写了 `_as_dict` 守卫，`record_conv`/`load_anon_sessions` 没有（F-12 口径未落到状态文件侧）。修复：① 收敛族补 `_as_int`（F-07 口径：只认非 bool int 与纯数字串，不折叠 float/bool），`_parse_pid` 委托之（单一语义）；② `_normalize_rotation` 全函数收敛、永不抛异常；③ 轮转读写八函数点用点收敛；④ `_load_json` 顶层类型守卫（类型坏 → 同 A21 隔离重建）；⑤ `record_conv` 排序键、匿名会话族逐层收敛。补 6 条回归（290→296）。|
| 40 | v1.8 修订 | **`PASS`（收敛后终检自检：F-35）** | 本轮靶：**代理端口字段不收敛**——`normalize_proxy` 只验 scheme/host 不验端口，坏端口（`99999`/`notaport`）一路潜到 `_parse_socks_proxy` 的 `u.port` 才炸（`ValueError: Port out of range`，出处是 urllib 内部而非「配置非法」）；`_tcp_alive` 裸 `int(port)` 对 `list`/`None`/`dict` 抛 TypeError、对 `float("inf")` 抛 OverflowError（只 catch 了 `(OSError, ValueError)`）；`resolve_proxy_info` 的 autodetect 循环 `int(p)` 对 `autodetect_ports=[inf]`（proxy.json 是不可信状态）抛 OverflowError。修复：`normalize_proxy` 端口校验（坏端口＝配置非法，与坏 scheme 同待遇 ValueError）；`_tcp_alive` 走 `_as_int` + 范围检查，任何不可信端口返回 False；autodetect 循环同款收敛，畸形项跳过。补 3 条回归（296→299）。|
| 41 | v1.8 修订 | **`PASS`（收敛后终检自检：F-36）** | 本轮靶：**剩余数字参数的 int() 出口**——全仓 `int()` 复核后确认三处真口子：① `AnonBrowser.__init__` 的 `int(port or CHATGPT_ANON_CDP_PORT or 9333)` **无兜底**（`CHATGPT_REPLY_MAX_CHARS`/`CHATGPT_ANON_IDLE_SEC` 两处已有 `except ValueError`，`--fence` 已转 `CustodyError`）——env=notaport 抛 ValueError、`port=[1]` 抛 TypeError、`port=inf` 抛 OverflowError，直接穿出构造器；② `_socks5_open` 的 `int(port)`/`int(cfg["port"])`/`struct.pack(">H", int(port))`——垃圾端口让 TypeError/OverflowError/struct.error 穿出，而本函数契约是「失败=OSError」；③ 同函数内 `p = cfg["password"].encode()` **遮蔽**端口变量（改名时暴露的既有隐患）。修复：`AnonBrowser` 端口走 `_as_int` + 范围校验，非法降级 9333 + WARN（与「变量写错不该让整次收割白跑」同口径）；`_socks5_open` 端口/配置先收敛，垃圾端口抛 OSError（契约内），变量改名 `dst_port`/`proxy_port`/`pw` 消除遮蔽。补 2 条回归（299→301）。|
| 42 | v1.8 修订 | **`PASS`（收敛后终检自检：F-37）** | 本轮靶：**str/path 参数入口**——公开 API 全量 fuzz（14 组垃圾 × id/cookie/交付/槽位四族）暴露：`safe_id(-1/3.5/True/list/dict/…)` 抛 **AttributeError**（`.strip` 炸）而非契约里的 ValueError（同函数对 str 才走拒绝路径——同一份不可信输入两种待遇）；`id8`/`cli_id` 同款；`parse_cookie_header`（文档写明「不抛错」）/`device_id_from_cookie`/`cookie_header` 对非 str 抛 AttributeError/TypeError；`extract_deliverable(None/3.5/inf)` 让 `os.path.isfile` 炸 TypeError（契约是 CustodyError；`isfile(True)` 还把 True 当 fd 1——F-31b 陷阱）；`release_slot([1])` 让 `os.path.exists` 炸 TypeError（契约是返回 False）。修复：`safe_id` 非 str 一律走 ValueError 拒绝路径；`id8`/`parse_cookie_header` 走 `_as_text`；`cookie_header` 走 `_as_seq`；`extract_deliverable`/`release_slot` 补路径类型守卫（`(str, bytes, os.PathLike)`，与 F-31b 锁判定同款）。补 4 条回归（301→305）。|



**第三十八轮改了什么（收敛后终检自检，非新会话裁定）**：**F-32 + F-33**——① **F-32**：`lease_heartbeat`/`lease_release`/`lease_reclaim`/`lease_acquire` 各补 `except OSError` 分支，分别返回 `False` / `False` / `(False, "write_failed")` / `None`；诊断细节一律走 `_diag(what, path, detail)` 落 WARN 日志，**不进返回值**——reason 是给机器做等值比较的稳定枚举，混入 `WinError 112` 文本后换个 Python 版本判定就变。语义方向均取保守侧：续期失败 = 本次没续上（租约可能因此过期）；释放失败 = 租约仍在（调用方记 `TIMEOUT_ORPHAN`，宁可留僵尸等人工闸口也不谎报已释放）；回收写失败 = `write_failed`（「没回收成」，不是「回收了但没写」）；领租约写失败 = `None`（没写成就是没拿到，绝不带病跑，否则绕过 B15 单实例保证）。② **F-33**（依用户裁定「429 限流换账号或者直接用匿名渠道」）：`--new` 显式主身份（`acct1`/`acct2`）遇 429 时，先把该身份记入 429 冷却（`COOLDOWN_429`），再转 `create_chat_rotating` —— 换另一个登录态，两者都 429 才启用匿名兜底。判据：429 时**会话尚未建成**，重试不会重复建会话、不会多花配额，所以显式指定身份只约束「用谁起」，不约束「限速后还能干什么」。401/403 不转（凭据失效要重新提取、风控短冷却自愈，换身份只会掩盖病因）；显式 `nologin` 已 429 也不转（再兜底还是匿名 = 无限自递归）。既有会话遇 429 只输出解释：会话归属固定，换号 404、匿名更读不到，只能按服务端时限退避或 `--new`。③ 探针实测（`_atomic_write`/`os.remove` 双失败）：四函数分别返回 `False`/`False`/`(False,'write_failed')`/`None`，无异常逃逸；正常路径 `acquire→heartbeat→reclaim(未过期)→release→reclaim(空)` 结果与修复前逐项一致。④ 补 7 条回归：`test_lease_heartbeat_write_failure_returns_false`、`test_lease_release_write_failure_returns_false`、`test_lease_reclaim_write_failure_returns_stable_reason`、`test_lease_acquire_write_failure_returns_none`、`test_explicit_account_429_escalates_to_rotation`、`test_explicit_account_401_does_not_escalate`、`test_explicit_nologin_429_does_not_recurse`；反证已做——回退四处 `except OSError` 后四条租约用例全 ERROR（`OSError` 逃逸），回退 429 转轮转后 `test_explicit_account_429_escalates_to_rotation` 报 `AssertionError: False is not true : 429 未进入轮转兜底`，均精确复现旧行为。**自检裁定：F-32/F-33 关闭；F-32 属「文档承诺 vs 实现」不一致（承诺确定性结局，实际让 OSError 逃逸），修复面限于四个对外函数的异常出口，未改动锁的两层互斥结构、`_atomic_write` 的原子性与 stale/回收判定顺序；F-33 属能力缺口（用户裁定要求的行为在显式身份路径上不存在），修复面限于 `main()` 的 `--new` 分支与既有会话的失败说明，未改动轮转计划、游标推进规则与匿名兜底放行条件。回归 290 全绿（1 条 POSIX-only skip）。**

**第三十九轮改了什么（收敛后终检自检，非新会话裁定）**：**F-34**——① 新增 `_as_int(value, default=0)` 入收敛族：只认「int 且非 bool」与「纯十进制数字串」（超长串在 3.11+ 让 `int()` 抛 ValueError，一并降级），float/bool/None/其余一律 default——**不折叠** `int(True)==1`、`int(3.5)==3`（F-07 口径）；`_parse_pid` 改为委托 `_as_int` + 正数判定，同一份不可信输入从此单一语义。② `_normalize_rotation` 全函数收敛、永不抛异常：非 dict 输入回空状态；`order` 走 `_as_seq` 过滤；`cursor` 走 `_as_int` 后取模（垃圾游标归零，不猜下标）；`created`/`failures` 逐键 `_as_int`；`unavailable` 逐项收敛成 `{reason, at, epoch, ttl}` 四字段（`_as_text`/`_as_float`）；`fallback_uses` 走 `_as_int`。③ 轮转读写 API（`is_unavailable`/`purge_expired`/`mark_unavailable`/`rotation_plan`/`anon_fallback_block_reason`）与匿名会话族（`load_anon_sessions`/`record_anon_session`/`mark_anon_archived`/`anon_session`）逐个点用点收敛——不假设调用方先 normalize（F-32「返回值是契约」在形状维度的复用）。④ `_load_json` 顶层类型守卫：`type(data) is not type(default)` 视同损坏，走既有 `.corrupt-<ts>` 隔离通道；`except` 收 `ValueError`（`JSONDecodeError` 是其子类，顺带把非法 UTF-8 的 `UnicodeDecodeError` 也纳入确定性结局）。⑤ `record_conv` 的 500 条淘汰排序键改 `_as_dict(data[x]).get("ts")`（坏条目不再让淘汰逻辑崩掉）。⑥ 探针实测 14 组形状/类型畸形 × normalize + 8 组轮转读写 + 6 组匿名会话 + 顶层类型坏：**0 逃逸**；`cursor=3.5/True/"3.5"` 归零、`cursor="7"` 保住计数（`7 % n`）。⑦ 补 6 条回归：`test_normalize_converges_polluted_shapes`、`test_normalize_does_not_fold_bool_or_float_cursor`、`test_rotation_helpers_total_on_polluted_unavailable`、`test_polluted_counters_increment_without_typeerror`、`test_top_level_type_mismatch_is_quarantined`、`test_anon_sessions_shape_pollution_never_escapes`；反证已做——回退 `_normalize_rotation`/`is_unavailable`/`load_anon_sessions`/`_load_json` 类型守卫后，14 个子用例红（10 ERROR 复现 ValueError/OverflowError/TypeError/AttributeError 逃逸 + 4 FAIL 复现折叠与未隔离），均精确复现旧行为。**自检裁定：F-34 关闭；属健壮性与一致性缺陷（F-12 口径未落到状态文件侧，同一不可信输入两种待遇），修复面限于收敛族补一件、状态读写入口的形状收敛与 `_load_json` 顶层守卫，未改动轮转计划/游标推进/兜底放行的业务语义、500 条淘汰策略与 `.corrupt-<ts>` 隔离机制本身；回归 296 全绿（1 条 POSIX-only skip）。**

**第四十轮改了什么（收敛后终检自检，非新会话裁定）**：**F-35**——① `normalize_proxy` 补端口校验：`urlsplit().port` 的 ValueError（超范围/非数字）改报「代理端口非法」，与坏 scheme/缺 host 同为配置非法 ValueError，错误出处从 urllib 内部拉回配置入口；② `_tcp_alive` 全函数收敛：`_as_int(port, None)` + 范围 1-65535，任何不可信端口返回 False（`int()` 的 TypeError/OverflowError 不得穿出，`True` 不折叠成端口 1）；③ `resolve_proxy_info` autodetect 循环同款 `_as_int` + 范围检查，`autodetect_ports` 畸形项（`inf`/`True`/`None`/dict/超范围）跳过不崩。④ 探针实测：`_tcp_alive` 10 组垃圾端口 0 逃逸；`normalize_proxy` 坏端口 3/3 报配置非法（合法 URL 不受影响）；`autodetect_ports` 7 组污染全部 `url=""` 干净降级。⑤ 补 3 条回归：`test_normalize_proxy_rejects_illegal_port`、`test_tcp_alive_never_raises_on_junk_port`、`test_autodetect_ports_pollution_never_escapes`；反证已做——回退三处后 8 个子用例红（5 ERROR 复现 TypeError/OverflowError 逃逸 + 3 FAIL 复现坏端口未拒），均精确复现旧行为。**自检裁定：F-35 关闭；属健壮性缺陷（异常出口未按契约折算 + 端口字段不收敛），修复面限于 `normalize_proxy` 的端口校验、`_tcp_alive` 与 autodetect 循环的入口收敛，未改动出口优先级、fail-closed 语义、socks 握手协议与 `_proxy_alive` 的真握手判定；回归 299 全绿（1 条 POSIX-only skip）。**

**第四十一轮改了什么（收敛后终检自检，非新会话裁定）**：**F-36**——① 全仓 `int()` 出口复核（按 F-35「数字字段一律 `_as_int`」教训扫一遍）：多数已在前几轮收口（`_as_ctime` 前置有限性检查短路、`_retry_after` 走 `_as_float`、`CHATGPT_REPLY_MAX_CHARS`/`CHATGPT_ANON_IDLE_SEC` 已有 `except ValueError`、`--fence` 已转 `CustodyError`），**三处真口子**：`AnonBrowser.__init__`（env/参数端口三类异常穿出构造器）、`_socks5_open`（端口三类异常 + struct.error 穿出，违背「失败=OSError」契约）、同函数密码变量 `p` 遮蔽端口变量。② 修复：`AnonBrowser` 端口 `_as_int` + 1-65535 校验，非法降级 9333 + WARN（环境变量写错不杀匿名通道，与 `CHATGPT_REPLY_MAX_CHARS` 「变量写错不该让整次收割白跑」同口径）；`_socks5_open` 入口 `dst_port`/`proxy_port` 双收敛（垃圾端口抛 OSError），认证段改名 `pw` 消除遮蔽。③ 探针实测：AnonBrowser 10 组垃圾配置全部降级 9333（合法 9444 不动）；`_socks5_open` 6 组垃圾端口/配置全部 OSError（0 TypeError/OverflowError/struct.error）。④ 补 2 条回归：`test_anon_browser_port_pollution_degrades_to_default`、`test_socks5_open_junk_port_raises_oserror_not_typeerror`；反证已做——回退两处后 10+ 子用例红（ERROR 复现三类异常逃逸 + FAIL 复现未降级），精确复现旧行为。**自检裁定：F-36 关闭；属健壮性缺陷（数字参数出口未按契约折算 + 变量遮蔽），修复面限于两个函数的入口收敛与改名，未改动 SOCKS5 握手协议、CDP 连接逻辑与降级默认值 9333 的语义；回归 301 全绿（1 条 POSIX-only skip）。**

**第四十二轮改了什么（收敛后终检自检，非新会话裁定）**：**F-37**——① 公开 API 异常出口全量 fuzz（14 组垃圾输入 × safe_id/id8/cli_id/split_name/parse_cookie_header/device_id_from_cookie/cookie_header/extract_deliverable/release_slot 等）：check_* 族全部 ScheduleError（契约内）、reply_digest/rotation_summary/device_summary/load_device_jar 全降级（已收口），**str/path 四族真口子**：id 校验族对非 str 抛 AttributeError 而非 ValueError；cookie 族违反「不抛错」承诺；交付提取与槽位释放让 os.path 炸 TypeError（其中 `isfile(True)` 把 bool 当文件描述符——F-31b 的 `open(True)` 陷阱在 os.path 上复现）。② 修复：`safe_id` 非 str 统一进 ValueError 拒绝路径（契约=「不合法 → ValueError」，不是 AttributeError）；`id8` 改 `_as_text`（bytes 解码、其余退 fallback，永不抛异常）；`parse_cookie_header` 改 `_as_text`（兑现「不抛错」文档承诺）；`cookie_header` 改 `_as_seq`；`extract_deliverable`/`release_slot` 补 `(str, bytes, os.PathLike)` 路径守卫（F-31b 同款），非法路径分别落 CustodyError / False。③ 探针实测：修复后同一批 14 组 × 6 函数 + 6 组路径 = **0 逃逸**（safe_id 全部 ValueError、extract 全部 CustodyError、其余全降级）。④ 补 4 条回归：`test_non_str_ids_use_contract_error_not_attributeerror`、`test_cookie_helpers_never_raise_on_non_str`、`test_extract_deliverable_non_path_is_custodyerror`、`test_release_slot_non_path_returns_false`；反证已做——回退六处后 25 个子用例 ERROR，精确复现 AttributeError/TypeError 旧行为。**自检裁定：F-37 关闭；属一致性缺陷（同一函数对 str/非 str 两种待遇，违反 F-12 口径；文档承诺「不抛错」被实现违背），修复面限于六个函数的入口收敛，未改动 id 白名单正则、cookie 解析语义、交付定位与槽位释放的业务规则；回归 305 全绿（1 条 POSIX-only skip）。**

**第三十一轮改了什么（依新会话裁定）**：**D-01**——`reply_digest` 将 `_split_parts(parts)` 与 `if malformed: return None` 移到 `author.get("role") != "assistant"` 判断**之前**（时间戳校验之后），确保**所有角色**的非法 part 都让完整性证明器整体不可用；assistant 的结构化 part 与「无文本」判断仍保留在角色判断之后，用户合法多模态 part 照常放行。补 1 条回归（268→269），三对照实测：user 非法 part→None、user 合法结构化 part→摘要成立、assistant 非法 part→None。

**收敛声明（第 32 轮，第 33～42 轮终检后仍成立）**：经三十二轮对抗审查，三个子系统全部收敛——①**锁/进程身份**：第 25 轮全文最终确认 PASS（逐函数 12 项 + 逐组合 7 组），第 36 轮补修 F-30（状态文件超大数值的 `OverflowError` 不得穿出锁判定，统一走 `_as_float`），第 38 轮补修 F-32（租约四函数的 `OSError` 出口必须落成文档承诺的返回值，诊断只进日志），第 39 轮补修 F-34（状态文件字段形状坏 = 「没有这个值」不是崩溃——收敛族补 `_as_int`，`_normalize_rotation` 全量收敛，`_load_json` 顶层类型坏同 A21 隔离），第 40 轮补修 F-35（代理端口字段收敛——配置非法在入口拒，探活函数永不抛异常），第 41 轮补修 F-36（剩余数字参数出口收口——AnonBrowser 端口降级、`_socks5_open` 契约内拒垃圾端口），第 42 轮补修 F-37（str/path 参数入口收敛——id/cookie/交付/槽位四族不再让 AttributeError/TypeError 穿出契约）；②**定时任务契约**：第 31 轮增量收敛（CR/LF 首尾绕过、内部空白、重复 rule part、非法/超长 INTERVAL、大小写不敏感、规范化下发、缺省语义全部覆盖，无新绕过路径），第 35 轮补修 `check_oneshot` 的 F-28（`now` 异常不得逃逸 TypeError）与 F-29（回显规范化，原串只留 audit 字段）；③**回复完整性/收割链路**：第 32 轮增量收敛（D-01 关闭，F-12～F-25 + D-01 全部核销），第 33 轮收敛后终检自检补修 F-26（digest/full 拼接口径统一）、第 34 轮补修 F-27（选节点口径对齐）、第 37 轮补修 F-31（SSE 解析器入口类型收敛，`_as_text`；锁判定入口路径守卫，F-31b）——均未推翻前述收敛结论。第 38 轮的 F-33 是**用户裁定带来的能力补齐**（429 换账号/匿名），不属对抗审查发现，不影响收敛结论。回归 **305 条全绿**（1 条 POSIX-only skip，含 PASS 后加证 1 条：`test_digest_none_never_leaves_harvest_complete` 钉住降级对称性）。收敛后不再开启新一轮对抗审查；新增功能或改动这三个子系统的实现时需重新走对抗审查。

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

**当前分布（49 条，按等级）**：**FULL 21 条**（核心判定 + 失败分支都有独立断言）；**PARTIAL 12 条**（含 A7/A20/B15：跨平台持久化与进程身份无法离线穷举，见矩阵）；**CODE_ONLY 3 条**（A9 A15 A18，有实现无回归）；**HUMAN 13 条**（A0 B0 B2 B3 B6 B7 B9 B13 B14 C1–C4）。**只有 FULL 才算「已机械化」**——按「有没有 test:」计数会系统性高估，这是第九轮审查的结论。

**已知缺口（按危害排序，写着就是为了下次被追）**：

1. **B2/B7 三态状态机没有共享实现**——每个任务各写一份，最容易在续跑时重复提交副作用；这是目前最大的机械化空白。
2. **B6 路径类型校验只在 prompt 里**——工作目录写错时，报错发生在半小时后的业务步骤，而不是 preflight。
3. **A7 缺「显式账号与登记冲突退 2」的回归**——代码路径存在，没有用例钉住。
4. **B9/B13/B5 的「已存在」判定依赖接口**——本地只能判「不该建」，判不了「该归档」。

补上一条，就把它从人判列移到测试列；这是唯一被承认的收敛方向。
