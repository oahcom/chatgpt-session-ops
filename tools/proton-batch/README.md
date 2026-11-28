# proton-batch — Proton Mail + ChatGPT 批量注册流水线

脚本化批量注册：**Proton 邮箱**（`account.proton.me`）→ **ChatGPT**（`auth.openai.com`），
账号与密码自动落库、断点续跑、失败可分阶段重试。

与 `tools/proton-registration/` 的关系：

| 目录 | 定位 |
|---|---|
| `tools/proton-registration/` | 早期**人工辅助**版：只打开页面 + 生成密码，不填表、不提交 |
| `tools/proton-batch/`（本目录） | **全自动批量**版：直接走站点 API，打码/接码/收码/代理全链编排 |

本目录**Proton 侧走真实浏览器**（`browse` CLI 驱动本机 Edge/Chrome），ChatGPT 侧仍是纯 HTTP。

### 实测结论（2026-11-26，务必先读）

| 结论 | 证据 |
|---|---|
| Proton 纯 HTTP 创建被 `potentially abusive traffic` 拦 | `POST /api/core/v4/users` 直接 422 |
| 页面 JS 赋值 + `dispatchEvent` 同样被判定为合成流量 | 换真实键盘后同一账号直接进 HV |
| **必须**用真实键盘输入用户名 | `browse type` / `click --ref` |
| HV 有三种形态：`CAPTCHA` / `Email` / `SMS`，由服务端按会话/IP 决定 | 同一出口短时间内多次注册后，从 CAPTCHA 切到 Email |
| CAPTCHA 是 Proton 自研拼图，可全自动解 | 已产出 2 个真实账号：`oahcom359993`、`oahcom296141` |
| Email 形态要求**非 Proton** 的真实邮箱 | `Please use a non-Proton Mail email address` |
| 一次性邮箱域名被拉黑 | `Email address verification temporarily disabled for this email domain`（mail.tm / euploading.com 实测） |

所以：**CAPTCHA 形态可全自动跑通；Email 形态需要一个可读的真实邮箱（IMAP）。**
当前实现遇到 Email/SMS 形态会标记 `skipped` 并换号重试（`ChallengeUnavailable`）。


---

## 1. 目录结构

```text
tools/proton-batch/
├── config.example.json        配置模板（复制为 config.json）
├── requirements.txt
├── run_batch.ps1              Windows 入口（PS 5.1 兼容）
├── README.md
├── proton_batch/
│   ├── cli.py                 子命令
│   ├── config.py              默认值 + 深合并
│   ├── pipeline.py            编排 / 断点续跑 / 导出
│   ├── store.py               SQLite 账号库（DPAPI 加密凭据）
│   ├── browser_driver.py      browse CLI 原语（snapshot/click/type/drag/execute）
│   ├── proton_browser.py      Proton 注册全流程（真实输入 + HV + 收尾）
│   ├── captcha_solver.py      拼图验证码求解（缺口分类 + 闭环校正）
│   ├── proton_api.py          Proton v4 API（历史兼容，已被风控封）
│   ├── chatgpt_api.py         ChatGPT 注册（含 sentinel PoW）
│   ├── captcha.py             hCaptcha / Arkose（2captcha、yescaptcha）
│   ├── sms.py                 sms-activate 接码
│   ├── mailbox.py             IMAP 收码（Proton Bridge / 任意 IMAP）
│   ├── httpclient.py          统一出口、重试、429 退避
│   ├── proxy.py               代理池 + 冷却
│   ├── passwords.py           加密安全随机密码
│   ├── usernamegen.py         `oahcom` + 随机数字
│   └── logging_util.py        日志 + 脱敏
├── data/                      运行态（gitignore）：accounts.db、logs/、work/
└── tests/                     16 条离线用例（含拼图求解器回归），不触网
```

---

## 2. 安装

