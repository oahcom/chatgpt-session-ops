# 事故记录：新会话投递失败导致 prompt 被自我删除

| 项 | 值 |
|---|---|
| 事故编号 | R381-prompt-loss |
| 发现时间 | 2026-11-24 12:00 (+08:00) |
| 发生窗口 | 2026-11-24 01:17:40 – 12:06:21（修复前；取自各文件首条 ts） |
| 影响 | 10 轮提示词永久丢失（不可恢复） |
| 修复提交 | `1546e93` |
| 修复 PR | #18 |
| 裁定来源 | ChatGPT R381：BUG CONFIRMED / DATA LOSS CONFIRMED，方案 A |

## 缺陷

`send_prompt` 异常分支对 `new_chat` 传 `target_conv=None`；`log_end` 里 `cid` 为空 →
走「无 CID」分支：把 `end` 写进 `unassigned/`，随后 `os.remove(begin_path)`。

`end` 记录**不含 prompt 字段**，`begin` 才是本轮提示词的唯一持久化副本 → 提示词彻底消失。

## 丢失明细（已脱敏：不含 prompt 正文、不含会话标识原文）

| # | 文件 | turn_id | 时间 | phases | 有 prompt | reply_state | status | 分类 |
|---|---|---|---|---|---|---|---|---|
| 1 | 20261124011731-6362f795-0b0fc489.jsonl | 20261124011731-6362f795 | 2026-11-24T01:17:40 | end | no | unavailable | 0 | `prompt_lost_due_to_begin_deletion` |
| 2 | 20261124102536-b363032f-2fb6144e.jsonl | 20261124102536-b363032f | 2026-11-24T10:25:37 | end | no | unavailable | None | `prompt_lost_due_to_begin_deletion` |
| 3 | 20261124103342-161acde8-d3c8fb7a.jsonl | 20261124103342-161acde8 | 2026-11-24T10:33:42 | end | no | unavailable | None | `prompt_lost_due_to_begin_deletion` |
| 4 | 20261124104133-94d35e06-3d80d8d5.jsonl | 20261124104133-94d35e06 | 2026-11-24T10:41:37 | end | no | unavailable | None | `prompt_lost_due_to_begin_deletion` |
| 5 | 20261124104421-c425d923-750904b4.jsonl | 20261124104421-c425d923 | 2026-11-24T10:44:22 | end | no | unavailable | None | `prompt_lost_due_to_begin_deletion` |
| 6 | 20261124104555-7e5df5bd-a029b05d.jsonl | 20261124104555-7e5df5bd | 2026-11-24T10:45:57 | end | no | unavailable | None | `prompt_lost_due_to_begin_deletion` |
| 7 | 20261124104621-efa23a28-6c8d4fbc.jsonl | 20261124104621-efa23a28 | 2026-11-24T10:46:23 | end | no | unavailable | None | `prompt_lost_due_to_begin_deletion` |
| 8 | 20261124105012-10ab0ae7-bbfd71cd.jsonl | 20261124105012-10ab0ae7 | 2026-11-24T10:50:13 | end | no | unavailable | None | `prompt_lost_due_to_begin_deletion` |
| 9 | 20261124115645-7a269cd8-530bf450.jsonl | 20261124115645-7a269cd8 | 2026-11-24T11:57:47 | end | no | unavailable | 0 | `prompt_lost_due_to_begin_deletion` |
| 10 | 20261124120621-68fca388-23a836a3.jsonl | 20261124120621-68fca388 | 2026-11-24T12:06:21 | end | no | unavailable | None | `prompt_lost_due_to_begin_deletion` |

> 表中 `file` 为日志文件名（turn 级时间戳+短哈希），不含任何对话内容。

## 修复

| 编号 | 内容 |
|---|---|
| R381-01 | `log_end` 无 CID 分支：若 begin 已在 pending，把 end 追加到**同一文件**并返回；不新建 unassigned、不删 begin |
| R381-02 | `_safe_conv_id` 三态：`""`=历史缺失 / 原值=合法 / `None`=畸形；R382-01 进一步收紧为严格小写 UUID |
| R381-03 | 全局不变量：begin 的 prompt 是唯一副本，删除/迁移/覆盖前必须证明已在别处完整落盘 |
| R381-04 | `--log-list --pending/--unassigned/--unresolved` 便于人工处置 |

**现存删除点**：`_migrate_pending` 末尾 1 处（回读确认 begin+end 齐全、payload 一致、conv_id 相符之后）。
**覆盖写**：0 处（`_log_write` 只追加）。

## 存量数据处置

- `logs/unassigned/` 空壳：**保留原样**，作为发生过丢失的证据；不伪造 prompt、不改写 reply_state
- `logs/pending/` 中断轮次：**保留原样**，待人工确认远端是否已执行，不自动补投（中断 ≠ 失败 ≠ 放弃）
- 原始 JSONL 留在私有日志目录（`logs/` 已 gitignore），本文件仅记录元数据

## 回归

- 新增 10 条测试，覆盖「新会话异常保留 prompt」「end 追加失败保留 begin」「三态矩阵」
  「畸形入口拒绝」「诊断文案区分」「无 cid 不删 conversations 里的 begin」「畸形 id 无路径穿越」
- 变异验证 5/5 定点命中
- 全量 606 passed / 6 skipped / 928 subtests
