# Proton Mail Registration Assistant

> 自动化批量版本见 `../proton-batch/`（API 直连 + 打码/接码/收码 + 断点续跑）。
> 本目录保留为**人工辅助**兜底路径：只打开注册页与生成密码，不填表、不提交。

## 1. 用途与边界

本目录提供 Proton Mail 注册过程的本地辅助工具。

目标注册入口：

https://account.proton.me/mail/signup

本工具只负责：

1. 使用用户已经配对的真实浏览器打开 Proton 注册页面。
2. 在本地生成加密安全的随机密码。
3. 为用户提供注册流程、故障处理和结果验证指引。

本工具不负责：

- 自动填写注册表单。
- 自动选择或购买套餐。
- 自动探测或循环尝试用户名。
- 自动提交注册请求。
- 自动解决 CAPTCHA、hCaptcha 或其他人机验证。
- 调用第三方打码服务。
- 自动绕过 Proton 风控。
- 自动创建多个账号。

所有注册信息的填写、验证、最终提交及账号确认均由用户本人在真实浏览器中完成。

Proton 对自动化注册存在服务条款限制。用户应自行承担账号可能受到限制或封禁的风险。本工具不尝试规避该限制。

## 2. 环境要求

运行环境：

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows |
| PowerShell | Windows PowerShell 5.1 或更高版本 |
| .NET | .NET Framework 4.8 或兼容运行时 |
| 浏览器 | 已安装并配对的真实 Chrome 或 Edge |
| 浏览器自动化工具 | browse CLI |
| browse CLI 路径 | `%USERPROFILE%\.local\bin\browse.exe` |
| 网络 | 可以访问 Proton 注册入口 |

不要求安装 PowerShell 7。

密码生成使用：

- `System.Security.Cryptography.RandomNumberGenerator.Create()`
- `RandomNumberGenerator.GetBytes()`

不使用仅适用于较新 .NET 运行时的 `RandomNumberGenerator.GetInt32()`。

不使用 `Get-Random` 生成密码。

## 3. 目录结构

```text
D:\code\chatgpt-session-ops\
└── tools\
    └── proton-registration\
        ├── launch.ps1
        ├── generate-password.ps1
        ├── README.md
        └── evidence\
```

文件职责：

| 文件 | 职责 |
|---|---|
| `launch.ps1` | 使用指定的已配对浏览器打开 Proton 注册页面 |
| `generate-password.ps1` | 本地生成加密安全随机密码 |
| `README.md` | 使用说明、边界、状态机及验收标准 |
| `evidence/` | 可选的脱敏执行证据目录 |

`evidence/` 目录可以在首次执行时手动创建。

## 4. 脚本职责、参数与用法

### 4.1 launch.ps1

用途：

打开 Proton Mail 注册入口，不执行注册表单操作。

路径：

`tools/proton-registration/launch.ps1`

参数：

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `-Browser` | String | `chrome` | 指定浏览器，只允许 `chrome` 或 `edge` |
| `-BrowsePath` | String | `%USERPROFILE%\.local\bin\browse.exe` | browse CLI 完整路径 |

Chrome：

```powershell
.\launch.ps1 -Browser chrome
```

Edge：

```powershell
.\launch.ps1 -Browser edge
```

指定 browse CLI 路径：

```powershell
.\launch.ps1 `
    -Browser chrome `
    -BrowsePath "$env:USERPROFILE\.local\bin\browse.exe"
```

支持的浏览器 ID：

| Browser 参数 | 实际浏览器 ID |
|---|---|
| `chrome` | `chrome-this-computer` |
| `edge` | `edge-this-computer` |

脚本不会自动填写注册表单，也不会自动提交注册请求。

### 4.2 generate-password.ps1

用途：

生成一个随机密码并输出到当前 PowerShell 控制台。

路径：

`tools/proton-registration/generate-password.ps1`

参数：

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `-Length` | Int | `32` | 密码长度，允许范围 24–64 |

默认生成 32 位密码：

```powershell
.\generate-password.ps1
```

显式指定 32 位：

```powershell
.\generate-password.ps1 -Length 32
```

