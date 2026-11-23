# MCP OAuth 连接器契约（ChatGPT 侧，2026-11-23 实测）

本文记录 ChatGPT 自定义 MCP 连接器从创建到删除的实测行为。全部结论来自抓包与可复现的 API 调用，不是官方文档推断。

**证据范围**：2026-11-23，单一部署（rmc-hub + nginx 网关）、六个账号。下文凡写成普遍陈述的地方，都应按「本次实测观察到」理解，不是对所有后端版本、账号类型、部署区域的承诺。

适用对象：`/backend-api/aip/connectors/*`。这些端点**不依赖已认证的浏览器登录 session**，但具体请求仍需按端点提供相应的 Bearer、cookie 与 sentinel PoW——`links/oauth` 实测需要 cookie + sentinel，见 §3。「不依赖登录态」不等于「不需要 cookie」。

---

## 0. 一句话结论

`type` 不是请求里能指定的字段，而是服务端对 MCP 端点做匿名探测后的**判定结果**；
`auth_request` 的形状决定这个判定能否生效；`WWW-Authenticate` 里有没有 `error` 参数决定探测走到哪一步。

三条都由本次实测支撑，但支撑范围仅限下文列出的分支；未列出的状态码和边界情况没有验证过，不要外推。

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

把返回的 `oauth_config` **整个对象**放进 `supported_auth` 数组（下面的 `…` 是占位说明，不是可直接执行的 JSON——字段以 `oauth_config` 的原样返回为准）：

```json
{
  "name": "…",
  "description": "",
  "mcp_url": "https://…/mcp",
  "logo_url": null,
  "auth_request": {
    "supported_auth": [
      { "...": "oauth_config 原样返回的字段" }
    ]
  }
}
```

### 形状错了会静默降级

| `auth_request` 写法 | 结果 |
|---|---|
| `{"supported_auth": [oauth_config]}` + `logo_url: null` | `200`，`supported_auth=[{"type":"OAUTH"}]` |
| 平铺 `oauth_config`（把 `oauth_config` 的字段直接放 `auth_request` 里） | `200`，但 `supported_auth=[{"type":"NONE"}]` |
| `{"type":"API_KEY"}` / `"BEARER"` / `"CUSTOM"` / `"NONE"` | `200`，`type` 一律 `NONE` |

**不要用「HTTP 200」当成功判据。** 必须回读 `supported_auth[].type`。

关于 `auth_request.type`：本次实测中，单独指定它为 `API_KEY` / `BEARER` / `CUSTOM` / `NONE` 都没有改变服务端最终判定，`supported_auth[].type` 仍由服务端探测结果决定。**不要把这条读成「整个 `auth_request` 无意义」**——上表第二行已经证明 `supported_auth` 的嵌套结构直接决定 OAuth 配置能否被识别。被忽略的只是那个 `type` 字段。

---

## 2. `type` 是怎么被判出来的

创建时后端会用 UA `openai-mcp/1.0.0` 对 `mcp_url` 发一次匿名 `POST`（JSON-RPC `initialize`）。

**已验证的分支（本部署，2026-11-23）**：

```
匿名 POST {mcp_url}
├─ 200                                   → 实测 type = NONE
└─ 401
     ├─ 挑战不带 error，且元数据发现成功     → 实测 type = OAUTH
     └─ 挑战带 error，或元数据发现失败       → 实测 424 mcp_error upstream_status=401
```

**这是三条已验证路径，不是穷尽状态机。** 下列情况没有测过，不要按上图推断：

| 未验证 | 为什么不能推断 |
|---|---|
| 400 / 403 / 404 | 可能直接失败，也可能被折算成别的错误码 |
| 3xx 重定向 | 是否跟随、探测目标是否随 `Location` 改变，未知 |
| 500 / 502 / 503 | 是否统一归 424，是否保留上游状态，未知 |
| 401 但完全没有 `WWW-Authenticate` | 会不会仍触发发现，未知 |
| 401 带多个挑战 | 选哪一个，未知 |
| 401 带 `error` 取 `invalid_token` 以外的值 | 是否同样提前终止，只验过 `invalid_token` |
| 元数据 200 但格式非法 | 算不算发现失败，未知 |
| 元数据可取但 `issuer` / `resource` 不匹配 | 是否通过客户端校验，未知 |

