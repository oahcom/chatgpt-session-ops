# 投递与编排契约基线（CONTRACT-BASELINE v1.0）

| 项 | 值 |
|---|---|
| 契约版本 | **v1.0** |
| 生效日期 | **2026-11-26**（+08:00） |
| 数字来源 | **2026-11-26 当日实测**：协调会话的投递日志、`SEND`/`TIMEOUT_PARTIAL` 记录、编号与合并台账、事实快照脚本 `facts.ps1` 输出 |
| 适用范围 | 本技能的一切投递（`--new` / `<conv_id>` / `--work-item`）、送达验证、提示词编写、Work Item 编号分配、PR 合并 |
| 优先级 | **高于 `SKILL.md` 与 `references/baselines.md` 中任何与之冲突的建议**（见 §6 冲突处置） |
| 复测要求 | **文中每个数字都是当日实测值，不是长期常数。复测后必须整份更新并升版本号**；只改一处数字、留下其它旧值，等于制造两套事实 |

**读法**：每条规则给「规则 / 依据（实测数字）/ 违反后果」三栏。依据栏的数字可以直接引用到提示词、复盘与裁决材料里；**引用前必须能在当日事实快照输出里找到出处**（CB-C11）。

---

## 0. 机读索引

机器可解析的规则表（供脚本、巡检、提示词生成器读取）。`enforce` 取值：`manual`（人工判据）、`grep:<正则>`（对投递日志判）、`script:<符号>`（脚本内已实现的常量/分支）。

