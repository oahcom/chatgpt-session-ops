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

## 六、正式验收裁决（DECISION-151）

> **裁决：PASS（正式通过）**
>
> 验收依据：提交 `5f3a765` + 本文档。
>
> 1. 首次生产批量登记 315 条，注册表 1112 → 1427，新增数与预期完全一致。
> 2. Highwater 与注册表同步，审计 `CONSISTENT 1427 / 1427`。
> 3. 第二次执行无新增：`eligible_registrations=0`、`registered=0`，重复与冲突均为零。
> 4. 第二次执行期间字节变化，经差异定位与隔离实验，确认来自并发 `record_conv()` 对既有记录的合法更新，**而非** `register_conversations_batch()`。
> 5. 字节不变断言按 §4.3 修正：跨会话场景以「注册表 ID 集合单调不减、无新增 ID」为幂等性判据；字节完全一致仅在独占写入时作为附加断言。
> 6. `registered_not_observed=23` 作为历史已知偏差保留，不删除、不自动清理，不构成阻断项。
> 7. 本次未改生产代码（仅后补 §6.2 的语义修正）、未删除注册表条目、未改 acct4/acct6 凭据。
>
> **不得再以本次并发写入造成的字节变化为由，将门槛 B 重新判定为未完成。**

### 6.1 三项子裁决

| 编号 | 裁决 | 内容 |
|---|---|---|
| Q1 | 接受修正 | 断言措辞按 §4.3 改；**不要求**为通过门槛 B 而给 batch 加全局写锁或阻止并发会话 |
| Q2 | 不删除 | 23 条历史不可观测记录保留；不改 `_registry_guard` 删除阈值；不引入自动删除路径；如需清理须另行裁决 |
| Q3 | PASS | 门槛 B 正式关闭 |

### 6.2 落地：`registered` ≠ `added_count`（已实现）

裁决附带指出的测试规范缺陷：**返回数量与实际新增数量必须严格区分**。

隔离实验中传入 50 条既有候选，`register_conversations_batch()` 返回 `registered=50`，但磁盘一字未写。把 `registered == 0` 当作零新增，是错误等价的。

已实现（`chatgpt_send.py`）：

```python
# register_conversations_batch() 结尾
# DECISION-151：`registered` 是「已验证归属的候选数」，不是「本次新增数」。
# 传入已存在的候选时 registered 照常返回全部，但磁盘可能一字未写。
_batch_last_added = len(additions)
register_conversations_batch.last_added_count = _batch_last_added

# --list-all --apply 调用点
reconciliation["registered"] = register_conversations_batch(registrations)
reconciliation["added_count"] = getattr(
    register_conversations_batch, "last_added_count", None
)
```

实测：

```
已有候选 50 条: registered=50  added_count=0  sha_equal=True
全新候选  3 条: registered=3   added_count=3
混合批次  2 条: registered=2   added_count=1（1 条已存在 + 1 条新增）
```

回归：`TestBatchAddedCountSemantics` 3 条。回归总数 336 → **339**。

### 6.3 工程原则（保留）

> **幂等性不等于整个共享文件永远不变化。**

多会话并发写入系统中，三个问题不能用「一个 SHA-256 是否相等」代替：

1. 本次操作有没有新增数据？→ `added_count`
2. 本次操作有没有破坏既有数据？→ ID 集合单调不减 + `_registry_guard`
3. 其他合法写入者有没有更新既有数据？→ 不在本次判据内

## 七、遗留与观察

1. **并发写入是常态，不是异常**：多个 WorkBuddy 会话共用同一 `conv-accounts.json`，`record_conv()` 会刷新既有记录的 `prompt`/`ts`。字节级相等不能作为跨会话场景的断言。
2. `registered_not_observed=23` 稳定存在：历史已登记但当前五个账号的会话列表都枚举不到。**按 DECISION-151 Q2 保留，不清理。** 不能因为当前不可观测，就反向推定历史登记无效。
3. acct6 归属记录样本仍少，配额特征需继续观察。
