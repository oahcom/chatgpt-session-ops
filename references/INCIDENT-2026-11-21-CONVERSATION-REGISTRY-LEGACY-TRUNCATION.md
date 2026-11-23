# INCIDENT 2026-11-21 — 会话注册表 legacy 剪裁（第二病因）

- **状态**：**CLOSED**（DECISION-144 D4：原剪裁机制已修复、历史数据已恢复，允许关闭）
- **事故编号**：`INCIDENT-2026-11-21-CONVERSATION-REGISTRY-LEGACY-TRUNCATION`
- **定级**：P2
- **裁定**：ChatGPT `DECISION-143`（`6b014426-11b4-83e8-81d5-fd7de03e54f8`，acct2）、
  `DECISION-144` 独立验收（`6b014d8f-e6f0-83ee-86b0-1a98bc102245`，acct3）
- **关联事故**：`INCIDENT-2026-11-20-CONVERSATION-REGISTRY-LOSS-AND-RECOVERY`（同一症状，独立病因，不合并）
- **剩余风险（独立追踪，不在本报告范围内）**：三个 legacy 裸写脚本的并发丢更新风险，
  见 §13。**关闭本报告不等于注册表治理全部完成。**

> **路径说明**：下文出现的 `C:/...`、`C:\...` 与 `2026-11-19-11-53-45/chatgpt-session-ops/`
> 是 2026-11-21 事故取证时的原始路径快照，作为证据原文保留，不代表当前路径。
> 权威仓库现位于本 checkout（相对路径 `.`，用 `git rev-parse --show-toplevel` 定位），
> 运行副本位于已安装的 WorkBuddy skills 目录；仓库策略见 `docs/RELEASE-AUTHORITY.md`。

## 1. 一句话

共享会话注册表 `conv-accounts.json` 存在**第二个写入者**——旧项目脚本
`WorkBuddy/2026-11-18-13-49-20/chatgpt_send_local.py`——它按 `ts` 只保留最近 500 条，
持续静默删除历史归属。11-20 那次「并发写入」修复后，这条删除路径仍在工作。

## 2. 与 11-20 事故的区别

| 项目 | 11-20 事故 | 11-21 事故（本文件） |
|---|---|---|
| 症状 | 注册表条目丢失 | 注册表条目丢失 |
| 根因 | 并发写入 + 无锁 restore | legacy 脚本按 ts 剪裁到 500 |
| 删除机制 | 并发覆盖 | 主动 `data.pop()` |
| 主脚本是否有问题 | 是（已修） | 否（主脚本从无 cap） |
| 修复 | 锁 + fail-closed restore | 封死第二写入者 + 单调性守卫 |

**不要把两者合并成一个根因。** 11-20 的修复是有效的，但它没有覆盖全部丢失路径。

## 3. 根因

`chatgpt_send_local.py:1806-1822`（修复前）：

```python
def record_conv(conv_id, account, prompt_file):
    ...
    def mutate(data):
        data[conv_id] = {"account": account, "prompt": prompt_file, "ts": _now()}
        if len(data) > 500:  # 只保留最近 500 条
            for k in sorted(data, key=lambda x: (data[x] or {}).get("ts") or "")[:len(data) - 500]:
                data.pop(k, None)
    _update_json(CONV_MAP_FILE, mutate, {})
```

- `BASE = r"C:/Users/Administrator/.workbuddy/skills/chatgpt-session-ops/scripts"`
- `CONV_MAP_FILE = os.path.join(BASE, "conv-accounts.json")` —— **与主脚本同一文件**
- 主脚本 `record_conv()` 无任何 cap；restore 路径明确注明不删（`chatgpt_send.py:2869-2871`）
- 因此**唯一的删除者就是这份 legacy 脚本**

## 4. 证据（2026-11-21 实测）

### 4.1 规模贴着 500 抖动

```
n=502   ts_min=2026-11-20 22:56:28   ts_max=2026-11-21 14:57:04   n-500=2
```

10 秒间隔连续采样：`500 → 500 → 500 → 503 → 503 → 504`（主脚本追加），
周期性回落到 500 附近（legacy 剪裁）。

### 4.2 刀切边界：存活条目 100% 是被刷新过 ts 的

`evidence/conv-accounts.pre-restore.json`（517 条）vs 当前注册表：