```powershell
cd D:\code\chatgpt-session-ops\tools\proton-batch
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

要求 Python ≥ 3.10（本机 3.12 已验证）。SOCKS 代理需要 `requests[socks]`（已在依赖里）。

---

## 3. 配置

`config.json` 与 `config.example.json` 同构。**必填项**：

| 键 | 说明 |
|---|---|
| `captcha.api_key` | 2captcha 或 yescaptcha 的 key；Proton 注册基本必过 hCaptcha |
| `captcha.hcaptcha_sitekey` | 从注册页 `h-captcha` 元素的 `data-sitekey` 抓，页面改版时更新 |
| `sms.api_key` | Proton 要求手机验证时启用（`sms-activate`） |
| `mail.username` / `mail.password` | 收 ChatGPT 验证码用。Proton Bridge 默认 `127.0.0.1:1143`，账号填完整邮箱 |
| `network.proxies` | 出口列表，如 `["http://user:pass@1.2.3.4:8080", "socks5h://1.2.3.4:1080"]` |

可选：

| 键 | 默认 | 说明 |
|---|---|---|
| `proton.username_digits` | 6 | 用户名随机数字位数 |
| `proton.verification` | `auto` | `auto` = captcha → email → sms 依次尝试；也可锁定单一路径 |
| `proton.login_verify` | true | 创建后用新密码登录一次，确认账号真的可用 |
| `chatgpt.sentinel` | true | 走 `chat-requirements` + PoW（关掉会被 403） |
| `network.max_concurrency` | 1 | 当前实现为串行；调大只影响预留字段，不建议改 |
| `network.delay_between_accounts_sec` | `[45,120]` | 账号间隔随机区间，防连续注册触发风控 |

`config.json` 已在 `.gitignore` 中，含打码/接码密钥，**不要提交**。

---

## 4. 使用

```powershell
# 冒烟：探测出口、Proton 端点、ChatGPT sentinel
.\run_batch.ps1 -Command probe

# 收信通道自检（Proton Bridge / IMAP；只打印主题，不打印正文）
.\run_batch.ps1 -Command mailtest

# 只注册 Proton
.\run_batch.ps1 -Command proton -Count 3

# 用库里已有 Proton 邮箱注册 ChatGPT
.\run_batch.ps1 -Command chatgpt -Limit 3

# 一条龙
.\run_batch.ps1 -Command all -Count 3

# 看状态
.\run_batch.ps1 -Command status

# 导出（明文密码，慎存）
.\run_batch.ps1 -Command export -Out data\accounts.csv
```

等价 Python 调用：

```powershell
python -m proton_batch proton --count 3
python -m proton_batch chatgpt --limit 3
python -m proton_batch all --count 3
python -m proton_batch status --json
python -m proton_batch probe
python -m proton_batch mailtest
```

退出码：`0` 全部达到目标；`1` 有失败；`130` 手工中断（数据保留）。

---

## 5. 状态机

Proton 阶段：

```text
pending ──创建接口返回 1000──▶ created ──登录校验通过──▶ verified
   │
   └──异常──▶ failed  （记录 proton_error，可 retry）
```

ChatGPT 阶段（沿用同一条账号记录）：

```text
none ──开始──▶ pending ──register+otp+create+session──▶ created
                  │
                  └──异常──▶ failed（可 retry）
