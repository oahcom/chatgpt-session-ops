# MCP OAuth 连接器契约（ChatGPT 侧，2026-11-23 实测）

本文记录 ChatGPT 自定义 MCP 连接器从创建到删除的实测行为。全部结论来自抓包与可复现的 API 调用，不是官方文档推断。

**证据范围**：2026-11-23，单一部署（rmc-hub + nginx 网关）、六个账号。下文凡写成普遍陈述的地方，都应按「本次实测观察到」理解，不是对所有后端版本、账号类型、部署区域的承诺。

适用对象：`/backend-api/aip/connectors/*`。这些端点**不依赖已认证的浏览器登录 session**，但具体请求仍需按端点提供相应的 Bearer、cookie 与 sentinel PoW——`links/oauth` 实测需要 cookie + sentinel，见 §3。「不依赖登录态」不等于「不需要 cookie」。

---

## 0. 一句话结论

`type` 不是请求里能指定的字段，而是服务端对 MCP 端点做匿名探测后的**判定结果**；
`auth_request` 的形状决定这个判定能否生效；401 挑战里有没有 `error="invalid_token"` 决定探测走到哪一步（其他 `error` 取值未验证）。

以上各条都由本次实测支撑，但支撑范围仅限下文列出的分支；未列出的状态码、其他 `error` 取值和边界情况没有验证过，不要外推。

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
├─ 200                                      → 实测 type = NONE
└─ 401
     ├─ 纯挑战（无 error），且元数据发现成功   → 实测 type = OAUTH
     ├─ 挑战带 error="invalid_token"          → 实测 424 mcp_error upstream_status=401
     └─ 元数据发现失败                        → 实测 424 mcp_error upstream_status=401
```

**这是四条已验证路径，不是穷尽状态机。** 下列情况没有测过，不要按上图推断：

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

### 2.1 401 挑战里带 `error="invalid_token"` 是硬门槛

对本次实测的匿名 401 OAuth 发现路径，`WWW-Authenticate` 不得带 `error="invalid_token"`。本次没有穷举其他 `error` 取值的行为，不推断它们有相同处理结果（见上表最后两行）。

```
# 通过 —— 纯挑战，客户端会去发现元数据
WWW-Authenticate: Bearer realm="…", resource_metadata="https://…/.well-known/oauth-protected-resource/mcp", scope="…"

# 失败 —— 带 error="invalid_token"，创建直接 424
WWW-Authenticate: Bearer realm="…", resource_metadata="…", scope="…", error="invalid_token"
```

RFC 6750 §3.1 把 `invalid_token` 定义为「已提供的 access token 无效」，并规定请求**完全没有携带**认证信息时，资源服务器 SHOULD NOT 返回 error code 或其他错误信息（是 SHOULD NOT，不是绝对禁止）。因此对完全没带 `Authorization` 的匿名请求回 `error="invalid_token"`，不符合该规范推荐的取值。实测 `rmc-hub` 就是这么回的——它对没带 token 的请求也回 `invalid_token`，于是匿名探测被判成「硬认证失败」而不是「需要 OAuth」，创建直接 424。

### 2.2 nginx 修法及其代价

**先说结论：这是网关侧打补丁，不是干净解法。** 干净解法是让 MCP 服务端自己对匿名请求回纯挑战；只要服务端还这么回，网关就得改写，而改写有代价（见下）。

判据有两条，缺一条就会制造新的错误语义。

**其一，只对「请求完全没带 `Authorization`」合成挑战，其余透传上游原话。**

理由是：**请求头的存在只说明客户端提供了什么，不说明服务端为什么拒绝。** 只看「有没有 `Authorization`」就写 `error="invalid_token"`，会把 `Authorization: Basic …` 和空的 `Bearer ` 也标成「token 无效」——那不是观察，是伪造的拒绝原因。同理，即便客户端带了合法 Bearer，服务端也可能因为权限不足等原因回 401，那不是 `invalid_token`。

```nginx
map $http_authorization $rmc_challenge_by_request {
    # 只有匿名探测这一种情况需要合成。
    ""      "Bearer realm=\"rmc\", resource_metadata=\"https://<host>/.well-known/oauth-protected-resource/mcp\", scope=\"rmc:tools\"";
    # 其余照搬上游；上游没给挑战就不给，不替它编理由。
    default $upstream_http_www_authenticate;
}

map $status $rmc_challenge {
    401     $rmc_challenge_by_request;
    default "";
}