| 项 | 数量 |
|---|---|
| pre-restore 总条目 | 517 |
| 仍在当前注册表 | 197 |
| 已消失 | 320 |
| 存活且 pre-ts 早于当前下界 | 164 |
| 其中 cur-ts 被刷新 | **164** |
| 其中 cur-ts 未被刷新 | **0** |

样本（pre-ts → cur-ts）：

```
2026-11-20 17:54:56 -> 2026-11-20 23:15:54
2026-11-20 20:43:01 -> 2026-11-21 03:05:54
2026-11-20 20:43:01 -> 2026-11-20 23:19:12
```

**判定依据**：条目存活与创建时间无关，只与「ts 是否被后续操作刷新」有关。
这是「按 ts 剪裁到 500」的指纹。并发丢写会随机分布，不会让存活集 100% 都是被刷新过的。

### 4.3 归属分布被剪成畸形

| 账号 | pre-restore | 剪裁后 |
|---|---:|---:|
| acct1 | 393 | 354 |
| acct2 | 97 | 110 |
| acct3 | 15 | 38 |
| acct4 | **12** | **0** |

acct4 只在 11-20 建过会话、之后再未刷新 → 全部沉底被砍。
acct3 因最近活跃 → ts 被刷新 → 存活并新增。

### 4.4 二次剪裁叠加

- `conv-accounts.json.bak-20261120-203204`（496 条）：**373 条**在当前注册表已不存在
- pre-restore 517 条：**320 条**已不存在
- 两次丢失是**叠加**的，不是同一次

### 4.5 第二写入者确实在跑

```
15713 11:01 timeout 900 python chatgpt_send_local.py --account acct1 6b012317-... prompts/req04-status-sync-fix-subheads-v2.md
15717 11:01 C:\...\python.exe chatgpt_send_local.py --account acct1 ...
```

工作目录 `WorkBuddy/2026-11-18-13-49-20/`，不属于 skill 安装目录。

## 5. F1 — 全部注册表引用者清单

`grep -rl --include="*.py" -e "conv-accounts" -e "CONV_MAP_FILE"` over `WorkBuddy/`：

| 文件 | 指向 | 写注册表 | 处置 |
|---|---|---|---|
| `.workbuddy/skills/.../scripts/chatgpt_send.py` | 本体 | 是（唯一授权） | 加单调性守卫 |
| `2026-11-18-13-49-20/chatgpt_send_local.py` | 本体 | **是（剪裁 500）** | **降级只读** |
| `2026-11-18-13-49-20/map_req01.py` | 本体 | 是（裸 `os.replace`，无锁） | 历史脚本，不主动运行 |
| `2026-11-18-13-49-20/fix_map_req01.py` | 本体 | 是（裸 `os.replace`，无锁） | 历史脚本，不主动运行 |
| `2026-11-18-13-49-20/_m42d_ruling.py` | 本体 | 是（裸 `json.dump`） | 历史脚本，不主动运行 |
| `2026-11-18-13-49-20/survey_convs.py` | 本体 | 否（只读） | 无风险 |
| `2026-11-18-13-49-20/harvest_*.py`, `_l0_*.py`, `_q8.py`, `_m41_conv_harvest.py` | 本体 | 否（只读） | 无风险 |
| `2026-11-18-13-12-48/chatgpt-ops/*.py` | `Path(__file__).parent`（本地，非本体） | 是（本地文件） | 无风险 |
| `2026-11-19-11-53-45/chatgpt-session-ops/`（仓库副本） | 自身 | 是（副本） | 测试用，无风险 |
| `2026-11-19-13-04-48/.workbuddy/tmp/`, `2026-11-19-22-52-37/.workbuddy/verify/`, `2026-11-20-11-43-18/.sandbox/` | 自身 | 是（副本） | 测试用，无风险 |

**结论**：真正指向生产注册表且具备写能力的，只有主脚本 + 4 个 legacy 脚本。
其中只有 `chatgpt_send_local.py` 带剪裁。另外 3 个是裸写（无锁、无剪裁），
若被重新运行会退化为 11-20 那类并发丢更新，故一并列入「历史脚本，不主动运行」。

## 6. F2 — 两份快照交叉比对

| 项 | 值 |
|---|---:|
| pre-restore | 517 |
| bak-20261120-203204 | 496 |
| 交集 | 109 |
| 并集 | 904 |
| 并集中缺失于当前注册表 | 585（恢复执行时为 586，期间新增 1 条） |
| 同 id 但 account 不一致 | **0** |
| 待恢复集与当前注册表冲突 | **0** |

无冲突，可整体恢复。

