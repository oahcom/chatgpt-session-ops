# MCP OAuth 连接器契约（ChatGPT 侧，2026-11-23 实测）

本文记录 ChatGPT 自定义 MCP 连接器从创建到删除的**真实**契约。全部结论来自抓包与可复现的 API 调用，不是官方文档推断。

适用对象：`/backend-api/aip/connectors/*`。身份只需要 access token + sentinel PoW，不依赖浏览器登录态。

---

## 0. 一句话结论

`type` 不是请求里能指定的字段，而是服务端对 MCP 端点做匿名探测后的**判定结果**；
`auth_request` 的形状决定这个判定能否生效；`WWW-Authenticate` 里有没有 `error` 参数决定探测走到哪一步。

---

## 1. 创建

```http
POST /backend-api/aip/connectors/mcp
```

必填：`mcp_url`（**不是** `base_url`）、`name`、`description`、`auth_request`、`logo_url`。

先取 OAuth 配置：

```http
POST /backend-api/aip/connectors/mcp/oauth_config
{"mcp_url": "<MCP endpoint>"}
→ {"oauth_config": {...}}
```

把返回的 `oauth_config` **整个对象**放进 `supported_auth` 数组：

```json
{
  "name": "…",
  "description": "",
  "mcp_url": "https://…/mcp",
  "logo_url": null,
  "auth_request": {"supported_auth": [ <oauth_config> ]}
}
```

### 形状错了会静默降级

| `auth_request` 写法 | 结果 |
|---|---|
| `{"supported_auth": [oauth_config]}` + `logo_url: null` | `200`，`supported_auth=[{"type":"OAUTH"}]` |
| 平铺 `oauth_config`（把 `oauth_config` 的字段直接放 `auth_request` 里） | `200`，但 `supported_auth=[{"type":"NONE"}]` |
| `{"type":"API_KEY"}` / `"BEARER"` / `"CUSTOM"` / `"NONE"` | `200`，`type` 一律 `NONE`——`auth_request.type` 被忽略 |

**不要用「HTTP 200」当成功判据。** 必须回读 `supported_auth[].type`。

---

## 2. `type` 是怎么被判出来的

创建时后端会用 UA `openai-mcp/1.0.0` 对 `mcp_url` 发一次匿名 `POST`（JSON-RPC `initialize`）：

```
匿名 POST {mcp_url}
├─ 200                      → type = NONE
└─ 401                      → 触发 OAuth 元数据发现
     ├─ GET /.well-known/oauth-protected-resource/mcp         (RFC 9728)
     ├─ GET /.well-known/oauth-authorization-server           (RFC 8414)
     └─ GET …/openid-configuration
          ├─ 全部成功         → type = OAUTH
          └─ 任一步失败        → 424 mcp_error upstream_status=401
```

### 2.1 `WWW-Authenticate` 的 `error` 参数是硬门槛

401 响应头里的挑战必须**不带** `error`：

```
# 通过 —— 纯挑战，客户端会去发现元数据
WWW-Authenticate: Bearer realm="…", resource_metadata="https://…/.well-known/oauth-protected-resource/mcp", scope="…"

# 失败 —— 带 error，创建直接 424
WWW-Authenticate: Bearer realm="…", resource_metadata="…", scope="…", error="invalid_token"
```

RFC 6750 规定 `error` 只在**出示过**无效凭据时才出现；对完全没带 `Authorization` 的请求回 `error="invalid_token"` 是不合规的。有些实现（实测：`rmc-hub`）就是这么回的，于是匿名探测被判定为「硬认证失败」而不是「需要 OAuth」。

**修法（nginx 侧，唯一必要改动）**：

```nginx
# 只对 401 给出纯挑战；nginx 会跳过空值的 add_header
map $status $challenge {
    401     "Bearer realm=\"rmc\", resource_metadata=\"https://<host>/.well-known/oauth-protected-resource/mcp\", scope=\"rmc:tools\"";
    default "";
}

location /mcp {
    proxy_hide_header WWW-Authenticate;
    add_header WWW-Authenticate $challenge always;
    proxy_pass http://127.0.0.1:8787;
}
```

### 2.2 不要做的两件事

- **不要放行匿名 `initialize`**：探测拿到 200 就判 `NONE`，连接器建出来没有认证。
- **不要用「返回空 tools 列表的匿名服务」绕过 424**：那是给探测造了个假 200，同样落到 `NONE`。

两条都试过，都能让创建「成功」，但产出的连接器全错。判断标准只有一个：回读 `supported_auth[].type == "OAUTH"`。

---

## 3. 关联（授权回调）