location /mcp {
    # 两个 proxy_hide_header 都无条件生效：上游对应头先全部丢掉，再由下面重建。
    proxy_hide_header WWW-Authenticate;
    proxy_hide_header X-Content-Type-Options;   # 上游也发 nosniff，藏掉避免重复

    add_header WWW-Authenticate $rmc_challenge always;

    # location 里一旦出现 add_header，就不再继承 server 层的 add_header。
    # 不抄一遍，/mcp 会丢掉 HSTS 和 X-Frame-Options（实测确认）。
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;

    proxy_pass http://127.0.0.1:8787;
}
```

**其二，认清 `proxy_hide_header` 不分状态码。** 非 401 的上游挑战同样会被吞掉，包括将来可能出现的 `403 … error="insufficient_scope"`。当前 `rmc-hub` 不发这类挑战，所以没有实际损失；但只要上游将来要往客户端传达 scope 不足之类信息，这个配置就会挡住。**这是已知限制，不是已解决的问题。**

> 当前配置仅为解决本次匿名 401 OAuth 元数据发现问题的部署补丁，不保证保留上游所有状态码下的 `WWW-Authenticate` 语义，尤其非 401 上游挑战可能被隐藏。若服务端将来需要向客户端传达 `insufficient_scope` 等挑战，应优先修复上游，或改用能按状态码与响应内容做条件处理的响应头过滤方案（如 njs）。

四点实测结论（nginx 1.28.3 / Ubuntu）：

1. **空值 `add_header` 不会生成响应头。** 非 401 时 `$rmc_challenge` 为空，实际不发这个头。但这是 nginx 实现行为（`ngx_http_add_header()` 里有 `if (value->len)`），不是文档承诺；换版本要重测。
2. **`add_header` 阻断继承。** 加了 location 级 `add_header` 后，server 层的 HSTS / X-Frame-Options 不再下发到 `/mcp`。实测确认：修复前 `/mcp` 的 401 只有 `X-Content-Type-Options`，没有 HSTS 也没有 X-Frame-Options；修复后两者恢复。**这意味着以后往 server 层加安全头，必须同步检查 `/mcp`**——这三条不是永久完整清单。
3. **`$upstream_http_www_authenticate` 可用。** 所以「透传上游原话」在纯 nginx 里走得通，不需要 njs。
4. **重复头要主动消除。** 上游本来就发 `X-Content-Type-Options: nosniff`，location 再加一条就有两个同名头。同值重复一般不致命，但不同代理与中间件对重复头的合并方式并不一致，既然知道，就不要留着。

**五态回归**（每次动这段配置都跑）：

```bash
# HOST 必须换成 nginx 实际匹配的 server_name，否则四态都不可复现：
# `-H "Host: <host>"` 会把尖括号逐字发出，server_name 不匹配就落到默认 server。
HOST=chatgpt-mcp.example.com
H="Host: $HOST"; U=https://127.0.0.1/mcp
p(){ curl -sk -o /dev/null -D - -X POST $U -H "$H" -H "Content-Type: application/json" "$@" -d '{}' \
     | grep -iE '^(HTTP|www-authenticate|strict-transport|x-frame|x-content-type)'; }