## 7. F3 — `restore_conv_map_minimal()` 的锁与覆盖行为

`chatgpt_send.py:2728`：

- 取 `_file_lock(path, strict=True)` —— 与正常写入者同一把锁
- 锁内重新读盘（`existed_before_read` 检测隔离）
- 已存在条目**一律保留**，绝不覆盖
- 只追加缺失且合法的条目
- 只做一次 `_atomic_write`
- 锁内回读校验 `verified != merged` 即抛错
- `pruned: 0`，永不剪裁

**结论**：满足 DECISION-143 对恢复入口的要求。

## 8. 处置（已落地）

### 8.1 legacy 脚本降级只读（DECISION-143 第 3 项）

`chatgpt_send_local.py` 三处封死：

1. `record_conv()` 直接 `raise RuntimeError`（原剪裁逻辑删除）
2. `_update_json()` 在 `path == CONV_MAP_FILE` 时 `raise RuntimeError`
3. `_save_json()` 同上

即使旧目录被重新打开、被其他函数间接调用，也无法写注册表。
旧目录的提示词、证据、审计文件全部保留。

### 8.2 主脚本单调性守卫（DECISION-143 第 5、6 项）

新增 `RegistryShrinkRefused` + `_registry_guard(path, before, after)`：

**第一层（单次写入）**：`set(before) - set(after)` 非空 → 拒绝写入。

**第二层（跨进程）**：高水位基准 `conv-accounts.json.highwater.json`，
存 `{"ids": [...], "count": N, "updated": ts}`。
每次写入前比对 `baseline.ids - set(after)`，非空 → 拒绝。
基准用独立 tmp+`os.replace` 落盘，不走 `_atomic_write`，
以免破坏「注册表每次 mutation 只写一次」的既有契约（该契约由
`test_restore_uses_one_atomic_write` 守护）。

**作用域**：`_update_json()`（`record_conv` 等全部正常写入）、
`register_conversations_batch()`、`restore_conv_map_minimal()` 三处统一挂载。
只对文件名 `conv-accounts.json` 生效，不影响轮转/匿名等其他状态文件。

### 8.3 历史恢复（DECISION-143 第 7、8 项）

- 备份：`scripts/conv-accounts.json.pre-143-20261121-153954`（503 条）
- 候选：pre-restore ∪ bak，按 `conv_id` 去重，pre 优先（冲突 0）
- 入口：`restore_conv_map_minimal()`
- 结果：`added_count=586`，`conflict_count=0`，`invalid_ids=0`，`pruned=0`
- 恢复后：**1091 条**（恢复后 1089 + 期间新增 2 条真实会话）
- 归属分布：acct1 712 / acct2 302 / acct3 54 / acct4 12 / nologin 7 / default 2
- 裸字符串：0
- 并集 904 条 **全部到位**（missing=0）

### 8.4 验收断言

- 恢复后连续采样 `1091 → 1092 → 1092 → 1092`，**无下降**
- 高水位 `count=1091` 与注册表 `len=1091` 一致
- 全量回归 `418 passed, 1 skipped`（`test_hardening` 313 条，声明自校验一致）

## 9. 新增回归

`tests/test_hardening.py::TestRegistryMonotonicGuard`（3 条）：

| 用例 | 断言 |
|---|---|
| `test_record_conv_refuses_to_drop_existing_id` | 单次 mutation 删既有 ID → `RegistryShrinkRefused`，磁盘不变 |
| `test_guard_detects_cross_process_deletion_via_highwater` | 外部把文件清空后再写 → 高水位拦截 |
| `test_guard_allows_pure_addition` | 纯新增不受影响 |

## 10. 验收标准（DECISION-143 §8）对照

- [x] 旧脚本不能写入注册表
- [x] 旧项目目录重新运行不会破坏注册表（三处封死，含间接路径）
- [x] 已确认不存在其他未授权写入者（F1 清单）
- [x] 主脚本不存在 500 条剪裁（从未有过）
- [x] 主脚本能够拒绝删除已有 ID 的 mutation
- [x] 跨进程完整性检查能够发现历史 ID 消失
- [x] 历史恢复已完成，冲突项有独立清单（冲突 0，清单见 §6）
- [x] acct4 等历史归属已核实（恢复后 acct4=12，与 pre-restore 一致）
- [x] 恢复后连续运行验证没有再次发生条目下降
- [x] 11-20 与 11-21 两份事故报告均已正确关联

