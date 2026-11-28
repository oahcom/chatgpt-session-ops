# 凭据目录结构（动态 acctN / nologin）

**这些目录不入库**（`.gitignore` 已排除 `cookies.json` / `access_token.txt` /
`devices/` / `device-cookie.txt`）。这里只说明**该放什么、放成什么样**，
不要提交任何真实凭据。

## 登录态（acctN）

每个目录两份文件：

```
accounts/acctN/
├── cookies.json       # cookie 数组，见下
└── access_token.txt   # 纯文本，单行，无尾随换行
```

`acct1` 优先使用 `accounts/acct1/`。为兼容旧版本地 CLI，如果该目录
不存在，才回退到默认凭据根（`CHATGPT_AUTH_DIR` 或 `scripts/` 本身）。
统一 MCP 部署时，凭据根固定为 `/var/lib/chatgpt-session-ops`，因此所有登录账号统一放在
`/var/lib/chatgpt-session-ops/accounts/acctN/`；不要把凭据放进只读的 `/opt/so-mcp` 应用树。

### cookies.json

顶层必须是**数组**（不是 `{"cookies": [...]}`），每项至少含 `name` / `value` / `domain`：

```json
[
  {"name": "__Secure-next-auth.session-token.0", "value": "...", "domain": ".chatgpt.com"},
  {"name": "__Secure-next-auth.session-token.1", "value": "...", "domain": ".chatgpt.com"},
  {"name": "oai-did", "value": "...", "domain": ".chatgpt.com"},
  {"name": "__cf_bm", "value": "...", "domain": ".openai.com"}
]
```

**`domain` 只允许 `chatgpt.com` / `openai.com` 及其子域**（A29 / DECISION-156）。
其余域一律被丢弃——脚本在 `__init__` 拼请求头、`warm_cookies()` 回写、
`Storage.getCookies` 导入三处都会过滤，所以混进来的 Google 会话发不出去、
也存不下来。**不要手工往里加非 OpenAI 域**，加了也只会被静默丢掉。

### access_token.txt

从 `/api/auth/session` 的 `accessToken` 字段取。**单行、无尾随换行**——
带换行会让长度校验差 1 字节，与其它账号不一致。

## 匿名态（nologin）

```
accounts/nologin/
├── device-cookie.txt          # 最新一份 Cookie 头（兼容旧版）
└── devices/<oai-did>.txt      # 按设备分档的 Cookie 头
    devices/<oai-did>.json     # 同设备完整 jar（含 HttpOnly），可回灌浏览器
```

匿名会话**只认 `oai-did`**：换浏览器 profile 后设备号变了，新 cookie 读旧会话
就是 404。`devices/` 扫掉 = 那些设备上的历史匿名会话永久失联。

## 提取方式

- **acct1**：`python scripts/extract_auth.py`（走 CDP，默认端口 9222）
- **acctN**：已登录的 InPrivate 窗口里同源 `fetch('/api/auth/session')` 取
  token、`document.cookie` 取可读 cookie，导出 JSON 再落到对应目录。
  浏览器 CSP/PNA 会拦发往 `127.0.0.1` 的 fetch，**兜底走「下载 JSON 再读文件」**。


## 自动发现与轮转

- MCP 部署以 CHATGPT_SESSION_OPS_ACCOUNTS_DIR=/var/lib/chatgpt-session-ops/accounts 作为只读凭据库；ChatGPTDirectClient 在 CHATGPT_SESSION_OPS_RUNTIME_DIR 下建立每个账号的运行时目录，并将凭据文件只读 symlink 到 vault。
- CLI 的 BASE 指向运行时目录，因此账号发现读取运行时镜像；每次预检都会同步新出现的 vault 账号，按数字自然序发现，不需要改代码或白名单。
- 凭据库只需要目录可遍历、文件可读；不要要求账号凭据文件可写。首次运行时运行时凭据路径是 vault symlink；若 warm_cookies 需要原子回写，symlink 会被安全替换为运行时本地 overlay，vault 原文件不变。若之后由操作员更新 vault 凭据，先删除对应账号运行时目录中的 overlay 凭据文件，再重启以重新链接。回复、conv-api、timeouts 等可变数据只写运行时目录。
- 只有 cookies.json 是数组、access_token.txt 非空，且 Cookie 数组至少包含一条非空 OpenAI 域 Cookie 的账号才进入轮转。该检查是文件结构检查，不代表服务端认证仍有效；401 会由运行时标记为不可用。
- --rotation-disable acctN 是持久人工禁用；只有 --rotation-enable acctN 才会解除。429/403 等临时冷却仍使用独立 TTL。
- 显式指定 --account acctN 是身份固定，不会在 429 时自动换号；不指定账号时才执行自动轮转。

## 三条纪律

1. **凭据内容判定身份，不按文件名。** 实测遇到过文件名写 `acct6-export.json`
   而内容是 acct5 的情况——按文件名装会挤掉正常账号，净损失一个身份。
   用 `sub`（JWT）或邮箱核对。
2. **不要在聊天里粘贴 cookie / access token。**
3. **重新提取必须由本人在浏览器完成登录**，没有非交互路径。服务端
   `token_revoked` 是 OAuth 吊销，重试/换代理/改文件都不会好。
