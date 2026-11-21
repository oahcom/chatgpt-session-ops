# 会话注册表异常记录审计说明

- **建立日期**：2026-11-21
- **裁定**：ChatGPT `DECISION-158`（`6b017d52-8a3c-83ec-8571-eb99be26e6f5`，acct2）
- **结论**：**9 条异常记录保持原样，本轮不修改任何业务数据**
- **适用范围**：`scripts/conv-accounts.json`（会话归属注册表）

---

## 1. 为什么单独写这份说明

`DECISION-158` 明确要求：这 9 条**不得补时间戳、不得删除、不得改变归属**，但必须有可追溯说明。

理由不是「懒得修」，而是三条硬约束：

1. **时间戳不可靠推导。** `ts` 是写入时刻的事实，不是能从记录顺序反推的数据。补一个猜测值等于往审计链里注入伪证。
2. **`note` 里的 `DO NOT dispatch` 是有意义的操作约束**，不是脏数据。删掉这些记录会解除约束。
3. **删除风险远大于收益。** `lookup_conversation_owner()` 靠这个文件判归属，删错一条 = 拿错账号读私有会话（A7 违例）。

---

## 2. 安全性质核验（裁定要求）

> 「缺少有效 `ts` 不得导致 `conv_owner()` 猜测账号归属。」

**核验结论：安全，不存在猜测路径。**

`lookup_conversation_owner()`（`chatgpt_send.py:3282`）的判定链**完全不读 `ts`**：

```python
record = data[conv_id]
if not isinstance(record, dict):
    return {"status": "invalid", "account": None}    # 非字典 → fail-closed

account = record.get("account")
if not validate_account_identity(account):
    return {"status": "invalid", "account": None}    # account 非法 → fail-closed

owner = record.get("owner")
if validate_account_identity(owner):
    if normalize(owner) != normalize(account):
        return {"status": "conflict", "account": None}   # 冲突 → fail-closed
return {"status": "known", "account": account}
```

- `ts` **一次都没被读取**
- `account` 缺失或非法 → 返回 `invalid`，`account=None`，**不猜测**
- `owner` 与 `account` 冲突 → 返回 `conflict`，**不猜测**

9 条异常记录的 `account` 全部合法（acct1 / acct2），所以归属判定**准确且确定**。`ts` 缺失不影响归属安全。

`conv_owner()`（`:3416`）同理，只 `.get("account")`。

---

## 3. 九条记录逐条说明

| ID | 长度 | account | 有 note | 标记 DO NOT dispatch | 分类 |
|---|---|---|---|---|---|
| `6afe889e` | 8 | acct2 | 否 | 否 | A. 短 ID + ts 缺失 |
| `6afeb34e` | 8 | acct2 | 否 | 否 | A. 短 ID + ts 缺失 |
| `6afeb7d8` | 8 | acct2 | 否 | 否 | A. 短 ID + ts 缺失 |
| `6afeb9ae` | 8 | acct2 | 否 | 否 | A. 短 ID + ts 缺失 |
| `6afedfdd-20cc-83e8-832a-b54fa690e412` | 36 | acct2 | 是 | **是** | B. 重复记录（run2） |
| `6afee158-e728-83ee-b058-abc1f668063c` | 36 | acct2 | 是 | **是** | B. 重复记录（run3） |
| `6afeee91-dba8-83ee-9cde-337b7e32c174` | 36 | acct2 | 是 | 否 | C. 人工收割 |
| `6afff365-00cc-83ee-87da-ab783357330e` | 36 | acct1 | 否 | 否 | D. ts 缺失 |
| `6afff5a9-4bbc-83e8-b4fd-a62077bfacf0` | 36 | acct2 | 否 | 否 | D. ts 缺失 |

### A. 四条 8 位短 ID（`6afe889e` / `6afeb34e` / `6afeb7d8` / `6afeb9ae`）

```json
{"account": "acct2", "prompt": null, "ts": null}
```