## 11. 不做的事（原则）

- **不以「恢复到 938 / 908 / 1091」作为成功标准。** 历史数字只作证据。
  成功标准是 ID 集合完整性、归属正确性、写入路径唯一性。
- **不删除旧项目目录。** 它含有历史提示词与审计证据，只降级为只读。
- **不合并两份事故报告。**

## 12. DECISION-144 追加处置（独立验收后补齐）

### 12.1 D1 — 三个 legacy 裸写脚本同批封死

| 文件 | 原写入方式 | 处置 |
|---|---|---|
| `map_req01.py` | 裸 `json.dump` + `os.replace`，无锁 | 首行 `raise SystemExit` |
| `fix_map_req01.py` | 裸写，无锁 | 首行 `raise SystemExit` |
| `_m42d_ruling.py` | 第 5 段裸 `json.dump` | 首行 `raise SystemExit` |

保留原脚本正文（提示词、审计材料、历史逻辑）在阻断语句之后，只读不执行。
运行时实测三个脚本均以 `read-only (DECISION-144 D1)` 退出。

### 12.2 D2 — 高水位从 fail-open 改为 fail-closed

原实现 `except OSError: pass` 已删除。按 D2 四态重写：

| 态 | 条件 | 行为 |
|---|---|---|
| A | 基准不存在 | 用**当前真实注册表**初始化（拒绝空集初始化），探针确认可写后落盘，然后放行 |
| B | 基准有效 | 正常比对 `known - after` |
| C | 基准损坏/缺 `ids`/不可写 | `RegistryBaselineUnavailable`，**拒绝 mutation** |
| D | 基准领先注册表 | `RegistryShrinkRefused`，**不自动降级**；人工核验后置 `CHATGPT_REGISTRY_HIGHWATER_RECONCILE=1` 重建 |

**提交顺序**：`_registry_guard()` 只做检查与初始化；
`_registry_commit_baseline()` 在注册表 `_atomic_write` **成功之后**才推进基准。
保证基准永不领先磁盘 —— 否则下一次正常写入会被自己的基准错误阻断。

### 12.3 D3 — 全部生产写入路径静态审查

AST 扫描 `chatgpt_send.py` 全部函数，筛出「触碰 `CONV_MAP_FILE` 且有写原语」的：

| 函数 | 写原语 | guard |
|---|---|---|
| `record_conv` | `_update_json` | ✅ 经 `_update_json` → `_registry_guard` |
| `register_conversations_batch` | `_atomic_write` | ✅ 直接调用 |
| `restore_conv_map_minimal` | `_atomic_write` | ✅ 直接调用 |
| `atomic_write_conv_map` | `_atomic_write` / `_save_json` | ✅ **本轮补挂**（原为漏点） |

`--list-all --apply` → `register_conversations_batch()` → `_registry_guard()`，
单一路径，不重复加 guard。

其余 17 个写原语函数（`save_rotation` / `record_anon_session` / `lease_acquire` 等）
均不触碰 `CONV_MAP_FILE`，不受此约束。

**唯一直接写注册表的原语**是 `_atomic_write(CONV_MAP_FILE, ...)`，
全部 4 处调用点均已前置 guard + 后置 baseline commit。

### 12.4 生产实测

- 基准与注册表一致：`registry 1095 == baseline 1095`
- 模拟 legacy 剪裁 20 条后尝试写入 → `RegistryShrinkRefused: registry lost 20 previously committed id(s) (cross-process)`，已还原

### 12.5 D4 — 关闭口径

本报告关闭的依据是**确定性 500 条剪裁机制已消灭 + 历史数据已恢复**，
不以「三个 legacy 裸写脚本全部封死」为前提（DECISION-144 D4 明确）。

§13 的剩余风险独立追踪，不并入本事故，也不把「潜在并发丢更新」误登记为「已发生的剪裁事故」。

## 13. 剩余风险（独立工作项，未闭环）

三个 legacy 裸写脚本已于 D1 封死。**仍开放的是治理层面的问题**：

- `WorkBuddy/2026-11-18-13-49-20/` 目录内仍有历史提示词与证据，
  未来若新建脚本直写 `conv-accounts.json`，现有守卫只拦「删除」，不拦「并发覆盖后重新新增」。
- 高水位基准只保证「已提交 ID 不消失」，不保证「同一 ID 的 account 归属不被改写」
  （归属改写由 `record_conv` 内部的冲突拒绝逻辑负责，属另一层）。