```

判定口径（不可含糊）：

| 现象 | 结论 |
|---|---|
| HTTP 200 | **不算**成功，必须看响应体 `Code == 1000` |
| 创建接口返回 1000，登录失败 | `created`，不是 `verified` |
| OTP 校验通过但没有 session | ChatGPT `failed`（`chatgpt_session` 为空） |
| 页面/接口超时 | `failed` + 错误原文，不重试到「看起来成功」 |

---

## 6. 断点续跑

- 每条账号在 `accounts` 表里有独立行，进度只由状态字段表达。
- 重跑 `proton --count N` 会新建 N 条，**不会**重新处理已有的 `verified`。
- `chatgpt` 只挑 `proton_status in (created, verified)` 且 `chatgpt_status in (none, failed)` 的记录。
- `retry --stage proton` 只列失败记录供人工判断（用户名可能已占用，需换候选，不自动重提）。
- `events` 表保留每一步的时间线，`status` 子命令可对照。

---

## 7. 失败分类

| 阶段 | 症状 | 处置 |
|---|---|---|
| Proton | `Code=12106/12111` | 用户名被占，自动换下一个候选（最多 `username_attempts`） |
| Proton | `Code=9001/12087/2026` | 人机验证失败：换打码服务/刷新 sitekey，或切 `verification=email/sms` |
| Proton | 创建 1000 但登录 Code≠1000 | 账号可能被风控标记，保留 `created` 不升级 `verified` |
| Proton | 全部候选都不可用 | 前缀被批量占用，改 `username_prefix` |
| ChatGPT | `403` / 响应含 `unusual activity` | 出口 IP 或 sentinel 失效：先 `probe` 看 sentinel，再换代理 |
| ChatGPT | `400` 注册接口 | 邮箱已注册，或密码不合规；看 `chatgpt_error` 原文 |
| ChatGPT | OTP 超时 | IMAP 配置错误或邮件被归档：检查 Bridge、`mailbox`、`from_contains` |
| 两者 | `ProxyError` / `Tunnel connection failed` | 代理节点失效，池子自动冷却并换下一个 |

---

## 8. 限速与出口纪律

- 一个账号的所有请求共用一个代理；失败后 `rotate_on_fail` 自动换节点。
- 429 按 `Retry-After` 退避（无该头时按 `retry_backoff_sec × 尝试次数`）。
- 账号之间默认 45–120 秒随机间隔，`max_concurrency=1` 串行。
- `proton` 与 `chatgpt` 共用同一套出口池，避免「注册走代理、收信直连」的不一致。

---

## 9. 凭据安全

- `proton_password` / `chatgpt_password` 落库前经 **Windows DPAPI** 加密（`v1dpapi:` 前缀，绑定当前用户）。
- 非 Windows 平台降级为 `v1plain:`（base64，仅防肩窥），生产请只用 Windows 或自行换 KMS。
- `export` 输出**明文密码**，只在你控制的目录里生成；文件已在 `.gitignore` 中。
- 日志经 `logging_util.redact()` 过滤密码、token、HV token、PoW proof。
- `data/` 整个目录不入库；`accounts.db` 是唯一可信来源。

---

## 10. 协议要点（改代码前先看）

### 10.1 2026-11-26 实测结论（务必先读）

| 结论 | 证据 |
|---|---|
| Proton 的 `POST /api/core/v4/auth/verification` 等端点**已 404** | `/api/core/v4/auth/verification`、`/verification/captcha` 均返回 `{"Code":404,"Error":"Path not found"}` |
| 陈旧 `x-pm-appversion` 会被拒 | `{"Code":5003,"Error":"This web page is out of date"}` |
| 新版 HV 字段名是 `HumanVerificationMethods` | 前端 bundle 里 `Details:{HumanVerificationToken, HumanVerificationMethods, Title}` |
| 注册页的人机校验是 **email challenge**，不是 hCaptcha | 表单页 iframe 为 `account-api.proton.me/challenge/v4/html?Name=email`；不出现 `data-sitekey` |
| **IP 被风控时直接拒注册，无法绕过** | 页面原文：`We are detecting potentially abusive traffic coming from your network and have blocked any further signups.` |
| ChatGPT 注册链路**不需要** sentinel PoW | 把 sentinel 放前面会 401；`csrf → signin → authorize → register` 可以走通 |
| OpenAI 拒一次性邮箱域名 | `POST /api/accounts/user/register` 对 mail.tm 域名返回 `400 account_creation_failed` |

当前机器走 `http://127.0.0.1:7000` 出口（`67.159.48.146`）**可以注册成功**（已产出 2 个账号），
但同一出口短时间高频注册会被提升验证等级（CAPTCHA → Email/SMS）。
直连出口与代理出口是同一机房 IP，直连会被判滥用，必须走代理。

