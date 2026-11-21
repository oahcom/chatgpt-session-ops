# INCIDENT-2026-11-20 — 会话归属注册表被历史裁剪器删除 387 条及恢复

- **状态**：CLOSED（事故恢复验收通过）
- **影响对象**：`scripts/conv-accounts.json`（会话归属注册表）
- **发现时间**：2026-11-20 21:38 前后
- **恢复完成**：2026-11-20 22:26（commit `e4ba59a`）
- **裁定来源**：ChatGPT 136 号（恢复实现）/ 138 号（关闭裁定）
- **⚠ 关联事故（后续发现，独立病因，不合并）**：`INCIDENT-2026-11-21-CONVERSATION-REGISTRY-LEGACY-TRUNCATION`
  —— 本报告的「并发写入 + 无锁 restore」修复是有效的，但共享注册表仍有**第二条丢失路径**：
  旧脚本 `WorkBuddy/2026-11-18-13-49-20/chatgpt_send_local.py` 按 `ts` 只保留最近 500 条，
  在本次修复之后继续静默删除历史归属（ChatGPT `DECISION-143` 定级 P2）。
  两者症状相同、根因不同，**不得合并为一个根因**。

---

## 一、事故经过

### 1.1 现象

`--list-all --apply` 前做跨账号对账时，候选集出现 377 条「越界 ID」：枚举到的会话 ID 在注册表里查不到。但该轮枚举只新增了 7 条记录。

排查后发现注册表被裁剪，数量轨迹：

```
886  ->  483  ->  502  ->  504  ->  508  ->  513  ->  517
```

对照备份（496 条）并集比对，**净丢失 387 条**，其中包含：

- 全部 7 条刚迁移完的 legacy 记录；
- `nologin` 7 条、`default` 2 条；
- 大量 11:31:49 之前写入的历史归属记录。

### 1.2 根因

旧版 `record_conv()` 带有 500 条全局裁剪：

```python
if len(data) > 500:
    # 按 ts 排序后砍掉最旧的部分
```

注册表被当成滚动缓存而不是归属档案。时间切面很干净：`min_ts` 固定上移到 11:34:26，而丢失记录最晚 `ts` 为 11:31:49；`513` 恰为「500 + 本轮新写入」。

真正的写入者是同机另一个 WorkBuddy 会话（PID 7100 / 2108，跑 `209-gate-verify-plan.md`），与本次任务共用同一 skill 安装和同一把跨进程锁。定位过程中发现的 3 个带 `if len(data) > 500:` 的旧副本各自 `BASE` 独立，均非元凶，仅作旁证。

主脚本在 21:38 时**已无裁剪逻辑**；此后 9 次采样 `min_ts` 不再上移，确认裁剪窗口已关闭。

---

## 二、恢复

### 2.1 方法

`restore_conv_map_minimal()`（`scripts/chatgpt_send.py`，commit `e4ba59a`），原则是**最小动作恢复**：

- 持 `_file_lock(strict=True)` 读取实时注册表；
- 只补「锁内确认缺失」的 ID，**绝不替换或修改既有记录**（live data always wins）；
- 合法 legacy 字符串在缺失时规范化为 `{"account":..., "prompt":None, "ts":None}`；
- 有新增时最多一次 `_atomic_write`；
- 锁内回读，与合并结果不一致即 `RuntimeError` fail closed；
- **不做任何裁剪**（`pruned` 恒为 0）。

候选来源：`union_candidate.json` —— 备份 496 条与当前 517 条的并集。

### 2.2 结果

```json
{"status":"ok","added_count":387,"preserved_count":513,"conflict_count":0,
 "conflicts":[],"invalid_ids":0,"final_count":904,"pruned":0}
```

| 验收项 | 期望 | 实测 |
|---|---|---|
| 恢复前记录数 | 517 | 517 |
| 恢复后记录数 | 904 | **904** |
| 补入 | 387 | **387** |
| 原有记录全部保留 | `set(pre) ⊆ set(post)` | **True** |
| 备份记录全部回补 | `set(backup) ⊆ set(post)` | **True** |
| 冲突 | 0 | **0** |
| 非法 ID | 0 | **0** |
| 裸字符串 | 0 | **0** |
| `pruned` | 0 | **0** |
| 测试套件 | 全绿 | **411 tests, OK (skipped=1)** |

并发保护实测：恢复执行时注册表已有 517 条（比候选生成时的 513 多 4 条），多出的 4 条是写入者在这期间新写的，**原样保留、未被回滚成候选快照**。

7 条丢失的迁移记录全部回来，`--owner` 均返回 `known:true` 且 exit 0；被裁掉的 `nologin` 7 / `default` 2 也一并回来。

### 2.3 事后采样

恢复后再次采样为 **908 条**（acct1 608 / acct2 264 / acct3 15 / acct4 12 / nologin 7 / default 2），`min_ts` 未上移。

> **904 是恢复完成时的验证值，908 是后续采样值，两者不是矛盾数据。** 归档时不得用 908 覆盖 904 的恢复报告。

---

## 三、账户枚举状态（同一时间窗）

