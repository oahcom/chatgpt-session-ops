# ChatGPT Backend API 协议细节（实测有效，2026-11-18）

## 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `https://chatgpt.com/backend-api/conversation/<conv_id>` | 拉全量会话 JSON（mapping 树），取 tail 与收割回复 |
| POST | `https://chatgpt.com/backend-api/sentinel/chat-requirements` | 取 `token` + `proofofwork{seed, difficulty}` |
| POST | `https://chatgpt.com/backend-api/conversation` | 发消息（SSE 响应） |

## 认证

- 头 `Cookie`：chatgpt.com / openai.com 域全部 cookie（含 httpOnly，须经 CDP `Network.getAllCookies` 提取，浏览器 document.cookie 拿不到）
- 头 `Authorization: Bearer <accessToken>`：来源同上（页面 JS 状态或 cookie 交换）
- 提取工具：`scripts/extract_auth.py`（Edge 调试端口 9222，页面须已登录 chatgpt.com）
- 认证失效表现为 401/403 且 body 无 "Unusual activity" → 重新提取，勿手填

## PoW 工作量证明（403 "Unusual activity" 的根因）

1. `POST /sentinel/chat-requirements`，body `{"p": null, "generator": "chat_requirements_cache_key", "proofofwork": null}` → 200 返回 `token` 与 `proofofwork.seed/difficulty`
2. 解算（`scripts/pow_solver.py`，毫秒级）：
   - 指纹数组 26 元素（UA/script_src/build/language/随机 nav-doc-win key…，服务端只验 hash）
   - payload = `base64(JSON.stringify(指纹))`（JSON 紧凑分隔符，UTF-8 → btoa 语义）
   - 逐 nonce 重试：`fnv1a_hex(seed + payload) 前 len(difficulty) hex ≤ difficulty`
   - 成品 token = `"gAAAAAB" + payload + "~S"`
   - FNV-1a-32 带 avalanche（`t ^= t>>16; t *= 0x85EBCA6B; t ^= t>>13; t *= 0xC2B2AE35; t ^= t>>16`）
3. 发送头：`OpenAI-Sentinel-Chat-Requirements-Token: <token>` + `OpenAI-Sentinel-Proof-Token: <proof>`
4. turnstile 挑战非硬性，可跳过

## POST /conversation 请求体

```json
{
  "action": "next",
  "conversation_id": "<目标 conv_id，必填！>",
  "messages": [{"id": "<uuid4>", "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["<提示词正文>"]},
                "metadata": {}}],
  "parent_message_id": "<tail 节点 id>",
  "model": "auto",
  "timezone_offset_min": 480,
  "suggestions": [],
  "history_and_training_disabled": false,
  "conversation_mode_kind": "primary_assistant",
  "force_paragen": false,
  "force_rate_limit": false,
  "reset_rate_limits": false,
  "websocket_request_id": "<uuid4>"
}
```

- **`conversation_id` 缺失时消息静默落入全新会话**（实测事故）。发送后必须解析 SSE 首个 `data:` 对象的 `conversation_id` 与目标比对。
- `parent_message_id` = GET conversation 的 mapping 中 `create_time` 最大的节点 id。GET 429 时用本地缓存 `conv-api/<id8>.json` 同法计算兜底。
- 开全新会话（Owner 模式）时**故意不带** `conversation_id`，此时也无 parent。

## SSE 响应解析

- 每行 `data: <json>`，`data: [DONE]` 结束。POST 返回 200 即消息已提交，流只是收发。
- 回复 = 最后一个 `author.role == "assistant"` 的 `content.parts` 拼接。
- **勿用 `with` 提前关连接**——会读空流；读到 EOF 再返回。

## 状态码处理

| 码 | 含义 | 动作 |
|---|---|---|
| 200 | 成功 | 解析 SSE 收割；比对 conversation_id |
| 401 | 认证失效 | 重跑 extract_auth.py |
| 403 "Unusual activity" | PoW 缺失/失败 | 重新 requirements + solve，非风控勿慌 |
| 429 | 限速 | 退避 ≥90s；GET 可用缓存 tail 兜底；发送器与补采器禁止并发 |
| 413 conversation_too_large | 会话满员 | `continue_full.py`：真实鼠标点「开始新聊天」→ 摘要续接 → 新会话重发 → 记 newchat-map.json |

## 页面层（P1，CDP）关键差异

- 输入框是 ProseMirror：**禁止 innerHTML**；用 `document.execCommand('insertText')` 或 CDP `Input.insertText`；清空 Ctrl+A+Backspace 真实键。
- 发送按钮匹配 `aria-label|data-testid` 的 `send|发送`，跳过 `aria-disabled`。
- 判流式：按钮含 `停止|stop`；收割条件 = 停止按钮消失 + 文本长度连续 3 次不变。
- 满员横幅「开始新聊天」必须**真实鼠标事件**（Input.dispatchMouseEvent）点击，js click 可能无效。
- Cloudflare「请稍候」质询：重启浏览器实例一次即过。