```json
{
  "contract": "CONTRACT-BASELINE",
  "version": "1.0",
  "date": "2026-11-26",
  "priority": "overrides SKILL.md and references/baselines.md on conflict",
  "rules": [
    {"id": "CB-A1", "area": "delivery-params", "rule": "默认不使用 --thinking", "evidence": "同输出量 thinking 411.5s vs auto 209s（约 2x）；方差约 10x；13% 的 thinking 投递在服务端 1700s 断流墙上白跑", "violation": "单次预算翻倍且不可预测，排程整体漂移", "enforce": "manual"},
    {"id": "CB-A2", "area": "delivery-params", "rule": "新任务一律 --new 开新会话", "evidence": "--new 只省一次 tail GET（1-2s），不省时间；413 全部发生在续接（13/13），--new 为 0；被截断会话会毒化，后续 413 率 21.4% vs 1.9%（11x）", "violation": "撞 413、毒化会话、重投浪费整轮", "enforce": "manual"},
    {"id": "CB-A3", "area": "delivery-params", "rule": "发送预算优先级 CLI --timeout > CHATGPT_SEND_TIMEOUT > 运行时 tier；tier = 非思考 420s / 思考 1800s；600 只是保留常量，不是有效默认预算", "evidence": "scripts/chatgpt_send.py 的 _send_prompt_impl 分档 `1800 if (thinking and model) else 420`，显式配置在分档之后覆盖并打印 `timeout: <N>s (source=cli|env)`，未显式配置不打印该行；DEFAULT_SEND_TIMEOUT=600 不被任何发送路径当成预算；tests.test_hardening 的 1800/420/显式覆盖三例为证", "violation": "把 600 当默认值：非思考投递过早截断，思考投递被压到 600s 白丢正文", "enforce": "script:_send_prompt_impl"},
    {"id": "CB-A4", "area": "delivery-params", "rule": "不要拆长提示词，一次给全", "evidence": "r(log10 提示词字符, 耗时)=0.197；r(回复长度, 耗时)=0.690；每多一轮白付 40-105s", "violation": "白付固定开销，且多一轮就多一次失败面", "enforce": "manual"},
    {"id": "CB-A5", "area": "delivery-params", "rule": "同账号可并行 2-3 路 POST，但必须逐条送达验证", "evidence": "POST 不持账号锁；SLOT_CAP 未启用（并行上限未生效）；并行收益来自首字等待重叠", "violation": "并行却没有逐条验证 = 批量静默丢失", "enforce": "manual"},
    {"id": "CB-B6", "area": "delivery-proof", "rule": "SEND <prompt> -> 200 是唯一送达凭证", "evidence": "SEND 行由 _send_prompt_impl() 在 api.send() 返回后打印（scripts/chatgpt_send.py；截断时改打 SEND_TRUNCATED，两者互斥）", "violation": "只能证明脚本启动过", "enforce": "grep:^SEND .* -> 200"},
    {"id": "CB-B7", "area": "delivery-proof", "rule": "`timeout:` 行只在显式配置 timeout 时打印（source=cli|env）；默认 --new 路径 POST 之前一行都不打，0 字节日志是允许出现的状态", "evidence": "打印点在 override 非 None 分支，未配置时不打印（注释明说不打印 `timeout:` 行）；全仓没有 source=default 的发送/超时打印路径，source=default 只出现在 thinking model 的优先 slug 诊断行", "violation": "把 0 字节当「未发送」或「进程被掐」，重复投递已送达的提示词", "enforce": "manual"},
    {"id": "CB-B8", "area": "delivery-proof", "rule": "判定器 judge_send_log()：SEND → SENT；SEND_TRUNCATED → TRUNCATED；否则 IN_FLIGHT，除非 无 SEND 且 <200 字节 且 mtime>300s 且 无同 pfile 存活进程 且 不在 3600s 退避窗口内", "evidence": "scripts/chatgpt_send.py 的 judge_send_log() 及其常量 200/300/3600；存活进程与 liveness_unknown 一律按 IN_FLIGHT（fail-closed）", "violation": "拿单条（0 字节、或 5 分钟 mtime）当判死依据，把在飞投递判成丢失", "enforce": "script:judge_send_log"},
    {"id": "CB-B9", "area": "delivery-proof", "rule": "投递必须在同一个未结束的回合内等待完成（长 timeout 前台调用）；默认路径 POST 前 0 字节日志不能单独作为 NOT_SENT / 进程被掐 / 投递失败的证据", "evidence": "默认 --new 非思考路径在 api.send() 之前没有 print；0 字节 ⇒ 进程死亡的因果在代码与测试里均为 UNVERIFIED；判定器只以「空 + 超 300s + 无存活进程 + 不在 3600s 退避窗口」给出 NOT_SENT 建议", "violation": "把正常在飞状态当故障：重投、双回复、双工作项", "enforce": "manual"},
    {"id": "CB-B10", "area": "delivery-proof", "rule": "读日志必须先按 BOM 识别编码（多数 UTF-8，个别 UTF-16LE）", "evidence": "UTF-8 硬读 UTF-16LE 会把中文连同其后的 ASCII 一起吞掉", "violation": "漏匹配 SEND / IncompleteRead，验证结论反向", "enforce": "manual"},
    {"id": "CB-C11", "area": "prompt-discipline", "rule": "提示词里的每个数字必须来自当期事实快照（facts.ps1），不得凭记忆", "evidence": "同日出现凭记忆数字与实测冲突，被迫整轮返工", "violation": "整轮投递作废，且污染裁决依据", "enforce": "manual"},
    {"id": "CB-C12", "area": "prompt-discipline", "rule": "一轮一条指令，一次给全；不发「若 A 则 B」条件指令", "evidence": "条件在错误的时点被满足并执行，当天两次事故", "violation": "会话在错误时点执行错误分支", "enforce": "manual"},
    {"id": "CB-C13", "area": "prompt-discipline", "rule": "「不要做 X」必须附：若你认为该裁决有误，停下来指出，不要先做再解释", "evidence": "缺该句时会话自行扩权后解释", "violation": "越权执行，事后解释无法撤销", "enforce": "manual"},
    {"id": "CB-D14", "area": "numbering", "rule": "按账号固定号段，各自在号段内顺延；禁止抢 max+1", "evidence": "当日编号撞击 11 次，消耗 133 分钟", "violation": "改号、返工、合并冲突", "enforce": "manual"},
    {"id": "CB-D15", "area": "numbering", "rule": "已合并的 Work Item 编号不得改写；改号只能发生在合并前并记 RENUMBERED_FROM", "evidence": "合并后改号使追溯链断裂", "violation": "审计链不可复原", "enforce": "manual"},
    {"id": "CB-E16", "area": "merge", "rule": "原子合并：刷新 → 16 项全绿 → 立刻合并，中间不插任何其它 API 调用", "evidence": "strict:true 存在时，刷新到合并必须在 1-2 分钟内完成", "violation": "刷新结果过期，合并被拒，重跑一整轮", "enforce": "manual"},
    {"id": "CB-E17", "area": "merge", "rule": "不为了省一次刷新关闭分支保护的 strict", "evidence": "strict 是「PR 经过当前 main 验证」的唯一载体", "violation": "以验证换时间，合并了未经 main 验证的树", "enforce": "manual"}
  ]
}
```