### 2.1 `WWW-Authenticate` 的 `error` 参数是硬门槛

在 401 上，挑战必须**不带** `error`：

```
# 通过 —— 纯挑战，客户端会去发现元数据
WWW-Authenticate: Bearer realm="…", resource_metadata="https://…/.well-known/oauth-protected-resource/mcp", scope="…"

# 失败 —— 带 error，创建直接 424
WWW-Authenticate: Bearer realm="…", resource_metadata="…", scope="…", error="invalid_token"
```

RFC 6750 §3.1 把 `invalid_token` 定义为「已提供的 access token 无效」，并规定请求**完全没有携带**认证信息时，资源服务器 SHOULD NOT 返回 error code 或其他错误信息（是 SHOULD NOT，不是绝对禁止）。因此对完全没带 `Authorization` 的匿名请求回 `error="invalid_token"`，不符合该规范推荐的取值。实测 `rmc-hub` 就是这么回的——它对没带 token 的请求也回 `invalid_token`，于是匿名探测被判成「硬认证失败」而不是「需要 OAuth」，创建直接 424。

### 2.2 nginx 修法及其代价

**先说结论：这是网关侧打补丁，不是干净解法。** 干净解法是让 MCP 服务端自己对匿名请求回纯挑战；只要服务端还这么回，网关就得改写，而改写有代价（见下）。

关键点是**判据必须落在请求上，不能落在状态码上**。只按 `$status == 401` 改写，会把「带了无效 token」的请求也改成纯挑战——那正好毁掉 RFC 6750 里唯一正确的信号，客户端收到纯挑战会以为该重做 OAuth 发现，而不是刷新 token。

```nginx
# 按请求是否携带 Authorization 分档，而不是按状态码一刀切。
map $http_authorization $rmc_challenge_by_request {
    ""      "Bearer realm=\"rmc\", resource_metadata=\"https://<host>/.well-known/oauth-protected-resource/mcp\", scope=\"rmc:tools\"";
    default "Bearer realm=\"rmc\", resource_metadata=\"https://<host>/.well-known/oauth-protected-resource/mcp\", scope=\"rmc:tools\", error=\"invalid_token\"";
}

map $status $rmc_challenge {
    401     $rmc_challenge_by_request;
    default "";
}

location /mcp {
    # proxy_hide_header 是无条件的：所有状态码的上游 WWW-Authenticate 都会被丢掉，
    # 然后由上面两个 map 重建。上游若在非 401 上也发挑战（如 403 insufficient_scope），
    # 那个挑战会被吞掉。
    proxy_hide_header WWW-Authenticate;
    add_header WWW-Authenticate $rmc_challenge always;

    # location 里一旦出现 add_header，就不再继承 server 层的 add_header。
    # 不抄一遍，/mcp 会丢掉 HSTS 和 X-Frame-Options（实测确认）。
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;

    proxy_pass http://127.0.0.1:8787;
}
```

`X-Content-Type-Options` 在实测里出现了两次（server 层一次、上游自带一次），值相同，无害；`strict-transport-security` 和 `x-frame-options` 只在下游恢复后出现一次。

三点实测结论（nginx 1.28.3 / Ubuntu）：

1. **空值 `add_header` 不会生成响应头。** 非 401 时 `$rmc_challenge` 为空，实际不发这个头。但这是 nginx 实现行为（`ngx_http_add_header()` 里有 `if (value->len)`），不是文档承诺；换版本要重测。
2. **`proxy_hide_header` 不分状态码。** 它隐藏的是该上游响应里的所有 `WWW-Authenticate`。代价如上。
3. **`add_header` 阻断继承。** 这是最容易被忽略的一条：加了 location 级 `add_header` 后，server 层的 HSTS / X-Frame-Options 不再下发到 `/mcp`。本项目实测确认过——修复前 `/mcp` 的 401 只有 `X-Content-Type-Options`，没有 HSTS 和 X-Frame-Options；修复后两者恢复。

改完必须验四态（`200` / `401` 匿名 / `401` 带坏 token / 非 401 错误），并确认没有重复的 `WWW-Authenticate`：