## 限速与并发纪律

- GET 配额紧张：同一账号同一时刻只允许一个进程发 GET；补采器与发送器错峰。
- 发送节奏：会话间 ≥60s，同会话等回复完成（2–7 分钟/条属正常）。
- 429 期间只用缓存 tail 维持发送，暂停全量补采。

## 匿名态（nologin）实测 — 2026-11-19

匿名能力走 `/backend-anon/` 前缀，不是 `/backend-api/`：

| 端点 | 结果 |
|---|---|
| `POST /backend-anon/sentinel/chat-requirements`（body 需 `{"p":null,"id":<oai-did>,"flow":"conversation"}`） | 200，`persona=chatgpt-noauth`，但 `turnstile.required=true`、`force_login=true` |
| `POST /backend-anon/conversation`（带 token + PoW，无 Turnstile） | **恒 403 `Unusual activity`** |
| `GET /backend-anon/conversation/<id>`（带页面 cookie） | **200，mapping 完整，可收割** |
| UI 建会话（真实浏览器） | 成功，落点 `https://chatgpt.com/uc/<id>` |

结论：匿名态 **POST 必须由浏览器完成**（Turnstile 由页面自动解），而 **GET 可以走 API**。因此实现为「UI 建/发 + API 收割」，cookie 取自页面 `document.cookie`（`oai-did` 等），无 httpOnly 依赖。

### 匿名会话的可写边界（实测 2026-11-19）

| 操作 | 结果 |
|---|---|
| 同一标签页连发两条 | **成功**，URL 保持在 `/uc/<同一 id>`，两条消息与回复连续累积 |
| 新标签页打开 `/uc/<id>` | **被重定向**到 `https://chatgpt.com/`，无法重开旧匿名会话 |
| 在 `/uc/<id>` 直接敲字发送 | 可（复用原 tab）；但一旦导航离开就再也进不去 |
| 按 id 调 `GET /backend-anon/conversation/<id>` | `200`，可读全文（13 分钟后仍有效） |
| 换陌生 `oai-did` 读同一 id | `404 {"code":"conversation_inaccessible","message":"请登录以查看此对话。"}` |

判定「会话是否还在」时别把所有非 200 都当失效：只有 `404 conversation_inaccessible` / `403` 才算会话不可用；`429`、`0`（连接异常）、`5xx` 都是通道问题，重试后应报「暂时读不到」而不是让调用方以为会话没了。

所以「续聊」= **复用创建它的那个标签页**（脚本按 targetId 记录在 `anon-sessions.json`），而不是导航到 URL；Tab 丢失即只能读不能写。

推论（已落进代码）：**新建匿名会话时不能导航现有页**。若当前附着的标签页 URL 是 `/uc/<id>`，`Page.navigate` 到首页等于永久毁掉那条会话的可写通道；正确做法是 `Target.createTarget` 另开一页再附上去。`--anon-list` 的 `live` 判定也是按 targetId 是否还在来算，不是按记录里的 status。

### 匿名会话的留存边界

| 检查项 | 观察 |
|---|---|
| `GET /backend-anon/conversations` | 恒 `200` + `0` 条：匿名会话不进任何列表/历史 |
| 归属 | 不属于任何账号，只与设备身份 `oai-did` 绑定；清 profile / 丢设备 cookie 即永久失联 |
| 唯一留存手段 | skill 侧 `anon-sessions.json`（id 对照表）+ `anon-sessions/<id8>.md`（全文归档） |
| 跨浏览器重启回读 | **可以**，前提是按设备分档存了 cookie：`accounts/nologin/devices/<oai-did>.txt`；`--anon-read` 用对照表里的 `device` 取对应快照（实测把 latest 换成陌生设备后仍 `200`） |

因此匿名通道的定位是**限速应急产能 + 一次性短任务**，不承担需要长期留存或跨机续接的任务。

浏览器侧三个坑：

1. 窗口未显示时 `document.visibilityState === "hidden"`，应用会挂起发送（按钮可见但点击无请求）→ 必须先 `Emulation.setFocusEmulationEnabled {enabled:true}`。
2. 无头模式一律卡在 Cloudflare「请稍候…」→ 必须用有头浏览器 + 临时 profile。
3. 输入框是 `<textarea id="mobile-composer-prompt">`：裸赋值不会被 React 采纳；用原生 `value` setter + `InputEvent('input')` 清空，用 CDP `Input.insertText` 输入，再对 `button[aria-label="发送消息"]` 做真实 `Input.dispatchMouseEvent`。

## 凭据残缺导致的伪风控

`cookies.json` 只有 `session-token` + `oai-did`（2 条）时：GET 全部 200，但 `POST /backend-api/conversation` 恒 403 `Unusual activity`。缺的是 Cloudflare 会话 cookie（`__cf_bm`、`_cfuvid`、`__cflb`）。用现有 cookie 访问 `https://chatgpt.com/` 接收 Set-Cookie 并合并回写即可自愈（`ChatGPTAPI.warm_cookies()`），无需重新登录提取。