---

## A. 投递参数

| 编号 | 规则 | 依据（2026-11-26 实测） | 违反后果 |
|---|---|---|---|
| **CB-A1** | **默认不使用 `--thinking`。** 只有用户显式要求深度推理、且已接受 2 倍时间预算时才开；开了就同时给足 `--timeout 1800`。 | 同输出量对比：thinking **411.5s** vs auto **209s**（约 **2×**）；thinking 的**方差约 10 倍**；thinking 有 **13%** 的投递在服务端 **1700s 断流墙**上白跑（连半截都收不到）。 | 单次投递预算翻倍且不可预测：排程按 auto 估的时间全错，长尾投递整轮作废。 |
| **CB-A2** | **新任务一律 `--new` 开新会话。** 不为了"省一次 GET"去续接老会话；既有会话投递（`<conv_id>` 路径）只用于明确需要多轮上下文的那一条链，且该会话一旦 413 或曾出现截断即弃用。 | `--new` 本身**不省时间**——只省一次 tail GET（**1–2s**）。但 413 的分布是压倒性的：**13/13 全部发生在续接**，`--new` 为 **0**。更关键的是会话被截断后会**毒化**：被截断会话的后续 413 率 **21.4%**，未截断会话 **1.9%**（**11×**）。 | 撞 413 → 整轮重投；毒化会话把 21.4% 的失败率带进后续每一轮。 |
| **CB-A3** | **发送预算按运行时 tier 走，不存在 600 秒默认值**：优先级 **CLI `--timeout` > `CHATGPT_SEND_TIMEOUT` > 运行时 tier**，tier 为 **非思考 420s / 思考 1800s**。超时产出 `TIMEOUT_PARTIAL`，并把已收内容落盘到 `<auth>/timeouts/`。确实需要长等待时**显式** `--timeout 1800`。 | 代码（本 revision，`scripts/chatgpt_send.py`）：`_send_prompt_impl` 内 `send_timeout = 1800 if (thinking and model) else 420`（:7093）；显式配置在分档**之后**覆盖，并打印 `timeout: <N>s (source=cli|env)`（:7098–7102）；**未显式配置时不打印该行**（:7096–7097）。`DEFAULT_SEND_TIMEOUT = 600`（:179）是**保留常量**，CLI 与默认路径都不把它当发送预算，**它不是当前有效的运行时默认发送预算**。测试：`test_hardening` 的 `test_cli_thinking_keeps_1800s_tier_without_timeout_flag`（1800）、`test_cli_thinking_tier_is_420_when_not_thinking`（420）、`test_cli_explicit_timeout_still_overrides_thinking`（900 且打印 `source=cli`）。历史分档证据：1800s 预算下 **162 次投递有 19 次（24.7%）**撞在服务端断流墙上，其中多数是纯白等。 | 把 600 当默认值：非思考投递过早截断；思考投递被压到 600s，白丢正文。 |
| **CB-A4** | **不要拆长提示词。** 一次给全，长度不是耗时主因。 | **r(log10 提示词字符, 耗时) = 0.197**（弱相关）；**r(回复长度, 耗时) = 0.690**（主因）。每多一轮，白付 **40–105 秒**固定开销。 | 拆成 3 轮 = 白付 2–3.5 分钟，外加 3 次失败面；且每轮都要重新送达验证。 |
| **CB-A5** | **同账号可并行 2–3 路 POST**（首字等待重叠是真实收益），**但并发必须配逐条送达验证**（CB-B6/B8）。GET 仍按 A15 单进程，不与 POST 混谈。 | POST **不持账号锁**；`SLOT_CAP` 未启用（代码里定义了上限但未生效）。并行收益来自等首字的时间重叠，不是吞吐上限被放大。 | 并行不验证 = 批量静默丢失（多个 0 字节/无 `SEND` 文件同时出现，且没人发现）；把并行套到 GET 上 = 一串 429。 |