### 10.2 Proton（浏览器模式，`proton.mode = "browser"`）

```text
account.proton.me/signup?plan=free&language=en
  → 若已登录，点 "create a new account"
  → 用户名（真实键盘，逐字校验）→ password + password-confirm（两个框一开始都在）
  → 提交 → Human Verification 弹窗
      ├ CAPTCHA 页签 → 拼图求解 → "下一步" → 恢复短语 → 显示名 → /apps
      └ 只有 Email/SMS → 抛 ChallengeUnavailable，换号重试
```

### 10.2.1 拼图求解算法（`captcha_solver.py`）

canvas 位图 809×875，CSS 显示约 370×400（比例 ≈0.457）。

1. **找缺口**：`gray < 120` 取暗区，去掉左上角滑块区，连通域筛选 `95≤w,h≤125 && area≥6000 && fill≥0.55`
   → 每张图稳定得到 2 个 110×110 缺口。
2. **分真假**：量三条轮廓特征（中段相对两端的位移）
   - `lm` 左边缘：真缺口 **> 0**（左侧内凹）
   - `rm` 右边缘：真缺口 **> 0**（右侧外凸）
   - `top` 上边缘：真缺口 **< 0**（顶部凸起）
   - 连续评分 `score = lm + rm - top`；实测真缺口 ≈55，干扰项 −19…42。
3. **拖动**：从滑块预览中心拖到缺口中心（页面坐标 = `oBox + cBox + canvas × 比例`）。
4. **闭环校正**：拖动后重新抓图，用 `TM_CCORR_NORMED` + 非白 mask 模板匹配滑块新位置
   （帧差法会被"缺口+滑块"并集带偏一半，实测偏差 10px 级）。
   残差 >2px 就再拖一次，最多 3 轮。实测 2 轮内收敛到 (3,3)。
5. **提交**：点"下一步"，用 `snapshot` 判断成败——失败文案在 captcha iframe 里，
   主文档 `innerText` 看不到（这是早期一直误判"未通过"的原因）。

时效：从抓图到点"下一步"必须在 ~60 秒内，超时报"验证超时"。
全流程实测 20–30 秒。

### 10.3 Proton（API 模式，`proton.mode = "api"`，仅兼容保留）

```text
GET  /core/v4/users/available?Name=<name>     -> Code 1000 = 可用
POST /auth/v4/users                           -> 创建（HV 由响应返回）
POST /auth/v4                                 -> 登录校验
```


### 10.4 ChatGPT（`chatgpt_api.py`，全程纯 HTTP）

```text
csrf      -> GET  https://chatgpt.com/api/auth/csrf
signin    -> POST https://chatgpt.com/api/auth/signin/openai?prompt=login&login_hint=...
authorize -> GET  authorize URL（跟随 302，收 chatgpt.com / auth.openai.com 两域 cookie）
register  -> POST https://auth.openai.com/api/accounts/user/register {username,password}
otp page  -> GET  continue_url（触发发信）
otp       -> POST /api/accounts/email-otp/validate {code}
about-you -> GET  continue_url
create    -> POST /api/accounts/create_account {name,birthdate}
callback  -> GET  page.payload.url
session   -> GET  https://chatgpt.com/api/auth/session
```

三条硬结论：
1. **不要**在注册前置 sentinel PoW，实测 401；注册链路本来就不需要它。
2. 必须逐跳保存 `Set-Cookie`，两个域的会话是分开的。
3. `register` 对一次性邮箱域名返回 `400 account_creation_failed`；用 `@proton.me` 才过。

---

## 11. 已知限制

1. **HV 形态由服务端决定**。同一出口短时间多次注册后，Proton 会把 `CAPTCHA` 换成 `Email/SMS`。
   遇到 Email/SMS 当前实现会 `skipped` 并换号；要跑通需要：
   - 一个可读的**真实**邮箱（`mail.host/port/username/password` 配 IMAP，域名不能是 proton.me、不能是一次性邮箱），或
   - `sms.api_key`（sms-activate）走 SMS 形态。
   冷却一段时间（30 分钟以上）+ 降低注册频率，CAPTCHA 会回来。