| 账号 | 状态 |
|---|---|
| acct1 | COMPLETE |
| acct2 | COMPLETE |
| acct3 | COMPLETE |
| acct4 | **BLOCKED_CREDENTIAL_RENEWAL**（`token_revoked`） |
| 三账号登记任务 | COMPLETE |
| 四账号整体枚举 | **INCOMPLETE** |

acct4 服务端返回 `401 token_revoked`，只能由用户重新登录并重新导出凭据，无脚本路径。其可见会话（约 20 条）仍属**未验证**范围，12 条已注册记录不构成完整性证明。**不得把 3/4 写成 4/4，也不得把 acct4 的未知余量当成零。**

三账号在不完整枚举下的可判定字段全为零：`registry_conflicts` / `duplicates` / `malformed_registry_records` / `invalid_ids` = 0/0/0/0，1167 条可见会话（800/342/25）全部已登记。

---

## 四、证据清单与 SHA-256

原始证据 2026-11-20 会话工作区 `WorkBuddy\2026-11-20-11-43-18\`：

| SHA-256 | 字节 | 文件 |
|---|---|---|
| `e3124b0966fca34ff0e0f5445bf7a458e2d22ced8e3b447fac9ad5b34392dd55` | 40606 | `evidence/restore_report.json` |
| `67782c4ce9ce842b7ec315c557266b5651acd5e29563f7b8a98e1a3f1c06e6ae` | 73914 | `evidence/conv-accounts.pre-restore.json` |
| `d4d73ccf764d3a1b6b26065148a771bbac702c40b2699b53b59d707b2c9b3125` | 90391 | `evidence/conv-accounts.prod-backup.json` |
| `01057c792551784fdf6aa223106b539dc1cae8bbe1131bfb72a453bb8c9ab26e` | 70288 | `evidence/conv-accounts.snapshot-2148.json` |
| `8ed486d0b01341a4fee4439482af23ac9268d42523dbc1585fba50398516965c` | 70877 | `evidence/conv-accounts.snapshot-2152.json` |
| `de736de313e4e03f9cf72a91b0fdc86d55775cf4edf6b322b1148aba7c85fab8` | 71871 | `evidence/conv-accounts.prod-current.json` |
| `2802ee9654c33c2fd5956342f5ba977c6716f376cff26fdb0348fa53c08f55c5` | 28047 | `authorized_684_ids.json` |
| `8bf650359f996f164b7952de4007f137fd342d37fe100aba90f25a2de575450f` | 147979 | `union_candidate.json` |
| `fbc114e32801e3c371b37b894e16e87e02dfe60b45d8fa1074eaab7125116549` | 84891 | `evidence/lost_391.json` |
| `70dc12385bd2ab336d4c6659b62518f9a7a51dc1b008901070f184c418116364` | 66363 | `evidence/added_records.json` |
| `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` | 2 | `evidence/merge_conflicts.json`（空并集 `{}`） |
| `9ad6725ccaa83924aa6eafcb7c5725b80dec283ab10eec05d00fe1f17e6d7556` | 1488 | `evidence/evidence_summary.json` |

关联 commit：`e4ba59a514b5f490e3560619fca2a13b409d24e5`（2026-11-20 22:26:29 +0800，`scripts/chatgpt_send.py` +190 / `tests/test_aid_2026_011_restore.py` +325）。

归档纪律：原始证据只读保存，不覆盖式修改；恢复报告与原始输入必须能通过上面的哈希对应；后续 908 采样另存，不覆盖 904 恢复结果。

---

## 五、关闭裁定与遗留

**事故恢复：ACCEPTED / CLOSED。** 依据：387 条已补回、517 条全留、496 条全回补、4 条并发新增未覆盖、411 测试通过、恢复后 904 且后续采样 908。

遗留（**均不阻塞本次关闭**）：

1. **acct4 凭据续期** —— 用户重新登录浏览器并导出 `access_token.txt` / `cookies.json`；脚本侧无路径。
2. **常规批量登记路径的独立生产验证** —— `register_conversations_batch()` 至今**生产执行 0 次**。必须在四账号枚举全部 `complete` 且不含 `token_revoked`、候选并非残缺结果、执行前已存注册表快照时才能跑 `--list-all` → 确认 `complete=true` → `--list-all --apply`；收尾断言：执行次数 >0、`added_count` 等于执行前缺失的有效 ID 数、原记录全保留、零重复/冲突/畸形/非法、**同候选第二次执行 `added_count == 0` 且注册表字节不变**。
3. 本文档与 `SKILL.md` fail-closed 条款即留痕本身。

**禁止的结论**：四账号均已收敛 / 批量登记已生产验证 / 因事故恢复成功而省略常规路径验证 / 为关闭工单绕过 fail-closed。

## 六、职责边界

```
restore_conv_map_minimal()   -> 事故恢复专用：补齐缺失、保留实时、不覆盖并发、不裁剪
register_conversations_batch() -> 常规登记执行体：--list-all --apply
```

恢复函数已覆盖本次事故场景，故**不为本次事故补跑** `register_conversations_batch()`；但该函数的常规路径仍需按第五节第 2 条独立验收。