---

## B. 送达验证（强制）

| 编号 | 规则 | 依据（2026-11-26 实测） | 违反后果 |
|---|---|---|---|
| **CB-B6** | **`SEND <prompt> -> 200` 是唯一送达凭证。** 没有这一行，只能证明脚本启动过，不能证明送达。汇报必须贴这一行原文，不得用"已投递""进程正常"替代。 | `SEND` 行由 `_send_prompt_impl()` 在 `api.send()` 返回后打印（`scripts/chatgpt_send.py`；本 revision 函数定义 :6999、POST :7103、打印 :7113 —— **行号随 revision 漂移，锚点以函数名与语义为准**），格式 `SEND <pfile> -> <status> respLen=<n> in <s>s`；截断时改打 `SEND_TRUNCATED pfile=<pfile> st=<s> bytes=<n>`（:7110），两者互斥。 | 把"启动了"当"送达了"：整条任务链在空转，下游却在等一个不存在的回复。 |
| **CB-B7** | **POST 之前不一定有日志。** `timeout: <N>s (source=cli|env)` 行**只在显式配置 timeout 时**打印；默认 `--new` 路径（未开 thinking、未给 `--timeout`/`CHATGPT_SEND_TIMEOUT`）在 `api.send()` 之前**一句都不打印**，因此 **0 字节日志是允许出现的状态**。 | 打印点在 `_send_prompt_impl` 的 `if override is not None:` 分支内（:7098–7102），同级注释明写"未显式配置时不打印 `timeout:` 行"（:7096–7097）。**全仓没有 `source=default` 的发送/超时打印路径**：`source=default` 只属于 `_preferred_thinking_slug()` 的 `thinking model: …（source=default）` 诊断行，与发送无关。 | 把 0 字节或"没有 timeout 行"当成"未发送/进程被掐"，重投 → 同一提示词两条回复、两个工作项。 |
| **CB-B8** | **判死不是三条件，而是判定器 `judge_send_log()`**：① `SEND … -> 200` → `SENT`；② `SEND_TRUNCATED` → `TRUNCATED`；③ 否则 `IN_FLIGHT`，**除非**同时满足「无 `SEND` 行 + <200 字节 + mtime > 300s + 无同 pfile 存活进程 + 不在 3600s 退避窗口内」才 `NOT_SENT`。存活进程查不到 = 按可能活着处理（fail-closed）。 | `judge_send_log()`（`scripts/chatgpt_send.py`，本 revision :351）；常量 `NOT_SENT_MAX_BYTES=200` / `NOT_SENT_MIN_AGE_SEC=300` / `INFLIGHT_BACKOFF_WINDOW=3600`（:252–254）。显式配置路径的 `timeout:` 首行判据由用例支持：`test_backoff_window_with_timeout_first_line_is_in_flight`、`test_same_log_past_backoff_window_is_not_sent`。**默认路径的 300s 只是"新鲜度"门槛，不是判死判据**：非思考 tier 本就有 420s，mtime > 5 分钟不足以说明任何投递结论。 | 拿单条判据（0 字节、或 5 分钟）判死：在飞投递被判丢失，幂等被破坏。 |
| **CB-B9** | **投递必须在同一个未结束的回合内等待完成**——用长 timeout 的**前台**调用（如 `timeout: 900000` ms），不要"短超时后台发射 + 结束回合"。**但 0 字节日志不是"进程被杀"的证据**：默认路径 POST 前本来就是 0 字节，它既不证明未发送，也不证明进程被掐，也不证明投递失败。 | `--new` 非思考路径在 `api.send()` 之前确实没有 `print`（:6999–7103 通读可见），所以 0 字节是该路径的正常中间态。"0 字节 ⇒ 进程立刻死亡"的因果**在代码与测试中都没有证据 = `UNVERIFIED`**；判定器给出 `NOT_SENT` 也只是"可以重投"的建议，不是"没送达"的证明。 | 把正常状态当故障：重投已送达的提示词；或反过来用 0 字节给失败背书，掩盖真正的失败原因。 |
| **CB-B10** | **读日志必须先按 BOM 识别编码**（多数 UTF-8，个别 UTF-16LE）。禁止 UTF-8 硬读。 | UTF-8 硬读 UTF-16LE：每个 ASCII 字节前夹 0x00，解码时中文与**其后的 ASCII 一起被吞掉**，`SEND`/`IncompleteRead` 这类关键行整段消失。 | 验证脚本给出"没有 SEND 行"的假结论 → 按 CB-B8 判死 → 重投一条实际已送达的提示词。 |