2. **用户名必须真实键盘输入**。JS 赋值会被判 `potentially abusive traffic`，这条不能优化掉。
3. **验证码有时效**（约 60–90 秒），抓图→拖动→提交必须连跑，中途不能停下来等人工判断。
4. **端点会漂移**。Proton 与 OpenAI 都会改路径、加字段。改版时用 `probe` 定位，
   `config.json` 里的 `*_path` 是为此留的开关。
5. **收码来源三选一**：`imap` / `proton-web` / `tempmail`。批量串行推荐 `proton-web`
   （浏览器注册完即登录态，直接读收件箱）；`tempmail` 会被 OpenAI 拒，仅调试用。
6. **账号存活率取决于出口质量**。同一 IP 连续注册会被限流，代理池比脚本参数更重要。

---

## 12. 回归测试

```powershell
cd tools/proton-batch
python -m pytest tests -q
```

16 条离线用例，分三个文件：

- `tests/test_offline.py`（13）：配置合并、用户名形状、密码强度、DPAPI 往返、
  SQLite CRUD/统计/唯一约束、代理轮换与冷却、邮件取码，以及 `auto` 验证路径
  「captcha 成功后不再尝试 email/sms」的短路行为。
- `tests/test_integration_fake.py`（1）：本地假 Proton 服务跑通
  「可用性 → 验证 → 创建 → 登录」，并断言 HV token/type 头真实随创建请求发出。
- `tests/test_captcha_solver.py`（2）：合成一张带真假缺口的图，断言轮廓评分
  选中形状匹配项；以及模板匹配能精确找到被移动的滑块（含"原位"排除）。

改 `captcha_solver.py`、`store.py`（落库/加密）、`proxy.py`、`mailbox.py` 的解析逻辑前先跑一遍。

---

## 13. 排程建议

按本仓库调度基线（1 小时维度，禁止天维度）：

```text
FREQ=HOURLY;INTERVAL=1
条件门控：库存 < 阈值 且 无 RUNNING 任务 -> 注册一批；否则静默退出
```

单批数量建议 3–5，配合 45–120 秒间隔；大批量一次性跑完会显著提高风控命中率。

---

## 11. 风控诊断与出口轮换（2026-11-27 实测）

### 11.1 结论：422 abusive 是网络级判定

用 `browse network` 抓注册提交，能直接看到根因：

```text
POST https://account.proton.me/api/core/v4/users  ->  422
{"Code":...,"Error":"We are detecting potentially abusive traffic coming from
 your network and have blocked any further signups."}
```

同一出口下，页面顶部会弹 `- alert "We are detecting potentially abusive traffic..."`。
注意：这条提示**只在 snapshot 的 alert 里**，`page_state()['txt']` 看不到 —— 封禁判定必须读 snapshot。

判定为封禁时 `proton_browser._check_blocked()` 抛 `ProtonBlocked`，
`pipeline._run_proton_browser` 捕获后**自动切换 Clash 出口节点**并重试同一账号。

### 11.2 换节点：直接讲 HTTP over 命名管道

Clash Verge（mihomo v1.19+）在 Windows 上把 external-controller 绑到命名管道，
`127.0.0.1:9097` 并不监听。管道名与 secret 从
`%APPDATA%/io.github.clash-verge-rev.clash-verge-rev/config.yaml` 读：

```text
external-controller-pipe: \\.\pipe\verge-mihomo-sidecar-release-<hash>
secret: <secret>
```

`proton_batch/clash.py` 用普通文件读写把 HTTP/1.1 请求写进管道（响应是 chunked，要自己拆）。
`members()` 只取真实节点，跳过 `自动选择/日本节点/狮城节点` 这类子策略组 —— 切到子组
只会改变它的内部选择，出口 IP 不会真正换。