指定 40 位：

```powershell
.\generate-password.ps1 -Length 40
```

密码至少包含：

- 一个大写英文字母。
- 一个小写英文字母。
- 一个数字。
- 一个特殊字符。

字符集：

```text
大写：ABCDEFGHJKLMNPQRSTUVWXYZ
小写：abcdefghijkmnopqrstuvwxyz
数字：23456789
特殊：!@#$%^&*-_=+
```

生成过程使用加密安全随机数源和拒绝采样，随后使用 Fisher-Yates 算法打乱字符顺序。

脚本不会把密码写入文件。

## 5. 浏览器选择口径

`launch.ps1` 必须显式指定浏览器。

支持：

| Browser 参数 | 实际浏览器 ID |
|---|---|
| `chrome` | `chrome-this-computer` |
| `edge` | `edge-this-computer` |

调用示例：

```powershell
.\launch.ps1 -Browser chrome
```

实际传递给 browse CLI 的参数：

```text
--browser=chrome-this-computer
```

Edge 对应：

```text
--browser=edge-this-computer
```

### PowerShell 5.1 参数兼容性

当前环境使用：

- browse CLI 0.1.5
- Windows PowerShell 5.1

实测发现，以下空格分隔形式不能正常传递参数值：

```text
--browser chrome-this-computer
```

CLI 返回：

```text
browse: --browser needs a value
```

以下等号分隔形式已经实测成功：

```text
--browser=chrome-this-computer
```

因此，`launch.ps1` 使用单个完整参数：

```powershell
$BrowserArgument = "--browser=$BrowserId"
```

不依赖 `browse status` 中的：

```text
default=true
```

原因：

浏览器默认值可能发生变化。

如果同时配对了 Edge 和 Chrome，不指定浏览器可能导致页面打开在非预期浏览器中。

不增加未经实测的 `--group` 或 `--keepOpen` 参数。

### 已知 CLI 参数限制

在 browse CLI 0.1.5 + Windows PowerShell 5.1 环境中：

1. 对已实测的带值参数，使用 `--flag=value` 形式。
2. 不使用 `--flag value` 形式传递 `--browser` 值。
3. `--group value` 也出现相同的参数解析问题。
4. 不通过 `--json` 内联传递 JSON 对象；PowerShell 5.1 对原生程序参数中的双引号处理可能导致 JSON 被错误解析。

已确认：

| 调用形式 | 实测结果 |
|---|---|
| `--browser chrome-this-computer` | FAIL |
| `--browser=chrome-this-computer` | PASS |
| `--group testgrp` | FAIL |
| `--json '{"url":"https://example.com",...}'` | FAIL |

上述结果是当前版本和运行环境下的实测结论，不应未经验证推广为所有 browse 版本、所有 PowerShell 版本的通用规则。

## 6. 导航成功判据

`browse navigate` 的成功与否以进程退出码为主要判据。

| 退出码 | 解释 |
|---|---|
| 0 | CLI 导航命令执行成功 |
| 非 0 | CLI 导航命令执行失败 |

当前实测成功调用：

```powershell
& $BrowsePath navigate `
    'https://example.com' `
    '--browser=chrome-this-computer'
```

成功时：

- 退出码为 0。
- stdout 可以返回 JSON。
- JSON 可以包含 `url`、`navigated`、`title`、`status` 和 `tab` 等字段。

stdout 存在内容不代表命令失败。

脚本使用 `2>&1` 收集 stdout 和 stderr，以便在失败时提供诊断信息。

退出码必须在原生程序执行后立即保存：

```powershell
$Output = @(
    & $BrowsePath navigate $RegistrationUrl $BrowserArgument 2>&1
)

$ExitCode = $LASTEXITCODE
```

不能在其他原生程序调用之后才读取 `$LASTEXITCODE`。

CLI 返回退出码 0，只说明导航命令执行成功。

它不保证：

- Proton 页面已经完全加载。
- 注册页面结构符合预期。
- 用户名可用。
- 注册验证通过。
- Proton 已创建账号。

重要区别：