> **投递侧最小闭环**：前台等落地 → 读日志（BOM 识别）→ 找 `SEND ... -> 200` → 再谈回复质量。缺任何一步都不算投递完成。

---

## C. 提示词纪律

| 编号 | 规则 | 依据（2026-11-26 实测） | 违反后果 |
|---|---|---|---|
| **CB-C11** | **数字必须来自当期实测，不得凭记忆。** 投递前先跑事实快照脚本（本项目 `facts.ps1`），提示词里每个数字都要能在输出里找到出处（连同取值时间）。 | 同日出现"凭记忆的数字"与实测冲突，整轮提示词作废重写；会话据此做出的判断无法复核。 | 裁决建立在错数字上；复盘时无法定位是记忆错还是环境变了。 |
| **CB-C12** | **一轮一条指令，一次给全。** 不发"若 A 则 B"的条件指令；分支判断留到下一轮，用当期实测结果决定。 | 条件在**错误的时点**被满足并执行——当天两次事故（条件在中间状态成立，会话按"若 A 则 B"动了手）。 | 会话在中间状态执行了不该执行的分支，动作已发生、不可撤回。 |
| **CB-C13** | 指令里若包含"不要做 X"，**必须**追加一句：**"若你认为该裁决有误，停下来指出，不要先做再解释。"** | 缺该句时会话自行扩权：先做了再解释"为什么这样做更好"，把越权包装成优化。 | 越权执行；事后解释无法撤销已发生的写操作。 |

---

## D. 编号分配

| 编号 | 规则 | 依据（2026-11-26 实测） | 违反后果 |
|---|---|---|---|
| **CB-D14** | **按账号固定号段**，各自在自己号段内顺延，从机制上消除撞击。**禁止所有人抢 `max+1`。** | 当天编号撞击 **11 次**，为协调撞击消耗 **133 分钟**；撞击全部发生在"读 max → +1 → 写"这段窗口。 | 改号返工；一次撞击平均吃掉十几分钟，且会把已发出的引用一起作废。 |
| **CB-D15** | **已合并的 Work Item 编号不得改写。** 改号只能发生在合并前，并记 `RENUMBERED_FROM`。 | 合并后改号使 PR/提交/裁决记录里的旧编号全部指向空处。 | 审计链断裂：无法从旧编号追到新编号，也无法证明"这是同一个工作项"。 |

---

## E. 合并

| 编号 | 规则 | 依据（2026-11-26 实测） | 违反后果 |
|---|---|---|---|
| **CB-E16** | **原子合并**：刷新 → **16 项全绿** → 立刻合并。中间**不插任何其它 API 调用**（查 issue、发消息、跑扫描都算插队）。`strict: true` 存在时，从刷新到合并必须在 **1–2 分钟**内完成。 | 刷新结果带时间戳语义：`strict` 校验的是"PR 相对**当前** main 的最新验证"。1–2 分钟是留给人点按钮的窗口，不是留给跑别的任务的窗口。 | 刷新结果过期 → 合并被拒 → 重跑整轮（刷新 + 16 项校验）。中间插一次 API 调用就足以把窗口耗尽。 |
| **CB-E17** | **不为了省一次刷新去关闭分支保护的 `strict`。** | 那等于拿"PR 已经过当前 main 验证"换时间——省下的是 1 次刷新，付出的是这道验证本身。 | 合并进未经验证的树；下次回归红时无法区分是谁引入的。 |

