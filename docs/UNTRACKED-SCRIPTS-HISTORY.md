# 未跟踪脚本生命周期记录

本文件记录 A 侧（WorkBuddy Skill 运行副本）脚本的出现与消失。它与
`docs/UNTRACKED-SCRIPTS-INVENTORY.md` 分工不同：

- **INVENTORY** 回答「现在有哪些文件、分别属于哪一类」——只描述当前状态。
- **HISTORY**（本文件）回答「过去出现过什么、什么时候没了、为什么」——只追加，不改写历史。

两份都不入库脚本本体；本文只记录事实，不构成发布授权。

---

## 2026-11-23：MCP OAuth connector 契约探索

当天为摸清 `/backend-api/aip/connectors/*` 的真实契约，在 A/scripts 里临时写了一批判定用的探索脚本。
契约固化为 `references/mcp-oauth-connector-contract.md` 后，这些脚本全部删除。

### 1. 历史快照中已有、当天删除（1 项）

| 文件名 | #95 快照中的分类 | 删除原因 |
|---|---|---|
| `mcp_link.py` | 本机运维（快照第 20 行） | 四步链路拆分为 `link_once.py`（执行）+ `verify_all.py`（校验），原脚本职责过载 |

这一项在 #95 的 49 行快照里**有**原始记录，因此不能计入「从未进入快照」。
原快照条目继续保留，作为历史证据。

### 2. 本轮创建、本轮删除、从未进入 #95 快照（17 项）

全部为一次性探索脚本，只在本轮使用过，从未提交到本仓库。

| 文件名 | 探索目的 | 删除原因 |
|---|---|---|
| `browser_link.py` | Playwright 走浏览器完成 connector 关联 | 改为 API 直连（`link_once.py`），浏览器路径无用 |
| `cap_callback.py` | 抓取回调请求的完整参数 | 回调端点已确认为 `links/oauth/callback` |
| `cap_create.py` | 抓取创建 connector 的请求体 | 已确认为 `{"mcp_url", "auth_request": {"supported_auth": [...]}, "logo_url": null}` |
| `cap_mcp_create.py` | 同上，侧重 UI 表单提交 | 同上 |
| `capture_full.py` | 抓取完整响应（含 header） | `WWW-Authenticate` 契约已确认，无需再抓 |
| `capture_oauth_req.py` | 抓取 OAuth 授权链路请求 | 四步链路已固化进 reference |
| `dump_dialog.py` | 打印 UI 弹窗 DOM | `/plugins` 入口已确认，UI 探索结束 |
| `fill_ui_create.py` | 表单填值提交创建 | 改为 API 创建，UI 路径不再使用 |
| `find_cb_endpoint.py` | 穷举回调落库端点 | 端点已确认，穷举无意义 |
| `get_code.py` | 从授权跳转链路里提取 `code` | 已并入 `link_once.py` |
| `mcp_create_oauth.py` | 创建带 oauth 的 connector（早期猜测写法） | 平铺 `auth_request` 是错误写法，已由 `mcp_create_ui.py` 取代 |
| `mcp_create_oauth2.py` | 同上，第二版猜测 | 同上 |
| `mcp_create_oauth3.py` | 同上，第三版猜测 | 同上 |
| `mcp_link2.py` | 同上，第二版 | 同上 |
| `post_callback.py` | 手工 POST 回调 | 已并入 `link_once.py` |
| `submit_ui_create.py` | UI 提交按钮点击 | 改为 API 创建 |
| `who2.py` | 早期身份探测 | 已由 `chatgpt_send.py` 的 `me` 命令覆盖 |

### 3. 另一项删除（1 项，未进入 #95 快照）

| 文件名 | 探索目的 | 删除原因 |
|---|---|---|
| `del_connector.py` | 用各种猜测端点删除 connector | 当时漏带 sentinel 头，误判成「无删除端点」；正确的 `DELETE` 契约已更正并写入 reference §5 |

### 4. 当天新增（进入当前 inventory）

`audit_rmc.py`、`cleanup_connectors.py`、`link_once.py`、`mcp_create_ui.py`、`probe_all_ids.py`。
其中 `cleanup_connectors.py` 与 `probe_all_ids.py` 在 #95 快照中已存在但标注「待核验」，
当天经源码核验后确认分类；`audit_rmc.py`、`link_once.py`、`mcp_create_ui.py` 为全新文件。

### 5. 口径说明

| 类别 | 数量 |
|---|---:|
| #95 快照中已有、当天删除 | 1 |
| 本轮创建、本轮删除、从未进入快照 | 17 |
| 另一项删除（未进入快照） | 1 |
| **当天删除合计** | **19** |
| 当天新增（全新文件） | 3 |
| 快照中已存在、当天经源码核验分类 | 2 |

「当天新增」与「快照核验」分开计数，两者不重叠。