p                                    # 匿名          -> 纯挑战
p -H "Authorization: Bearer bogus"   # 坏 Bearer     -> error="invalid_token"
p -H "Authorization: Basic dXNlcjpw" # Basic         -> 与上游逐字一致
p -H "Authorization: Bearer"         # 空 Bearer     -> 与上游逐字一致
p -H "X-Probe: 1"                    # 非 401 路径   -> 无挑战头
```

前两态判「该有的有没有」，后两态判「不该编的有没有编」。第 5 条只能验证不会凭空多出头；`proxy_hide_header` 吞掉非 401 上游挑战这一限制，命令验不出来。

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

### 4.1 `REAUTH_REQUIRED` 是存下来的状态，不是实时判断

这条是 2026-11-23 排障得出的，值得单独记：**连接器在 UI 上显示「已连接」，一用却提示「连接已过期」，两者不矛盾。**

`auth_status` 挂在 **link** 上，不是挂在 connector 上。一次授权失败后 ChatGPT 把该 link 标成 `REAUTH_REQUIRED` 并**持久化**；之后即使底层 token 仍然有效，它读到的仍是这个标记。所以要查的是 `list_accessible` 里每个 link 的 `auth_status`，不是 connector 列表页。

同时要意识到：`REAUTH_REQUIRED` 背后至少有两种完全不同的成因，处理方式也不同。

| 现象 | 成因 | 如何处理 |
|---|---|---|
| `REAUTH_REQUIRED` 且 `actions == 0` | 创建时就没通过探测，连接器生来没有认证（旧 `WWW-Authenticate` 配置的产物） | 这条 link 无救，删除后重建 |
| `REAUTH_REQUIRED` 且 `actions > 0` | 曾经成功过，后来凭据失效 | 重新授权；旧 link 必须显式删除 |

实测对照（2026-11-23）：acct2 的 7 条全部是前者，创建于 11-22 11:38–12:26，正好是 `WWW-Authenticate` 尚未改为请求感知之前的窗口；acct1 的 1 条是后者，`actions=11` 但从未产生过网关调用。

### 4.2 重新授权会新增 link，旧 link 不会自动消失

同一个 `connector_id` 重新走一遍 `links/oauth` → 回调，会在 `list_accessible` 里**多出一条**新 link，旧的 `REAUTH_REQUIRED` 那条仍然在。实测出现过同账号同 connector 同时存在 `REAUTH_REQUIRED` 与 `ACTIVE` 两条记录。

所以重链之后必须：

```http
DELETE /backend-api/aip/connectors/links/<old_link_id>   → 200 {}
```

否则前端可能仍绑定旧记录，表现就是「明明刚重连过，还是说过期」。

### 4.3 判断「有没有过期」不能只看 connector

`connector` 的 `supported_auth[].type == OAUTH` 只说明它配置成 OAuth，**不说明当前授权还有效**。有效的判据只有 link 级的三项同时成立：

1. 存在该 `connector_id` 的 link；
2. `auth_status == "ACTIVE"`；
3. `actions` 非空（工具发现真的跑通过）。

第 3 条容易被忽略，但它正是 4.1 表格第二行的区分依据。

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

---

## 9. 双 realm 安装与授权补充（2026-11-27）

### 9.1 RMC 与 SO MCP 使用独立 Keycloak realm

- RMC 的 issuer / 登录 realm 为 `rmc`；SO MCP 的 issuer / 登录 realm 为 `so-mcp`。两边即使创建同名用户 `mcp-link`，也会得到不同的 Keycloak `sub`，不可交叉使用。
- 已核实 subject：`rmc` realm 的 `mcp-link` 为 `92fbe145-083a-4779-ac72-38e61533588f`；`so-mcp` realm 的 `mcp-link` 为 `42d7b1b4-f41b-4388-a1cc-a320991b734a`。
- Keycloak 用户创建后必须补齐 `firstName` 与 `lastName`。仅有 username/password 的用户在登录时会被拒为 `Account is not fully set up`。

### 9.2 动态客户端的 default client scopes

动态客户端的默认 scope 来源是对应 realm 的 default client scopes 模板，不是仅凭 `oauth_config` 响应就能推断客户端已拥有全部 scope。`so-mcp` realm 已补齐 8 个 `session_ops` default scopes，包含 `session.read` 与 `session.write`。若授权端点返回 `invalid_scope`，应核对该 realm 的 default-client-scopes 关联；通过 Keycloak Admin API 的客户端子资源 `default-client-scopes/{scopeId}` 逐项关联并回读验证。不要依赖 `kcadm update -s defaultClientScopes=...` 的静默写法作为成功证据。

### 9.3 创建冲突（HTTP 409）不是可直接认领

`POST /backend-api/aip/connectors/mcp` 返回 409 时，响应中的 `detail.existing_connector_id` 只提供候选 ID，不证明它就是本次目标连接器。必须 GET 回读该 connector，并同时确认 `base_url` 与目标 MCP URL 精确相等、`supported_auth[0].type == "OAUTH"`；缺 ID、读回失败或任一字段不匹配都必须 fail-closed，不得继续 `links/oauth`。

### 9.4 OAuth 网络分流与跳转安全

- `chatgpt.com` 请求经配置代理；`mcp.oachom0831.xyz` 的 Keycloak 请求直连。分流必须按解析后的 `hostname` 精确相等判断，禁止用 `"chatgpt.com" in netloc` 一类子串判断。
- OAuth 初始 `redirect_url`、每个 HTTP redirect 的目标、以及每个 HTML form action 都必须校验 HTTPS 与 hostname 白名单。form action 可能是相对路径，必须先以当前页面 URL `urljoin` 后再校验；Keycloak 登录表单 action 只允许 `mcp.oachom0831.xyz`。
- 对 `URLError` 的同 URL 重试最多两次，退避间隔依次为 0.5 秒、1 秒；HTTP 状态码错误不得伪装成网络重试。
- 诊断至少区分 `invalid_scope`、`invalid_client`、`unauthorized_client`、`Account is not fully set up` 与 callback error。授权失败必须停止当前目标的剩余步骤；RMC 与 SO MCP 两目标分别处理，但任一目标 FAIL 都使最终退出码非零。