- CLI 返回退出码 0：导航命令成功。
- 页面实际显示注册界面：注册入口已经打开。
- Proton 明确确认账号创建成功：注册提交得到成功结果。
- 使用新凭据登录成功并进入对应 Mail 页面：达到最终验收标准。

不能将 HTTP 200、页面跳转或 CLI 成功返回直接认定为账号创建成功。

## 7. 密码生成与保管

### 7.1 生成规则

默认密码长度：

```text
32 characters
```

随机源：

```text
System.Security.Cryptography.RandomNumberGenerator
```

随机索引采用拒绝采样，避免简单取模导致的模偏差。

### 7.2 存储规则

密码：

- 不写入仓库。
- 不写入脚本文件。
- 不写入 `.env`。
- 不写入 evidence。
- 不主动写入日志。
- 不通过命令行参数传递。
- 不提交 Git。
- 不保存到普通文本文件。

生成后立即复制到用户自己的密码管理器。

### 7.3 PowerShell Transcript

特别注意：

PowerShell Transcript 可以记录控制台输出。

因此，生成密码前必须确认当前会话没有启用：

```powershell
Start-Transcript
```

如果已经启用，应先停止 Transcript：

```powershell
Stop-Transcript
```

完成密码管理器保存后，再继续其他操作。

不要使用以下方式生成密码：

```powershell
.\generate-password.ps1 > password.txt
```

不要使用：

```powershell
.\generate-password.ps1 | Tee-Object password.txt
```

### 7.4 中断恢复

如果注册过程已经中断：

1. 优先从密码管理器取回原密码。
2. 回到原浏览器标签页。
3. 检查当前注册状态。
4. 根据状态机决定继续还是停止。

不要为了恢复操作而重新生成密码。

重新生成密码会产生新的凭据，无法恢复之前的密码。

## 8. 注册执行步骤

### 阶段 1：打开注册页面

进入工具目录：

```powershell
cd D:\code\chatgpt-session-ops\tools\proton-registration
```

确认浏览器已配对：

```powershell
& "$env:USERPROFILE\.local\bin\browse.exe" status
```

打开 Chrome 注册页面：

```powershell
.\launch.ps1 -Browser chrome
```

或者：

```powershell
.\launch.ps1 -Browser edge
```

检查真实浏览器中是否出现 Proton 注册页面。

此时状态：

```text
PAGE_OPENED
```

### 阶段 2：用户名

用户名规则：

```text
oahcom + 随机数字
```

建议数字长度：

```text
6
```

示例：

```text
oahcom482731
```

示例仅用于说明格式，不代表该用户名可用。

由用户本人在 Proton 注册界面检查可用性。

如果用户名已占用，可以手动更换候选用户名。

建议最多尝试三个候选用户名。

如果出现风控限制，停止继续尝试，不通过更换 IP 或其他方式规避限制。

用户名不可用时，状态：

```text
USERNAME_REJECTED
```

### 阶段 3：生成和设置密码

执行：

```powershell
.\generate-password.ps1
```

复制生成的密码到密码管理器。

随后由用户本人在 Proton 注册界面输入密码。

不要将密码粘贴到 ChatGPT 对话、仓库文件或日志中。

### 阶段 4：恢复邮箱

如果 Proton 要求辅助或恢复邮箱，由用户本人填写：

```text
1491856051@qq.com
```

这是用户提供的自有邮箱。

是否必须验证该邮箱，以 Proton 页面实际要求为准。

不得伪造恢复邮箱验证结果。

### 阶段 5：人机验证

如果出现以下验证：

- CAPTCHA
- hCaptcha
- 邮箱验证
- 手机验证
- 其他人机验证
- 风控挑战

全部由用户本人在真实浏览器中完成。

本工具不：

- 自动识别验证码。
- 自动调用第三方打码服务。
- 伪造验证成功状态。
- 绕过风控。

等待用户处理时，记录：

```text
HUMAN_VERIFICATION_REQUIRED
```

验证失败时，记录：

```text
VERIFICATION_FAILED
```

如果出现明确风控拦截，记录：

```text
RISK_BLOCKED
```

并停止注册操作。

### 阶段 6：检查并提交