- 8 位**符合** A19 的 `^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$`，格式上合法
- 但真实 ChatGPT conv_id 是 36 位 UUID，8 位不像真会话 id
- `prompt: null`，说明不是通过正常 `record_conv(conv_id, account, prompt_file)` 写入的
- **不删的理由**：仅凭长度短就判定「不是真 id」是推断，不是证据。它可能是早期版本的合法 id，也可能是别处写入的短标识。删掉的代价是永久失去归属信息，保留的代价只是 4 条冗余。

### B. 两条重复记录（`6afedfdd` / `6afee158`）

```json
{"account": "acct2", "prompt": "state/relay-a-issue-align.md",
 "note": "DUPLICATE run2 of A session (5-field variant); DO NOT dispatch; authoritative=6afedebe"}
{"account": "acct2", "prompt": "state/relay-a-issue-align.md",
 "note": "DUPLICATE run3 of A session (backup 41.6K); DO NOT dispatch; authoritative=6afedebe"}
```

- 明确标注**重复运行**，权威记录是 `6afedebe`
- `DO NOT dispatch` 是**人工写下的操作禁令**
- **不删的理由**：这是有意保留的取证记录（同一 prompt 跑了 3 次，留痕说明为什么会有 3 个会话）。删了就看不出「曾经重跑过」。

### C. 一条人工收割（`6afeee91`）

```json
{"account": "acct2", "prompt": "state/relay-l2-03-i-security-global-forget.md",
 "note": "L2-03 security/global-forget; harvested manually (dispatch task killed before self-register)"}
```

- 投递任务在自登记前被杀，回复靠人工收割
- **不删的理由**：这正是 `note` 存在的意义——记录「为什么这条没有正常登记流程的字段」。删了就丢失了这个异常事件。

### D. 两条 UUID + `ts: null`（`6afff365` / `6afff5a9`）

```json
{"account": "acct1", "prompt": null, "ts": null}
{"account": "acct2", "prompt": null, "ts": null}
```

- 36 位合法 UUID，`prompt: null`
- 推测是早期 `record_conv()` 版本或手工写入
- **不删的理由**：id 合法、account 合法，归属信息完整。`ts` 缺失只影响排序，不影响路由。

---

## 4. 这些记录的已知影响（不是缺陷，是特性）

| 影响面 | 是否存在 | 说明 |
|---|---|---|
| 会话归属判定 | ❌ 无 | 只看 `account`，与 `ts` 无关 |
| `--list-all --apply` 候选筛选 | ❌ 无 | 候选来自实时枚举，不依赖注册表 `ts` |
| 按时间排序输出 | ⚠️ 轻微 | `ts: null` 的记录排在最前（空串排序） |
| 幂等性判定 | ❌ 无 | 用 `added_count`，不用 `ts`（DECISION-151） |
| 注册表高水位校验 | ❌ 无 | 校验 count + sha256，不看单条 `ts` |

**唯一可见影响是排序**，且只影响人工查看时的顺序，不影响任何自动决策。

---

## 5. 后续处置原则

`DECISION-158` 明确：

> 「不要再为已经明确保留的数据异常反复发起修复任务，除非出现新的安全证据。」

所以：

- **不要**再提「清理这 9 条」的任务
- **不要**给它们补 `ts`（补了就是伪证）
- **如果**未来发现某条记录的 `account` 是错的（会真的导致读错会话），那是**新证据**，按新事故处理
- 如果 `record_conv()` 将来加上 `ts` 必填校验，这 9 条会成为历史遗留——届时按「历史记录完整性原则」（E1）**追加勘误**，不改原始记录

---

## 6. 自查命令

```bash
# 列出所有 ts 缺失的记录（应恒为 9 条）
python - <<'EOF'
import json
d = json.load(open("scripts/conv-accounts.json", encoding="utf-8"))
bad = [k for k, v in d.items() if isinstance(v, dict) and not v.get("ts")]
print(len(bad), sorted(bad))
EOF
```

若数量**变多**，说明有新的写入路径绕过了 `record_conv()` 的 `ts` 赋值——那是新缺陷，需要查。
若数量**变少**，说明有人删了记录——违反本裁定，需要追责。
