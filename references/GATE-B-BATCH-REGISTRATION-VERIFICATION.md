# GATE-B — 常规批量登记生产验证记录

- **执行日期**：2026-11-21 17:21 – 17:33（GMT+8）
- **执行者**：Owner 授权，AI 机械执行
- **依据**：`register_conversations_batch()` 的独立生产验证（门槛 B）
- **相关裁决**：DECISION-147（账号扩容至五身份）、DECISION-145（Q2/Q3 注册表治理）

## 一、前置条件核对（执行前）

`python scripts/chatgpt_send.py --list-all`（只读，无 `--apply`）：

| 检查项 | 要求 | 实测 | 结论 |
|---|---|---|---|
| 各账号 `state` | 全部 `complete` | acct1/acct2/acct3/acct4/acct6 均 `complete` | 通过 |
| 各账号 `status` | 全部 200 | 全部 200 | 通过 |
| `reconciliation.complete` | `true` | `true` | 通过 |
| 候选残缺 | 无 | `invalid_ids=0`、`malformed_registry_records=0` | 通过 |
| 重复 | 无 | `duplicates=0` | 通过 |
| 冲突 | 无 | `registry_conflicts=0` | 通过 |
| 待登记数 | > 0 | `eligible_registrations=315` | 通过 |

执行前快照（`conv-accounts.json.pre-gateB-20261121-172156` 与 `.highwater.json.pre-gateB-20261121-172156`）：

```
conv-accounts.json                  bytes=185858  sha256=a6cfca69dd2f0cd2...
conv-accounts.json.highwater.json   bytes=48886   sha256=b86e27eddb210f85...
registry entries = 1112
```

## 二、第一次执行（`--list-all --apply`）

```
apply_requested            True
complete                   True
eligible_registrations     315
registered                 315      <-- 全部登记成功
observed_not_registered    315
registered_not_observed     23
duplicates                  0
registry_conflicts          0
malformed_registry_records  0
invalid_ids                 0
```

登记后即时核对：

```
registry   1427      (= 1112 + 315，完全吻合)
highwater  1427      (equal = True)
audit      CONSISTENT 1427 / 1427
bare strings 0
```

按账号归属：acct1 891 / acct2 421 / acct3 89 / acct4 15 / acct6 2 / nologin 7 / default 2。

## 三、第二次执行（幂等性验证）

```
eligible_registrations   315 -> 0     <-- 候选已被消化
registered               315 -> 0     <-- 无新增
observed_not_registered  315 -> 0
registered_not_observed   23 -> 23    <-- 历史已登记但当前枚举不到，两轮一致
duplicates                 0 -> 0
registry_conflicts         0 -> 0
```

**幂等性成立**：同一候选集第二次执行不产生任何新登记。

## 四、关于「注册表字节不变」的核查（重要）

原定收尾断言是「同候选第二次 `added_count == 0` 且注册表字节不变」。
实测发现：第二次 `registered=0` 成立，但**注册表字节变了**（225233 → 225234，sha 不同）。

### 4.1 差异定位

对两次内容做 `sort_keys` 规范化 diff，差异只有两条**已有记录**：

```
6b00735c  prompt: .../reqs/prompts/320-return.md  ->  .../reqs/prompts/321-return.md
          ts:     2026-11-21 17:20:27              ->  2026-11-21 17:29:19
6b0111c7  prompt: prompts/fix-rusqlite-043-api-r1.md -> prompts/compiler-state-machine-r1.md
          ts:     2026-11-21 17:05:51              ->  2026-11-21 17:31:33
```

两条记录的 `prompt` 与 `ts` 都被改写，而**批量登记写入的记录 `prompt` 恒为 `None`**（`register_conversations_batch` 不写 `prompt`）。
因此这两条改动**不可能**来自 batch。

### 4.2 并发写入者证据

同一时刻注册表里最新记录：

```
2026-11-21 17:29:19  6b00735c  acct3  .../2026-11-20-17-01-48/reqs/prompts/321-return.md
2026-11-21 17:31:33  6b0111c7  acct2  prompts/compiler-state-machine-r1.md
2026-11-21 17:32:16  6b00b0c9  acct2  prompts/259-shallow-clone-invalidated-258.md
```

这些路径指向**另一个 WorkBuddy 会话目录**（`2026-11-20-17-01-48`）与另一条工作线（rusqlite / compiler-state-machine）。
即：另一个并发会话正在用 `chatgpt_send.py` 投递提示词，其 `record_conv()` 改写了既有记录的 `prompt`/`ts`。

### 4.3 隔离验证（决定性）

在隔离副本上直接调用 `register_conversations_batch()`，传入 50 条**已存在**的候选：

```
candidates sent       50
registered returned   50
bytes before/after    225235 -> 225235
sha equal             True          <-- 字节完全未变
all already present   True
```

**结论**：batch 对「已在注册表中且归属一致」的候选**不写盘**。生产环境的字节变化由并发 `record_conv()` 引起，与门槛 B 的验证目标无关。

## 五、门槛 B 判定

| 验收项 | 结果 |
|---|---|
| 前置：五账号 `complete=true`、无 `token_revoked` | 通过 |
| 前置：候选非残缺（`invalid_ids=0`、`malformed=0`、`duplicates=0`、`conflicts=0`） | 通过 |
| 前置：执行前存快照 | 通过（两份备份 + sha256 记录） |
| 首次执行登记数 | 315，与 `eligible_registrations` 相等 |
| 注册表增量 | 1112 → 1427，差值恰为 315 |
| 高水位同步 | 1427 == 1427，`audit=CONSISTENT` |
| 第二次 `added_count` | **0** |
| 第二次注册表字节 | 因并发 `record_conv()` 而变；batch 本身经隔离验证**不写盘** |
| 幂等性 | 通过 |

**判定：门槛 B 通过。** 「字节不变」这一条按实际语义修正为「batch 不产生写入」，并已用隔离实验证明；不把并发写入者的改动记在 batch 账上。

## 六、遗留与观察

1. **并发写入是常态，不是异常**：多个 WorkBuddy 会话共用同一 `conv-accounts.json`，`record_conv()` 会刷新既有记录的 `prompt`/`ts`。字节级相等不能作为跨会话场景的断言，应改为「ID 集合单调 + batch 无新增」。
2. `registered_not_observed=23` 稳定存在：历史已登记但当前五个账号的会话列表都枚举不到。可能是被服务端回收或属于已删会话。**未做清理**，因为删除 ID 被 `_registry_guard` 一律拒绝（阈值 0），符合设计。
3. acct6 仅 2 条归属记录（1 次 smoke + 1 次自动轮转 + 1 次报告投递），样本量不足以评估其配额特征。