---

## 6. 冲突处置（本契约与旧建议）

本契约生效后，下列旧建议**已失效**，执行时一律以契约为准。`SKILL.md` 内对应位置已改为指向本契约，不再保留第二套说法。

| 旧建议位置 | 旧说法 | 现口径 |
|---|---|---|
| `SKILL.md` §2「开全新会话」命令行注释 | `--thinking --new`「思考模式（评审/裁决必开）」 | **CB-A1**：默认不开；显式要求才开，并同时 `--timeout 1800` |
| `SKILL.md` §2 结尾段 | "都要后台跑，前台 120s 会截断" | **CB-B9**：投递/探针必须在同一未结束回合内前台等完（长 timeout），不以后台发射代替等待 |
| `SKILL.md`「会话归属与路由」 | "429 时对原身份退避至少 90 秒…**不要并发运行同一账号请求**" | **CB-A5**：POST 可并行 2–3 路（必须逐条验证）；429 退避按 `references/baselines.md` A17「先定性、按返回体真实时限」 |
| `SKILL.md`「协调纪律」#3 | "同一账号同时只允许一个 GET 进程；发送器与补采器错峰；**会话间发送 ≥60s**" | GET 单进程**保留**（A15）；固定 60s 发送间距**由 CB-A5 取代**（见 `references/baselines.md` A16 的取代注记） |
| `SKILL.md` 基线索引 A15–A18 行 | "会话间 ≥60s" | 同上：GET 单进程 + 发送间隔按 CB-A5 |
| `SKILL.md` 基线索引 A27–A28 行 | "要思考就必须真开"（未说默认值） | 语义不变，但**默认值由 CB-A1 决定：不开**；开则必须解析出 `-t-`，否则 `ThinkingModelUnavailable` |
| `references/baselines.md` A16 | "会话间 ≥60s；超时上限 非思考 7 分钟 / 思考 30 分钟；超时标 `TIMEOUT`" | 发送间距 → **CB-A5**；超时预算与落盘 → **CB-A3**（tier 非思考 420s / 思考 1800s，显式 `--timeout`/`CHATGPT_SEND_TIMEOUT` 覆盖，超时 `TIMEOUT_PARTIAL` 落 `<auth>/timeouts/`）。A16 的同会话串行、超时禁止自动重投两条**继续有效** |

**纪律**：发现第三处旧数字与新实测冲突时，**改契约 + 改指向**，不在别处另写一句"实际以某处为准"。

---

## 7. 复测协议（改数字的唯一合法路径）

1. 跑事实快照脚本（`facts.ps1`）拿到当期数字，连同取值时间一起记录。
2. 逐条对照 §A–§E：只更新有实测支撑的格子；没有新实测的格子保留原值**并标注"未复测（沿用 2026-11-26）"**——不得凭空改数。
3. 同步三处：本文表格、§0 机读 JSON、`SKILL.md` 里引用该契约的一行摘要。三处不一致即视为文档缺陷。
4. 升版本号（v1.0 → v1.1），并在提交信息里写明"数字来自 <日期> 实测"。
5. 回归：`python -m unittest tests.test_skill_reference_index` 必须绿（本文件在 `SKILL.md` 的 `references/` 索引里）。

---

## 8. 与其他基线的关系

- 本契约只覆盖**投递参数、送达验证、提示词纪律、编号、合并**五块，其余（认证、匿名、MCP、安全、排程）仍按 `references/baselines.md`。
- 与 A 类条款冲突时以本契约为准（例如 A16 的固定 60s 间距）；**未被本契约覆盖的 A 类条款继续全效**——"契约更新"不是"基线作废"。
- `references/evidence-matrix.md` 记录的是**基线条款**的证明；本契约的证据是**当期实测数字**，不进入该矩阵，但复测记录必须与契约同版本保存。
