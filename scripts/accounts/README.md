# 凭据目录约定

本目录**只放结构说明，不放任何凭据**。`cookies.json`、`access_token.txt`、`device-cookie.txt` 与 `devices/` 全部被 `.gitignore` 排除。

## 布局

```
scripts/
├── cookies.json                  ← acct1
├── access_token.txt              ← acct1
└── accounts/
    ├── acct2/
    │   ├── cookies.json          ← acct2
    │   └── access_token.txt
    ├── acct3/
    │   ├── cookies.json          ← acct3
    │   └── access_token.txt
    └── nologin/                  ← 脚本自动生成，不要手写
        ├── device-cookie.txt
        └── devices/<oai-did>.txt
```

acct1 也可以整目录外置：设 `CHATGPT_AUTH_DIR=/path/to/dir`，脚本会去那里读 `cookies.json` 与 `access_token.txt`。

## 格式

**cookies.json** — Playwright/CDP 风格的 cookie 数组（字段超集即可，脚本只取 `name` / `value` / `domain`）：

```json
[
  {"name": "__Secure-next-auth.session-token", "value": "...", "domain": "chatgpt.com"},
  {"name": "oai-did", "value": "...", "domain": "chatgpt.com"},
  {"name": "__cf_bm", "value": "...", "domain": "chatgpt.com"}
]
```

**access_token.txt** — 单行，`/backend-api` 的 Bearer token（JWT 以 `eyJ` 开头），不带 `Bearer ` 前缀。

## 别只放两条 cookie

只存 `session-token` + `oai-did` 时 GET 全通、但 `POST /backend-api/conversation` 恒 403 `Unusual activity`——缺的是 Cloudflare 会话 cookie（`__cf_bm`、`__cfuvid`、`__cflb`）。脚本首次 403 会自动 `warm_cookies()`（访问首页接收 Set-Cookie 并回写 `cookies.json`）再重试，实测可自愈；但优先一次性提取完整 cookie 更稳。

## 怎么拿

```bash
# Edge 以调试端口启动并登录 ChatGPT 后
python scripts/extract_auth.py
```

只覆盖默认账号（acct1）。额外身份（acct2 / acct3）需在已登录的 InPrivate 窗口经 DevConsole 同源取 token + 可读 cookie，导出到 `accounts/acct2/`、`accounts/acct3/`；本机回环接收服务被 CSP/PNA 拦下时，兜底下载 JSON 再读取。导出的 cookie 若缺 CF 系列，首轮 `warm_cookies()` 会补齐。

## nologin/

匿名态没有账号，只有**设备身份**。`device-cookie.txt` 是当前浏览器的 `oai-did`，`devices/<oai-did>.txt` 是按设备分档的快照——`--anon-read` 会用对照表里记录的设备号取对应快照，所以换 profile 后旧匿名会话仍可读。

**删掉 `devices/` 等于永久失去那些设备上的历史匿名会话**，服务端没有任何副本。

## 安全

- 不要提交、不要粘贴到聊天或 issue。
- `access_token.txt` 有效期到期（JWT `exp`）后重新提取，不要手工续期。
- 多机共用请把凭据放在加密盘并用 `CHATGPT_AUTH_DIR` 指过去，不要放进仓库工作区。