```http
POST /backend-api/aip/connectors/links/oauth
{"connector_id": "...", "name": "Primary", "callback_url": "https://chatgpt.com/connector_platform_oauth_redirect"}
→ {"redirect_url": "<authorization endpoint URL>"}
```

然后：GET `redirect_url` → 提交 Keycloak 登录表单 → 拿到最终重定向 URL（带 `code`）。

**落库靠 POST，不是 GET**：

```http
POST /backend-api/aip/connectors/links/oauth/callback
{"full_redirect_url": "<含 code 的完整回调 URL>"}
```

裸 GET 那个 `connector_platform_oauth_redirect?code=…` 只会返回 SPA 外壳，**不会**绑定账号。

### 3.1 code 有时效

分两个进程做（先取 code、再另起进程提交）会得到：

```
400 {"detail":"The authorization is invalid or has expired"}
```

取 code → 登录 → 提交回调**必须在同一进程内连续完成**。

### 3.2 `links/oauth` 只对自有 connector 生效

拿别人的私有连接器（`connector_status=ONLY_ME`）去 link，任何账号都是 `403 Unauthorized`。
**403 的解读是「无权访问该 connector」，不是「缺 session」。**

---

## 4. 校验

```http
POST /backend-api/aip/connectors/links/list_accessible
{"principals": []}
```

- body 必须是 `{"principals": []}`；传 `[{"type":"USER","id":…}]` 会 422（枚举只认大写 `USER`/`WORKSPACE`/`ORG`/`PROJECT`/`ENT_TENANT`）。
- 返回的 link 对象里看三项：`connector_id`、`actions`（数量 = 实际发现的工具数）、`auth_status`（`ACTIVE` 才算通）。

`actions` 为空**不等于**认证失败——它是 ChatGPT 那次工具发现被 401 拒了之后不再重拉的结果。但 `auth_status != ACTIVE` 就一定是认证没通。

---

## 5. 删除

```http
DELETE /backend-api/aip/connectors/<connector_id>   → 200 {}
```

早前记录过「没有删除端点」，那是错的：`DELETE` 和 `POST …/delete` 都试过，失败原因是当时请求没带 sentinel 头。带上 `OpenAI-Sentinel-Chat-Requirements-Token` + `-Proof-Token` 后 `DELETE` 稳定返回 `200 {}`，随后 `GET` 同一 id 返回 404。

---

## 6. UI 路径（浏览器兜底）

- 入口是 `/plugins`，**不是** `/settings/connectors`（后者对免费账号白屏，body 为空）。
- `/plugins` → 顶栏「添加」→「添加自定义 MCP 服务器」。
- 侧栏里也有一个「添加」，用途不同；用 JS `.click()` 会打到侧栏，必须用真实鼠标坐标点顶栏那个。

---

## 7. 端到端验证：不要相信「调用成功」这句话

模型经常在没真正调用工具的情况下，凭上下文或记忆复述一份看起来很真的结果。判据必须落在服务端日志上。

有效的验证顺序：

1. **只让它报设备清单** —— 不可靠。实测六个账号全说成功，服务端日志里一条 `POST /mcp` 都没有，且不同账号报出两套互相矛盾的清单。
2. **随机 nonce 塞进命令** —— 好一点，但 nonce 写在提示词里，模型可以直接复述，仍然不可信。
3. **服务器上生成随机内容的文件，让模型 `cat` 它** —— 可信。文件内容既不在提示词里，也无法从上下文推断，只有真跑到命令才拿得到。

配套核对（两侧都要看）：

```bash
# 网关侧：每个账号应有成对的 list_devices + exec_command
grep 'POST /mcp' /var/log/nginx/mcp_auth.log | tail

# 服务侧：outcome=ok 才算通
journalctl -u <hub> --since "-10min" | grep 'tool="exec_command"'
```

两侧时间戳能一一对应，才算「这个账号的 ChatGPT 真的摸到了服务器」。

---

## 8. 复现清单

| 步骤 | 端点 | 成功判据 |
|---|---|---|
| 取 OAuth 配置 | `POST …/mcp/oauth_config` | 200 + `oauth_config` 非空 |
| 建连接器 | `POST …/mcp` | 200 + `supported_auth[0].type == "OAUTH"` |
| 取授权 URL | `POST …/links/oauth` | 200 + `redirect_url` |
| 提交回调 | `POST …/links/oauth/callback` | 200 |
| 校验 | `POST …/links/list_accessible` | 该 `connector_id` 出现且 `auth_status == "ACTIVE"` |
| 实跑 | ChatGPT 会话内调用工具 | 网关日志有 `POST /mcp` 200 + 服务端 `outcome=ok` |