```bash
# 匿名：应只看到纯挑战，且带 HSTS
curl -sk -o /dev/null -D - -X POST https://127.0.0.1/mcp -H "Host: <host>" -d '{}' | grep -iE '^(HTTP|www-authenticate|strict-transport|x-frame)'
# 坏 token：应看到 error="invalid_token"
curl -sk -o /dev/null -D - -X POST https://127.0.0.1/mcp -H "Host: <host>" -H "Authorization: Bearer bogus" -d '{}' | grep -i 'www-authenticate'
```

### 2.3 不要做的两件事

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

### 3.3 三个凭据各管什么

容易混在一起的三件事，分工不同：

| 凭据 | 作用 | 本次实测是否需要 |
|---|---|---|
| `access_token`（Bearer） | 身份认证，`/backend-api/me` 认这个 | 所有 `aip/*` 都需要 |
| cookie | 绑定浏览器侧会话上下文 | `links/oauth` 实测需要；`list_accessible` 不需要 |
| sentinel PoW（`OpenAI-Sentinel-Chat-Requirements-Token` / `-Proof-Token`） | 请求完整性校验，缺失时纯 Bearer 会 403 | 除 `me` 之外的写操作都需要 |

「不需要已登录的浏览器 session」指的是：`/backend-api/me` 在浏览器匿名态下也返回 200（`ua-…`、`email:""`），所以不用为「cookie 里没有 `__Secure-next-auth.session-token`」去批量登录六个邮箱。**这不等于任何请求都不需要 cookie。**

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
2. **随机 nonce 塞进命令** —— 好一点，但 nonce 写在提示词里，模型可以直接复述，仍然不可信。实测这一轮只有 5 条 `exec_command`，不是 6 条。
3. **服务器上生成随机内容的文件，让模型 `cat` 它** —— 显著更强。文件内容既不在提示词里，也无法从上下文推断。但这是**降低**伪造可能，不是理论上的不可伪造。

### 7.1 第 3 级要配什么才算闭环

单靠「它答对了随机串」仍不够，因为「答对」和「真的执行过」是两件事。完整判据：

```
服务器生成随机内容 + 独立记下 SHA-256
  → 让模型用工具 cat 该路径并回报内容
  → 网关日志有对应 POST /mcp
  → 服务端日志有同一次 exec_command，outcome=ok
  → 回报内容与服务器原文逐字节比对（或比 SHA-256）
```

**`outcome=ok` 只证明服务端执行成功，不证明输出送达了模型。** 工具结果可能在传输环节丢失，模型也可能没收到就编了一段。所以必须做最后那步内容比对，不能只看日志绿灯。

### 7.2 日志关联的强度

时间戳对应是**辅助**关联，不是强关联：并发场景下同一分钟里可能有多条调用。有条件时应当用请求级 ID（`Mcp-Session-Id` 或上游自己的 request id）做一一对应。本次六账号是串行跑的（每账号间隔约 25 秒），时间戳足够区分，但这是测试排布带来的便利，不是方法本身的强度。

### 7.3 两侧核对

```bash
# 网关侧：每个账号应有成对的 list_devices + exec_command
grep 'POST /mcp' /var/log/nginx/mcp_auth.log | tail

# 服务侧：outcome=ok 才算通
journalctl -u <hub> --since "-10min" | grep 'tool="exec_command"'
```

注意两侧时区：nginx 日志用 `+0800`，hub journal 打 UTC。对不齐会误判成「没调用」。

---

## 8. 复现清单

「发现」和「验证」不是一回事：元数据 HTTP 200 只说明请求成功，不代表元数据符合协议，也不代表 `issuer` / `resource` 通过客户端校验。所以每一步都要回读判据字段，不能只看状态码。

| 步骤 | 端点 | 成功判据 |
|---|---|---|
| 取 OAuth 配置 | `POST …/mcp/oauth_config` | 200 + `oauth_config` 非空 |
| 建连接器 | `POST …/mcp` | 200 **且** `supported_auth[0].type == "OAUTH"`（只看 200 会漏掉静默降级成 `NONE`） |
| 取授权 URL | `POST …/links/oauth` | 200 + `redirect_url` |
| 提交回调 | `POST …/links/oauth/callback` | 200 |
| 校验 | `POST …/links/list_accessible` | 该 `connector_id` 出现 **且** `auth_status == "ACTIVE"` |
| 实跑 | ChatGPT 会话内调用工具 | 网关日志有 `POST /mcp` 200 + 服务端 `outcome=ok` + 回报内容与服务器原文一致 |