由用户本人检查：

1. 用户名是否正确。
2. 密码是否正确。
3. 恢复邮箱是否正确。
4. 套餐及相关条件是否符合预期。
5. 页面是否还有未完成的验证。

确认后由用户本人点击最终提交按钮。

本工具不执行提交操作。

提交后如果 Proton 要求额外验证：

```text
PENDING_VERIFICATION
```

不能直接记录为注册成功。

### 阶段 7：登录验证

注册流程完成后：

1. 确认 Proton 已接受账号创建。
2. 使用新账号凭据进行登录。
3. 确认登录账号是本次创建的账号。
4. 确认进入对应 Proton Mail 页面。
5. 确认能够看到该账号的 Mail 界面。

完成上述检查后，才可以记录：

```text
REGISTERED_VERIFIED
```

## 9. 状态机与可重入规则

状态定义：

| 状态 | 含义 |
|---|---|
| `NOT_STARTED` | 尚未开始注册 |
| `PAGE_OPENED` | 注册页面已经打开 |
| `USERNAME_REJECTED` | 当前用户名不可用 |
| `HUMAN_VERIFICATION_REQUIRED` | 需要人工完成验证 |
| `VERIFICATION_FAILED` | 人机验证未通过 |
| `RISK_BLOCKED` | Proton 风控阻断 |
| `PENDING_VERIFICATION` | 已提交，等待额外验证 |
| `REGISTERED_UNVERIFIED` | 已提交，但尚未完成登录验证 |
| `REGISTERED_VERIFIED` | 新账号登录成功，已确认 Mail 页面 |

重入规则：

| 当前状态 | 允许的操作 | 重入规则 |
|---|---|---|
| `NOT_STARTED` | 打开注册页面 | 可以重新启动 |
| `PAGE_OPENED` | 人工继续注册 | 回到原标签页继续 |
| `USERNAME_REJECTED` | 更换用户名 | 可以人工更换候选值 |
| `HUMAN_VERIFICATION_REQUIRED` | 人工验证 | 优先继续原标签页 |
| `VERIFICATION_FAILED` | 查看失败原因 | 不自动重复提交 |
| `RISK_BLOCKED` | 停止操作 | 不自动重试，不规避风控 |
| `PENDING_VERIFICATION` | 完成额外验证 | 继续原账号验证流程 |
| `REGISTERED_UNVERIFIED` | 使用新凭据登录 | 先确认现有账号，不重新注册 |
| `REGISTERED_VERIFIED` | 结束 | 不再创建账号 |

### 重复提交规则

任何情况下：

- 不因脚本重启自动提交。
- 不因浏览器断开自动重新提交。
- 不因超时自动重新提交。
- 不因页面跳转异常而假定注册失败并重新创建。
- 不对同一个注册流程进行并发操作。

如果无法确认提交是否成功：

1. 检查原浏览器标签页。
2. 检查 Proton 页面给出的结果。
3. 必要时使用已保存的凭据尝试登录。
4. 在结果明确之前保持未确认状态。

## 10. 证据留存口径

证据目录：

```text
tools/proton-registration/evidence/
```

可以保存：

- 注册页面截图。
- 不含密码和验证秘密的页面快照。
- Proton 显示的非敏感错误提示。
- CLI 导航结果。
- 操作时间。
- 当前状态。
- 脱敏后的失败原因。

禁止保存：

- 明文密码。
- 密码输入框内容。
- 邮箱验证码。
- 手机验证码。
- CAPTCHA 答案。
- Cookie。
- Session Token。
- Authorization Header。
- 完整 HAR。
- 包含认证信息的网络请求。
- 可直接用于登录的浏览器存储数据。

证据命名建议：

```text
evidence/
    navigation-result.txt
    registration-page.png
    failure-page.png
    execution-status.txt
```

保存证据之前应检查是否包含个人信息或认证秘密。

如果截图中包含敏感信息，应先脱敏再保存。

不要为了留存证据而导出完整浏览器会话。

## 11. 失败分类与处置

### 11.1 页面结构变化

表现：