```python
from proton_batch.clash import Clash
c = Clash(cfg, log)
print(c.current(), c.egress_ip())
c.next_node()          # 顺序取下一个真实节点
```

### 11.3 表单输入的三个硬坑

1. **用户名 input 在 iframe 里**（`iframe[title="Email address"]`），主文档的 `#username`
   只是隐藏镜像；Playwright 选择器 `iframe[...] >> frame >> input` 不被 browse 支持。
   正确做法：`page.frameLocator(...).locator('input')` 聚焦 + `page.keyboard.type()` 真实按键。
2. **`browse execute` 跑在 Node 上下文**，没有 `document`；DOM 操作一律
   `await page.evaluate(...)`，或改用 `browse eval --frame <iframe选择器>`。
3. 密码框 `#password` / `#password-confirm` 在主文档，`fill --selector` 可用。

### 11.4 提交后不要重复点

创建请求期间按钮是 `disabled` 且文案带 `Loading`，此时点击会等 10 秒后抛错。
`_submit_until_captcha` 现在：点一次 → 最多轮询 50 秒 → 只有非 Loading 才判定为失败/封禁。

---

## 12. 人机校验（HV）实测结论（2026-11-27）

### 12.1 `dialog[open]` 不是可见性判据

Proton 的 HV 弹窗是 `<dialog class="modal-two-dialog human-verification-modal">`，
可见时 **`open` 属性可能不存在**：

```text
openProp=false  display=flex  visibility=visible  opacity=1  rect=730x343  tabCount=2
```

所以可见性必须按计算样式判：自身与**全部祖先** `display!=none`、`visibility` 不在
`hidden|collapse`、`opacity>0`，且 `getBoundingClientRect()` 宽高 > 0。
JS 只回传原始样式，判定在 `browser_driver.dialog_visible()`（Python，可离线测）。

`challenge_ui_initialized = dialogExists && dialogVisible && tabCount>=1` ——
**UI 可见 ≠ 验证通过**，两者必须分开。

### 12.2 两种 422 必须分开

| 响应体 | 语义 | 处理 |
|---|---|---|
| `Error: We are detecting potentially abusive traffic...` | 网络级反滥用拒绝 | 终止整批，不换节点 |
| `Error: Human verification required` + `Details.HumanVerificationMethods` | 服务端签发 HV 挑战 | 走第一方页面流程 |

后者带 `HumanVerificationToken` / `WebUrl` / `ExpiresAt`，但**不要自己构造或注入 token**；
让第一方页面自己完成。

### 12.3 Email 形态可全自动完成

弹窗内是标准表单（`dialog input[type="email"]` + `button:has-text("Get verification code")`）：

1. 填邮箱 → 点 `Get verification code` → 页面提示 `Code sent to <地址>`
2. IMAP 读码（Proton 发件人是 `no-reply@verify.proton.me`，主题 `Proton Verification Code`）
3. 填码 → 提交 → `https://account.proton.me/apps`

实测一条完整成功链路：`oahcom510401@proton.me`（库 id=53）。

注意：成功提示也是 `alert`，别把 `Code sent to ...` 当成拒绝。

### 12.4 三个会拖垮批量的坑

1. **同一收件地址有发码频率上限**：连续 ~5 次后 Proton 不再发码（`MailError: 等待邮件验证码超时`），
   但页面仍显示已发送。批量要么多个邮箱，要么拉开时间。
2. **已登录态会污染后续注册**：注册成功后浏览器会话是登录状态，下一次 `signup` 会先
   命中「检测到已登录态」，且 HV 方式退化成 `SMS`/`Manual`（不再给 `Email`）。
   注册之间必须登出（`proton_logout.py`）。
3. **HV 方式由服务端按风险决定**：同一出口不同时刻可能是 `Email/SMS`、`SMS/Manual`，
   或含 `CAPTCHA`。代码只处理 CAPTCHA 与 Email 两条。
