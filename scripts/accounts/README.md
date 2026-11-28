# 凭据目录结构（acct1–acct6 / nologin）

**这些目录不入库**（`.gitignore` 已排除 `cookies.json` / `access_token.txt` /
`devices/` / `device-cookie.txt`）。这里只说明**该放什么、放成什么样**，
不要提交任何真实凭据。

## 登录态（acct2 / acct3 / acct4 / acct5 / acct6）

每个目录两份文件：

```
accounts/<acct>/
├── cookies.json       # cookie 数组，见下
└── access_token.txt   # 纯文本，单行，无尾随换行
```

`acct1` 优先使用 `accounts/acct1/`。为兼容旧版本地 CLI，如果该目录
不存在，才回退到默认凭据根（`CHATGPT_AUTH_DIR` 或 `scripts/` 本身）。
统一 MCP 部署时，凭据根固定为 `/var/lib/so-mcp`，因此六个登录账号统一放在
`/var/lib/so-mcp/accounts/<acct>/`；不要把凭据放进只读的 `/opt/so-mcp` 应用树。

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
- **acct2–acct6**：已登录的 InPrivate 窗口里同源 `fetch('/api/auth/session')` 取
  token、`document.cookie` 取可读 cookie，导出 JSON 再落到对应目录。
  浏览器 CSP/PNA 会拦发往 `127.0.0.1` 的 fetch，**兜底走「下载 JSON 再读文件」**。

## 三条纪律

1. **凭据内容判定身份，不按文件名。** 实测遇到过文件名写 `acct6-export.json`
   而内容是 acct5 的情况——按文件名装会挤掉正常账号，净损失一个身份。
   用 `sub`（JWT）或邮箱核对。
2. **不要在聊天里粘贴 cookie / access token。**
3. **重新提取必须由本人在浏览器完成登录**，没有非交互路径。服务端
   `token_revoked` 是 OAuth 吊销，重试/换代理/改文件都不会好。