- 注册入口无法正常显示。
- 页面元素缺失。
- 页面出现新的流程或套餐选择方式。
- 页面显示异常或无法继续。

处置：

1. 检查 Proton 页面是否正常加载。
2. 检查页面 URL。
3. 确认是否发生正常重定向。
4. 保存脱敏截图。
5. 暂停操作，人工检查。

不要使用猜测的选择器或自动化点击代替人工检查。

### 11.2 用户名被占用

表现：

Proton 明确提示用户名不可用或已经被占用。

处置：

1. 确认提示确实针对用户名。
2. 人工更换随机数字。
3. 再次检查可用性。
4. 建议最多三个候选值。

不要自动循环探测用户名。

### 11.3 风控拦截

表现：

- 明确提示注册受到限制。
- 页面要求额外风控验证。
- 注册操作被拒绝。
- Proton 明确提示稍后再试。

处置：

1. 停止注册操作。
2. 保存脱敏证据。
3. 记录 `RISK_BLOCKED`。
4. 不通过代理、IP 轮换或其他方式规避限制。

### 11.4 验证未通过

表现：

- CAPTCHA 验证失败。
- 邮箱验证失败。
- 手机验证失败。
- 其他人机验证未通过。

处置：

1. 检查 Proton 提示。
2. 由用户本人判断是否允许重新进行验证。
3. 不使用第三方打码服务。
4. 不伪造验证通过状态。
5. 如果出现风控阻断，停止操作。

### 11.5 网络问题

表现：

- DNS 解析失败。
- 页面超时。
- 连接被重置。
- Proton 页面无法访问。

处置：

1. 检查网络连接。
2. 检查 Proton 服务是否可访问。
3. 检查浏览器是否仍保持配对。
4. 回到原标签页确认当前注册状态。
5. 不因网络超时直接重复提交。

如果提交结果不明确，使用：

```text
REGISTERED_UNVERIFIED
```

或根据实际情况保持原状态，直到确认结果。

## 12. 最终验收判据

只有同时满足以下条件，才允许记录：

```text
REGISTERED_VERIFIED
```

验收条件：

1. Proton 已明确接受账号创建。
2. 新账号凭据已经保存在用户自己的密码管理器中。
3. 使用新凭据成功完成登录。
4. 登录身份确认是本次创建的账号。
5. 成功进入对应的 Proton Mail 页面。
6. 页面能够正常显示该账号的 Mail 界面。

以下情况均不能视为最终验收成功：

- 注册页面打开成功。
- 用户名检查通过。
- 密码生成成功。
- CAPTCHA 已经显示。
- 用户点击过提交按钮。
- HTTP 返回 200。
- Proton 页面发生跳转。
- 账号仍在等待额外验证。
- 仅凭页面截图推断注册成功。

最终状态：

```text
REGISTERED_VERIFIED
```

如果没有完成新凭据登录和 Mail 页面确认，必须保留未验证状态，不得报告为注册完成。

---

## 13. 操作命令速查

进入目录：

```powershell
cd D:\code\chatgpt-session-ops\tools\proton-registration
```

检查 PowerShell：

```powershell
$PSVersionTable.PSVersion
```

检查 browse：

```powershell
Test-Path "$env:USERPROFILE\.local\bin\browse.exe"
```

检查配对浏览器：

```powershell
& "$env:USERPROFILE\.local\bin\browse.exe" status
```

打开 Chrome：

```powershell
.\launch.ps1 -Browser chrome
```

打开 Edge：

```powershell
.\launch.ps1 -Browser edge
```

生成默认 32 位密码：

```powershell
.\generate-password.ps1
```

生成 40 位密码：

```powershell
.\generate-password.ps1 -Length 40
```

---

## 14. 工具边界总结

本工具的职责止于：

```text
打开注册页面
      |
      v
生成随机密码
      |
      v
用户人工填写注册信息
      |
      v
用户人工完成验证
      |
      v
用户人工提交
      |
      v
用户使用新凭据登录
      |
      v
确认 Mail 页面
```

不自动填写、不自动提交、不自动解验证码、不绕过风控。

最终成功标准始终是：

```text
REGISTERED_VERIFIED
```
